"""Reproductive health, substance-use logs, and wearable data."""
from healthledger.runtime import *  # noqa: F401,F403


@mcp.tool(annotations={"title": "Add reproductive health record", "readOnlyHint": False, "idempotentHint": False})
def add_reproductive_record(
    record_type: str,
    start_date: str | None = None,
    end_date: str | None = None,
    flow_intensity: str | None = None,
    pain_level: float | None = None,
    cervical_mucus: str | None = None,
    ovulation_predicted_date: str | None = None,
    gestational_age_weeks: float | None = None,
    due_date: str | None = None,
    outcome: str | None = None,
    method: str | None = None,
    insertion_date: str | None = None,
    removal_date: str | None = None,
    replacement_due_date: str | None = None,
    source: str | None = None,
    notes: str | None = None,
    extra_json: str | None = None,
    user: str | None = None,
) -> dict:
    """Store menstrual cycle, pregnancy, contraception, fertility sign, or related reproductive record."""
    u = _tool_user(user, "add_reproductive_record")
    clean_type = _keyish(record_type, "record_type")
    row = {
        "user": u,
        "created_ts": _now_iso(),
        "record_type": clean_type,
        "start_date": _parse_date(start_date, "start_date"),
        "end_date": _parse_date(end_date, "end_date"),
        "flow_intensity": _optional_text(flow_intensity, "flow_intensity", max_chars=80),
        "pain_level": _optional_finite_float(pain_level, "pain_level"),
        "cervical_mucus": _optional_text(cervical_mucus, "cervical_mucus", max_chars=120),
        "ovulation_predicted_date": _parse_date(ovulation_predicted_date, "ovulation_predicted_date"),
        "gestational_age_weeks": _optional_finite_float(gestational_age_weeks, "gestational_age_weeks"),
        "due_date": _parse_date(due_date, "due_date"),
        "outcome": _optional_text(outcome, "outcome", max_chars=160),
        "method": _optional_text(method, "method", max_chars=160),
        "insertion_date": _parse_date(insertion_date, "insertion_date"),
        "removal_date": _parse_date(removal_date, "removal_date"),
        "replacement_due_date": _parse_date(replacement_due_date, "replacement_due_date"),
        "source": _optional_text(source, "source", max_chars=200),
        "notes": _optional_text(notes, "notes"),
        "extra_json": _json_text(extra_json, "extra_json"),
    }
    _audit("add_reproductive_record", f"{_audit_user(u)} type={clean_type}")
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO reproductive_records(user, created_ts, record_type, start_date, end_date, flow_intensity, "
            "pain_level, cervical_mucus, ovulation_predicted_date, gestational_age_weeks, due_date, outcome, method, "
            "insertion_date, removal_date, replacement_due_date, source, notes, extra_json) "
            "VALUES (:user, :created_ts, :record_type, :start_date, :end_date, :flow_intensity, :pain_level, "
            ":cervical_mucus, :ovulation_predicted_date, :gestational_age_weeks, :due_date, :outcome, :method, "
            ":insertion_date, :removal_date, :replacement_due_date, :source, :notes, :extra_json)",
            row,
        )
        row["id"] = cur.lastrowid
    return row


@mcp.tool(annotations={"title": "List reproductive health records", "readOnlyHint": True, "idempotentHint": True})
def list_reproductive_records(
    record_type: str | None = None,
    since: str | None = None,
    until: str | None = None,
    user: str | None = None,
    limit: int = 200,
) -> dict:
    """List reproductive health records, optionally filtered by type/date."""
    u = _tool_user(user, "list_reproductive_records")
    lo, hi = _range_bounds(since, until)
    lim = _limit(limit, default=200)
    sql = (
        "SELECT * FROM reproductive_records WHERE user=? "
        "AND substr(COALESCE(start_date, due_date, replacement_due_date, created_ts), 1, 10) BETWEEN ? AND ?"
    )
    args: list = [u, lo.split("T", 1)[0], hi.split("T", 1)[0]]
    if record_type:
        sql += " AND record_type=?"
        args.append(_keyish(record_type, "record_type"))
    sql += " ORDER BY COALESCE(start_date, due_date, replacement_due_date, created_ts) DESC, id DESC LIMIT ?"
    args.append(lim)
    _audit("list_reproductive_records", f"{_audit_user(u)} limit={lim}")
    with _db() as conn:
        rows = _rows(conn.execute(sql, args))
    return {"user": u, "count": len(rows), "reproductive_records": rows}


@mcp.tool(annotations={"title": "Analyze reproductive trend", "readOnlyHint": True, "idempotentHint": True, "statementType": "descriptive"})
def analyze_reproductive_trend(user: str | None = None, limit: int = 24) -> dict:
    """Return descriptive cycle length/duration stats from stored cycle records only."""
    u = _tool_user(user, "analyze_reproductive_trend")
    lim = _limit(limit, default=24)
    _audit("analyze_reproductive_trend", f"{_audit_user(u)} limit={lim}")
    with _db() as conn:
        rows = _rows(conn.execute(
            "SELECT id, start_date, end_date, flow_intensity, pain_level FROM reproductive_records "
            "WHERE user=? AND record_type='cycle' AND start_date IS NOT NULL "
            "ORDER BY start_date DESC LIMIT ?",
            (u, lim),
        ))
        upcoming = _rows(conn.execute(
            "SELECT id, record_type, method, due_date, replacement_due_date FROM reproductive_records "
            "WHERE user=? AND (due_date IS NOT NULL OR replacement_due_date IS NOT NULL) "
            "ORDER BY COALESCE(due_date, replacement_due_date) ASC LIMIT 20",
            (u,),
        ))
    ordered = list(reversed(rows))
    starts = [datetime.strptime(r["start_date"], "%Y-%m-%d").date() for r in ordered]
    cycle_lengths = [
        float((starts[i] - starts[i - 1]).days)
        for i in range(1, len(starts))
        if (starts[i] - starts[i - 1]).days >= 1
    ]
    period_lengths = []
    for r in ordered:
        if r.get("start_date") and r.get("end_date"):
            start = datetime.strptime(r["start_date"], "%Y-%m-%d").date()
            end = datetime.strptime(r["end_date"], "%Y-%m-%d").date()
            if end >= start:
                period_lengths.append(float((end - start).days + 1))
    cycle_length_days = {
        "count": len(cycle_lengths),
        "min": min(cycle_lengths) if cycle_lengths else None,
        "max": max(cycle_lengths) if cycle_lengths else None,
        "mean": round(statistics.fmean(cycle_lengths), 2) if cycle_lengths else None,
        "median": round(statistics.median(cycle_lengths), 2) if cycle_lengths else None,
    }
    period_length_days = {
        "count": len(period_lengths),
        "min": min(period_lengths) if period_lengths else None,
        "max": max(period_lengths) if period_lengths else None,
        "mean": round(statistics.fmean(period_lengths), 2) if period_lengths else None,
        "median": round(statistics.median(period_lengths), 2) if period_lengths else None,
    }
    latest_date = rows[0]["start_date"] if rows else None
    return _envelope(
        value=cycle_length_days["mean"],
        unit="days",
        days_stale=_days_since(latest_date) if latest_date else None,
        source_ids=[r["id"] for r in rows],
        user=u,
        cycle_records=len(rows),
        cycle_length_days=cycle_length_days,
        period_length_days=period_length_days,
        upcoming_reproductive_dates=upcoming,
        disclaimer="Descriptive reproductive tracking only; not fertility, contraception, or medical advice.",
    )


@mcp.tool(annotations={"title": "Add substance use log", "readOnlyHint": False, "idempotentHint": False})
def add_substance_use_log(
    substance: str,
    amount: float | None = None,
    unit: str | None = None,
    timestamp: str | None = None,
    frequency: str | None = None,
    route: str | None = None,
    context: str | None = None,
    notes: str | None = None,
    user: str | None = None,
) -> dict:
    """Store time-varying substance exposure such as alcohol, nicotine, caffeine, cannabis, or other."""
    u = _tool_user(user, "add_substance_use_log")
    clean_substance = _keyish(substance, "substance")
    row = {
        "user": u,
        "created_ts": _now_iso(),
        "timestamp": _parse_ts(timestamp),
        "substance": clean_substance,
        "amount": _optional_finite_float(amount, "amount"),
        "unit": _optional_text(unit, "unit", max_chars=80),
        "frequency": _optional_text(frequency, "frequency", max_chars=160),
        "route": _optional_text(route, "route", max_chars=80),
        "context": _optional_text(context, "context", max_chars=300),
        "notes": _optional_text(notes, "notes"),
    }
    _audit("add_substance_use_log", f"{_audit_user(u)} substance_hash={_fingerprint(clean_substance)}")
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO substance_use_logs(user, created_ts, timestamp, substance, amount, unit, frequency, route, context, notes) "
            "VALUES (:user, :created_ts, :timestamp, :substance, :amount, :unit, :frequency, :route, :context, :notes)",
            row,
        )
        row["id"] = cur.lastrowid
    return row


@mcp.tool(annotations={"title": "List substance use logs", "readOnlyHint": True, "idempotentHint": True})
def list_substance_use_logs(
    substance: str | None = None,
    since: str | None = None,
    until: str | None = None,
    user: str | None = None,
    limit: int = 200,
) -> dict:
    """List substance-use logs, optionally filtered by substance/date."""
    u = _tool_user(user, "list_substance_use_logs")
    lo, hi = _range_bounds(since, until)
    lim = _limit(limit, default=200)
    sql = "SELECT * FROM substance_use_logs WHERE user=? AND timestamp BETWEEN ? AND ?"
    args: list = [u, lo, hi]
    if substance:
        sql += " AND substance=?"
        args.append(_keyish(substance, "substance"))
    sql += " ORDER BY timestamp DESC, id DESC LIMIT ?"
    args.append(lim)
    _audit("list_substance_use_logs", f"{_audit_user(u)} limit={lim}")
    with _db() as conn:
        rows = _rows(conn.execute(sql, args))
    return {"user": u, "count": len(rows), "substance_use_logs": rows}


@mcp.tool(annotations={"title": "Analyze substance trend", "readOnlyHint": True, "idempotentHint": True, "statementType": "descriptive"})
def analyze_substance_trend(
    substance: str,
    since: str | None = None,
    until: str | None = None,
    user: str | None = None,
) -> dict:
    """Return descriptive daily-total stats for one substance when numeric amounts exist."""
    u = _tool_user(user, "analyze_substance_trend")
    lo, hi = _range_bounds(since, until)
    clean_substance = _keyish(substance, "substance")
    _audit("analyze_substance_trend", f"{_audit_user(u)} substance_hash={_fingerprint(clean_substance)}")
    with _db() as conn:
        rows = _rows(conn.execute(
            "SELECT id, timestamp, amount, unit FROM substance_use_logs "
            "WHERE user=? AND substance=? AND amount IS NOT NULL AND timestamp BETWEEN ? AND ? "
            "ORDER BY timestamp ASC",
            (u, clean_substance, lo, hi),
        ))
    if not rows:
        return _envelope(value=None, user=u, substance=clean_substance, count=0)
    daily: dict[str, float] = {}
    for r in rows:
        day = r["timestamp"].split("T", 1)[0]
        daily[day] = daily.get(day, 0.0) + float(r["amount"])
    points = [(f"{day}T00:00:00+00:00", value) for day, value in sorted(daily.items())]
    values = list(daily.values())
    latest_days_stale = _days_since(rows[-1]["timestamp"])
    total_amount = round(sum(values), 4)
    return _envelope(
        value=total_amount,
        unit=rows[-1]["unit"],
        days_stale=latest_days_stale,
        source_ids=[r["id"] for r in rows],
        user=u,
        substance=clean_substance,
        count=len(rows),
        logged_days=len(daily),
        latest_days_stale=latest_days_stale,
        total_amount=total_amount,
        mean_per_logged_day=round(statistics.fmean(values), 4),
        median_per_logged_day=round(statistics.median(values), 4),
        daily_totals=[{"date": day, "total": total} for day, total in sorted(daily.items())][-30:],
        trend=_linreg_per_day(points) if len(points) >= 2 else None,
        disclaimer="Descriptive substance-use tracking only; not treatment or medical advice.",
    )


@mcp.tool(annotations={"title": "Add wearable source", "readOnlyHint": False, "idempotentHint": False})
def add_wearable_source(
    name: str,
    source_type: str | None = None,
    manufacturer: str | None = None,
    model: str | None = None,
    external_id: str | None = None,
    first_seen_ts: str | None = None,
    last_sync_ts: str | None = None,
    notes: str | None = None,
    user: str | None = None,
) -> dict:
    """Store a wearable/app/data-source identity such as Apple Health, Garmin, Oura, Fitbit, CGM, or scale."""
    u = _tool_user(user, "add_wearable_source")
    clean_name = _required_text(name, "name", max_chars=160)
    row = {
        "user": u,
        "created_ts": _now_iso(),
        "name": clean_name,
        "source_type": _optional_text(source_type, "source_type", max_chars=120),
        "manufacturer": _optional_text(manufacturer, "manufacturer", max_chars=120),
        "model": _optional_text(model, "model", max_chars=120),
        "external_id": _optional_text(external_id, "external_id", max_chars=200),
        "first_seen_ts": _parse_ts(first_seen_ts) if first_seen_ts else None,
        "last_sync_ts": _parse_ts(last_sync_ts) if last_sync_ts else None,
        "notes": _optional_text(notes, "notes"),
    }
    _audit("add_wearable_source", f"{_audit_user(u)} source_hash={_fingerprint(clean_name)}")
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO wearable_sources(user, created_ts, name, source_type, manufacturer, model, external_id, "
            "first_seen_ts, last_sync_ts, notes) "
            "VALUES (:user, :created_ts, :name, :source_type, :manufacturer, :model, :external_id, "
            ":first_seen_ts, :last_sync_ts, :notes)",
            row,
        )
        row["id"] = cur.lastrowid
    return row


@mcp.tool(annotations={"title": "List wearable sources", "readOnlyHint": True, "idempotentHint": True})
def list_wearable_sources(user: str | None = None, limit: int = 100) -> dict:
    """List wearable/app data sources."""
    u = _tool_user(user, "list_wearable_sources")
    lim = _limit(limit, default=100)
    _audit("list_wearable_sources", f"{_audit_user(u)} limit={lim}")
    with _db() as conn:
        rows = _rows(conn.execute(
            "SELECT * FROM wearable_sources WHERE user=? ORDER BY name ASC, id ASC LIMIT ?",
            (u, lim),
        ))
    return {"user": u, "count": len(rows), "wearable_sources": rows}


def _metadata_json_text(value, field: str = "metadata_json") -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return _json_text(value, field)
    try:
        return json.dumps(value, separators=(",", ":"), sort_keys=True)
    except TypeError as e:
        raise ValueError(f"{field} must be JSON-serializable") from e


def _wearable_sample_row(payload: dict, user: str) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("each wearable sample must be an object")
    source_id = payload.get("source_id")
    confidence = payload.get("confidence")
    return {
        "user": user,
        "created_ts": _now_iso(),
        "source_id": int(source_id) if source_id else None,
        "source_name": _optional_text(payload.get("source_name"), "source_name", max_chars=160),
        "sample_type": _keyish(payload.get("sample_type"), "sample_type"),
        "start_ts": _parse_ts(payload.get("start_ts")),
        "end_ts": _parse_ts(payload.get("end_ts")) if payload.get("end_ts") else None,
        "value": _finite_float(payload.get("value"), "value"),
        "unit": _optional_text(payload.get("unit"), "unit", max_chars=80),
        "aggregation": _optional_text(payload.get("aggregation"), "aggregation", max_chars=80),
        "confidence": _optional_finite_float(confidence, "confidence") if confidence is not None else None,
        "metadata_json": _metadata_json_text(payload.get("metadata_json", payload.get("metadata"))),
        "notes": _optional_text(payload.get("notes"), "notes"),
    }


def _insert_wearable_sample(conn: sqlite3.Connection, row: dict) -> int:
    cur = conn.execute(
        "INSERT INTO wearable_samples(user, created_ts, source_id, source_name, sample_type, start_ts, end_ts, value, "
        "unit, aggregation, confidence, metadata_json, notes) "
        "VALUES (:user, :created_ts, :source_id, :source_name, :sample_type, :start_ts, :end_ts, :value, "
        ":unit, :aggregation, :confidence, :metadata_json, :notes)",
        row,
    )
    return int(cur.lastrowid)


@mcp.tool(annotations={"title": "Add wearable sample", "readOnlyHint": False, "idempotentHint": False})
def add_wearable_sample(
    sample_type: str,
    value: float,
    start_ts: str | None = None,
    unit: str | None = None,
    end_ts: str | None = None,
    source_id: int | None = None,
    source_name: str | None = None,
    aggregation: str | None = None,
    confidence: float | None = None,
    metadata_json: str | None = None,
    notes: str | None = None,
    user: str | None = None,
) -> dict:
    """Store one wearable sample. Use import_wearable_samples for batches."""
    u = _tool_user(user, "add_wearable_sample")
    row = _wearable_sample_row({
        "source_id": source_id,
        "source_name": source_name,
        "sample_type": sample_type,
        "start_ts": start_ts,
        "end_ts": end_ts,
        "value": value,
        "unit": unit,
        "aggregation": aggregation,
        "confidence": confidence,
        "metadata_json": metadata_json,
        "notes": notes,
    }, u)
    _audit("add_wearable_sample", f"{_audit_user(u)} sample_type={row['sample_type']} ts={row['start_ts']}")
    with _db() as conn:
        row["id"] = _insert_wearable_sample(conn, row)
    return row


@mcp.tool(annotations={"title": "Import wearable samples", "readOnlyHint": False, "idempotentHint": False})
def import_wearable_samples(samples_json: str, user: str | None = None) -> dict:
    """Bulk import wearable samples from a JSON array of objects. Capped per call."""
    u = _tool_user(user, "import_wearable_samples")
    samples = _json_list(samples_json, "samples_json")
    if len(samples) > MAX_WEARABLE_IMPORT_ROWS:
        raise ValueError(f"samples_json has {len(samples)} rows; max {MAX_WEARABLE_IMPORT_ROWS} per call")
    rows = [_wearable_sample_row(sample, u) for sample in samples]
    _audit("import_wearable_samples", f"{_audit_user(u)} count={len(rows)}")
    ids = []
    with _db() as conn:
        for row in rows:
            ids.append(_insert_wearable_sample(conn, row))
    return {
        "user": u,
        "inserted": len(ids),
        "ids": ids[:20],
        "ids_truncated": len(ids) > 20,
        "max_rows_per_call": MAX_WEARABLE_IMPORT_ROWS,
    }


@mcp.tool(annotations={"title": "List wearable samples", "readOnlyHint": True, "idempotentHint": True})
def list_wearable_samples(
    sample_type: str | None = None,
    since: str | None = None,
    until: str | None = None,
    source_id: int | None = None,
    user: str | None = None,
    limit: int = 500,
) -> dict:
    """List wearable samples, optionally filtered by type/source/date."""
    u = _tool_user(user, "list_wearable_samples")
    lo, hi = _range_bounds(since, until)
    lim = _limit(limit, default=500)
    sql = "SELECT * FROM wearable_samples WHERE user=? AND start_ts BETWEEN ? AND ?"
    args: list = [u, lo, hi]
    if sample_type:
        sql += " AND sample_type=?"
        args.append(_keyish(sample_type, "sample_type"))
    if source_id:
        sql += " AND source_id=?"
        args.append(int(source_id))
    sql += " ORDER BY start_ts DESC, id DESC LIMIT ?"
    args.append(lim)
    _audit("list_wearable_samples", f"{_audit_user(u)} limit={lim}")
    with _db() as conn:
        rows = _rows(conn.execute(sql, args))
    return {"user": u, "count": len(rows), "wearable_samples": rows}


@mcp.tool(annotations={"title": "Analyze wearable trend", "readOnlyHint": True, "idempotentHint": True, "statementType": "descriptive"})
def analyze_wearable_trend(
    sample_type: str,
    since: str | None = None,
    until: str | None = None,
    source_id: int | None = None,
    user: str | None = None,
) -> dict:
    """Return descriptive stats/trend for one wearable sample type."""
    u = _tool_user(user, "analyze_wearable_trend")
    lo, hi = _range_bounds(since, until)
    clean_type = _keyish(sample_type, "sample_type")
    sql = (
        "SELECT id, start_ts, created_ts, value, unit, aggregation, source_name FROM wearable_samples "
        "WHERE user=? AND sample_type=? AND start_ts BETWEEN ? AND ?"
    )
    args: list = [u, clean_type, lo, hi]
    if source_id:
        sql += " AND source_id=?"
        args.append(int(source_id))
    sql += " ORDER BY start_ts ASC"
    _audit("analyze_wearable_trend", f"{_audit_user(u)} sample_type={clean_type}")
    with _db() as conn:
        rows = _rows(conn.execute(sql, args))
    if not rows:
        return _envelope(value=None, user=u, sample_type=clean_type, count=0)
    values = [float(r["value"]) for r in rows]
    points = [(r["start_ts"], float(r["value"])) for r in rows]
    latest = rows[-1]
    latest_days_stale = _days_since(latest["start_ts"])
    return _envelope(
        value=float(latest["value"]),
        unit=latest["unit"],
        days_stale=latest_days_stale,
        source_ids=[r["id"] for r in rows],
        user=u,
        sample_type=clean_type,
        count=len(rows),
        latest=latest,
        latest_days_stale=latest_days_stale,
        min=min(values),
        max=max(values),
        mean=round(statistics.fmean(values), 4),
        median=round(statistics.median(values), 4),
        trend=_linreg_per_day(points) if len(points) >= 2 else None,
        disclaimer="Descriptive wearable-data trend only; not diagnosis or medical advice.",
    )
