"""Три режима ответа: уверенный с проверенной цитатой, 2–3 варианта, честное «в документах нет»."""
from __future__ import annotations

import difflib
import json
import logging
import random
import re
import time
import uuid

from app.llm import LLMError
from app.llm.prompts import (ANSWER_SYSTEM, CONFLICT_SYSTEM, REWRITE_SYSTEM, answer_user, conflict_user,
                             looks_like_injection, rewrite_user)
from app.profanity import REFUSAL_TEXT, ProfanityFilter
from app.rewrite import rule_rewrite, split_question
from app.search import Candidate, Searcher
from app.text import normalize, stem

log = logging.getLogger(__name__)

CONFIDENT, UNCERTAIN, NOT_FOUND = "confident", "uncertain", "not_found"


def _canon(text: str) -> str:
    text = normalize(text)
    text = re.sub(r"[^\w%№§]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def verify_quote(quote: str, source: str, min_ratio: float = 0.92) -> bool:
    """Цитата должна дословно (с точностью до регистра, кавычек и пробелов) встречаться в тексте пункта."""
    q, s = _canon(quote), _canon(source)
    if len(q) < 12 and q != s:
        return bool(q) and len(s) < 40 and q in s
    if q in s:
        return True
    m = difflib.SequenceMatcher(None, q, s, autojunk=False).find_longest_match(0, len(q), 0, len(s))
    if m.size / max(len(q), 1) >= min_ratio:
        return True
    # допускаем мелкие расхождения (ё/е, опечатка) на окне той же длины
    best = 0.0
    step = max(1, len(q) // 4)
    for start in range(0, max(1, len(s) - len(q) + 1), step):
        best = max(best, difflib.SequenceMatcher(None, q, s[start : start + len(q)], autojunk=False).ratio())
    return best >= min_ratio


def verbatim_part(quote: str, source: str, min_chars: int = 25, min_share: float = 0.5) -> str | None:
    """Самый длинный дословный кусок цитаты, который есть в источнике (по границам слов).
    Нужен, когда модель чуть перефразировала начало цитаты: показываем только то, что реально написано в пункте."""
    q, src = quote.strip(), source
    ql, sl = q.lower().replace("ё", "е"), src.lower().replace("ё", "е")
    m = difflib.SequenceMatcher(None, ql, sl, autojunk=False).find_longest_match(0, len(ql), 0, len(sl))
    if m.size < min_chars or m.size < min_share * len(q):
        return None
    start, end = m.b, m.b + m.size
    # не обрываем слова: сдвигаемся к ближайшим границам внутри совпадения
    if start > 0 and src[start - 1].isalnum():
        nxt = src.find(" ", start)
        start = nxt + 1 if 0 <= nxt < end else start
    if end < len(src) and src[end].isalnum():
        prv = src.rfind(" ", start, end)
        end = prv if prv > start else end
    part = src[start:end].strip(" ,;:")
    return part if len(part) >= min_chars else None


# Числа: целые, дробные, с пробелами-разделителями тысяч, даты через точку
NUM_RE = re.compile(r"\d+(?:[ \u00a0\u202f]\d{3})*(?:[.,]\d+)*")


def _numbers(text: str) -> set[str]:
    out: set[str] = set()
    for m in NUM_RE.finditer(text or ""):
        n = re.sub(r"[ \u00a0\u202f]", "", m.group(0)).replace(",", ".").strip(".")
        out.add(n)
        out.update(p for p in n.split(".") if p)  # 14.11.2025 → ещё 14, 11, 2025
        if "." in n:
            out.add(n.rstrip("0").rstrip(".") if re.fullmatch(r"\d+\.\d+", n) else n)
    return out


def unsupported_numbers(answer: str, sources: list) -> list[str]:
    """Числа и даты из ответа, которых нет ни в тексте, ни в номере, ни в названии процитированных пунктов."""
    answer = re.sub(r"\[\d+\]", " ", answer or "")
    haystack = " ".join(f"{getattr(c, 'doc_title', '')} {getattr(c, 'clause_no', '')} {getattr(c, 'section', '')} "
                        f"{getattr(c, 'text', c if isinstance(c, str) else '')}" for c in sources)
    allowed = _numbers(haystack)
    bad = []
    for m in NUM_RE.finditer(answer):
        n = re.sub(r"[ \u00a0\u202f]", "", m.group(0)).replace(",", ".").strip(".")
        if not n or n in allowed or n in bad:
            continue
        parts = [re.sub(r"[.,]$", "", x) for x in re.split(r"[ \u00a0\u202f]", m.group(0)) if x]
        if len(parts) > 1 and all(x in allowed for x in parts):
            continue  # «14 120» в ответе могли быть двумя числами: 14 и 120
        bad.append(n)
    return bad


def doc_date(mtime: float) -> str:
    import datetime as _dt

    return _dt.datetime.fromtimestamp(float(mtime)).strftime("%d.%m.%Y")


def _trim(text: str, limit: int = 700) -> str:
    """Начало пункта до границы предложения, не длиннее limit символов."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    dot = max(cut.rfind(". "), cut.rfind("; "))
    return (cut[: dot + 1] if dot > limit // 3 else cut.rsplit(" ", 1)[0]) + " …"


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = int(n)
    word = many if 11 <= n % 100 <= 14 else one if n % 10 == 1 else few if 2 <= n % 10 <= 4 else many
    return f"{n} {word}"



def clip_text(text: str, limit: int) -> str:
    """Укорачивает пункт для запроса к модели по границе предложения; цитаты проверяются по полному тексту."""
    if limit <= 0 or len(text) <= limit:
        return text
    cut = text[:limit]
    m = max(cut.rfind(". "), cut.rfind("; "), cut.rfind("\n"))
    return (cut[:m + 1] if m > limit * 0.5 else cut).rstrip()


class Trace:
    """Ход работы агента: шаги с итогом и временем, показываются в ответе API и в интерфейсе."""

    def __init__(self) -> None:
        self.steps: list[dict] = []

    def add(self, step: str, title: str, status: str, detail: str, t: float | None = None,
            ms: int | None = None) -> None:
        if ms is None and t is not None:
            ms = int((time.perf_counter() - t) * 1000)
        self.steps.append({"step": step, "title": title, "status": status, "detail": detail, "ms": ms})


def parse_json(text: str) -> dict:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("в ответе модели нет JSON")
    return json.loads(m.group(0))


class AnswerService:
    def __init__(self, settings, searcher: Searcher, llm, memory, boxes, rng: random.Random | None = None) -> None:
        self.settings = settings
        self.searcher = searcher
        self.llm = llm
        self.memory = memory
        self.boxes = boxes
        self.rng = rng or random.Random()
        self.profanity = ProfanityFilter.from_settings(settings)

    def _distinct(self, cands: list[Candidate]) -> list[Candidate]:
        seen, out = set(), []
        for c in cands:
            if c.key in seen:
                continue
            seen.add(c.key)
            out.append(c)
        return out

    def _owner(self, box: str | None, question: str) -> dict | None:
        if not box:
            return None
        o = self.boxes.owner_for(box, question)
        if o is None:
            return None
        d = o.as_dict()
        d["box"] = box
        d["box_name"] = self.boxes.name(box)
        return d

    # --- агент ---------------------------------------------------------------------------
    def ask(self, question: str, boxes: list[str] | None = None, *, strict: bool = False,
            search_query: str | None = None, record: bool = True, route: bool = True, pre=None) -> dict:
        """Шаги агента: проверка вопроса → маршрут → поиск → реранк → оценка уверенности →
        (если мало уверенности) переформулировка и повторный поиск → ответ модели →
        проверка цитаты и чисел → решение: ответ, уточнение (варианты) или «в документах нет»."""
        t0 = time.perf_counter()
        s = self.settings
        question = question.strip()
        trace = Trace()
        if self.profanity.check(question):
            trace.add("guard", "Проверка вопроса", "fail", "Вопрос отклонён фильтром нецензурной лексики")
            return self._refused(t0, trace)
        trace.add("guard", "Проверка вопроса", "ok", "Фильтр лексики пройден")

        t = time.perf_counter()
        res = pre if pre is not None else self.searcher.search(search_query or question, boxes, strict=strict,
                                                               route=route)
        self._trace_search(trace, res, t)
        cands = self._distinct(res.candidates)
        mode, top, ambiguous_with = self._assess(cands)
        trace.add("assess", "Оценка уверенности", "ok" if mode == CONFIDENT else "warn",
                  self._assess_detail(mode, top))

        # Повторный поиск по переформулированному вопросу, если уверенности не хватило
        tried = {normalize(question)}
        rewrite_tokens = [0, 0]
        iteration = 0
        while (mode != CONFIDENT and s.agent_rewrite and iteration < s.agent_max_rewrites
               and self._has_documents()):
            iteration += 1
            t = time.perf_counter()
            queries, how, toks = self._reformulate(question, tried)
            rewrite_tokens[0] += toks[0]
            rewrite_tokens[1] += toks[1]
            if not queries:
                trace.add("rewrite", "Переформулировка", "skip",
                          "Новых формулировок нет: повторный поиск не нужен", t)
                break
            before = top
            for q in queries:
                tried.add(normalize(q))
                extra = self.searcher.search(q, boxes or res.route.boxes or None, strict=strict, route=route)
                cands = self._merge(cands, extra.candidates)
            mode, top, ambiguous_with = self._assess(cands)
            better = top > before + 1e-6
            shown = "; ".join(f"«{q}»" for q in queries)
            trace.add("rewrite", f"Переформулировка и повторный поиск ({iteration})", "ok" if better else "warn",
                      f"{'Модель' if how == 'llm' else 'Правила'}: {shown}. Лучшая оценка "
                      f"{before:.2f} → {top:.2f}" + ("" if better else ", лучше не стало"), t)
            if not better:
                break

        answer_id = uuid.uuid4().hex
        out: dict = {
            "answer_id": answer_id, "question": question, "score": round(top, 4),
            "boxes": [{"box": b, "name": self.boxes.name(b)} for b in res.route.boxes],
            "route_method": res.route.method, "searched_all": res.searched_all,
            "citations": [], "variants": [], "practice": [], "advice": "", "owner": None,
            "warnings": [], "answer_kind": None, "iterations": iteration,
        }
        prompt_tokens, completion_tokens = rewrite_tokens
        provider = self.llm.provider if any(rewrite_tokens) else ""

        if mode == CONFIDENT:
            ctx = [c for c in cands[:s.answer_contexts] if c.score >= max(s.uncertain_threshold, top * 0.5)]
            suspicious = [i + 1 for i, c in enumerate(ctx) if looks_like_injection(c.text)]
            if suspicious:
                trace.add("injection", "Проверка текста пунктов", "warn",
                          "В пунктах " + ", ".join(f"[{n}]" for n in suspicious)
                          + " есть похожие на команды фразы: модели сказано считать их данными")
            t = time.perf_counter()
            data, llm_failed = None, False
            try:
                r = self.llm.chat(ANSWER_SYSTEM, answer_user(question, [
                    {"ref": c.ref, "section": c.section, "text": clip_text(c.text, s.answer_ctx_chars)} for c in ctx]),
                    json_mode=True)
                prompt_tokens += r.prompt_tokens
                completion_tokens += r.completion_tokens
                provider = r.provider
                data = parse_json(r.text)
                trace.add("generate", "Ответ модели", "ok",
                          f"{r.provider}: {r.prompt_tokens}+{r.completion_tokens} токенов", t)
            except LLMError as e:
                log.error("LLM недоступна: %s", e)  # подробности только в логе
                llm_failed = True
                out["llm_error"] = "llm_unavailable"
                trace.add("generate", "Ответ модели", "fail", "Модель недоступна: показываю дословно пункт", t)
            except (ValueError, json.JSONDecodeError) as e:
                log.warning("Не разобран ответ модели: %s", e)
                trace.add("generate", "Ответ модели", "fail", "Ответ модели не разобран", t)

            if llm_failed:
                self._quote_only(out, ctx[0], question, "Языковая модель сейчас недоступна, поэтому показываю "
                                 "дословно пункт, который лучше всего подходит к вопросу.")
                trace.add("verify_quote", "Проверка цитаты", "ok", "Цитата взята из пункта дословно")
            else:
                citations = self._verified_citations(data, ctx) if data else []
                if data and data.get("answerable") and not citations and not s.strict_quotes:
                    citations = self._fallback_citation(data, ctx)
                if data and data.get("answerable") and citations:
                    trace.add("verify_quote", "Проверка цитаты", "ok",
                              f"Подтверждено цитат: {len(citations)} из {len(data.get('quotes') or []) or 1}")
                    text = self._clean_refs(str(data.get("answer") or "").strip(), len(ctx))
                    citations = self._complete_citations(data, text, ctx, citations)
                    advice = str(data.get("advice") or "").strip()
                    cited = [ctx[c["n"] - 1] for c in citations]
                    bad = unsupported_numbers(text, cited) if s.check_numbers else []
                    # число только в совете (часто взято из соседнего пункта): прячем совет, сам ответ остаётся
                    bad_advice = unsupported_numbers(advice, cited) if s.check_numbers and advice and not bad else []
                    if bad_advice:
                        advice = ""
                    main = ctx[citations[0]["n"] - 1]
                    if bad:
                        trace.add("verify_numbers", "Проверка чисел и дат", "warn",
                                  "В ответе модели есть числа, которых нет в цитируемых пунктах: "
                                  + ", ".join(bad[:5]) + ". Ответ скрыт, показана только цитата")
                        out["answer_check"] = "numbers_mismatch"
                        self._quote_only(out, main, question, "Сформулированный ответ скрыт: в нём были числа, "
                                         "которых нет в документе. Ниже — дословный текст пункта.",
                                         citations=citations)
                    else:
                        if bad_advice:
                            trace.add("verify_numbers", "Проверка чисел и дат", "warn",
                                      "В совете были числа, которых нет в цитируемых пунктах: "
                                      + ", ".join(bad_advice[:5]) + ". Совет скрыт, ответ проверен")
                            out["answer_check"] = "advice_dropped"
                        else:
                            trace.add("verify_numbers", "Проверка чисел и дат", "ok",
                                      "Все числа и даты ответа есть в цитируемых пунктах" if NUM_RE.search(text)
                                      else "Чисел в ответе нет")
                        out["text"] = text
                        out["advice"] = advice
                        out["citations"] = citations
                        out["answer_kind"] = "answer"
                        out["practice"] = self.memory.practice_for(main.doc_id, main.clause_no)
                        out["owner"] = self._owner(main.box, question)
                    if data.get("conflict"):
                        out["conflict"] = True
                        trace.add("conflict", "Проверка противоречий", "warn",
                                  "Модель отметила, что пункты противоречат друг другу")
                    elif s.conflict_check and out.get("answer_kind") == "answer":
                        cpt, cct = self._internal_conflict(question, ctx, out, trace)
                        prompt_tokens += cpt
                        completion_tokens += cct
                    if "injection" in (data.get("flags") or []):
                        out["warnings"].append("В тексте документа были фразы, похожие на команды. "
                                               "Они проигнорированы, ответ построен только по существу пункта.")
                elif (data is not None and not data.get("answerable") and top >= s.quote_fallback_score
                      and ambiguous_with is None):
                    # реранкер уверен в первом пункте, а модель не справилась: отдаём пункт дословно со ссылкой
                    trace.add("verify_quote", "Проверка цитаты", "warn",
                              f"Модель не сформулировала ответ, но пункт оценён на {top:.2f}: показываю его дословно")
                    out["quote_check"] = "reranker_fallback"
                    self._quote_only(out, ctx[0], question, "Модель не смогла сформулировать краткий ответ, "
                                     "поэтому показываю дословно пункт, который лучше всего подходит к вопросу.")
                else:
                    out["quote_check"] = "failed" if data and data.get("answerable") else "no_answer"
                    trace.add("verify_quote", "Проверка цитаты", "fail",
                              "Цитата не подтвердилась текстом пункта" if out["quote_check"] == "failed"
                              else "Модель не нашла ответа в пунктах")
                    mode = UNCERTAIN if top >= s.uncertain_threshold else NOT_FOUND
            if mode == CONFIDENT and ambiguous_with is not None:
                out["warnings"].append(
                    f"Похожий по смыслу пункт есть и в другом документе: {ambiguous_with.ref}"
                    f"{self._doc_date_suffix(ambiguous_with.doc_id)}. Если редакции расходятся, уточните, какая действует.")

        if mode == UNCERTAIN:
            variants = [c for c in cands if c.score >= s.uncertain_threshold * 0.5][: s.max_variants]
            if len(variants) < 2 and len(cands) > 1:
                variants = cands[:2]
            out["text"] = "Точного ответа не нашлось, но похоже, что нужное есть в этих пунктах. Это то, что вы искали?"
            out["variants"] = [self._variant(c) for c in variants]
            out["owner"] = self._owner(variants[0].box if variants else self._route_box(res), question)

        if mode == NOT_FOUND:
            box = self._route_box(res)
            out["text"] = "В загруженных документах ответа на этот вопрос нет."
            out["owner"] = self._owner(box, question)
            if out["owner"]:
                out["text"] += " Лучше уточнить у ответственного по направлению."
            out["practice"] = self.memory.similar_practice(res.q_vec)
            if getattr(self.searcher, "indexer", None) is not None and self.searcher.indexer.running:
                out["text"] += " Сейчас идёт индексация документов — попробуйте спросить ещё раз через минуту."

        decision = {CONFIDENT: ("ok", "Ответ с проверенной цитатой" if out["answer_kind"] == "answer"
                                else "Только цитата из документа"),
                    UNCERTAIN: ("warn", f"Уточнение: предложены варианты ({len(out['variants'])})"),
                    NOT_FOUND: ("warn", "В документах ответа нет: честно сообщаю и предлагаю ответственного")}[mode]
        trace.add("decide", "Решение", decision[0], decision[1])

        out["mode"] = mode
        if mode == CONFIDENT:
            out["ask_feedback"] = self.rng.random() < s.feedback_sample_rate
        else:
            out["ask_feedback"] = True
        out["latency_ms"] = int((time.perf_counter() - t0) * 1000)
        out["tokens"] = {"prompt": prompt_tokens, "completion": completion_tokens}
        out["steps"] = trace.steps
        self._attach_dates(out)
        out["_candidates"] = cands
        if not record:
            return out
        self.memory.record_answer(
            answer_id, question, res.q_vec, res.route.boxes, mode, top,
            [{"point_id": c.point_id, "doc_id": c.doc_id, "doc_title": c.doc_title, "clause_no": c.clause_no,
              "box": c.box, "score": round(c.score, 4)} for c in cands[:5]],
            provider, prompt_tokens, completion_tokens, out["latency_ms"],
        )
        log.info("ответ mode=%s score=%.3f boxes=%s iterations=%d latency=%dms", mode, top, res.route.boxes,
                 iteration, out["latency_ms"])
        return out

    def _relevant_sentences(self, question: str, text: str, limit: int = 2, width: int = 380) -> str:
        """Предложения пункта, которые лучше всего пересекаются с вопросом по основам слов."""
        words = {stem(w) for w in re.findall(r"[а-яёa-z0-9]+", normalize(question)) if len(w) > 3}
        sents = [x.strip() for x in re.split(r"(?<=[.!?;:])\s+|\n+", text) if x.strip()]
        scored = []
        for i, x in enumerate(sents):
            have = {stem(w) for w in re.findall(r"[а-яёa-z0-9]+", normalize(x)) if len(w) > 3}
            scored.append((len(words & have), -i, i))
        keep = sorted(i for _, _, i in sorted(scored, reverse=True)[:limit])
        return " ".join(sents[i][:width] for i in keep if scored[i][0] > 0)

    def _internal_conflict(self, question: str, ctx: list[Candidate], out: dict, trace: "Trace") -> tuple[int, int]:
        """Противоречие между пунктами одного документа: отдельная проверка по самым подходящим предложениям.
        Расхождение принимается только если обе цитаты дословно есть в разных пунктах."""
        t = time.perf_counter()
        doc_id = ctx[0].doc_id
        same = [c for c in ctx if c.doc_id == doc_id]
        if len(same) < 2:
            return 0, 0
        clauses = []
        for c in same:
            txt = self._relevant_sentences(question, c.text)
            if txt:
                clauses.append((c, txt))
        if len(clauses) < 2:
            return 0, 0
        try:
            r = self.llm.chat(CONFLICT_SYSTEM, conflict_user(question, [
                {"ref": c.ref, "text": txt} for c, txt in clauses]), json_mode=True)
            data = parse_json(r.text)
        except (LLMError, ValueError, json.JSONDecodeError) as e:
            log.info("проверка противоречий не выполнена: %s", type(e).__name__)
            return 0, 0
        used = (r.prompt_tokens, r.completion_tokens)
        try:
            n1, n2 = int(data.get("n1")), int(data.get("n2"))
        except (TypeError, ValueError):
            n1 = n2 = 0
        if not data.get("conflict") or n1 == n2 or not (1 <= n1 <= len(clauses) and 1 <= n2 <= len(clauses)):
            trace.add("conflict", "Проверка противоречий", "ok", "Пункты одного документа не противоречат друг другу", t)
            return used
        c1, c2 = clauses[n1 - 1][0], clauses[n2 - 1][0]
        quotes = []
        for c, key in ((c1, "quote1"), (c2, "quote2")):
            q = str(data.get(key) or "").strip().strip("«»\"")
            if q and not verify_quote(q, c.text):
                q = verbatim_part(q, c.text) or ""
            quotes.append(q)
        if not all(quotes):
            trace.add("conflict", "Проверка противоречий", "ok", "Расхождение не подтвердилось цитатами из пунктов", t)
            return used
        why = str(data.get("why") or "").strip().rstrip(".")
        why = re.sub(r"\s*vs\.?\s*", " или ", why, flags=re.I)
        why = re.sub(r"\s+", " ", why).strip(" .")
        cits = []
        for n, (c, q) in enumerate(((c1, quotes[0]), (c2, quotes[1])), start=1):
            cits.append({"n": n, "point_id": c.point_id, "doc_id": c.doc_id, "doc_title": c.doc_title,
                         "clause_no": c.clause_no, "page": c.page, "ref": c.ref, "quote": q, "verified": True,
                         "box": c.box})
        out["conflict"] = True
        out["citations"] = cits
        # Вводный тезис («расходятся, ответить нельзя, вопрос передан») ставит assistant в notice,
        # чтобы он не повторялся в тексте, совете и блоке эскалации.
        out["text"] = (f"П. {c1.shown_no} говорит: «{quotes[0]}» [1]. "
                       f"П. {c2.shown_no} говорит: «{quotes[1]}» [2]."
                       + (f" Суть расхождения: {why}." if why else ""))
        out["advice"] = "Какое правило действует, решает служба качества."
        out["answer_check"] = "internal_conflict"
        trace.add("conflict", "Проверка противоречий", "warn",
                  f"Найдено расхождение внутри документа: п. {c1.shown_no} и п. {c2.shown_no}", t)
        return used

    # --- шаги агента ---------------------------------------------------------------------
    def _trace_search(self, trace: "Trace", res, t: float) -> None:
        st = res.stats or {}
        names = ", ".join(self.boxes.name(b) for b in res.route.boxes) or "все документы"
        method = {"manual": "выбраны пользователем", "rules": "по ключевым словам", "embeddings": "по смыслу",
                  "embeddings-low": "направление не определилось", "none": "направление не определилось",
                  "llm": "выбраны моделью"}.get(res.route.method, res.route.method)
        trace.add("route", "Маршрут", "ok", f"Направления: {names} ({method})", ms=st.get("route_ms"))
        trace.add("retrieve", "Гибридный поиск", "ok" if res.candidates else "warn",
                  f"Векторный поиск: {st.get('vector', 0)}, по словам (BM25): {st.get('bm25', 0)}, "
                  f"после слияния: {st.get('fused', 0)}"
                  + (". Расширен на все документы" if res.searched_all and res.route.boxes else ""),
                  ms=st.get("retrieve_ms"))
        top = res.candidates[0] if res.candidates else None
        trace.add("rerank", "Реранжирование", "ok" if top else "skip",
                  (f"Проверено пунктов: {st.get('reranked', 0)}. Лучший: {top.ref} ({top.score:.2f})"
                   + (f"; учтены отзывы: {_plural(st.get('memory_adjusted'), 'пункт', 'пункта', 'пунктов')}" if st.get("memory_adjusted") else ""))
                  if top else "Подходящих пунктов нет", ms=st.get("rerank_ms"))

    def _assess(self, cands: list[Candidate]):
        """Режим по оценке лучшего пункта. Третье значение — пункт другого документа с почти такой же оценкой."""
        s = self.settings
        top = cands[0].score if cands else 0.0
        ambiguous_with = None
        if top >= s.confident_threshold:
            if s.ambiguity_margin > 0:
                for c in cands[1:4]:
                    if c.doc_id != cands[0].doc_id and top - c.score < s.ambiguity_margin:
                        ambiguous_with = c
                        break
            return CONFIDENT, top, ambiguous_with
        if top >= s.uncertain_threshold:
            return UNCERTAIN, top, None
        return NOT_FOUND, top, None

    def _assess_detail(self, mode: str, top: float) -> str:
        s = self.settings
        if mode == CONFIDENT:
            return f"Оценка {top:.2f} ≥ порога уверенности {s.confident_threshold:.2f}"
        if mode == UNCERTAIN:
            return f"Оценка {top:.2f} ниже порога {s.confident_threshold:.2f}: ответ неоднозначен"
        return f"Оценка {top:.2f} ниже порога {s.uncertain_threshold:.2f}: подходящего пункта не видно"

    def _has_documents(self) -> bool:
        try:
            return bool(self.searcher.db.query("SELECT 1 FROM clauses LIMIT 1"))
        except Exception:
            return True

    def _reformulate(self, question: str, tried: set[str]) -> tuple[list[str], str, tuple[int, int]]:
        """Новые формулировки для поиска: сначала модель (если разрешено), при сбое — правила."""
        s = self.settings
        queries: list[str] = []
        how, toks = "rules", (0, 0)
        if s.agent_rewrite_use_llm:
            try:
                r = self.llm.chat(REWRITE_SYSTEM, rewrite_user(question), json_mode=True)
                toks = (r.prompt_tokens, r.completion_tokens)
                data = parse_json(r.text)
                cand = [str(data.get("query") or "")] + [str(x) for x in (data.get("subquestions") or [])][:3]
                queries = [q.strip()[:300] for q in cand if q and q.strip()]
                how = "llm"
            except (LLMError, ValueError, json.JSONDecodeError, AttributeError) as e:
                log.info("переформулировка моделью не удалась, беру правила: %s", type(e).__name__)
        if not [q for q in queries if normalize(q) not in tried]:
            how = "rules"
            queries = [q for q in [rule_rewrite(question), *split_question(question)] if q]
        out, seen = [], set(tried)
        for q in queries:
            k = normalize(q)
            if k and k not in seen:
                seen.add(k)
                out.append(q)
        return out[:3], how, toks

    def _merge(self, cands: list[Candidate], more: list[Candidate]) -> list[Candidate]:
        """Объединяет кандидатов. Оценка, полученная по переформулировке, чуть снижается:
        отвечаем всё-таки на исходный вопрос."""
        penalty = self.settings.agent_rewrite_penalty
        by_key = {c.key: c for c in cands}
        for c in more:
            c.score = max(0.0, c.score - penalty)
            old = by_key.get(c.key)
            if old is None or c.score > old.score:
                by_key[c.key] = c
        return sorted(by_key.values(), key=lambda c: -c.score)

    def _quote_only(self, out: dict, c: Candidate, question: str, text: str,
                    citations: list[dict] | None = None) -> None:
        """Режим «только цитата»: дословный текст пункта без формулировки модели."""
        if not citations:
            citations = [{"n": 1, "point_id": c.point_id, "doc_id": c.doc_id, "doc_title": c.doc_title,
                          "clause_no": c.clause_no, "page": c.page, "ref": c.ref, "quote": _trim(c.text),
                          "verified": True, "auto_quote": True, "box": c.box}]
        out["text"] = text
        out["advice"] = ""
        out["citations"] = citations
        out["answer_kind"] = "quote_only"
        out["practice"] = self.memory.practice_for(c.doc_id, c.clause_no)
        out["owner"] = self._owner(c.box, question)

    @staticmethod
    def _clean_refs(text: str, n_ctx: int) -> str:
        """Убирает ссылки [n] на пункты, которых не было в контексте."""
        return re.sub(r"\[(\d+)\]", lambda m: m.group(0) if 1 <= int(m.group(1)) <= n_ctx else "", text).strip()

    def _attach_dates(self, out: dict) -> None:
        """Дата файла (редакция) у каждой цитаты и варианта: жюри и сотрудник видят, насколько документ свежий."""
        items = out["citations"] + out["variants"]
        ids = sorted({i["doc_id"] for i in items})
        if not ids:
            return
        try:
            rows = self.searcher.db.query(
                f"SELECT doc_id, mtime FROM files WHERE doc_id IN ({','.join('?' * len(ids))})", tuple(ids))
        except Exception:
            return
        dates = {r["doc_id"]: doc_date(r["mtime"]) for r in rows if r["mtime"]}
        for i in items:
            i["doc_date"] = dates.get(i["doc_id"])

    def _doc_date_suffix(self, doc_id: str) -> str:
        try:
            rows = self.searcher.db.query("SELECT mtime FROM files WHERE doc_id=?", (doc_id,))
        except Exception:
            return ""
        if rows and rows[0]["mtime"]:
            return f" (файл от {doc_date(rows[0]['mtime'])})"
        return ""

    def _refused(self, t0: float, trace: "Trace | None" = None) -> dict:
        """Вежливый отказ без обращения к поиску и модели. Текст вопроса не сохраняется и не пишется в лог."""
        log.info("вопрос отклонён фильтром нецензурной лексики")
        return {
            "answer_id": uuid.uuid4().hex, "question": "", "mode": "refused", "text": REFUSAL_TEXT, "score": 0.0,
            "boxes": [], "route_method": "profanity_filter", "searched_all": False, "citations": [],
            "variants": [], "practice": [], "advice": "", "owner": None, "ask_feedback": False,
            "latency_ms": int((time.perf_counter() - t0) * 1000), "tokens": {"prompt": 0, "completion": 0},
            "steps": trace.steps if trace else [], "warnings": [], "answer_kind": None, "iterations": 0,
        }

    def _route_box(self, res) -> str | None:
        if res.route.boxes:
            return res.route.boxes[0]
        if res.route.scores:
            return max(res.route.scores, key=res.route.scores.get)
        return None

    def _variant(self, c: Candidate) -> dict:
        return {"point_id": c.point_id, "doc_id": c.doc_id, "doc_title": c.doc_title, "clause_no": c.clause_no,
                "page": c.page, "ref": c.ref, "section": c.section, "text": c.text, "box": c.box,
                "score": round(c.score, 4), "practice": self.memory.practice_for(c.doc_id, c.clause_no, 1)}

    def _complete_citations(self, data: dict, text: str, ctx: list[Candidate], citations: list[dict]) -> list[dict]:
        """Ответ из нескольких частей ссылается на пункты, цитату из которых модель дала неточно:
        для таких пунктов берём начало пункта дословно, чтобы ссылка и проверка чисел опирались на них."""
        have = {c["n"] for c in citations}
        refs = {int(m) for m in re.findall(r"\[(\d+)\]", text)}
        for u in data.get("used") or []:
            try:
                refs.add(int(u))
            except (TypeError, ValueError):
                pass
        out = list(citations)
        for n in sorted(refs):
            if 1 <= n <= len(ctx) and n not in have:
                c = ctx[n - 1]
                quote = re.split(r"(?<=[.!?])\s", c.text, maxsplit=1)[0]
                out.append({"n": n, "point_id": c.point_id, "doc_id": c.doc_id, "doc_title": c.doc_title,
                            "clause_no": c.clause_no, "page": c.page, "ref": c.ref, "quote": quote,
                            "verified": True, "auto_quote": True, "box": c.box})
        return out

    def _fallback_citation(self, data: dict, ctx: list[Candidate]) -> list[dict]:
        """Модель не дала дословную цитату: показываем начало пункта, на который она сослалась."""
        try:
            n = int((data.get("used") or [1])[0])
        except (TypeError, ValueError):
            n = 1
        n = n if 1 <= n <= len(ctx) else 1
        c = ctx[n - 1]
        quote = re.split(r"(?<=[.!?])\s", c.text, maxsplit=1)[0]
        return [{"n": n, "point_id": c.point_id, "doc_id": c.doc_id, "doc_title": c.doc_title,
                 "clause_no": c.clause_no, "page": c.page, "ref": c.ref, "quote": quote, "verified": True,
                 "auto_quote": True, "box": c.box}]

    def _verified_citations(self, data: dict, ctx: list[Candidate]) -> list[dict]:
        out = []
        for q in data.get("quotes") or []:
            try:
                n = int(q.get("n"))
            except (TypeError, ValueError):
                continue
            if not 1 <= n <= len(ctx):
                continue
            c = ctx[n - 1]
            quote = str(q.get("quote") or "").strip().strip("«»\"")
            if quote and not verify_quote(quote, c.text):
                part = verbatim_part(quote, c.text)
                if part:
                    log.info("цитата сокращена до дословной части: %r", part[:80])
                    quote = part
                else:
                    quote = ""
            if not quote:
                log.info("цитата не подтверждена источником: %r", str(q.get("quote"))[:80])
                continue
            out.append({"n": n, "point_id": c.point_id, "doc_id": c.doc_id, "doc_title": c.doc_title,
                        "clause_no": c.clause_no, "page": c.page, "ref": c.ref, "quote": quote, "verified": True,
                        "box": c.box})
        return out
