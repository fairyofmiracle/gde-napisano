"""Инкрементальная индексация: хэш файла → хэши пунктов → только изменения в Qdrant."""
from __future__ import annotations

import hashlib
import logging
import threading
import uuid
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from app.boxes import GENERAL_BOX, BoxDirectory
from app.db import Database
from app.parsing import SUPPORTED_EXTENSIONS, Clause, ParseError, doc_title_from_path, parse_file

log = logging.getLogger(__name__)
_NS = uuid.UUID("6f1c2b1e-3d4a-4c5b-9e8f-0a1b2c3d4e5f")


@dataclass
class IndexStats:
    files_seen: int = 0
    added: int = 0
    updated: int = 0
    unchanged: int = 0
    removed: int = 0
    clauses_added: int = 0
    clauses_removed: int = 0
    clauses_kept: int = 0
    errors: list[str] = field(default_factory=list)

    def merge(self, other: IndexStats) -> None:
        for k in ("files_seen", "added", "updated", "unchanged", "removed", "clauses_added", "clauses_removed",
                  "clauses_kept"):
            setattr(self, k, getattr(self, k) + getattr(other, k))
        self.errors.extend(other.errors)

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def doc_id_for(rel_path: str) -> str:
    return hashlib.sha1(rel_path.encode("utf-8")).hexdigest()[:16]


def search_text(title: str, section: str, clause_no: str, text: str, kind: str = "clause") -> str:
    head = [title]
    if section:
        head.append(section)
    if clause_no:
        head.append(clause_no if kind == "table_row" else f"п. {clause_no}")
    return ". ".join(head) + ". " + text


class Indexer:
    def __init__(self, settings, db: Database, store, embedder, bm25, boxes: BoxDirectory) -> None:
        self.settings = settings
        self.db = db
        self.store = store
        self.embedder = embedder
        self.bm25 = bm25
        self.boxes = boxes
        self.lock = threading.RLock()
        self.running = False
        self.docs_dir = Path(settings.docs_dir).resolve()
        self.docs_dir.mkdir(parents=True, exist_ok=True)

    # --- служебное ---------------------------------------------------------------------
    def prepare(self) -> None:
        """Проверить коллекцию; если модель эмбеддингов сменилась — индекс строится заново."""
        model_key = f"{self.settings.embed_provider}:{self.settings.embed_model}"
        changed = self.db.get_meta("embed_model") not in ("", model_key)
        created = self.store.ensure(recreate=changed)
        if created or changed:
            self.db.execute("DELETE FROM clauses")
            self.db.execute("DELETE FROM files")
        self.db.set_meta("embed_model", model_key)
        self.rebuild_bm25()

    def rebuild_bm25(self) -> None:
        rows = self.db.query(
            "SELECT c.point_id, f.box, f.title, c.section, c.clause_no, c.text, c.kind "
            "FROM clauses c JOIN files f ON f.doc_id = c.doc_id"
        )
        self.bm25.rebuild([(r["point_id"], r["box"],
                            search_text(r["title"], r["section"], r["clause_no"], r["text"], r["kind"])) for r in rows])

    def box_for(self, rel_path: str, clauses: list[Clause] | None, title: str) -> str:
        first = rel_path.split("/", 1)[0] if "/" in rel_path else ""
        if first in self.boxes.boxes or first == GENERAL_BOX:
            return first
        by_name = self.boxes.box_for_doc(rel_path, title)
        if by_name:
            return by_name
        sample = title + " " + " ".join(c.text for c in (clauses or [])[:15])
        return self.boxes.guess_box(sample)

    def _iter_files(self):
        for p in sorted(self.docs_dir.rglob("*")):
            if not p.is_file() or p.suffix.lower() not in SUPPORTED_EXTENSIONS:
                continue
            rel = p.relative_to(self.docs_dir)
            if any(part.startswith((".", "~$")) for part in rel.parts):
                continue
            yield p, rel.as_posix()

    # --- основное ----------------------------------------------------------------------
    def sync(self) -> IndexStats:
        """Пройти папку документов: новые и изменённые проиндексировать, удалённые убрать из индекса."""
        with self.lock:
            self.running = True
            try:
                stats = IndexStats()
                seen: set[str] = set()
                for path, rel in self._iter_files():
                    seen.add(rel)
                    stats.merge(self._index_one(path, rel))
                for row in self.db.query("SELECT doc_id, rel_path FROM files"):
                    if row["rel_path"] not in seen:
                        stats.merge(self._remove(row["doc_id"]))
                self.rebuild_bm25()
                log.info("Индексация: %s", stats.as_dict())
                return stats
            finally:
                self.running = False

    def index_path(self, path: Path) -> IndexStats:
        with self.lock:
            rel = path.resolve().relative_to(self.docs_dir).as_posix()
            stats = self._index_one(path, rel)
            self.rebuild_bm25()
            return stats

    def remove_doc(self, doc_id: str) -> IndexStats:
        with self.lock:
            stats = self._remove(doc_id)
            self.rebuild_bm25()
            return stats

    def _remove(self, doc_id: str) -> IndexStats:
        ids = [r["point_id"] for r in self.db.query("SELECT point_id FROM clauses WHERE doc_id=?", (doc_id,))]
        self.store.delete(ids)
        self.db.execute("DELETE FROM clauses WHERE doc_id=?", (doc_id,))
        self.db.execute("DELETE FROM files WHERE doc_id=?", (doc_id,))
        return IndexStats(removed=1, clauses_removed=len(ids))

    def _index_one(self, path: Path, rel: str) -> IndexStats:
        stats = IndexStats(files_seen=1)
        doc_id = doc_id_for(rel)
        sha = file_sha256(path)
        st = path.stat()
        old = self.db.query("SELECT sha256, status FROM files WHERE doc_id=?", (doc_id,))
        if old and old[0]["sha256"] == sha and old[0]["status"] == "ok":
            stats.unchanged = 1
            stats.clauses_kept = self.db.query("SELECT COUNT(*) AS n FROM clauses WHERE doc_id=?", (doc_id,))[0]["n"]
            return stats

        title = doc_title_from_path(path)
        old_ids = {r["point_id"] for r in self.db.query("SELECT point_id FROM clauses WHERE doc_id=?", (doc_id,))}
        try:
            clauses = parse_file(path, self.settings, title=title)
        except ParseError as e:
            # старое содержимое больше не соответствует файлу — убираем его из поиска
            self.store.delete(list(old_ids))
            self.db.execute("DELETE FROM clauses WHERE doc_id=?", (doc_id,))
            self._save_file(doc_id, rel, title, self.box_for(rel, None, title), sha, st, 0, "error", str(e))
            stats.errors.append(f"{rel}: {e}")
            stats.clauses_removed = len(old_ids)
            return stats
        except Exception as e:  # неожиданный сбой разбора не должен ронять всю индексацию
            log.exception("Сбой разбора %s", rel)
            stats.errors.append(f"{rel}: внутренняя ошибка разбора ({type(e).__name__})")
            return stats

        box = self.box_for(rel, clauses, title)
        occurrence: Counter[str] = Counter()
        new: dict[str, Clause] = {}
        for c in clauses:
            h = c.text_hash
            pid = str(uuid.uuid5(_NS, f"{doc_id}:{h}:{occurrence[h]}"))
            occurrence[h] += 1
            new[pid] = c

        to_delete = old_ids - new.keys()
        to_add = [pid for pid in new if pid not in old_ids]
        if to_add:
            texts = [search_text(title, new[p].section, new[p].clause_no, new[p].text, new[p].kind) for p in to_add]
            vectors = self.embedder.embed_passages(texts)
            payloads = [{"doc_id": doc_id, "box": box, "clause_no": new[p].clause_no} for p in to_add]
            self.store.upsert(to_add, vectors, payloads)
        self.store.delete(list(to_delete))
        prev_box = self.db.query("SELECT box FROM files WHERE doc_id=?", (doc_id,))
        if prev_box and prev_box[0]["box"] != box and (old_ids & new.keys()):
            self.store.set_box(doc_id, box)

        self._save_file(doc_id, rel, title, box, sha, st, len(clauses), "ok", "")
        self.db.execute("DELETE FROM clauses WHERE doc_id=?", (doc_id,))
        self.db.executemany(
            "INSERT INTO clauses(point_id, doc_id, ord, clause_no, section, page, kind, part, text, text_hash) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            [(pid, doc_id, c.ord, c.clause_no, c.section, c.page, c.kind, c.part, c.text, c.text_hash)
             for pid, c in new.items()],
        )
        if old:
            stats.updated = 1
        else:
            stats.added = 1
        stats.clauses_added = len(to_add)
        stats.clauses_removed = len(to_delete)
        stats.clauses_kept = len(new) - len(to_add)
        return stats

    def _save_file(self, doc_id, rel, title, box, sha, st, n, status, error) -> None:
        self.db.execute(
            "INSERT INTO files(doc_id, rel_path, title, box, sha256, size, mtime, n_clauses, status, error, indexed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,datetime('now')) ON CONFLICT(doc_id) DO UPDATE SET "
            "title=excluded.title, box=excluded.box, sha256=excluded.sha256, size=excluded.size, mtime=excluded.mtime,"
            " n_clauses=excluded.n_clauses, status=excluded.status, error=excluded.error, indexed_at=excluded.indexed_at",
            (doc_id, rel, title, box, sha, st.st_size, st.st_mtime, n, status, error),
        )
