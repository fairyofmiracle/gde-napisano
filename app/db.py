"""SQLite: манифест индекса, история ответов, отзывы и практика."""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS files (
    doc_id TEXT PRIMARY KEY,
    rel_path TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    box TEXT NOT NULL DEFAULT '',
    sha256 TEXT NOT NULL,
    size INTEGER NOT NULL DEFAULT 0,
    mtime REAL NOT NULL DEFAULT 0,
    n_clauses INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'ok',
    error TEXT NOT NULL DEFAULT '',
    indexed_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS clauses (
    point_id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL REFERENCES files(doc_id) ON DELETE CASCADE,
    ord INTEGER NOT NULL,
    clause_no TEXT NOT NULL,
    section TEXT NOT NULL,
    page INTEGER,
    kind TEXT NOT NULL,
    part INTEGER NOT NULL DEFAULT 0,
    text TEXT NOT NULL,
    text_hash TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS clauses_doc ON clauses(doc_id);
CREATE TABLE IF NOT EXISTS answers (
    answer_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    question TEXT NOT NULL,
    q_vec BLOB,
    boxes TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL,
    top_score REAL,
    candidates TEXT NOT NULL DEFAULT '[]',
    llm_provider TEXT NOT NULL DEFAULT '',
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    answer_id TEXT NOT NULL,
    question TEXT NOT NULL,
    q_vec BLOB,
    doc_id TEXT NOT NULL DEFAULT '',
    doc_title TEXT NOT NULL DEFAULT '',
    clause_no TEXT NOT NULL DEFAULT '',
    helpful INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS feedback_clause ON feedback(doc_id, clause_no);
CREATE TABLE IF NOT EXISTS practice (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    answer_id TEXT NOT NULL,
    question TEXT NOT NULL,
    q_vec BLOB,
    doc_id TEXT NOT NULL DEFAULT '',
    doc_title TEXT NOT NULL DEFAULT '',
    clause_no TEXT NOT NULL DEFAULT '',
    box TEXT NOT NULL DEFAULT '',
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'new'
);
"""


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Мягкие миграции для баз, созданных прежними версиями."""
        with self._lock:
            cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(feedback)")}
            if "client" not in cols:
                self.conn.execute("ALTER TABLE feedback ADD COLUMN client TEXT NOT NULL DEFAULT ''")
            # один голос на (ответ, пункт): старые повторы схлопываем до последнего
            self.conn.execute(
                "DELETE FROM feedback WHERE id NOT IN (SELECT MAX(id) FROM feedback GROUP BY answer_id, doc_id, clause_no)")
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS feedback_one_vote ON feedback(answer_id, doc_id, clause_no)")
            self.conn.commit()

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def executemany(self, sql: str, rows) -> None:
        with self._lock:
            self.conn.executemany(sql, rows)
            self.conn.commit()

    def query(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def get_meta(self, key: str, default: str = "") -> str:
        rows = self.query("SELECT value FROM meta WHERE key=?", (key,))
        return rows[0]["value"] if rows else default

    def set_meta(self, key: str, value: str) -> None:
        self.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                     (key, value))

    def close(self) -> None:
        with self._lock:
            self.conn.close()
