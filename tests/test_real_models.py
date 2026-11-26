"""Проверки на настоящих моделях. Запуск: RUN_SLOW=1 pytest tests/test_real_models.py (нужен кэш моделей)."""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("RUN_SLOW") != "1", reason="медленный тест, нужен RUN_SLOW=1")


def test_cross_encoder_scores_in_unit_interval():
    from app.index.models import CrossEncoderReranker

    rr = CrossEncoderReranker("BAAI/bge-reranker-v2-m3")
    scores = rr.score("Сколько дней ежегодного отпуска?", [
        "Продолжительность ежегодного оплачиваемого отпуска составляет 28 календарных дней.",
        "Пароль меняется каждые 90 дней.",
        "",
    ])
    assert all(0.0 <= s <= 1.0 for s in scores)
    assert scores[0] > 0.5 > scores[1]


def test_end_to_end_three_modes(services_factory, sample_docs):
    import shutil

    svc = services_factory(embed_provider="e5", reranker_provider="cross-encoder")
    shutil.copytree(sample_docs, svc.indexer.docs_dir, dirs_exist_ok=True)
    svc.indexer.sync()
    a = svc.answers.ask("Сколько календарных дней ежегодного оплачиваемого отпуска положено?")
    assert a["mode"] == "confident" and a["citations"][0]["clause_no"] == "3.1"
    b = svc.answers.ask("Можно ли приводить собаку в офис?")
    assert b["mode"] == "not_found" and b["owner"]
    c = svc.answers.ask("Можно ли работать из дома?")
    assert c["mode"] in ("uncertain", "confident")
