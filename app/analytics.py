"""Аналитика для службы качества: неудачные поиски, эскалации, повторы, востребованные документы,
время ответа и стоимость запроса."""
from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict

from app.text import normalize

FAILED = ("not_found", "uncertain", "by_division_empty")


def _pct(values: list[int], p: float) -> int:
    if not values:
        return 0
    v = sorted(values)
    k = min(len(v) - 1, max(0, int(round(p * (len(v) - 1)))))
    return int(v[k])


def build(db, settings, limit: int = 5000) -> dict:
    rows = db.query("SELECT answer_id, created_at, question, mode, outcome, division, latency_ms, prompt_tokens, "
                    "completion_tokens, llm_provider, cited, escalated FROM answers ORDER BY created_at DESC LIMIT ?",
                    (limit,))
    total = len(rows)
    by_outcome = Counter((r["outcome"] or r["mode"]) for r in rows)
    lat = [int(r["latency_ms"]) for r in rows if (r["outcome"] or r["mode"]) not in ("offtopic", "refused")]
    failed = [{"created_at": r["created_at"], "question": r["question"], "outcome": r["outcome"] or r["mode"],
               "division": r["division"], "escalated": bool(r["escalated"])}
              for r in rows if (r["outcome"] or r["mode"]) in ("not_found", "uncertain") or r["escalated"]][:50]
    groups: dict[str, list] = defaultdict(list)
    for r in rows:
        groups[normalize(r["question"])].append(r)
    repeats = sorted(({"question": g[0]["question"], "count": len(g),
                       "outcomes": dict(Counter((x["outcome"] or x["mode"]) for x in g)),
                       "escalated": sum(1 for x in g if x["escalated"])}
                      for k, g in groups.items() if k and len(g) > 1), key=lambda x: -x["count"])[:20]
    docs, clauses = Counter(), Counter()
    for r in rows:
        for c in json.loads(r["cited"] or "[]"):
            docs[c["doc_title"]] += 1
            clauses[(c["doc_title"], c["clause_no"])] += 1
    esc = db.query("SELECT id, created_at, question, reason, status, delivery, faq_id FROM escalations "
                   "ORDER BY id DESC LIMIT 100")
    esc_status = Counter(r["status"] for r in esc)
    faq_rows = db.query("SELECT id, question, hits, doc_title, clause_no FROM faq WHERE active=1 ORDER BY hits DESC")
    thr = max(1, settings.review_repeat_threshold)
    review = [{"kind": "faq_repeats", "question": r["question"], "count": r["hits"],
               "doc": f"{r['doc_title']} п. {r['clause_no']}".strip() if r["doc_title"] else "",
               "text": f"Разъяснение выдано {r['hits']} раз(а) — стоит внести ответ в сам регламент"}
              for r in faq_rows if r["hits"] >= thr]
    review += [{"kind": "repeated_failures", "question": x["question"], "count": x["count"], "doc": "",
                "text": f"Вопрос задали {x['count']} раз(а) без подтверждённого ответа — пробел в регламентах"}
               for x in repeats if x["count"] >= thr and (x["outcomes"].get("not_found", 0)
                                                          + x["outcomes"].get("uncertain", 0)) >= thr]
    llm_rows = [r for r in rows if (r["prompt_tokens"] or r["completion_tokens"])]
    tok = [int(r["prompt_tokens"]) + int(r["completion_tokens"]) for r in llm_rows]
    avg_tok = statistics.mean(tok) if tok else 0.0
    per_q_cloud = avg_tok / 1000 * settings.llm_price_per_1k_tokens
    avg_sec = (statistics.mean(lat) / 1000) if lat else 0.0
    per_q_local = settings.stand_cost_per_hour / 3600 * avg_sec if settings.stand_cost_per_hour else 0.0
    return {
        "total": total,
        "by_outcome": dict(by_outcome),
        "latency_ms": {"avg": int(statistics.mean(lat)) if lat else 0, "p50": _pct(lat, 0.5), "p95": _pct(lat, 0.95),
                       "max": max(lat) if lat else 0},
        "failed": failed,
        "repeats": repeats,
        "popular_docs": [{"doc_title": d, "count": n} for d, n in docs.most_common(10)],
        "popular_clauses": [{"doc_title": d, "clause_no": c, "count": n} for (d, c), n in clauses.most_common(15)],
        "escalations": {"total": len(esc), "by_status": dict(esc_status),
                        "by_reason": dict(Counter(r["reason"] for r in esc))},
        "review_signals": review,
        "cost": {"llm_requests": len(llm_rows), "avg_tokens": round(avg_tok, 1),
                 "price_per_1k_tokens": settings.llm_price_per_1k_tokens,
                 "per_question_cloud_rub": round(per_q_cloud, 4),
                 "stand_cost_per_hour": settings.stand_cost_per_hour,
                 "per_question_local_rub": round(per_q_local, 4),
                 "provider": settings.llm_provider},
    }
