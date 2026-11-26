"""Прогон тестовых вопросов на настоящих моделях (e5 + реранкер), подбор порогов и метрики.

    python scripts/evaluate.py --questions eval/questions.csv --out eval

Документы — условные регламенты из scripts/sample_content.py (создаются во временной папке).
LLM по умолчанию фейковая: метрики касаются поиска и выбора режима; LLM_PROVIDER=ollama — с настоящей моделью.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.config import Settings  # noqa: E402
from app.services import build_services  # noqa: E402
from make_sample_docs import main as make_docs  # noqa: E402
from sample_content import EXPECTED  # noqa: E402


def load_questions(path: Path) -> list[dict]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def hit(cands, expected) -> int | None:
    """Позиция (1..) первого ожидаемого пункта среди кандидатов."""
    exp = {(d, n) for d, n in expected}
    for i, c in enumerate(cands, start=1):
        if (c["doc_title"], c["clause_no"]) in exp:
            return i
    return None


def classify(top: float, conf: float, unc: float) -> str:
    return "confident" if top >= conf else ("uncertain" if top >= unc else "not_found")


def score_grid(rows: list[dict], conf: float, unc: float) -> dict:
    m = {"conf_ok": 0, "conf_wrong": 0, "unc": 0, "nf_answerable": 0, "na_nf": 0, "na_unc": 0, "na_conf": 0,
         "amb_conf_ok": 0, "amb_unc": 0, "amb_conf_wrong": 0, "amb_nf": 0}
    for r in rows:
        mode = classify(r["top"], conf, unc)
        if r["kind"] == "answerable":
            if mode == "confident":
                m["conf_ok" if r["hit1"] else "conf_wrong"] += 1
            elif mode == "uncertain":
                m["unc"] += 1
            else:
                m["nf_answerable"] += 1
        elif r["kind"] == "no_answer":
            m[{"not_found": "na_nf", "uncertain": "na_unc", "confident": "na_conf"}[mode]] += 1
        else:
            if mode == "confident":
                m["amb_conf_ok" if r["hit1"] else "amb_conf_wrong"] += 1
            elif mode == "uncertain":
                m["amb_unc"] += 1
            else:
                m["amb_nf"] += 1
    # цель: уверенно и верно отвечать, честно говорить «нет»; уверенная ошибка стоит дороже всего
    m["objective"] = (2 * m["conf_ok"] + 1 * m["unc"] - 4 * m["conf_wrong"] - 2 * m["nf_answerable"]
                      + 2 * m["na_nf"] + 0.5 * m["na_unc"] - 4 * m["na_conf"]
                      + 1 * (m["amb_unc"] + m["amb_conf_ok"]) - 2 * m["amb_conf_wrong"])
    return m


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", default=str(ROOT / "eval" / "questions.csv"))
    ap.add_argument("--out", default=str(ROOT / "eval"))
    ap.add_argument("--llm", default="fake")
    ap.add_argument("--confident", type=float, default=None, help="порог уверенного ответа (по умолчанию из настроек)")
    ap.add_argument("--uncertain", type=float, default=None, help="порог вариантов (по умолчанию из настроек)")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    tmp = Path(tempfile.mkdtemp(prefix="docagent_eval_"))
    make_docs(tmp / "docs")
    settings = Settings(data_dir=tmp / "runtime", docs_dir=tmp / "docs", qdrant_url="", qdrant_path=":memory:",
                        llm_provider=args.llm, index_on_start=False, rate_limit_per_minute=0,
                        boxes_csv=ROOT / "data" / "boxes.csv")
    t0 = time.perf_counter()
    svc = build_services(settings, rng=random.Random(0))
    stats = svc.indexer.sync()
    index_s = time.perf_counter() - t0
    print("индекс:", stats.as_dict(), f"{index_s:.1f} с")
    svc.searcher.search("прогрев")  # загрузка моделей не входит в замер

    rows = []
    for q in load_questions(Path(args.questions)):
        qid = int(q["id"])
        kind = "no_answer" if q["type"] == "no_answer" else ("ambiguous" if q["type"] == "ambiguous" else "answerable")
        t = time.perf_counter()
        res = svc.searcher.search(q["question"])
        ms = (time.perf_counter() - t) * 1000
        seen, cands = set(), []
        for c in res.candidates:
            if c.key not in seen:
                seen.add(c.key)
                cands.append({"doc_title": c.doc_title, "clause_no": c.clause_no, "score": round(c.score, 4)})
        h = hit(cands, EXPECTED.get(qid, []))
        rows.append({"id": qid, "question": q["question"], "type": q["type"], "kind": kind,
                     "route": ",".join(res.route.boxes) or "-", "route_method": res.route.method,
                     "expected_boxes": q["boxes"], "top": cands[0]["score"] if cands else 0.0,
                     "second": cands[1]["score"] if len(cands) > 1 else 0.0,
                     "top_ref": f'{cands[0]["doc_title"]}, п. {cands[0]["clause_no"]}' if cands else "",
                     "hit1": h == 1, "hit3": h is not None and h <= 3, "rank": h or "", "search_ms": round(ms)})
        print(f'{qid:>2} {kind:<10} top={rows[-1]["top"]:.3f} hit1={rows[-1]["hit1"]!s:<5} {rows[-1]["top_ref"]}')

    # подбор порогов по сетке
    best = None
    grid = [round(x * 0.01, 2) for x in range(2, 100)]
    for conf in grid:
        for unc in grid:
            if unc >= conf:
                continue
            m = score_grid(rows, conf, unc)
            key = (m["objective"], -m["conf_wrong"] - m["na_conf"], conf)
            if best is None or key > best[0]:
                best = (key, conf, unc, m)
    _, best_conf, best_unc, best_m = best
    conf = args.confident if args.confident is not None else settings.confident_threshold
    unc = args.uncertain if args.uncertain is not None else settings.uncertain_threshold
    m = score_grid(rows, conf, unc)
    # проверка через полный путь ответа с рабочими порогами (режим и проверка цитаты)
    svc.settings.confident_threshold, svc.settings.uncertain_threshold = conf, unc
    modes = {}
    latencies = []
    for r in rows:
        ans = svc.answers.ask(r["question"])
        r["mode"] = ans["mode"]
        r["cited"] = ans["citations"][0]["ref"] if ans["citations"] else ""
        r["first_variant"] = ans["variants"][0]["ref"] if ans["variants"] else ""
        r["quote_verified"] = bool(ans["citations"] and ans["citations"][0]["verified"])
        r["quote_check"] = ans.get("quote_check", "")
        r["quote"] = ans["citations"][0]["quote"][:200] if ans["citations"] else ""
        r["answer_text"] = ans.get("text", "")[:300]
        latencies.append(ans["latency_ms"])
        modes.setdefault(r["kind"], {}).setdefault(ans["mode"], 0)
        modes[r["kind"]][ans["mode"]] += 1

    answerable = [r for r in rows if r["kind"] == "answerable"]
    ambiguous = [r for r in rows if r["kind"] == "ambiguous"]
    no_answer = [r for r in rows if r["kind"] == "no_answer"]
    ans_scores = [r["top"] for r in answerable if r["hit1"]]
    na_scores = [r["top"] for r in no_answer]
    summary = {
        "documents": stats.files_seen, "clauses": stats.clauses_added, "index_seconds": round(index_s, 1),
        "questions": len(rows), "answerable": len(answerable), "ambiguous": len(ambiguous), "no_answer": len(no_answer),
        "hit@1_answerable": round(sum(r["hit1"] for r in answerable) / len(answerable), 3),
        "hit@3_answerable": round(sum(r["hit3"] for r in answerable) / len(answerable), 3),
        "thresholds": {"confident": conf, "uncertain": unc},
        "metrics_at_thresholds": m, "modes": modes,
        "grid_optimum": {"confident": best_conf, "uncertain": best_unc, "metrics": best_m},
        "route_contains_expected_box": round(sum(
            1 for r in rows if r["route"] == "-" or set(r["route"].split(",")) & set(r["expected_boxes"].split(";"))
        ) / len(rows), 3),
        "confident_correct_cited": sum(1 for r in answerable if r["mode"] == "confident" and r["cited"]
                                       and r["cited"].startswith(r["top_ref"].split(", п. ")[0])),
        "uncertain_first_variant_correct": sum(1 for r in answerable if r["mode"] == "uncertain" and r["hit1"]),
        "downgraded_by_llm": {k: sum(1 for r in rows if r["quote_check"] == k) for k in ("failed", "no_answer")},
        "score_answerable_correct": {"min": min(ans_scores), "median": statistics.median(ans_scores)},
        "score_no_answer": {"max": max(na_scores), "median": statistics.median(na_scores)},
        "search_ms_median": statistics.median(r["search_ms"] for r in rows),
        "answer_ms_median": statistics.median(latencies), "answer_ms_max": max(latencies),
        "llm": args.llm,
    }
    with (out / "results.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    svc.close()


if __name__ == "__main__":
    main()
