"""Retrieval & grounding tools.

Closes the gap where free-text history was opaque (exact-key only) and computed
claims couldn't be traced to a row:

  * semantic_search — relevance-ranked full-text over all free text (SQLite FTS5;
    local, no embeddings/network).
  * get_record      — resolve a (table, id) citation to the exact row.
  * data_coverage   — expose what exists / is absent / is stale, as data.
"""
from healthledger.runtime import *  # noqa: F401,F403

# table -> (free-text columns to index, best-effort date expression)
_SEARCH_CORPUS = {
    "notes": (["title", "body", "tags"], "ts"),
    "events": (["name", "detail", "note"], "ts"),
    "conditions": (["name", "body_site", "notes"], "onset_date"),
    "allergies": (["allergen", "reaction", "notes"], "noted_date"),
    "medications": (["name", "generic_name", "indication", "instructions", "notes"], "start_date"),
    "medication_logs": (["medication_name", "status", "dose_taken", "note"], "COALESCE(taken_ts, scheduled_ts, created_ts)"),
    "lab_reports": (["title", "summary", "lab_name", "ordering_provider"], "COALESCE(collection_date, report_date, created_ts)"),
    "lab_results": (["analyte", "value_text", "flag", "notes"], "result_date"),
    "biomarkers": (["biomarker", "value_text", "category", "notes"], "measured_date"),
    "tumors": (["tumor_name", "cancer_type", "body_site", "biomarker_summary", "notes"], "diagnosis_date"),
    "encounters": (["encounter_type", "provider", "facility", "reason", "assessment", "plan", "notes"], "encounter_date"),
    "procedures": (["name", "body_site", "provider", "outcome", "notes"], "procedure_date"),
    "imaging_reports": (["modality", "body_site", "facility", "findings", "impression", "notes"], "imaging_date"),
    "immunizations": (["vaccine", "provider", "facility", "notes"], "immunization_date"),
    "documents": (["title", "summary", "content_text", "tags"], "document_date"),
    "family_history": (["relation", "condition_name", "cause_of_death", "notes"], None),
    "care_tasks": (["title", "task_type", "notes"], "due_date"),
    "health_records": (["title", "body", "tags", "extra_json"], "record_date"),
    "reproductive_records": (["record_type", "method", "outcome", "source", "notes"], "start_date"),
    "substance_use_logs": (["substance", "context", "notes"], "timestamp"),
}


def _table_columns(conn, table: str) -> set:
    return {r["name"] for r in _rows(conn.execute(f"PRAGMA table_info({table})"))}


def _fts_match_query(query: str) -> str | None:
    """OR of prefix terms — favours recall; BM25 handles ranking."""
    terms = [t for t in re.findall(r"[A-Za-z0-9]+", query.lower()) if len(t) >= 2][:16]
    if not terms:
        return None
    return " OR ".join(f"{t}*" for t in terms)


@mcp.tool(annotations={"title": "Semantic search", "readOnlyHint": True, "idempotentHint": True})
def semantic_search(query: str, user: str | None = None, limit: int = 20,
                    tables: str | None = None) -> dict:
    """Relevance-ranked full-text search across all free-text health history.

    Unlike search_records (exact case-insensitive substring), this builds a
    transient SQLite FTS5 index over every free-text field — notes, event
    details, encounter reasons/assessments/plans, lab flags, imaging findings,
    document text, care-task notes, and more — stems terms, and ranks hits by
    BM25. So the model can query history by meaning/keywords instead of an exact
    key and gets the best matches first. This is lexical ranking (local, no
    embeddings or network), not vector semantics.

    Every hit carries source_table + record_id (feed them to get_record to pull
    the exact row) and a highlighted snippet, so findings can be grounded in a row.

    Args:
        query: free-text query; terms are OR-ed with prefix + stemming for recall.
        user: which person; defaults to the primary user.
        limit: max hits to return (ranked best-first).
        tables: optional comma-separated subset of tables to search.
    """
    u = _tool_user(user, "semantic_search")
    lim = _limit(limit, default=20)
    clean_query = _required_text(query, "query", max_chars=256)
    match = _fts_match_query(clean_query)
    _audit("semantic_search", f"{_audit_user(u)} query_hash={_fingerprint(clean_query)} limit={lim}")
    if match is None:
        return {"user": u, "query": clean_query, "count": 0,
                "message": "query has no searchable terms (need 2+ letters/digits)"}
    wanted = {t.strip().lower() for t in tables.split(",") if t.strip()} if tables else None
    with _db() as conn:
        conn.execute("DROP TABLE IF EXISTS temp.search_idx")
        conn.execute(
            "CREATE VIRTUAL TABLE temp.search_idx USING fts5("
            "src UNINDEXED, rid UNINDEXED, ref_date UNINDEXED, content, tokenize='porter unicode61')"
        )
        indexed = []
        for table, (text_cols, date_expr) in _SEARCH_CORPUS.items():
            if wanted is not None and table not in wanted:
                continue
            present = [c for c in text_cols if c in _table_columns(conn, table)]
            if not present:
                continue
            content = " || ' ' || ".join(f"COALESCE({c}, '')" for c in present)
            try:
                rows = conn.execute(
                    f"SELECT id, {date_expr or 'NULL'} AS d, {content} AS content "
                    f"FROM {table} WHERE user=?", (u,),
                ).fetchall()
            except sqlite3.OperationalError:
                continue
            if rows:
                conn.executemany(
                    "INSERT INTO temp.search_idx(src, rid, ref_date, content) VALUES (?, ?, ?, ?)",
                    [(table, r[0], r[1], r[2]) for r in rows],
                )
                indexed.append(table)
        hits = _rows(conn.execute(
            "SELECT src AS source_table, rid AS record_id, ref_date AS date, "
            "snippet(search_idx, 3, '[', ']', ' … ', 12) AS snippet, bm25(search_idx) AS bm25 "
            "FROM search_idx WHERE search_idx MATCH ? ORDER BY bm25(search_idx) LIMIT ?",
            (match, lim),
        ))
    for h in hits:
        h["relevance"] = round(-float(h.pop("bm25")), 4)  # higher = more relevant
    return {
        "user": u,
        "query": clean_query,
        "match_expression": match,
        "tables_searched": indexed,
        "count": len(hits),
        "hits": hits,
        "note": "Ranked full-text (BM25) over free text; call get_record(source_table, record_id) to cite a hit.",
    }


@mcp.tool(annotations={"title": "Get a record by id", "readOnlyHint": True, "idempotentHint": True})
def get_record(table: str, record_id: int, user: str | None = None) -> dict:
    """Fetch one exact stored row by table + id — the citation primitive.

    Search and analysis tools return source_table / source_ids; this resolves one
    of those to the full row, so a statement can be grounded in the actual data.

    Args:
        table: a health data table (e.g. 'lab_results', 'notes', 'metrics').
        record_id: the integer id of the row.
        user: which person; defaults to the primary user.
    """
    u = _tool_user(user, "get_record")
    t = _required_text(table, "table", max_chars=64).strip().lower()
    if t not in DATA_TABLES:
        raise ValueError(f"table must be one of: {', '.join(sorted(DATA_TABLES))}")
    rid = int(record_id)
    _audit("get_record", f"{_audit_user(u)} table={t} id={rid}")
    with _db() as conn:
        rows = _rows(conn.execute(f"SELECT * FROM {t} WHERE id=? AND user=?", (rid, u)))
    if not rows:
        return {"user": u, "table": t, "record_id": rid, "found": False,
                "message": "no such record for this user"}
    return {"user": u, "table": t, "record_id": rid, "found": True, "record": rows[0]}


# table -> date expression for recency (only tables with dated, user-scoped rows).
_COVERAGE_DATE_EXPR = {
    "metrics": "ts", "events": "ts", "notes": "ts",
    "lab_results": "COALESCE(result_date, created_ts)",
    "biomarkers": "COALESCE(measured_date, created_ts)",
    "wearable_samples": "start_ts",
    "substance_use_logs": "timestamp",
    "encounters": "COALESCE(encounter_date, created_ts)",
    "procedures": "COALESCE(procedure_date, created_ts)",
    "imaging_reports": "COALESCE(imaging_date, created_ts)",
    "immunizations": "COALESCE(immunization_date, created_ts)",
    "medications": "COALESCE(start_date, created_ts)",
    "medication_logs": "COALESCE(taken_ts, scheduled_ts, created_ts)",
    "conditions": "COALESCE(onset_date, created_ts)",
    "allergies": "COALESCE(noted_date, created_ts)",
    "tumors": "COALESCE(diagnosis_date, created_ts)",
    "documents": "COALESCE(document_date, created_ts)",
    "care_tasks": "COALESCE(due_date, created_ts)",
    "reproductive_records": "COALESCE(start_date, created_ts)",
    "family_history": "created_ts",
    "lab_reports": "COALESCE(collection_date, report_date, created_ts)",
    "health_records": "COALESCE(record_date, created_ts)",
    "wearable_sources": "created_ts",
}

# per-signal inventories: label -> (table, name column, date expression)
_COVERAGE_INVENTORIES = {
    "metrics": ("metrics", "metric", "ts"),
    "labs": ("lab_results", "analyte", "COALESCE(result_date, created_ts)"),
    "biomarkers": ("biomarkers", "biomarker", "COALESCE(measured_date, created_ts)"),
    "wearables": ("wearable_samples", "sample_type", "start_ts"),
    "substances": ("substance_use_logs", "substance", "timestamp"),
}


@mcp.tool(annotations={"title": "Data coverage / what's missing", "readOnlyHint": True, "idempotentHint": True})
def data_coverage(user: str | None = None, source: str | None = None,
                  name: str | None = None) -> dict:
    """Expose what data actually exists — and what's absent or stale — as data.

    Purpose: let the model check the record before asserting, instead of
    confabulating around missing values. Two modes:

      * source + name given → coverage for that one signal: present?, count,
        first/last date, and days since the last reading.
      * otherwise → a whole-record inventory: per-domain counts with latest date
        and staleness, an explicit list of EMPTY domains, and a per-signal
        inventory (which metrics / analytes / biomarkers / wearable types /
        substances are tracked, each with count + last date + staleness).

    Args:
        source: 'metric' | 'wearable' | 'lab' | 'biomarker' | 'substance' (with name).
        name: a specific signal to scope the coverage check to.
        user: which person; defaults to the primary user.

    Descriptive availability only — it reports absence, it does not recommend tests.
    """
    u = _tool_user(user, "data_coverage")
    _audit("data_coverage", f"{_audit_user(u)} source={source or '*'}")
    with _db() as conn:
        if source and name:
            skey, cfg = _series_source(source)
            clean = _keyish(_required_text(name, "name"), "name")
            ts_expr = f"COALESCE({cfg['ts_col']}, created_ts)" if cfg["coalesce_created"] else cfg["ts_col"]
            row = conn.execute(
                f"SELECT COUNT(*) AS n, MIN({ts_expr}) AS first, MAX({ts_expr}) AS last "
                f"FROM {cfg['table']} WHERE user=? AND {cfg['name_col']}=? AND {cfg['value_col']} IS NOT NULL",
                (u, clean),
            ).fetchone()
            n, last = row["n"], row["last"]
            return {
                "user": u, "source": skey, "name": clean,
                "present": n > 0, "count": n,
                "first": row["first"], "latest": last,
                "days_since_last": _days_since(last) if last else None,
                "message": None if n else "no readings recorded for this signal",
            }
        domains, empty = {}, []
        for table, date_expr in _COVERAGE_DATE_EXPR.items():
            try:
                r = conn.execute(
                    f"SELECT COUNT(*) AS n, MAX({date_expr}) AS last FROM {table} WHERE user=?", (u,)
                ).fetchone()
            except sqlite3.OperationalError:
                r = conn.execute(f"SELECT COUNT(*) AS n, NULL AS last FROM {table} WHERE user=?", (u,)).fetchone()
            if r["n"] == 0:
                empty.append(table)
                continue
            domains[table] = {"count": r["n"], "latest": r["last"],
                              "days_since_last": _days_since(r["last"]) if r["last"] else None}
        inventories = {}
        for label, (table, name_col, date_expr) in _COVERAGE_INVENTORIES.items():
            rows = _rows(conn.execute(
                f"SELECT {name_col} AS name, COUNT(*) AS n, MAX({date_expr}) AS last "
                f"FROM {table} WHERE user=? GROUP BY {name_col} ORDER BY n DESC", (u,)
            ))
            inventories[label] = [
                {"name": r["name"], "count": r["n"], "latest": r["last"],
                 "days_since_last": _days_since(r["last"]) if r["last"] else None}
                for r in rows
            ]
    return {
        "user": u,
        "as_of": _now_iso(),
        "populated_domains": domains,
        "empty_domains": sorted(empty),
        "tracked_signals": inventories,
        "note": "Absence is reported explicitly so it can be cited rather than assumed.",
    }
