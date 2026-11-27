"""Ход работы в виде расследования: улики → сверка → вердикт.

Собирается только из того, что уже сделал конвейер: найденные и процитированные пункты, шаги проверки
(порог, цитата, числа, противоречия), пробелы в базе и эскалация. Новых шагов и выводов здесь не придумывается.
"""
from __future__ import annotations

EVIDENCE_MAX = 5

# шаги конвейера, которые относятся к сверке, и их короткие подписи
CHECK_LABELS = {
    "guard": "Тема вопроса",
    "clarify": "Подразделение",
    "faq": "База разъяснений",
    "assess": "Порог уверенности",
    "rewrite": "Повторный поиск",
    "injection": "Текст пунктов",
    "verify_quote": "Цитата",
    "verify_numbers": "Числа и даты",
    "conflict": "Противоречия",
}

CONFIDENCE_RU = {"high": "высокая", "medium": "средняя", "low": "низкая"}


def _link(doc_id: str | None, point_id: str | None) -> str | None:
    return f"/doc/{doc_id}?p={point_id}" if doc_id and point_id else None


def _item(src: dict, role: str, score: float | None) -> dict:
    return {"ref": src.get("ref") or "", "doc_id": src.get("doc_id"), "point_id": src.get("point_id"),
            "clause_no": src.get("clause_no") or "", "label": src.get("label") or "", "link": src.get("link") or _link(src.get("doc_id"), src.get("point_id")),
            "score": round(float(score), 2) if score is not None else None, "role": role}


def _cited(out: dict) -> list[dict]:
    items = list(out.get("citations") or [])
    for sec in out.get("sections") or []:
        items += list(sec.get("citations") or [])
    return items


def _variants(out: dict) -> list[dict]:
    items = list(out.get("variants") or [])
    for sec in out.get("sections") or []:
        items += list(sec.get("variants") or [])
    return items


def evidence(out: dict, cands: list, settings) -> dict:
    scores = {c.point_id: c.score for c in cands or []}
    role_cited = "conflict" if out.get("conflict") and out.get("answer_check") == "internal_conflict" else "cited"
    items, seen = [], set()
    for src, role in [(c, role_cited) for c in _cited(out)] + [(v, "variant") for v in _variants(out)]:
        pid = src.get("point_id")
        if not pid or pid in seen:
            continue
        seen.add(pid)
        items.append(_item(src, role, scores.get(pid, src.get("score"))))
    floor = settings.uncertain_threshold
    for c in cands or []:
        if len(items) >= EVIDENCE_MAX:
            break
        if c.point_id in seen or c.score < floor:
            continue
        seen.add(c.point_id)
        items.append(_item({"ref": c.ref, "doc_id": c.doc_id, "point_id": c.point_id, "clause_no": c.clause_no,
                            "label": c.label},
                           "candidate", c.score))
    cited_n = sum(1 for i in items if i["role"] in ("cited", "conflict"))
    mode = out.get("mode")
    if mode in ("refused", "offtopic"):
        summary = "Поиск по документам не проводился"
    elif mode == "faq":
        f = out.get("faq") or {}
        summary = "Разъяснение службы качества" + (f" по запросу ГН-{f['escalation_id']}" if f.get("escalation_id") else "")
    elif not items:
        summary = "Подходящих пунктов не найдено"
    else:
        summary = (f"Отобрано для проверки: {len(cands)}" if cands else f"Найдено пунктов: {len(items)}")
        if cited_n:
            summary += f", в ответе: {cited_n}"
    return {"summary": summary, "items": items[:max(EVIDENCE_MAX, cited_n)]}


def _lower(text: str) -> str:
    """Первая буква строчная, если это не сокращение («Модель» → «модель», «SLA» остаётся)."""
    if len(text) > 1 and text[0].isupper() and not text[1].isupper():
        return text[0].lower() + text[1:]
    return text


def _short_ref(i: dict) -> str:
    no = i.get("label") or i.get("clause_no")
    return f"п. {no}" if no else (i.get("ref") or "")


def check(out: dict, ev: dict, settings) -> dict:
    items = []
    by_div = out.get("mode") == "by_division"
    for st in out.get("steps") or []:
        label = CHECK_LABELS.get(st.get("step"))
        if not label or by_div:
            continue
        if st["step"] == "guard" and st.get("status") == "ok":
            continue
        if st["step"] == "conflict" and out.get("answer_check") == "internal_conflict":
            continue  # расхождение ниже показано парой пунктов
        items.append({"label": label, "status": st.get("status") or "ok", "detail": st.get("detail") or ""})
    cited = [i for i in ev["items"] if i["role"] == "cited"]
    if out.get("conflict"):
        pair = [i for i in ev["items"] if i["role"] in ("conflict", "cited")][:2]
        if len(pair) == 2:
            items.append({"label": "Расходятся", "status": "warn",
                          "detail": f"{_short_ref(pair[0])} и {_short_ref(pair[1])} дают разные правила"})
    elif out.get("mode") == "confident" and len(cited) >= 2:
        docs = {i["doc_id"] for i in cited}
        items.append({"label": "Согласуются", "status": "ok",
                      "detail": ", ".join(_short_ref(i) for i in cited)
                                + (" одного документа" if len(docs) == 1 else f" из {len(docs)} документов")
                                + ": цитаты подтверждены текстом пунктов"})
    elif out.get("mode") == "confident" and len(cited) == 1:
        items.append({"label": "Опора", "status": "ok", "detail": f"Ответ опирается на один пункт: {cited[0]['ref']}"})
    for w in out.get("warnings") or []:
        if w.startswith("Похожий по смыслу пункт"):
            items.append({"label": "Другая редакция", "status": "warn", "detail": w})
    for name in out.get("missing_docs") or []:
        items.append({"label": "Пробел", "status": "warn", "detail": f"Документа «{name}» нет в выданной базе"})
    if out.get("mode") == "not_found" and not out.get("missing_docs"):
        items.append({"label": "Пробел", "status": "warn",
                      "detail": f"Ни один пункт не набрал порога {settings.uncertain_threshold:.2f}: "
                                "в базе этого нет"})
    if by_div:
        for sec in out.get("sections") or []:
            st = {"confident": ("ok", "ответ с цитатой"), "uncertain": ("warn", "точного пункта нет, есть близкие"),
                  "not_found": ("warn", "в регламенте ответа нет")}.get(sec.get("mode"), ("warn", sec.get("mode") or ""))
            if sec.get("conflict"):
                st = ("warn", "пункты расходятся")
            items.append({"label": sec.get("division_name") or sec.get("division") or "", "status": st[0],
                          "detail": st[1]})
    warn = [i for i in items if i["status"] in ("warn", "fail")]
    if not items:
        summary = "Сверять нечего"
    elif warn:
        summary = "; ".join(f"{i['label'].lower()}: {_lower(i['detail'])[:120]}" for i in warn[:2])
    else:
        summary = "Всё сходится: " + ", ".join(i["label"].lower() for i in items)
    return {"summary": summary, "items": items}


def verdict(out: dict, ev: dict, settings) -> dict:
    mode = out.get("mode")
    score = float(out.get("score") or 0.0)
    esc = out.get("escalation") or None
    kind, title, conf, why = mode or "", "", None, ""
    ct, ut = settings.confident_threshold, settings.uncertain_threshold
    if mode == "refused":
        kind, title, why = "refused", "Вопрос отклонён", "Сработал фильтр нецензурной лексики"
    elif mode == "offtopic":
        kind, title = "offtopic", "Вопрос не о регламентах"
        why = next((s["detail"] for s in out.get("steps") or [] if s.get("step") == "guard"), "")
    elif mode == "clarify":
        kind, title = "clarify", "Нужно уточнить подразделение"
        why = next((s["detail"] for s in out.get("steps") or [] if s.get("step") == "clarify"), "")
    elif mode == "faq":
        kind, title, conf = "faq", "Ответ из базы разъяснений", "high"
        why = "Ответ дал эксперт службы качества; в самих регламентах его нет"
    elif out.get("conflict"):
        pair = [i for i in ev["items"] if i["role"] in ("conflict", "cited")][:2]
        verified = out.get("answer_check") == "internal_conflict"
        kind, title, conf = "conflict", "Противоречие в документах", "high" if verified else "medium"
        why = (f"{_short_ref(pair[0])} и {_short_ref(pair[1])} говорят разное" if len(pair) == 2
               else "Пункты дают разные правила")
        why += (", обе цитаты сверены с текстом" if verified else ", расхождение отметила модель")
        why += ". Выбирать одну норму нельзя"
    elif mode == "confident":
        quote_only = out.get("answer_kind") == "quote_only"
        kind = "quote_only" if quote_only else "answer"
        title = "Показан пункт дословно" if quote_only else "Ответ найден"
        flags = [s for s in out.get("steps") or [] if s.get("status") in ("warn", "fail")
                 and s.get("step") in ("verify_quote", "verify_numbers", "injection")]
        ambiguous = any(w.startswith("Похожий по смыслу пункт") for w in out.get("warnings") or [])
        conf = "high" if score >= max(0.8, ct) and not quote_only and not flags and not ambiguous else "medium"
        parts = [f"лучший пункт оценён на {score:.2f} при пороге {ct:.2f}"]
        if not quote_only:
            parts.append("цитаты найдены в тексте пунктов дословно")
            if not flags:
                parts.append("числа и даты совпадают")
        if flags:
            parts.append(flags[0]["detail"][:120].rstrip(".").lower())
        if ambiguous:
            parts.append("похожий пункт есть в другом документе")
        why = "; ".join(parts)
        why = why[0].upper() + why[1:]
    elif mode == "uncertain":
        kind, title, conf = "variants", "Точного пункта нет, есть близкие", "low"
        n = len(out.get("variants") or [])
        if score >= ct:
            reason = ("цитата модели не подтвердилась текстом пункта" if out.get("quote_check") == "failed"
                      else "модель не нашла в пунктах ответа на вопрос")
            why = f"Лучший пункт оценён на {score:.2f}, но {reason}; предложено вариантов: {n}"
        else:
            why = f"Лучшая оценка {score:.2f} ниже порога {ct:.2f}; предложено вариантов: {n}"
    elif mode == "not_found":
        kind, title = "not_found", "Не написано"
        conf = "high" if score < max(ut, ct * 0.5) else "medium"
        if out.get("missing_docs"):
            why = f"Ответ в документе «{out['missing_docs'][0]}», которого нет в базе"
        else:
            why = f"Лучшая оценка {score:.2f}, порог {ut:.2f}: подходящего пункта в базе нет"
    elif mode == "by_division":
        secs = out.get("sections") or []
        ok = [s for s in secs if s.get("mode") == "confident"]
        kind, title = "by_division", "Ответ отдельно по подразделениям"
        conf = "high" if secs and len(ok) == len(secs) else "medium" if ok else "low"
        why = f"Нормы подразделений могут отличаться; ответ с цитатой есть для {len(ok)} из {len(secs)}"
    if esc:
        ticket = f"ГН-{esc.get('id')}"
        why = (why.rstrip(".") + ". " if why else "") + f"Вопрос передан в службу качества, заявка {ticket}"
        if kind in ("not_found", "variants"):
            kind = "escalation"
            title = ("Не написано, передано в службу качества" if mode == "not_found"
                     else "Точного пункта нет, передано в службу качества")
    return {"kind": kind, "title": title, "confidence": conf,
            "confidence_label": CONFIDENCE_RU.get(conf) if conf else None,
            "score": round(score, 2) if mode not in ("refused", "offtopic", "clarify", "faq") else None,
            "why": why.rstrip(".") + "." if why else "", "escalation_id": esc.get("id") if esc else None}


def build(out: dict, cands: list, settings) -> dict:
    ev = evidence(out, cands, settings)
    return {"evidence": ev, "check": check(out, ev, settings), "verdict": verdict(out, ev, settings)}

