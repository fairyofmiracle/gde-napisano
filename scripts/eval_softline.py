"""Прогон тестовых вопросов кейса Softline по настоящим регламентам и сводка по категориям A–G.

    python scripts/eval_softline.py --docs data/docs --questions eval/test_questions.json --out eval

Модели — из .env/окружения (EMBED_*, RERANKER_*, LLM_PROVIDER). Данные пишутся во временную папку:
рабочая база и outbox не трогаются, реальных писем нет (SMTP отключён на время прогона).
Проверки автоматические и грубые: режим ответа, номер пункта, ключевые числа. Финальная оценка — глазами.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import statistics
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.services import build_services  # noqa: E402

CATS = {"A": "прямые вопросы", "B": "нет в базе → письмо", "C": "уточнение подразделения",
        "D": "расхождения и отсутствующие документы", "E": "вне базы → отказ", "F": "дообучение через эксперта",
        "G": "самопроверка чисел"}
EXPERT_F01 = ("ОТВЕТ: Суточные по России — 700 рублей за каждый день командировки, включая выходные и праздничные "
              "дни, в пути; основание — разъяснение службы качества (тестовый пример).")


def clause_set(s: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)+", s or ""))


def cited(ans: dict) -> set[str]:
    out = {c["clause_no"] for c in ans.get("citations") or []}
    for sec in ans.get("sections") or []:
        out |= {c["clause_no"] for c in sec.get("citations") or []}
        out |= {v["clause_no"] for v in sec.get("variants") or [] if v.get("clause_no")}
    out |= {v["clause_no"] for v in ans.get("variants") or [] if v.get("clause_no")}
    return out


def numbers_ok(ans: dict, qid: str) -> bool | None:
    blob = json.dumps(ans, ensure_ascii=False)
    need = {"G01": ["2 недели", "3 рабочих"], "G02": ["2 лет"], "G03": ["10", "6 месяцев"]}.get(qid)
    if not need:
        return None
    bad = {"G01": ["5 дней", "пять дней"], "G02": ["1 год", "3 года"], "G03": []}[qid]
    return all(n in blob for n in need) and not any(b in ans.get("text", "") for b in bad)


def check(item: dict, ans: dict, extra: dict) -> tuple[bool, str]:
    cat, qid = item["category"], item["id"]
    exp = clause_set(item.get("expected_clause", ""))
    got = cited(ans)
    mode = ans["mode"]
    if cat == "A":
        ok = mode == "confident" and bool(exp & got)
        return ok, f"режим {mode}, пункты {sorted(got)} (ожидали {sorted(exp)})"
    if cat == "B":
        ok = mode in ("not_found", "uncertain") and bool(ans.get("escalation"))
        return ok, f"режим {mode}, письмо {'да' if ans.get('escalation') else 'нет'}"
    if cat == "C":
        first = extra["first"]
        ok = first["mode"] in ("clarify", "by_division") and mode == "by_division" and len(ans["sections"]) >= 2
        return ok, f"сначала {first['mode']}, «не знаю» → {mode} ({len(ans.get('sections') or [])} подразд.), пункты {sorted(got)}"
    if cat == "D":
        if qid in ("D04", "D05"):
            ok = bool(ans.get("missing_docs"))
            return ok, f"режим {mode}, нет в базе: {ans.get('missing_docs')}"
        if qid == "D03":
            ok = bool(ans.get("conflict")) or mode in ("uncertain", "by_division") or len(exp & got) >= 2
            return ok, f"режим {mode}, противоречие {ans.get('conflict')}, пункты {sorted(got)}"
        ok = mode in ("by_division", "clarify") or bool(ans.get("conflict"))
        return ok, f"режим {mode}, пункты {sorted(got)}"
    if cat == "E":
        return mode == "offtopic", f"режим {mode}"
    if cat == "F":
        if qid == "F01":
            return bool(ans.get("escalation")), f"режим {mode}, письмо {'да' if ans.get('escalation') else 'нет'}"
        return mode == "faq" and "700" in ans.get("text", ""), f"режим {mode}"
    if cat == "G":
        n = numbers_ok(ans, qid)
        ok = bool(n) and mode in ("confident", "by_division")
        return ok, f"режим {mode}, числа {'верны' if n else 'не найдены/ошибка'}, пункты {sorted(got)}"
    return False, "?"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--docs", type=Path, default=ROOT / "data" / "docs")
    ap.add_argument("--questions", type=Path, default=ROOT / "eval" / "test_questions.json")
    ap.add_argument("--boxes", type=Path, default=ROOT / "examples" / "boxes_softline.csv")
    ap.add_argument("--glossary", type=Path, default=ROOT / "examples" / "glossary.tsv")
    ap.add_argument("--out", type=Path, default=ROOT / "eval")
    ap.add_argument("--only", default="", help="например A или A01,B02")
    args = ap.parse_args()
    items = json.loads(args.questions.read_text(encoding="utf-8"))
    if args.only:
        keys = set(args.only.split(","))
        items = [i for i in items if i["id"] in keys or i["category"] in keys]
    tmp = Path(tempfile.mkdtemp(prefix="gn-eval-"))
    docs = tmp / "docs"
    shutil.copytree(args.docs, docs)
    s = Settings(data_dir=tmp / "runtime", docs_dir=docs, qdrant_path=str(tmp / "runtime" / "qdrant"),
                 boxes_csv=args.boxes, glossary_tsv=args.glossary, smtp_host="", imap_host="",
                 index_on_start=False, demo_ui_only=False)
    t = time.perf_counter()
    svc = build_services(s)
    st = svc.indexer.sync()
    print(f"индекс: {st.added} файлов, {time.perf_counter() - t:.1f} с, LLM {svc.llm.provider}/{svc.llm.model}",
          flush=True)
    rows = []
    for item in items:
        sid = f"eval-{item['id']}"
        extra = {}
        t0 = time.perf_counter()
        if item["id"] == "F02":
            f01 = svc.db.query("SELECT id FROM escalations WHERE session_id='eval-F01' ORDER BY id DESC LIMIT 1")
            if f01:
                svc.escalations.answer(f01[0]["id"], EXPERT_F01, expert="quality-test@example.local")
        ans = svc.assistant.ask(item["question"], session_id=sid)
        if item["category"] == "C":
            extra["first"] = ans
            if ans["mode"] == "clarify":
                ans = svc.assistant.ask(item["question"], session_id=sid, division="unknown")
        ms = int((time.perf_counter() - t0) * 1000)
        ok, why = check(item, ans, extra)
        rows.append({"id": item["id"], "category": item["category"], "question": item["question"], "ok": ok,
                     "why": why, "ms": ms, "mode": ans["mode"], "text": ans.get("text"),
                     "notice": ans.get("notice"),
                     "citations": [{"ref": c["ref"], "quote": c.get("quote")} for c in ans.get("citations") or []],
                     "sections": [{"division": x["division_name"], "mode": x["mode"], "text": x["text"],
                                   "refs": [c["ref"] for c in x["citations"]]} for x in ans.get("sections") or []],
                     "escalation": bool(ans.get("escalation")), "missing_docs": ans.get("missing_docs"),
                     "glossary": ans.get("glossary"), "tokens": ans.get("tokens"),
                     "first_mode": extra.get("first", {}).get("mode")})
        print(f"{item['id']} {'OK ' if ok else 'BAD'} {ms:6d} мс  {why}", flush=True)
    by = defaultdict(list)
    for r in rows:
        by[r["category"]].append(r)
    lines = ["# Прогон тестовых вопросов Softline", "",
             f"LLM: {svc.llm.provider} / {svc.llm.model}; эмбеддинги {s.embed_model}; реранкер {s.reranker_model}",
             f"Дата прогона: {time.strftime('%d.%m.%Y %H:%M')}", "",
             "| Категория | Что проверяем | Прошло | Медиана, с | Макс, с |", "|---|---|---|---|---|"]
    for c in sorted(by):
        rs = by[c]
        lines.append(f"| {c} | {CATS.get(c, '')} | {sum(r['ok'] for r in rs)}/{len(rs)} | "
                     f"{statistics.median(r['ms'] for r in rs) / 1000:.1f} | {max(r['ms'] for r in rs) / 1000:.1f} |")
    tot = sum(r["ok"] for r in rows)
    lines += ["", f"Итого: {tot}/{len(rows)}; медиана {statistics.median(r['ms'] for r in rows) / 1000:.1f} с", "",
              "| № | Итог | мс | Детали |", "|---|---|---|---|"]
    lines += [f"| {r['id']} | {'да' if r['ok'] else 'нет'} | {r['ms']} | {r['why']} |" for r in rows]
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "softline_results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    (args.out / "softline_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines[:16]))
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
