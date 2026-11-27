"""Отчёт «Где не написано?»: пробелы в регламентах по уже собранным данным и простой обход корпуса."""
from __future__ import annotations

import csv
import io
import json
import re
from collections import Counter, defaultdict

from app.guard import missing_documents
from app.text import normalize, tokens

# Явные отсылки к приказам и распоряжениям, которых нет среди загруженных названий
_ORDER = re.compile(
    r"\b((?:приказ\w*|распоряжени\w*|постановлени\w*)\s+"
    r"(?:(?:№|N|No|номер)\s*)?[\w./\-]+"
    r"(?:\s+от\s+\d{1,2}[./]\d{1,2}[./]\d{2,4})?)",
    re.I,
)
_CANCELLED = re.compile(r"утратил\w*\s+сил\w*|признан\w*\s+утративш\w*|отмен[её]н\w*", re.I)
_JACCARD = 0.75


def _public_clause(item: dict) -> dict:
    doc_id = item.get("doc_id") or ""
    point_id = item.get("point_id") or ""
    clause_no = item.get("clause_no") or ""
    title = item.get("doc_title") or ""
    ref = item.get("ref") or (f"{title}, п. {clause_no}".strip(" ,") if title or clause_no else "")
    link = item.get("link") or (f"/doc/{doc_id}?p={point_id}" if doc_id and point_id else (
        f"/doc/{doc_id}" if doc_id else None))
    quote = (item.get("quote") or item.get("text") or "").strip()
    if len(quote) > 240:
        quote = quote[:239].rsplit(" ", 1)[0] + "…"
    return {"doc_id": doc_id, "doc_title": title, "clause_no": clause_no, "ref": ref,
            "point_id": point_id or None, "link": link, "quote": quote}


def _parse_found(raw) -> list[dict]:
    if not raw:
        return []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return [_public_clause(x) for x in data if isinstance(x, dict)]


def _with_links(db, clauses: list[dict]) -> list[dict]:
    """Добавляет /doc/{id}?p=..., если в заявке был doc_id и номер пункта, но не было point_id."""
    for c in clauses:
        if c.get("doc_id") and c.get("point_id"):
            c["link"] = f"/doc/{c['doc_id']}?p={c['point_id']}"
            continue
        if c.get("doc_id") and c.get("clause_no"):
            rows = db.query("SELECT point_id FROM clauses WHERE doc_id=? AND clause_no=? ORDER BY ord LIMIT 1",
                            (c["doc_id"], c["clause_no"]))
            if rows:
                c["point_id"] = rows[0]["point_id"]
                c["link"] = f"/doc/{c['doc_id']}?p={c['point_id']}"
    return clauses


def _stems(text: str) -> set[str]:
    return set(tokens(text or ""))


def _merge_similar(groups: list[dict]) -> list[dict]:
    """Склеивает группы с почти одинаковыми вопросами (доля общих основ слов)."""
    if len(groups) < 2:
        return groups
    stems = [_stems(g["question"]) for g in groups]
    parent = list(range(len(groups)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(groups)):
        for j in range(i + 1, len(groups)):
            a, b = stems[i], stems[j]
            if not a or not b:
                continue
            if len(a & b) / len(a | b) >= _JACCARD:
                pi, pj = find(i), find(j)
                if pi != pj:
                    parent[pj] = pi
    buckets: dict[int, list[int]] = defaultdict(list)
    for i in range(len(groups)):
        buckets[find(i)].append(i)
    out = []
    for idxs in buckets.values():
        if len(idxs) == 1:
            out.append(groups[idxs[0]])
            continue
        items = [groups[i] for i in idxs]
        items.sort(key=lambda g: -g["count"])
        head = dict(items[0])
        tickets, statuses = [], Counter()
        last = head.get("last_at") or ""
        for g in items:
            tickets.extend(g.get("ticket_ids") or [])
            for k, n in (g.get("status") or {}).items():
                statuses[k] += n
            if (g.get("last_at") or "") > last:
                last = g["last_at"]
                head["question"] = g["question"]
        head["count"] = sum(g["count"] for g in items)
        head["ticket_ids"] = sorted(set(tickets))
        head["status"] = dict(statuses)
        head["last_at"] = last
        out.append(head)
    out.sort(key=lambda g: (-g["count"], g.get("last_at") or ""), reverse=False)
    out.sort(key=lambda g: -g["count"])
    return out


def _unanswered(db) -> list[dict]:
    rows = db.query("SELECT id, created_at, question, norm_question, reason, division, status "
                    "FROM escalations WHERE reason='not_found' ORDER BY id DESC")
    buckets: dict[str, dict] = {}
    for r in rows:
        key = r["norm_question"] or normalize(r["question"])
        g = buckets.get(key)
        if g is None:
            g = {"question": r["question"], "count": 0, "ticket_ids": [], "division": r["division"] or "",
                 "status": Counter(), "last_at": r["created_at"]}
            buckets[key] = g
        g["count"] += 1
        g["ticket_ids"].append(int(r["id"]))
        g["status"][r["status"] or "open"] += 1
        if r["created_at"] > g["last_at"]:
            g["last_at"] = r["created_at"]
            g["question"] = r["question"]
            g["division"] = r["division"] or g["division"]
    groups = []
    for g in buckets.values():
        groups.append({
            "question": g["question"], "count": g["count"],
            "ticket_ids": sorted(g["ticket_ids"]),
            "division": g["division"],
            "status": dict(g["status"]),
            "last_at": g["last_at"],
        })
    groups.sort(key=lambda x: (-x["count"], x["last_at"]))
    return _merge_similar(groups)


def _conflicts(db) -> list[dict]:
    rows = db.query("SELECT id, created_at, question, found, division, status "
                    "FROM escalations WHERE reason='conflict' ORDER BY id DESC")
    out = []
    for r in rows:
        out.append({
            "ticket_id": int(r["id"]),
            "created_at": r["created_at"],
            "question": r["question"],
            "division": r["division"] or "",
            "status": r["status"] or "",
            "clauses": _with_links(db, _parse_found(r["found"])),
        })
    return out


def _hot_clauses(db, limit: int = 20) -> list[dict]:
    rows = db.query("SELECT reason, found FROM escalations")
    counts: Counter = Counter()
    reasons: dict[tuple, Counter] = defaultdict(Counter)
    meta: dict[tuple, dict] = {}
    for r in rows:
        for c in _parse_found(r["found"]):
            if not (c.get("doc_title") or c.get("clause_no") or c.get("doc_id")):
                continue
            key = (c.get("doc_id") or "", c.get("doc_title") or "", c.get("clause_no") or "")
            counts[key] += 1
            reasons[key][r["reason"] or ""] += 1
            meta[key] = c
    out = []
    for key, n in counts.most_common(limit):
        c = meta[key]
        item = {
            "doc_id": c.get("doc_id") or "",
            "doc_title": c.get("doc_title") or "",
            "clause_no": c.get("clause_no") or "",
            "ref": c.get("ref") or "",
            "link": c.get("link"),
            "point_id": c.get("point_id"),
            "count": n,
            "reasons": dict(reasons[key]),
        }
        out.append(_with_links(db, [item])[0])
    return out


def _snippet(text: str, start: int, end: int, width: int = 140) -> str:
    lo = max(0, start - 40)
    hi = min(len(text), end + width)
    piece = text[lo:hi].strip()
    if lo:
        piece = "…" + piece
    if hi < len(text):
        piece = piece + "…"
    return re.sub(r"\s+", " ", piece)


def _missing_refs(db) -> list[dict]:
    titles_rows = db.query("SELECT doc_id, title FROM files WHERE status='ok'")
    titles = [r["title"] for r in titles_rows]
    title_set = {t.lower() for t in titles}
    clauses = db.query(
        "SELECT c.point_id, c.clause_no, c.text, c.doc_id, f.title AS doc_title "
        "FROM clauses c JOIN files f ON f.doc_id=c.doc_id WHERE f.status='ok'")
    found: dict[str, dict] = {}
    for r in clauses:
        text = r["text"] or ""
        mentions = []
        for m in _ORDER.finditer(text):
            mentions.append((m.group(1).strip(), m.start(), m.end(), "order"))
        for name in missing_documents(text, titles or [""], strict=False):
            # координаты первого вхождения
            pos = text.lower().find(name.split()[-1].lower()) if name else -1
            mentions.append((name, max(0, pos), max(0, pos) + len(name), "regulation"))
        if not mentions:
            if _CANCELLED.search(text):
                key = f"__cancelled__:{r['doc_id']}:{r['clause_no']}"
                item = found.setdefault(key, {
                    "mention": "формулировка об отмене или утрате силы",
                    "kind": "cancelled",
                    "sources": [],
                })
                item["sources"].append({
                    "doc_id": r["doc_id"], "doc_title": r["doc_title"], "clause_no": r["clause_no"],
                    "link": f"/doc/{r['doc_id']}?p={r['point_id']}",
                    "snippet": _snippet(text, 0, min(80, len(text))),
                })
            continue
        cancelled = bool(_CANCELLED.search(text))
        for name, start, end, kind in mentions:
            low = name.lower()
            if low in title_set or any(low in t.lower() or t.lower() in low for t in titles):
                # если документ есть в базе, в этот список его не кладём
                continue
            key = low
            item = found.setdefault(key, {"mention": name, "kind": kind, "sources": []})
            if cancelled and item["kind"] == "regulation":
                item["kind"] = "cancelled"
            src = {
                "doc_id": r["doc_id"], "doc_title": r["doc_title"], "clause_no": r["clause_no"],
                "link": f"/doc/{r['doc_id']}?p={r['point_id']}",
                "snippet": _snippet(text, start, end),
            }
            if src not in item["sources"]:
                item["sources"].append(src)
    # убираем пустые и слишком общие
    out = [v for v in found.values() if v["sources"]]
    out.sort(key=lambda x: (-len(x["sources"]), x["mention"].lower()))
    return out


def build(db) -> dict:
    unanswered = _unanswered(db)
    conflicts = _conflicts(db)
    hot = _hot_clauses(db)
    missing = _missing_refs(db)
    return {
        "unanswered": unanswered,
        "conflicts": conflicts,
        "hot_clauses": hot,
        "missing_refs": missing,
        "corpus_conflict_scan": {
            "done": False,
            "reason": "Полный проход по всем парам пунктов не выполнялся: он долгий и требует отдельного прогона. "
                      "Ниже только противоречия, которые уже нашлись при ответах на вопросы.",
        },
        "totals": {
            "unanswered_questions": sum(g["count"] for g in unanswered),
            "unanswered_groups": len(unanswered),
            "conflicts": len(conflicts),
            "hot_clauses": len(hot),
            "missing_refs": len(missing),
        },
    }


def to_markdown(data: dict) -> str:
    t = data.get("totals") or {}
    lines = [
        "# Где не написано?",
        "",
        "Отчёт о пробелах в загруженных регламентах: вопросы без ответа, противоречия, "
        "пункты, из-за которых чаще всего создают заявки, и ссылки на документы вне базы.",
        "",
        f"Вопросов без ответа: {t.get('unanswered_questions', 0)} "
        f"({t.get('unanswered_groups', 0)} групп). Противоречий: {t.get('conflicts', 0)}. "
        f"Ссылок на документы вне базы: {t.get('missing_refs', 0)}.",
        "",
        "## Вопросы без ответа в регламентах",
        "",
    ]
    if not data.get("unanswered"):
        lines.append("Пока нет заявок с причиной «в регламентах нет ответа».")
        lines.append("")
    for g in data.get("unanswered") or []:
        tickets = ", ".join(f"ГН-{i}" for i in g.get("ticket_ids") or [])
        status = ", ".join(f"{k}: {v}" for k, v in (g.get("status") or {}).items())
        div = f" Подразделение: {g['division']}." if g.get("division") else ""
        lines.append(f"- {g['question']} (раз задавали: {g['count']}.{div} Заявки: {tickets or '—'}. {status})")
    lines += ["", "## Противоречия", ""]
    if not data.get("conflicts"):
        lines.append("Пока нет заявок из-за расхождения пунктов.")
        lines.append("")
    for c in data.get("conflicts") or []:
        lines.append(f"- ГН-{c['ticket_id']}: {c['question']}")
        for cl in c.get("clauses") or []:
            ref = cl.get("ref") or f"{cl.get('doc_title', '')} п. {cl.get('clause_no', '')}".strip()
            link = cl.get("link") or ""
            lines.append(f"  - {ref}" + (f" ({link})" if link else "") + (f" — «{cl['quote']}»" if cl.get("quote") else ""))
    lines += ["", "## Пункты, из-за которых чаще всего создают заявки", ""]
    if not data.get("hot_clauses"):
        lines.append("Пока нет пунктов, связанных с заявками.")
        lines.append("")
    for h in data.get("hot_clauses") or []:
        reasons = ", ".join(f"{k}: {v}" for k, v in (h.get("reasons") or {}).items())
        link = h.get("link") or ""
        lines.append(f"- {h.get('ref') or (h.get('doc_title', '') + ', п. ' + h.get('clause_no', ''))}: "
                     f"{h['count']} заявок" + (f" ({reasons})" if reasons else "") + (f" {link}" if link else ""))
    lines += ["", "## Ссылки на документы, которых нет в базе", ""]
    if not data.get("missing_refs"):
        lines.append("В загруженных пунктах не нашлось ссылок на отсутствующие документы.")
        lines.append("")
    for m in data.get("missing_refs") or []:
        kind = {"order": "приказ или распоряжение", "regulation": "регламент",
                "cancelled": "отмена или утрата силы"}.get(m.get("kind"), m.get("kind") or "")
        lines.append(f"- {m['mention']} ({kind})")
        for s in m.get("sources") or []:
            lines.append(f"  - {s.get('doc_title', '')}, п. {s.get('clause_no', '')}"
                         + (f" ({s['link']})" if s.get("link") else "")
                         + (f" — {s['snippet']}" if s.get("snippet") else ""))
    scan = data.get("corpus_conflict_scan") or {}
    if not scan.get("done") and scan.get("reason"):
        lines += ["", "## Полный проход по корпусу", "", scan["reason"], ""]
    return "\n".join(lines).rstrip() + "\n"


def to_csv(data: dict) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["section", "key", "count", "details", "link"])
    for g in data.get("unanswered") or []:
        tickets = ", ".join(f"ГН-{i}" for i in g.get("ticket_ids") or [])
        w.writerow(["unanswered", g["question"], g["count"], tickets, ""])
    for c in data.get("conflicts") or []:
        refs = " | ".join(cl.get("ref") or "" for cl in c.get("clauses") or [])
        links = " | ".join(cl.get("link") or "" for cl in c.get("clauses") or [] if cl.get("link"))
        w.writerow(["conflict", c["question"], 1, f"ГН-{c['ticket_id']}; {refs}", links])
    for h in data.get("hot_clauses") or []:
        w.writerow(["hot_clause", h.get("ref") or "", h["count"],
                    "; ".join(f"{k}={v}" for k, v in (h.get("reasons") or {}).items()), h.get("link") or ""])
    for m in data.get("missing_refs") or []:
        src = " | ".join(
            f"{s.get('doc_title', '')} п. {s.get('clause_no', '')}" for s in m.get("sources") or [])
        link = " | ".join(s.get("link") or "" for s in m.get("sources") or [] if s.get("link"))
        w.writerow(["missing_ref", m["mention"], len(m.get("sources") or []), src, link])
    return buf.getvalue()
