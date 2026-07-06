"""Core capture & retrieval: metrics, events, notes, profile."""
from healthledger.runtime import *  # noqa: F401,F403


@mcp.tool(annotations={"title": "Log a health metric", "readOnlyHint": False, "idempotentHint": False})
def log_metric(
    metric: str,
    value: float,
    unit: str | None = None,
    note: str | None = None,
    timestamp: str | None = None,
    user: str | None = None,
) -> dict:
    """Record one quantitative reading (a point in a time series).

    Args:
        metric: snake_case name, e.g. 'weight_kg', 'systolic_bp', 'glucose_mgdl',
            'sleep_hours', 'heart_rate', 'mood' (0-10), 'spo2', 'temperature_c'.
        value: numeric value.
        unit: unit string, e.g. 'kg', 'mmHg', 'mg/dL', 'hours', 'bpm', '%', '°C'.
        note: optional context ('after run', 'fasting', 'left arm').
        timestamp: when it was measured. ISO8601, 'YYYY-MM-DD', or 'now' (default).
        user: which person this belongs to; defaults to the primary user.

    Returns the stored row.
    """
    u = _tool_user(user, "log_metric")
    ts = _parse_ts(timestamp)
    m = _metric(metric)
    val = _finite_float(value, "value")
    clean_unit = _optional_text(unit, "unit", max_chars=32)
    clean_note = _optional_text(note, "note")
    _audit("log_metric", f"{_audit_user(u)} metric_hash={_fingerprint(m)} ts={ts}")
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO metrics(user, ts, metric, value, unit, note) VALUES (?,?,?,?,?,?)",
            (u, ts, m, val, clean_unit, clean_note),
        )
        rid = cur.lastrowid
    return {"id": rid, "user": u, "ts": ts, "metric": m, "value": val,
            "unit": clean_unit, "note": clean_note}


@mcp.tool(annotations={"title": "Log a health event", "readOnlyHint": False, "idempotentHint": False})
def log_event(
    category: str,
    name: str,
    detail: str | None = None,
    severity: float | None = None,
    timestamp: str | None = None,
    user: str | None = None,
) -> dict:
    """Record a discrete event: a symptom, a medication dose, a meal, or an activity.

    Args:
        category: one of 'symptom', 'medication', 'meal', 'activity', 'other'.
        name: short label, e.g. 'headache', 'ibuprofen', 'lunch', '5k run'.
        detail: specifics — dose ('400 mg'), food ('chicken salad'), distance, etc.
        severity: optional magnitude, e.g. symptom intensity 0-10.
        timestamp: ISO8601, 'YYYY-MM-DD', or 'now' (default).
        user: which person; defaults to the primary user.

    Returns the stored row.
    """
    u = _tool_user(user, "log_event")
    ts = _parse_ts(timestamp)
    cat = _category(category)
    clean_name = _required_text(name, "name", max_chars=160)
    clean_detail = _optional_text(detail, "detail")
    clean_severity = _optional_finite_float(severity, "severity")
    _audit("log_event", f"{_audit_user(u)} category={cat} ts={ts}")
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO events(user, ts, category, name, detail, severity, note) "
            "VALUES (?,?,?,?,?,?,?)",
            (u, ts, cat, clean_name, clean_detail, clean_severity, None),
        )
        rid = cur.lastrowid
    return {"id": rid, "user": u, "ts": ts, "category": cat, "name": clean_name,
            "detail": clean_detail, "severity": clean_severity}


@mcp.tool(annotations={"title": "Log a free-form note", "readOnlyHint": False, "idempotentHint": False})
def log_note(
    body: str,
    title: str | None = None,
    tags: str | None = None,
    timestamp: str | None = None,
    user: str | None = None,
) -> dict:
    """Record a free-form health journal entry.

    Args:
        body: the note text.
        title: optional short title.
        tags: optional comma-separated tags ('sleep,stress').
        timestamp: ISO8601, 'YYYY-MM-DD', or 'now' (default).
        user: which person; defaults to the primary user.
    """
    u = _tool_user(user, "log_note")
    ts = _parse_ts(timestamp)
    clean_title = _optional_text(title, "title", max_chars=160)
    clean_body = _required_text(body, "body")
    clean_tags = _optional_text(tags, "tags", max_chars=256)
    _audit("log_note", f"{_audit_user(u)} ts={ts}")
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO notes(user, ts, title, body, tags) VALUES (?,?,?,?,?)",
            (u, ts, clean_title, clean_body, clean_tags),
        )
        rid = cur.lastrowid
    return {"id": rid, "user": u, "ts": ts, "title": clean_title, "body": clean_body,
            "tags": clean_tags}


@mcp.tool(annotations={"title": "Set a profile field", "readOnlyHint": False, "idempotentHint": True})
def set_profile(key: str, value: str, user: str | None = None) -> dict:
    """Set (upsert) a durable profile fact for a user.

    Args:
        key: e.g. 'age', 'height_cm', 'conditions', 'allergies', 'medications',
            'blood_type', 'sex', 'goal'.
        value: the value to store (free text).
        user: which person; defaults to the primary user.
    """
    u = _tool_user(user, "set_profile")
    k = _profile_key(key)
    clean_value = _required_text(value, "value")
    _audit("set_profile", f"{_audit_user(u)} key_hash={_fingerprint(k)}")
    with _db() as conn:
        conn.execute(
            "INSERT INTO profile(user, key, value) VALUES (?,?,?) "
            "ON CONFLICT(user, key) DO UPDATE SET value=excluded.value",
            (u, k, clean_value),
        )
    return {"user": u, "key": k, "value": clean_value}


@mcp.tool(annotations={"title": "Get profile", "readOnlyHint": True, "idempotentHint": True})
def get_profile(user: str | None = None) -> dict:
    """Return all durable profile facts for a user as a key/value map."""
    u = _tool_user(user, "get_profile")
    _audit("get_profile", _audit_user(u))
    with _db() as conn:
        rows = _rows(conn.execute(
            "SELECT key, value FROM profile WHERE user=? ORDER BY key", (u,)))
    return {"user": u, "profile": {r["key"]: r["value"] for r in rows}}


@mcp.tool(annotations={"title": "Delete a profile field", "readOnlyHint": False, "destructiveHint": True, "idempotentHint": True})
def delete_profile(key: str, user: str | None = None) -> dict:
    """Delete one durable profile fact by key. Use for corrections/removals."""
    u = _tool_user(user, "delete_profile")
    k = _profile_key(key)
    _audit("delete_profile", f"{_audit_user(u)} key_hash={_fingerprint(k)}")
    with _db() as conn:
        cur = conn.execute("DELETE FROM profile WHERE user=? AND key=?", (u, k))
        n = cur.rowcount
    return {"deleted": bool(n), "user": u, "key": k, "rows_affected": n}


@mcp.tool(annotations={"title": "Get metric history", "readOnlyHint": True, "idempotentHint": True})
def get_metrics(
    metric: str | None = None,
    since: str | None = None,
    until: str | None = None,
    user: str | None = None,
    limit: int = 200,
) -> dict:
    """Return raw metric readings, newest first, optionally filtered by metric/date.

    Args:
        metric: restrict to one metric name; omit for all.
        since / until: ISO8601 or 'YYYY-MM-DD' bounds (inclusive).
        user: which person; defaults to the primary user.
        limit: max rows (capped by server).
    """
    u = _tool_user(user, "get_metrics")
    lo, hi = _range_bounds(since, until)
    lim = _limit(limit, default=200)
    clean_metric = _metric(metric) if metric else None
    metric_part = f" metric_hash={_fingerprint(clean_metric)}" if clean_metric else " metric=*"
    _audit("get_metrics", f"{_audit_user(u)}{metric_part} since={lo} until={hi} limit={lim}")
    sql = "SELECT id, ts, metric, value, unit, note FROM metrics WHERE user=? AND ts BETWEEN ? AND ?"
    args: list = [u, lo, hi]
    if clean_metric:
        sql += " AND metric=?"
        args.append(clean_metric)
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(lim)
    with _db() as conn:
        rows = _rows(conn.execute(sql, args))
    return {"user": u, "count": len(rows), "metrics": rows}


@mcp.tool(annotations={"title": "Get event history", "readOnlyHint": True, "idempotentHint": True})
def get_events(
    category: str | None = None,
    since: str | None = None,
    until: str | None = None,
    user: str | None = None,
    limit: int = 200,
) -> dict:
    """Return recorded events (symptoms/medications/meals/activities), newest first."""
    u = _tool_user(user, "get_events")
    lo, hi = _range_bounds(since, until)
    lim = _limit(limit, default=200)
    clean_category = _category(category) if category else None
    category_part = f" category={clean_category}" if clean_category else " category=*"
    _audit("get_events", f"{_audit_user(u)}{category_part} since={lo} until={hi} limit={lim}")
    sql = ("SELECT id, ts, category, name, detail, severity FROM events "
           "WHERE user=? AND ts BETWEEN ? AND ?")
    args: list = [u, lo, hi]
    if clean_category:
        sql += " AND category=?"
        args.append(clean_category)
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(lim)
    with _db() as conn:
        rows = _rows(conn.execute(sql, args))
    return {"user": u, "count": len(rows), "events": rows}


@mcp.tool(annotations={"title": "Get notes", "readOnlyHint": True, "idempotentHint": True})
def get_notes(
    since: str | None = None,
    until: str | None = None,
    tag: str | None = None,
    user: str | None = None,
    limit: int = 100,
) -> dict:
    """Return journal notes, newest first, optionally filtered by date or tag substring."""
    u = _tool_user(user, "get_notes")
    lo, hi = _range_bounds(since, until)
    lim = _limit(limit, default=100)
    clean_tag = _optional_text(tag, "tag", max_chars=64)
    tag_part = f" tag_hash={_fingerprint(clean_tag)}" if clean_tag else " tag=*"
    _audit("get_notes", f"{_audit_user(u)}{tag_part} since={lo} until={hi} limit={lim}")
    sql = "SELECT id, ts, title, body, tags FROM notes WHERE user=? AND ts BETWEEN ? AND ?"
    args: list = [u, lo, hi]
    if clean_tag:
        sql += " AND tags LIKE ? ESCAPE '\\'"
        args.append(_like_pattern(clean_tag))
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(lim)
    with _db() as conn:
        rows = _rows(conn.execute(sql, args))
    return {"user": u, "count": len(rows), "notes": rows}


@mcp.tool(annotations={"title": "List tracked metrics", "readOnlyHint": True, "idempotentHint": True})
def list_metrics(user: str | None = None) -> dict:
    """List which metrics have data for a user, with counts and the latest value of each."""
    u = _tool_user(user, "list_metrics")
    _audit("list_metrics", _audit_user(u))
    with _db() as conn:
        rows = _rows(conn.execute(
            """
            SELECT m.metric,
                   COUNT(*)                AS n,
                   MIN(m.ts)               AS first_ts,
                   MAX(m.ts)               AS last_ts
            FROM metrics m WHERE m.user=?
            GROUP BY m.metric ORDER BY m.metric
            """, (u,)))
        # attach latest value per metric
        for r in rows:
            last = conn.execute(
                "SELECT value, unit FROM metrics WHERE user=? AND metric=? "
                "ORDER BY ts DESC, id DESC LIMIT 1", (u, r["metric"])).fetchone()
            r["latest_value"] = last["value"] if last else None
            r["unit"] = last["unit"] if last else None
    return {"user": u, "metrics": rows}


@mcp.tool(annotations={"title": "Analyze a metric", "readOnlyHint": True, "idempotentHint": True})
def analyze_metric(
    metric: str,
    since: str | None = None,
    until: str | None = None,
    user: str | None = None,
) -> dict:
    """Compute analysis-ready statistics and a trend for one metric over a window.

    Returns count, first/last/latest, min/max, mean, median, standard deviation,
    and a least-squares linear trend (slope per day, projected next value, direction).
    These are descriptive statistics for interpretation — not a diagnosis.

    Args:
        metric: the metric name to analyse (e.g. 'weight_kg').
        since / until: ISO8601 or 'YYYY-MM-DD' bounds (inclusive).
        user: which person; defaults to the primary user.
    """
    u = _tool_user(user, "analyze_metric")
    lo, hi = _range_bounds(since, until)
    m = _metric(metric)
    _audit("analyze_metric", f"{_audit_user(u)} metric_hash={_fingerprint(m)} since={lo} until={hi}")
    with _db() as conn:
        rows = _rows(conn.execute(
            "SELECT ts, value, unit FROM metrics WHERE user=? AND metric=? "
            "AND ts BETWEEN ? AND ? ORDER BY ts ASC", (u, m, lo, hi)))
    if not rows:
        return {"user": u, "metric": m, "count": 0,
                "message": f"no '{m}' readings for user '{u}' in this window."}
    values = [r["value"] for r in rows]
    points = [(r["ts"], r["value"]) for r in rows]
    unit = rows[-1]["unit"]
    stats = {
        "count": len(values),
        "unit": unit,
        "first_ts": rows[0]["ts"],
        "last_ts": rows[-1]["ts"],
        "first_value": values[0],
        "latest_value": values[-1],
        "min": min(values),
        "max": max(values),
        "mean": round(statistics.fmean(values), 4),
        "median": round(statistics.median(values), 4),
        "stdev": round(statistics.pstdev(values), 4) if len(values) > 1 else 0.0,
    }
    stats["trend"] = _linreg_per_day(points)
    return {"user": u, "metric": m, "window": {"since": lo, "until": hi}, "stats": stats}
