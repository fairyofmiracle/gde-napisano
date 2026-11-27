from __future__ import annotations

import random
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from app.config import Settings  # noqa: E402
from app.llm.fake import FakeLLM  # noqa: E402
from app.services import build_services  # noqa: E402


def make_settings(tmp_path: Path, **kw) -> Settings:
    base = dict(
        data_dir=tmp_path / "runtime", docs_dir=tmp_path / "docs", boxes_csv=ROOT / "examples" / "boxes.csv",
        qdrant_url="", qdrant_path=":memory:", embed_provider="fake", reranker_provider="fake", llm_provider="fake",
        admin_token="", index_on_start=False, rate_limit_per_minute=0, confident_threshold=0.6,
        uncertain_threshold=0.15, box_fallback_threshold=0.5, feedback_sample_rate=0.02,
        # механику переформулировки проверяем на любой неуверенности; порог «серой зоны» — в test_stream.py
        agent_rewrite_llm_min_score=0.0,
    )
    base.update(kw)
    return Settings(_env_file=None, **base)


@pytest.fixture
def settings(tmp_path):
    return make_settings(tmp_path)


@pytest.fixture
def services_factory(tmp_path):
    created = []

    def factory(llm=None, rng=None, **kw):
        s = make_settings(tmp_path, **kw)
        svc = build_services(s, llm=llm or FakeLLM(), rng=rng or random.Random(1))
        created.append(svc)
        return svc

    yield factory
    for svc in created:
        svc.close()


@pytest.fixture(scope="session")
def sample_docs(tmp_path_factory):
    from make_sample_docs import main

    out = tmp_path_factory.mktemp("sample_docs")
    main(out)
    return out


def has_tesseract() -> bool:
    return shutil.which("tesseract") is not None


def has_soffice() -> bool:
    return shutil.which("soffice") is not None
