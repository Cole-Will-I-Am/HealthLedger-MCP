"""Cryptographic data integrity — hash-chained row-level provenance.

Adds an immutable, verifiable chain of custody to every health data table.
Each new row is cryptographically linked to the previous row for the same
user, creating a tamper-evident chain that can be verified at any time.

After 20-30 years of data accumulation, this is what proves the records
are authentic, complete, and unmodified — the foundation for data that
can be sold, shared with researchers, or presented as legal evidence.

Architecture:
- Every data table gets three chain columns: chain_hash (SHA-256),
  chain_prev (points to previous row's hash), chain_seq (monotonic).
- A chain tip table stores the most recent hash per (user, table).
- verify_integrity() walks every chain end-to-end, recomputes each hash,
  and reports any breaks or gaps.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Optional

from healthledger.config import DATA_TABLES
from healthledger.db import _db, _rows
from healthledger.timeutil import _now_iso

logger = logging.getLogger(__name__)

__all__ = [
    "CHAIN_COLUMNS",
    "CHAIN_TABLES",
    "chain_hash_row",
    "chain_insert",
    "get_chain_tip",
    "verify_integrity",
    "verify_all_users",
    "_migrate_chain_schema",
]

# Tables that get chain columns (all user-scoped data tables).
# Profile and schema_meta are excluded — profile is key/value facts,
# schema_meta is metadata.
CHAIN_TABLES = tuple(t for t in DATA_TABLES if t not in {"profile"})

# Columns added to every chain-enabled table.
CHAIN_COLUMNS = [
    ("chain_hash", "chain_hash TEXT"),
    ("chain_prev", "chain_prev TEXT"),
    ("chain_seq", "chain_seq INTEGER"),
]

# Chain tips — one row per (user, table) tracking the head of each chain.
CHAIN_TIP_DDL = """
    CREATE TABLE IF NOT EXISTS chain_tips (
        user        TEXT NOT NULL,
        table_name  TEXT NOT NULL,
        tip_hash    TEXT NOT NULL,
        tip_seq     INTEGER NOT NULL DEFAULT 0,
        updated_ts  TEXT NOT NULL,
        PRIMARY KEY (user, table_name)
    );
"""


def _migrate_chain_schema(conn) -> None:
    """Add chain columns to all data tables and create chain_tips.

    Safe to call on every startup — uses ALTER TABLE IF NOT EXISTS
    pattern via PRAGMA table_info.
    """
    from healthledger.schema import _ensure_columns

    for table in CHAIN_TABLES:
        _ensure_columns(conn, table, CHAIN_COLUMNS)

    conn.execute(CHAIN_TIP_DDL)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS ix_chain_tips_user
        ON chain_tips(user, table_name);
    """)


def chain_hash_row(row: dict, prev_hash: Optional[str] = None) -> str:
    """Compute SHA-256 hash for a row, chaining to previous row's hash.

    The hash covers: previous row's hash + sorted key/value pairs of the
    current row (excluding chain columns and id). This means any change to
    any value produces a completely different hash, breaking the chain.

    Args:
        row: dictionary of column -> value for this row.
        prev_hash: the chain_hash of the immediately preceding row
                   (None for the first row in a chain).

    Returns:
        64-character lowercase hex SHA-256 digest.
    """
    # Exclude chain columns and auto-generated fields from the hash payload
    excluded = {"chain_hash", "chain_prev", "chain_seq", "id"}
    payload = {}
    for k, v in sorted(row.items()):
        if k not in excluded:
            payload[k] = v

    canonical = json.dumps(payload, sort_keys=True, default=str)
    base = (prev_hash or "") + canonical
    return hashlib.sha256(base.encode("utf-8")).hexdigest()


def chain_insert(conn, table: str, user: str, row_data: dict) -> dict:
    """Insert a row with chain fields, updating the tip.

    Call this instead of a raw INSERT for any chain-enabled table.
    It reads the current tip, computes the chain hash, adds chain
    columns to row_data, and updates chain_tips atomically.

    Args:
        conn: SQLite connection.
        table: table name.
        user: user label.
        row_data: column -> value dict (without chain fields).

    Returns:
        The row_data dict with chain_hash, chain_prev, and chain_seq added.
    """
    if table not in CHAIN_TABLES:
        return row_data  # not a chain-enabled table

    # Read current tip
    tip = conn.execute(
        "SELECT tip_hash, tip_seq FROM chain_tips WHERE user=? AND table_name=?",
        (user, table),
    ).fetchone()

    prev_hash = tip["tip_hash"] if tip else None
    next_seq = (tip["tip_seq"] + 1) if tip else 1

    # Compute chain hash for this row
    chained = dict(row_data)
    chained["chain_seq"] = next_seq
    chained["chain_prev"] = prev_hash
    chained["chain_hash"] = chain_hash_row(chained, prev_hash)

    # Update the tip
    now = _now_iso()
    conn.execute(
        """INSERT INTO chain_tips (user, table_name, tip_hash, tip_seq, updated_ts)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(user, table_name) DO UPDATE SET
               tip_hash = excluded.tip_hash,
               tip_seq  = excluded.tip_seq,
               updated_ts = excluded.updated_ts""",
        (user, table, chained["chain_hash"], next_seq, now),
    )

    return chained


def get_chain_tip(conn, user: str, table: str) -> Optional[dict]:
    """Return the current chain tip for a user + table."""
    row = conn.execute(
        "SELECT tip_hash, tip_seq, updated_ts FROM chain_tips "
        "WHERE user=? AND table_name=?",
        (user, table),
    ).fetchone()
    return dict(row) if row else None


def verify_integrity(user: str, table: Optional[str] = None) -> dict:
    """Verify the hash chain for a user across all (or one) data tables.

    Walks every row in chain_seq order, recomputes each hash from the
    stored data + previous hash, and compares to the stored chain_hash.
    Reports any breaks, gaps, or missing chain data.

    Args:
        user: which person to verify.
        table: optional single table name. If omitted, verifies all
               chain-enabled tables.

    Returns:
        dict with keys: user, verified_at, tables (list of per-table
        results), total_rows, broken_rows, intact.
    """
    tables = [table] if table else list(CHAIN_TABLES)
    results = []
    total_rows = 0
    total_broken = 0

    with _db() as conn:
        for tbl in sorted(tables):
            rows = conn.execute(
                f"SELECT * FROM {tbl} WHERE user=? ORDER BY chain_seq ASC",
                (user,),
            ).fetchall()

            if not rows:
                results.append({
                    "table": tbl,
                    "rows": 0,
                    "broken": 0,
                    "message": "no data",
                })
                continue

            table_rows = 0
            table_broken = 0
            prev_hash = None
            breaks = []

            for row in rows:
                row_dict = dict(row)
                table_rows += 1

                stored_hash = row_dict.get("chain_hash")
                stored_seq = row_dict.get("chain_seq")

                if not stored_hash:
                    breaks.append({
                        "chain_seq": stored_seq,
                        "id": row_dict.get("id"),
                        "reason": "missing chain_hash",
                    })
                    table_broken += 1
                    continue

                # Recompute expected hash
                expected = chain_hash_row(row_dict, prev_hash)
                if expected != stored_hash:
                    breaks.append({
                        "chain_seq": stored_seq,
                        "id": row_dict.get("id"),
                        "expected_hash": expected,
                        "stored_hash": stored_hash,
                        "reason": "hash mismatch",
                    })
                    table_broken += 1

                prev_hash = stored_hash

            # Verify tip matches
            tip = get_chain_tip(conn, user, tbl)
            tip_ok = (
                tip and tip["tip_hash"] == prev_hash and tip["tip_seq"] == table_rows
            ) if prev_hash else True

            total_rows += table_rows
            total_broken += table_broken

            results.append({
                "table": tbl,
                "rows": table_rows,
                "broken": table_broken,
                "intact": table_broken == 0,
                "tip_matches": tip_ok,
                "tip": tip,
                "last_hash": prev_hash,
                "breaks": breaks[:20],  # cap for output size
            })

    return {
        "user": user,
        "verified_at": _now_iso(),
        "intact": total_broken == 0,
        "total_rows": total_rows,
        "broken_rows": total_broken,
        "tables_checked": len(results),
        "tables_broken": sum(1 for r in results if r["broken"] > 0),
        "tables": results,
    }


def verify_all_users() -> dict:
    """Verify integrity chains for every user in the database.

    Returns:
        dict keyed by user, each value is a verify_integrity() result.
    """
    with _db() as conn:
        users = [
            r["user"] for r in conn.execute(
                "SELECT DISTINCT user FROM chain_tips ORDER BY user"
            ).fetchall()
        ]

    results = {}
    for u in users:
        results[u] = verify_integrity(u)

    return {
        "verified_at": _now_iso(),
        "users": len(users),
        "all_intact": all(r["intact"] for r in results.values()),
        "results": results,
    }
