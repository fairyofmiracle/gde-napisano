from __future__ import annotations

from app.index.models import FakeEmbedder


class CountingEmbedder(FakeEmbedder):
    def __init__(self):
        self.passages = 0

    def embed_passages(self, texts):
        self.passages += len(texts)
        return super().embed_passages(texts)


DOC_V1 = """Положение о пропусках
1. Пропуска
1.1. Постоянный пропуск оформляется в первый рабочий день.
1.2. Временный пропуск для гостя заказывается за 1 час до визита.
1.3. Утерянный пропуск восстанавливается за 500 рублей.
"""


def test_incremental_add_change_delete(tmp_path, services_factory):
    import random

    from app.llm.fake import FakeLLM
    from app.services import build_services
    from tests.conftest import make_settings

    emb = CountingEmbedder()
    s = make_settings(tmp_path)
    svc = build_services(s, llm=FakeLLM(), embedder=emb, rng=random.Random(0))
    try:
        docs = s.docs_dir
        (docs / "admin").mkdir(parents=True)
        f = docs / "admin" / "Пропуска.txt"
        f.write_text(DOC_V1, encoding="utf-8")

        st = svc.indexer.sync()
        assert (st.added, st.clauses_added) == (1, 3)
        assert svc.store.count() == 3 and emb.passages == 3

        # без изменений — ничего не пересчитывается
        st = svc.indexer.sync()
        assert st.unchanged == 1 and st.clauses_added == 0 and emb.passages == 3

        # меняем один пункт: одна новая точка, одна удалённая, две остаются
        f.write_text(DOC_V1.replace("500 рублей", "700 рублей"), encoding="utf-8")
        st = svc.indexer.sync()
        assert (st.updated, st.clauses_added, st.clauses_removed, st.clauses_kept) == (1, 1, 1, 2)
        assert svc.store.count() == 3 and emb.passages == 4
        texts = [r["text"] for r in svc.db.query("SELECT text FROM clauses")]
        assert any("700 рублей" in t for t in texts) and not any("500 рублей" in t for t in texts)
        res = svc.searcher.search("Сколько стоит восстановить утерянный пропуск?")
        assert "700 рублей" in res.candidates[0].text

        # сокращаем документ: удалённые пункты уходят из индекса
        f.write_text("1. Пропуска\n1.1. Постоянный пропуск оформляется в первый рабочий день.\n", encoding="utf-8")
        st = svc.indexer.sync()
        assert st.clauses_removed == 2 and svc.store.count() == 1

        # удаляем файл: в Qdrant и SQLite ничего не остаётся
        f.unlink()
        st = svc.indexer.sync()
        assert st.removed == 1 and svc.store.count() == 0
        assert svc.db.query("SELECT COUNT(*) n FROM clauses")[0]["n"] == 0
        assert svc.searcher.search("временный пропуск для гостя").candidates == []
    finally:
        svc.close()


def test_box_from_folder_and_guess(services_factory):
    svc = services_factory()
    docs = svc.indexer.docs_dir
    (docs / "it").mkdir(parents=True)
    (docs / "it" / "a.txt").write_text("1.1. Пароль меняется каждые 90 дней.", encoding="utf-8")
    (docs / "b.txt").write_text("1.1. Заявка на закупку согласуется руководителем. Порог конкурса закупки 500 тысяч.",
                                encoding="utf-8")
    svc.indexer.sync()
    boxes = {r["rel_path"]: r["box"] for r in svc.db.query("SELECT rel_path, box FROM files")}
    assert boxes["it/a.txt"] == "it"
    assert boxes["b.txt"] == "procurement"


def test_parse_error_is_reported_and_old_points_removed(services_factory):
    svc = services_factory()
    f = svc.indexer.docs_dir / "x.txt"
    svc.indexer.docs_dir.mkdir(parents=True, exist_ok=True)
    f.write_text("1.1. Текст пункта про отпуск.", encoding="utf-8")
    svc.indexer.sync()
    assert svc.store.count() == 1
    f.write_text("   ", encoding="utf-8")
    st = svc.indexer.sync()
    assert st.errors and svc.store.count() == 0
    row = svc.db.query("SELECT status, error FROM files")[0]
    assert row["status"] == "error" and row["error"]
