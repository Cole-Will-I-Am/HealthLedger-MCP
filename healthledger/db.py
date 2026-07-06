"""SQLite connection helper and row adapter."""
from __future__ import annotations

from healthledger.config import *  # noqa: F401,F403


@contextmanager
def _db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        yield conn
        conn.commit()
    finally:
        conn.close()

def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]
