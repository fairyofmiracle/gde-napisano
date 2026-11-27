"""Диалог с сотрудником поверх поиска по регламентам.

Порядок: фильтр лексики → вопрос вне базы → разъяснения службы качества (FAQ) → подразделение
(явно в вопросе, выбрано раньше в сессии или уточняем) → ответ по регламентам → ссылки на документы,
которых нет в базе → эскалация письмом при «нет ответа / противоречие / недостаточная уверенность».
"""
from __future__ import annotations

import json
import logging
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from app.escalation import EscalationError
from app.guard import missing_documents, off_topic

log = logging.getLogger(__name__)

UNKNOWN = "unknown"
# «Как правильно?», «в одном месте … в другом …», «сравни» — человек сам спрашивает про расхождение
_COMPARE = re.compile(r"как\s+правильно|в\s+одном\s+(месте|документе|регламенте).+в\s+друг\w+|сравни\w*|"
                      r"отлича\w+|разниц\w+|во\s+всех\s+подразделени\w+|для\s+всех\s+подразделени\w+|"
                      r"общ\w+\s+срок|средн\w+", re.I | re.S)

ESC_TEXT = ("Не смог ответить по регламентам, отправил запрос в службу качества ({to}), вы в копии ({cc}). "
            "Когда эксперт ответит, его разъяснение появится в базе, и я смогу отвечать им.")
ESC_PARTIAL = "Ниже то, что удалось найти. Это не подтверждённый ответ: сверяйтесь с пунктами."
ESC_CONFLICT = ("Пункты регламентов расходятся, поэтому я не выбираю один вариант и передал вопрос в службу "
                "качества ({to}), вы в копии ({cc}). Что написано в документах:")
BY_DIVISION_TEXT = ("Порядок зависит от подразделения. Ниже — отдельно для каждого, сроки и правила не смешиваю "
                    "и не усредняю.")
CLARIFY_TEXT = ("Ответ зависит от подразделения: в регламентах {names} это описано по-разному. "
                "В каком подразделении вы работаете?")
FAQ_LABEL = "Разъяснение службы качества"


class Assistant:
    def __init__(self, settings, db, boxes, searcher, answers, memory, glossary, faq, escalations) -> None:
        self.settings = settings
        self.db = db
        self.boxes = boxes
        self.searcher = searcher
        self.answers = answers
        self.memory = memory
        self.glossary = glossary
        self.faq = faq
        self.escalations = escalations

    # --- сессия (имитация профиля сотрудника) -----------------------------------------
    def session(self, session_id: str) -> dict:
        if session_id:
            rows = self.db.query("SELECT * FROM sessions WHERE session_id=?", (session_id,))
            if rows:
                return dict(rows[0])
        return {"session_id": session_id, "division": "", "clarify_count": 0, "pending_question": "",
                "user_email": "", "user_name": ""}

    def save_session(self, sess: dict) -> None:
        if not sess.get("session_id"):
            return
        self.db.execute(
            "INSERT INTO sessions(session_id, division, clarify_count, pending_question, user_email, user_name,"
            " updated_at) VALUES(?,?,?,?,?,?,datetime('now')) ON CONFLICT(session_id) DO UPDATE SET"
            " division=excluded.division, clarify_count=excluded.clarify_count,"
            " pending_question=excluded.pending_question, user_email=excluded.user_email,"
            " user_name=excluded.user_name, updated_at=datetime('now')",
            (sess["session_id"], sess.get("division") or "", int(sess.get("clarify_count") or 0),
             sess.get("pending_question") or "", sess.get("user_email") or "", sess.get("user_name") or ""))

    def division_options(self) -> list[dict]:
        return [{"id": b, "label": self.boxes.name(b)} for b in self.boxes.ids()]

    # --- основной вход ------------------------------------------------------------------
    def ask(self, question: str, *, session_id: str = "", division: str | None = None,
            boxes: list[str] | None = None, user_email: str = "", user_name: str = "") -> dict:
        t0 = time.perf_counter()
        s = self.settings
        q = (question or "").strip()
        sess = self.session(session_id)
        if user_email:
            sess["user_email"] = user_email.strip()
        if user_name:
            sess["user_name"] = user_name.strip()
        if division == "auto":
            sess["division"] = ""
            sess["clarify_count"] = 0
        elif division and (division == UNKNOWN or division in self.boxes.boxes):
            sess["division"] = division
        cur_div = sess.get("division") or ""

        if self.answers.profanity.check(q):
            return self._finish(self.answers._refused(t0), sess, outcome="refused", record=False)

        ot = off_topic(q)
        if ot:
            out = self._base(q, "offtopic", ot.text, t0)
            out["steps"] = [{"step": "guard", "title": "Проверка вопроса", "status": "warn",
                             "detail": "Вопрос не о регламентах: " + {"money": "личная мотивация / деньги по сделке",
                                                                      "generate": "просьба составить документ или анализ",
                                                                      "smalltalk": "разговор не по делу"}[ot.kind],
                             "ms": None}]
            return self._finish(out, sess, outcome="offtopic")

        search_q, terms = self.glossary.expand(q)

        hit = self.faq.lookup(q, division=cur_div if cur_div != UNKNOWN else "")
        if hit:
            return self._finish(self._faq_answer(q, hit, t0), sess, outcome="faq")

        explicit = [b for b in self.boxes.mentioned(q) if b in self.boxes.boxes]
        manual = [b for b in (boxes or []) if b in self.boxes.boxes]
        targets = manual or explicit or ([cur_div] if cur_div and cur_div != UNKNOWN else [])

        if len(targets) == 1:
            out = self.answers.ask(q, targets, strict=True, search_query=search_q)
            src = "manual" if manual else ("question" if explicit else "session")
            out["division"] = {"id": targets[0], "name": self.boxes.name(targets[0]), "source": src}
            label = {"question": "(подразделение названо в вопросе)", "session": "(подразделение из профиля сессии)"}
            for st in out.get("steps") or []:
                if src in label and st.get("detail"):
                    st["detail"] = st["detail"].replace("(выбраны пользователем)", label[src])
        elif len(targets) > 1:
            out = self._by_division(q, search_q, targets, t0)
        else:
            pre = self.searcher.search(search_q, None, route=False)
            compare = bool(_COMPARE.search(q))
            divs = self._divergent(pre, relaxed=compare)
            if len(divs) >= 2 and self.boxes.boxes:
                if cur_div == UNKNOWN or compare or int(sess.get("clarify_count") or 0) >= s.clarify_max:
                    out = self._by_division(q, search_q, divs, t0)
                else:
                    return self._clarify(q, divs, sess, t0)
            else:
                out = self.answers.ask(q, None, pre=pre, search_query=search_q, route=False)
        sess["pending_question"] = ""
        out["_t0"] = t0

        self._missing_docs(q, out)
        self._maybe_escalate(q, out, sess)
        out["glossary"] = self.glossary.explain(q, *[c.get("quote", "") for c in out.get("citations") or []])
        return self._finish(out, sess, outcome=self._outcome(out))

    # --- подразделения ------------------------------------------------------------------
    def _divergent(self, res, relaxed: bool = False) -> list[str]:
        """Подразделения, в регламентах которых нашлись близкие по оценке пункты.
        relaxed — человек сам спрашивает про различия: берём все подразделения с подходящими пунктами."""
        s = self.settings
        cands = res.candidates[:12]
        if not cands or not self.boxes.boxes:
            return []
        top = cands[0].score
        if relaxed:
            seen = []
            for c in cands:
                if c.box in self.boxes.boxes and c.score >= s.uncertain_threshold and c.box not in seen:
                    seen.append(c.box)
            return seen
        if top < max(s.uncertain_threshold, s.confident_threshold * 0.7):
            return []
        best: dict[str, float] = {}
        for c in cands:
            if c.box in self.boxes.boxes:
                best[c.box] = max(best.get(c.box, 0.0), c.score)
        floor = max(s.uncertain_threshold, top * s.clarify_ratio)
        return [b for b, sc in sorted(best.items(), key=lambda x: -x[1]) if sc >= floor]

    def _clarify(self, q: str, divs: list[str], sess: dict, t0: float) -> dict:
        sess["clarify_count"] = int(sess.get("clarify_count") or 0) + 1
        sess["pending_question"] = q
        names = ", ".join(self.boxes.name(d) for d in divs)
        out = self._base(q, "clarify", CLARIFY_TEXT.format(names=names), t0)
        opts = self.division_options()
        opts.sort(key=lambda o: (o["id"] not in divs, divs.index(o["id"]) if o["id"] in divs else 0))
        out["clarify"] = {"question": "В каком вы подразделении?", "pending_question": q,
                          "options": opts + [{"id": UNKNOWN, "label": "Не знаю, покажите все варианты"}],
                          "divisions_found": divs, "attempt": sess["clarify_count"]}
        out["steps"] = [{"step": "clarify", "title": "Уточнение подразделения", "status": "warn",
                         "detail": f"Подходящие пункты есть в регламентах: {names}. Нормы могут отличаться, "
                                   "поэтому сначала уточняю подразделение", "ms": None}]
        return self._finish(out, sess, outcome="clarify")

    def _by_division(self, q: str, search_q: str, divs: list[str], t0: float) -> dict:
        def one(d):
            return d, self.answers.ask(q, [d], strict=True, search_query=search_q, record=False)

        with ThreadPoolExecutor(max_workers=max(1, min(len(divs), self.settings.llm_max_concurrency))) as ex:
            results = list(ex.map(one, divs))
        out = self._base(q, "by_division", BY_DIVISION_TEXT, t0)
        sections, cands, steps = [], [], []
        pt = ct = 0
        for d, a in results:
            sections.append({"division": d, "division_name": self.boxes.name(d), "mode": a["mode"],
                             "text": a.get("text", ""), "citations": a.get("citations") or [],
                             "variants": a.get("variants") or [], "advice": a.get("advice", ""),
                             "warnings": a.get("warnings") or [], "conflict": bool(a.get("conflict")),
                             "answer_kind": a.get("answer_kind")})
            cands += a.get("_candidates") or []
            pt += a["tokens"]["prompt"]
            ct += a["tokens"]["completion"]
            steps += [{**st, "title": f"{self.boxes.name(d)}: {st['title']}"} for st in a.get("steps") or []]
        out["sections"] = sections
        out["tokens"] = {"prompt": pt, "completion": ct}
        out["steps"] = steps
        out["_candidates"] = sorted(cands, key=lambda c: -c.score)
        out["score"] = round(max((c.score for c in cands), default=0.0), 4)
        out["conflict"] = any(x["conflict"] for x in sections)
        out["division"] = {"id": "", "name": ", ".join(self.boxes.name(d) for d in divs), "source": "all"}
        return out

    # --- разъяснения службы качества ----------------------------------------------------
    def _faq_answer(self, q: str, hit: dict, t0: float) -> dict:
        out = self._base(q, "faq", hit["answer"], t0)
        out["faq"] = {"id": hit["id"], "label": FAQ_LABEL, "created_at": hit["created_at"],
                      "question": hit["question"], "expert": hit["expert"], "escalation_id": hit["escalation_id"],
                      "doc_title": hit["doc_title"], "clause_no": hit["clause_no"], "hits": hit["hits"],
                      "review_suggested": hit["review_suggested"]}
        if hit["review_suggested"]:
            out["warnings"].append(f"Этот вопрос задавали уже {hit['hits']} раз(а): службе качества стоит "
                                   "пересмотреть регламент и добавить ответ в сам документ.")
        out["steps"] = [{"step": "faq", "title": "База разъяснений", "status": "ok",
                         "detail": f"Найдено разъяснение службы качества (похожесть {hit['similarity']:.2f}), "
                                   "регламенты не менялись", "ms": None}]
        return out

    # --- документы, которых нет в базе ---------------------------------------------------
    def _missing_docs(self, q: str, out: dict) -> None:
        titles = [r["title"] for r in self.db.query("SELECT title FROM files")]
        if not titles:
            return
        names = missing_documents(q, titles)
        cited = {c["point_id"] for c in out.get("citations") or []}
        cited |= {v["point_id"] for v in out.get("variants") or []}
        cited |= {c["point_id"] for sec in out.get("sections") or [] for c in sec["citations"] + sec["variants"]}
        for c in out.get("_candidates") or []:
            if c.point_id in cited:
                names += [n for n in missing_documents(c.text, titles, strict=True) if n not in names]
        if not names:
            return
        out["missing_docs"] = names
        for n in names:
            out.setdefault("warnings", []).append(
                f"Документа «{n}» нет в выданной базе регламентов. Его содержание я не знаю и не пересказываю.")
        if out["mode"] == "not_found":
            out["text"] = (f"Порядок описан в документе «{names[0]}», но этого документа нет в выданной базе, "
                           "поэтому его содержание я не знаю.")
            out["no_escalation"] = True

    # --- эскалация ----------------------------------------------------------------------
    def _maybe_escalate(self, q: str, out: dict, sess: dict) -> None:
        s = self.settings
        mode = out["mode"]
        reason = None
        if out.get("conflict"):
            reason = "conflict"
        elif mode == "not_found" and not out.get("no_escalation"):
            reason = "not_found"
        elif mode == "uncertain" and s.escalate_on_uncertain:
            reason = "low_confidence"
        elif mode == "by_division" and all(x["mode"] == "not_found" for x in out.get("sections") or []):
            reason = "not_found"
        if reason is None:
            return
        found = [{"ref": c.ref, "text": c.text, "doc_id": c.doc_id, "doc_title": c.doc_title,
                  "clause_no": c.clause_no} for c in (out.get("_candidates") or [])[:3]
                 if c.score >= s.uncertain_threshold]
        div = sess.get("division") or ""
        try:
            esc = self.escalations.create(
                q, reason, found, user_email=sess.get("user_email") or "", user_name=sess.get("user_name") or "",
                division=div if div != UNKNOWN else "",
                division_name=self.boxes.name(div) if div and div != UNKNOWN else "",
                answer_id=out["answer_id"], session_id=sess.get("session_id") or "")
        except EscalationError as e:
            log.warning("эскалация не создана: %s", e)
            return
        out["escalation"] = esc
        to, cc = esc["to"], esc["cc"]
        if reason == "conflict":
            out["notice"] = ESC_CONFLICT.format(to=to, cc=cc)
        elif reason == "low_confidence":
            out["text"] = ESC_TEXT.format(to=to, cc=cc) + " " + ESC_PARTIAL
        else:
            out["text"] = ESC_TEXT.format(to=to, cc=cc)
        out.setdefault("steps", []).append({
            "step": "escalate", "title": "Запрос в службу качества", "status": "warn",
            "detail": ("Письмо отправлено" if esc["delivery"] == "smtp" else "Письмо сохранено в исходящих (SMTP не "
                       "настроен)") + f": {to}, копия {cc}" + (" (повтор, новое письмо не создано)"
                                                               if esc.get("duplicate") else ""), "ms": None})

    # --- общее --------------------------------------------------------------------------
    def _base(self, q: str, mode: str, text: str, t0: float) -> dict:
        return {"answer_id": uuid.uuid4().hex, "question": q, "mode": mode, "text": text, "score": 0.0, "boxes": [],
                "route_method": "", "searched_all": False, "citations": [], "variants": [], "practice": [],
                "advice": "", "owner": None, "ask_feedback": False, "latency_ms": 0,
                "tokens": {"prompt": 0, "completion": 0}, "warnings": [], "answer_kind": None, "iterations": 0,
                "steps": [], "_t0": t0}

    @staticmethod
    def _outcome(out: dict) -> str:
        if out["mode"] == "confident":
            return out.get("answer_kind") or "answer"
        return out["mode"]

    def _finish(self, out: dict, sess: dict, outcome: str, record: bool = True) -> dict:
        t0 = out.pop("_t0", None)
        if t0 is not None:
            out["latency_ms"] = int((time.perf_counter() - t0) * 1000)
        cands = out.pop("_candidates", None) or []
        out.pop("no_escalation", None)
        out["outcome"] = outcome
        out["session_id"] = sess.get("session_id") or None
        sd = sess.get("division") or ""
        out["profile"] = {"division": sd, "division_name": self.boxes.name(sd) if sd and sd != UNKNOWN else "",
                          "user_email": sess.get("user_email") or self.settings.demo_user_email}
        for c in out.get("citations") or []:
            c["link"] = f"/doc/{c['doc_id']}?p={c['point_id']}"
        for v in out.get("variants") or []:
            v["link"] = f"/doc/{v['doc_id']}?p={v['point_id']}"
        for sec in out.get("sections") or []:
            for c in sec["citations"] + sec["variants"]:
                c["link"] = f"/doc/{c['doc_id']}?p={c['point_id']}"
        self.save_session(sess)
        if record:
            self._record(out, cands, sess, outcome)
        return out

    def _record(self, out: dict, cands, sess: dict, outcome: str) -> None:
        cited = [{"doc_id": c["doc_id"], "doc_title": c["doc_title"], "clause_no": c["clause_no"]}
                 for c in out.get("citations") or []]
        for sec in out.get("sections") or []:
            cited += [{"doc_id": c["doc_id"], "doc_title": c["doc_title"], "clause_no": c["clause_no"]}
                      for c in sec["citations"]]
        if out.get("faq"):
            cited.append({"doc_id": "faq", "doc_title": FAQ_LABEL, "clause_no": str(out["faq"]["id"])})
        if not self.memory.get_answer(out["answer_id"]):
            self.memory.record_answer(
                out["answer_id"], out["question"], None, [], out["mode"], out.get("score"),
                [{"point_id": c.point_id, "doc_id": c.doc_id, "doc_title": c.doc_title, "clause_no": c.clause_no,
                  "box": c.box, "score": round(c.score, 4)} for c in cands[:5]],
                "", out["tokens"]["prompt"], out["tokens"]["completion"], out["latency_ms"])
        else:
            self.db.execute("UPDATE answers SET mode=?, latency_ms=? WHERE answer_id=?",
                            (out["mode"], out["latency_ms"], out["answer_id"]))
        div = (out.get("division") or {}).get("id") or sess.get("division") or ""
        self.db.execute("UPDATE answers SET outcome=?, division=?, session_id=?, cited=?, escalated=? "
                        "WHERE answer_id=?", (outcome, div, sess.get("session_id") or "",
                                              json.dumps(cited, ensure_ascii=False),
                                              int(bool(out.get("escalation"))), out["answer_id"]))
