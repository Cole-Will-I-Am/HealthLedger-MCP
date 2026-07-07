"""Labs, biomarkers, oncology, documents, family history, generic records."""
from healthledger.runtime import *  # noqa: F401,F403


@mcp.tool(annotations={"title": "Add a lab report", "readOnlyHint": False, "idempotentHint": False})
def add_lab_report(
    title: str,
    report_date: str | None = None,
    collection_date: str | None = None,
    lab_name: str | None = None,
    ordering_provider: str | None = None,
    summary: str | None = None,
    source: str | None = None,
    document_id: int | None = None,
    notes: str | None = None,
    user: str | None = None,
) -> dict:
    """Store metadata for a lab/bloodwork report. Add individual results separately."""
    u = _tool_user(user, "add_lab_report")
    clean_title = _required_text(title, "title", max_chars=240)
    row = {
        "user": u,
        "created_ts": _now_iso(),
        "report_date": _parse_date(report_date, "report_date"),
        "collection_date": _parse_date(collection_date, "collection_date"),
        "lab_name": _optional_text(lab_name, "lab_name", max_chars=160),
        "ordering_provider": _optional_text(ordering_provider, "ordering_provider", max_chars=160),
        "title": clean_title,
        "summary": _optional_text(summary, "summary"),
        "source": _optional_text(source, "source", max_chars=200),
        "document_id": int(document_id) if document_id else None,
        "notes": _optional_text(notes, "notes"),
    }
    _audit("add_lab_report", f"{_audit_user(u)} title_hash={_fingerprint(clean_title)}")
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO lab_reports(user, created_ts, report_date, collection_date, lab_name, ordering_provider, "
            "title, summary, source, document_id, notes) "
            "VALUES (:user, :created_ts, :report_date, :collection_date, :lab_name, :ordering_provider, "
            ":title, :summary, :source, :document_id, :notes)",
            row,
        )
        row["id"] = cur.lastrowid
    return row


@mcp.tool(annotations={"title": "List lab reports", "readOnlyHint": True, "idempotentHint": True})
def list_lab_reports(
    since: str | None = None,
    until: str | None = None,
    user: str | None = None,
    limit: int = 100,
) -> dict:
    """List lab/bloodwork report containers, newest first."""
    u = _tool_user(user, "list_lab_reports")
    lo, hi = _range_bounds(since, until)
    lo_date, hi_date = lo.split("T", 1)[0], hi.split("T", 1)[0]
    lim = _limit(limit, default=100)
    _audit("list_lab_reports", f"{_audit_user(u)} limit={lim}")
    with _db() as conn:
        rows = _rows(conn.execute(
            "SELECT * FROM lab_reports WHERE user=? "
            "AND substr(COALESCE(collection_date, report_date, created_ts), 1, 10) BETWEEN ? AND ? "
            "ORDER BY COALESCE(collection_date, report_date, created_ts) DESC, id DESC LIMIT ?",
            (u, lo_date, hi_date, lim),
        ))
    return {"user": u, "count": len(rows), "lab_reports": rows}


@mcp.tool(annotations={"title": "Add a lab result", "readOnlyHint": False, "idempotentHint": False})
def add_lab_result(
    analyte: str,
    value: str,
    unit: str | None = None,
    result_date: str | None = None,
    report_id: int | None = None,
    numeric_value: float | None = None,
    ref_low: float | None = None,
    ref_high: float | None = None,
    ref_text: str | None = None,
    flag: str | None = None,
    specimen: str | None = None,
    notes: str | None = None,
    user: str | None = None,
) -> dict:
    """Store one lab/bloodwork result. Numeric values can later be trended."""
    u = _tool_user(user, "add_lab_result")
    clean_analyte = _keyish(analyte, "analyte")
    clean_value = _required_text(value, "value", max_chars=120)
    numeric = _optional_finite_float(numeric_value, "numeric_value")
    if numeric is None:
        numeric = _try_numeric(clean_value)
    row = {
        "user": u,
        "created_ts": _now_iso(),
        "report_id": int(report_id) if report_id else None,
        "result_date": _parse_date(result_date, "result_date", default_today=True),
        "analyte": clean_analyte,
        "value_text": clean_value,
        "numeric_value": numeric,
        "unit": _optional_text(unit, "unit", max_chars=40),
        "ref_low": _optional_finite_float(ref_low, "ref_low"),
        "ref_high": _optional_finite_float(ref_high, "ref_high"),
        "ref_text": _optional_text(ref_text, "ref_text", max_chars=160),
        "flag": _optional_text(flag, "flag", max_chars=60),
        "specimen": _optional_text(specimen, "specimen", max_chars=120),
        "notes": _optional_text(notes, "notes"),
    }
    _audit("add_lab_result", f"{_audit_user(u)} analyte_hash={_fingerprint(clean_analyte)}")
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO lab_results(user, created_ts, report_id, result_date, analyte, value_text, numeric_value, "
            "unit, ref_low, ref_high, ref_text, flag, specimen, notes) "
            "VALUES (:user, :created_ts, :report_id, :result_date, :analyte, :value_text, :numeric_value, "
            ":unit, :ref_low, :ref_high, :ref_text, :flag, :specimen, :notes)",
            row,
        )
        row["id"] = cur.lastrowid
    return row


@mcp.tool(annotations={"title": "List lab results", "readOnlyHint": True, "idempotentHint": True})
def list_lab_results(
    analyte: str | None = None,
    since: str | None = None,
    until: str | None = None,
    user: str | None = None,
    limit: int = 200,
) -> dict:
    """List lab results, optionally filtered by analyte and date range."""
    u = _tool_user(user, "list_lab_results")
    lo, hi = _range_bounds(since, until)
    lim = _limit(limit, default=200)
    sql = "SELECT * FROM lab_results WHERE user=? AND substr(COALESCE(result_date, created_ts), 1, 10) BETWEEN ? AND ?"
    args: list = [u, lo.split("T", 1)[0], hi.split("T", 1)[0]]
    if analyte:
        clean_analyte = _keyish(analyte, "analyte")
        sql += " AND analyte=?"
        args.append(clean_analyte)
    sql += " ORDER BY COALESCE(result_date, created_ts) DESC, id DESC LIMIT ?"
    args.append(lim)
    _audit("list_lab_results", f"{_audit_user(u)} limit={lim}")
    with _db() as conn:
        rows = _rows(conn.execute(sql, args))
    return {"user": u, "count": len(rows), "lab_results": rows}


@mcp.tool(annotations={"title": "Analyze lab trend", "readOnlyHint": True, "idempotentHint": True, "statementType": "descriptive"})
def analyze_lab_trend(analyte: str, since: str | None = None, until: str | None = None, user: str | None = None) -> dict:
    """Return descriptive trend stats for numeric lab results for one analyte."""
    u = _tool_user(user, "analyze_lab_trend")
    lo, hi = _range_bounds(since, until)
    clean_analyte = _keyish(analyte, "analyte")
    _audit("analyze_lab_trend", f"{_audit_user(u)} analyte_hash={_fingerprint(clean_analyte)}")
    with _db() as conn:
        rows = _rows(conn.execute(
            "SELECT id, result_date, created_ts, numeric_value, unit, ref_low, ref_high, ref_text, flag FROM lab_results "
            "WHERE user=? AND analyte=? AND numeric_value IS NOT NULL "
            "AND substr(COALESCE(result_date, created_ts), 1, 10) BETWEEN ? AND ? "
            "ORDER BY COALESCE(result_date, created_ts) ASC",
            (u, clean_analyte, lo.split("T", 1)[0], hi.split("T", 1)[0]),
        ))
    if not rows:
        return _envelope(value=None, user=u, analyte=clean_analyte, count=0)
    values = [r["numeric_value"] for r in rows]
    points = [(r["result_date"] + "T00:00:00+00:00", r["numeric_value"]) for r in rows if r["result_date"]]
    latest = rows[-1]
    latest_days_stale = _days_since(latest["result_date"] or latest["created_ts"])
    return _envelope(
        value=latest["numeric_value"],
        unit=latest["unit"],
        ref_low=latest["ref_low"],
        ref_high=latest["ref_high"],
        ref_text=latest["ref_text"],
        days_stale=latest_days_stale,
        source_ids=[r["id"] for r in rows],
        user=u,
        analyte=clean_analyte,
        count=len(rows),
        latest=latest,
        latest_days_stale=latest_days_stale,
        min=min(values),
        max=max(values),
        mean=round(statistics.fmean(values), 4),
        median=round(statistics.median(values), 4),
        trend=_linreg_per_day(points) if len(points) >= 2 else None,
        disclaimer="Descriptive lab trend only; not diagnosis or medical advice.",
    )


@mcp.tool(annotations={"title": "Add a biomarker", "readOnlyHint": False, "idempotentHint": False})
def add_biomarker(
    biomarker: str,
    value: str,
    category: str | None = None,
    measured_date: str | None = None,
    unit: str | None = None,
    numeric_value: float | None = None,
    ref_low: float | None = None,
    ref_high: float | None = None,
    ref_text: str | None = None,
    flag: str | None = None,
    source: str | None = None,
    notes: str | None = None,
    user: str | None = None,
) -> dict:
    """Store a biomarker observation, including oncology/genetic/inflammatory markers."""
    u = _tool_user(user, "add_biomarker")
    clean_marker = _keyish(biomarker, "biomarker")
    clean_value = _required_text(value, "value", max_chars=120)
    numeric = _optional_finite_float(numeric_value, "numeric_value") or _try_numeric(clean_value)
    row = {
        "user": u,
        "created_ts": _now_iso(),
        "biomarker": clean_marker,
        "category": _optional_text(category, "category", max_chars=120),
        "measured_date": _parse_date(measured_date, "measured_date", default_today=True),
        "value_text": clean_value,
        "numeric_value": numeric,
        "unit": _optional_text(unit, "unit", max_chars=40),
        "ref_low": _optional_finite_float(ref_low, "ref_low"),
        "ref_high": _optional_finite_float(ref_high, "ref_high"),
        "ref_text": _optional_text(ref_text, "ref_text", max_chars=160),
        "flag": _optional_text(flag, "flag", max_chars=60),
        "source": _optional_text(source, "source", max_chars=200),
        "notes": _optional_text(notes, "notes"),
    }
    _audit("add_biomarker", f"{_audit_user(u)} biomarker_hash={_fingerprint(clean_marker)}")
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO biomarkers(user, created_ts, biomarker, category, measured_date, value_text, numeric_value, "
            "unit, ref_low, ref_high, ref_text, flag, source, notes) "
            "VALUES (:user, :created_ts, :biomarker, :category, :measured_date, :value_text, :numeric_value, "
            ":unit, :ref_low, :ref_high, :ref_text, :flag, :source, :notes)",
            row,
        )
        row["id"] = cur.lastrowid
    return row


@mcp.tool(annotations={"title": "List biomarkers", "readOnlyHint": True, "idempotentHint": True})
def list_biomarkers(
    biomarker: str | None = None,
    category: str | None = None,
    since: str | None = None,
    until: str | None = None,
    user: str | None = None,
    limit: int = 200,
) -> dict:
    """List biomarker observations, optionally filtered by marker/category/date."""
    u = _tool_user(user, "list_biomarkers")
    lo, hi = _range_bounds(since, until)
    lim = _limit(limit, default=200)
    sql = (
        "SELECT * FROM biomarkers WHERE user=? "
        "AND substr(COALESCE(measured_date, created_ts), 1, 10) BETWEEN ? AND ?"
    )
    args: list = [u, lo.split("T", 1)[0], hi.split("T", 1)[0]]
    if biomarker:
        sql += " AND biomarker=?"
        args.append(_keyish(biomarker, "biomarker"))
    if category:
        sql += " AND category=?"
        args.append(_optional_text(category, "category", max_chars=120))
    sql += " ORDER BY COALESCE(measured_date, created_ts) DESC, id DESC LIMIT ?"
    args.append(lim)
    _audit("list_biomarkers", f"{_audit_user(u)} limit={lim}")
    with _db() as conn:
        rows = _rows(conn.execute(sql, args))
    return {"user": u, "count": len(rows), "biomarkers": rows}


@mcp.tool(annotations={"title": "Analyze biomarker trend", "readOnlyHint": True, "idempotentHint": True, "statementType": "descriptive"})
def analyze_biomarker_trend(biomarker: str, since: str | None = None, until: str | None = None, user: str | None = None) -> dict:
    """Return descriptive trend stats for numeric biomarker observations."""
    u = _tool_user(user, "analyze_biomarker_trend")
    lo, hi = _range_bounds(since, until)
    clean_marker = _keyish(biomarker, "biomarker")
    _audit("analyze_biomarker_trend", f"{_audit_user(u)} biomarker_hash={_fingerprint(clean_marker)}")
    with _db() as conn:
        rows = _rows(conn.execute(
            "SELECT id, measured_date, created_ts, numeric_value, unit, ref_low, ref_high, ref_text, flag FROM biomarkers "
            "WHERE user=? AND biomarker=? AND numeric_value IS NOT NULL "
            "AND substr(COALESCE(measured_date, created_ts), 1, 10) BETWEEN ? AND ? "
            "ORDER BY COALESCE(measured_date, created_ts) ASC",
            (u, clean_marker, lo.split("T", 1)[0], hi.split("T", 1)[0]),
        ))
    if not rows:
        return _envelope(value=None, user=u, biomarker=clean_marker, count=0)
    values = [r["numeric_value"] for r in rows]
    points = [(r["measured_date"] + "T00:00:00+00:00", r["numeric_value"]) for r in rows if r["measured_date"]]
    latest = rows[-1]
    latest_days_stale = _days_since(latest["measured_date"] or latest["created_ts"])
    return _envelope(
        value=latest["numeric_value"],
        unit=latest["unit"],
        ref_low=latest["ref_low"],
        ref_high=latest["ref_high"],
        ref_text=latest["ref_text"],
        days_stale=latest_days_stale,
        source_ids=[r["id"] for r in rows],
        user=u,
        biomarker=clean_marker,
        count=len(rows),
        latest=latest,
        latest_days_stale=latest_days_stale,
        min=min(values),
        max=max(values),
        mean=round(statistics.fmean(values), 4),
        median=round(statistics.median(values), 4),
        trend=_linreg_per_day(points) if len(points) >= 2 else None,
        disclaimer="Descriptive biomarker trend only; not diagnosis or medical advice.",
    )


@mcp.tool(annotations={"title": "Add a tumor record", "readOnlyHint": False, "idempotentHint": False})
def add_tumor_record(
    tumor_name: str,
    cancer_type: str | None = None,
    body_site: str | None = None,
    diagnosis_date: str | None = None,
    status: str = "active",
    stage: str | None = None,
    grade: str | None = None,
    size_value: float | None = None,
    size_unit: str | None = None,
    biomarker_summary: str | None = None,
    treatment_status: str | None = None,
    source: str | None = None,
    notes: str | None = None,
    user: str | None = None,
) -> dict:
    """Store tumor/cancer-related structured information as user-provided data."""
    u = _tool_user(user, "add_tumor_record")
    clean_name = _required_text(tumor_name, "tumor_name", max_chars=200)
    clean_status = _status(status)
    row = {
        "user": u,
        "created_ts": _now_iso(),
        "cancer_type": _optional_text(cancer_type, "cancer_type", max_chars=160),
        "tumor_name": clean_name,
        "body_site": _optional_text(body_site, "body_site", max_chars=160),
        "diagnosis_date": _parse_date(diagnosis_date, "diagnosis_date"),
        "status": clean_status,
        "stage": _optional_text(stage, "stage", max_chars=80),
        "grade": _optional_text(grade, "grade", max_chars=80),
        "size_value": _optional_finite_float(size_value, "size_value"),
        "size_unit": _optional_text(size_unit, "size_unit", max_chars=40),
        "biomarker_summary": _optional_text(biomarker_summary, "biomarker_summary"),
        "treatment_status": _optional_text(treatment_status, "treatment_status", max_chars=200),
        "source": _optional_text(source, "source", max_chars=200),
        "notes": _optional_text(notes, "notes"),
    }
    _audit("add_tumor_record", f"{_audit_user(u)} tumor_hash={_fingerprint(clean_name)} status={clean_status}")
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO tumors(user, created_ts, cancer_type, tumor_name, body_site, diagnosis_date, status, stage, "
            "grade, size_value, size_unit, biomarker_summary, treatment_status, source, notes) "
            "VALUES (:user, :created_ts, :cancer_type, :tumor_name, :body_site, :diagnosis_date, :status, :stage, "
            ":grade, :size_value, :size_unit, :biomarker_summary, :treatment_status, :source, :notes)",
            row,
        )
        row["id"] = cur.lastrowid
    return row


@mcp.tool(annotations={"title": "List tumor records", "readOnlyHint": True, "idempotentHint": True})
def list_tumor_records(status: str | None = None, user: str | None = None, limit: int = 200) -> dict:
    """List tumor/cancer-related records. Descriptive storage only."""
    u = _tool_user(user, "list_tumor_records")
    lim = _limit(limit, default=200)
    sql = "SELECT * FROM tumors WHERE user=?"
    args: list = [u]
    if status:
        sql += " AND status=?"
        args.append(_status(status))
    sql += " ORDER BY COALESCE(diagnosis_date, created_ts) DESC, id DESC LIMIT ?"
    args.append(lim)
    _audit("list_tumor_records", f"{_audit_user(u)} limit={lim}")
    with _db() as conn:
        rows = _rows(conn.execute(sql, args))
    return {"user": u, "count": len(rows), "tumors": rows}


@mcp.tool(annotations={"title": "Add document", "readOnlyHint": False, "idempotentHint": False})
def add_document(
    title: str,
    document_type: str,
    document_date: str | None = None,
    source: str | None = None,
    provider: str | None = None,
    facility: str | None = None,
    tags: str | None = None,
    summary: str | None = None,
    content_text: str | None = None,
    source_uri: str | None = None,
    user: str | None = None,
) -> dict:
    """Store document/report metadata and extracted text. Binary files are not stored here."""
    u = _tool_user(user, "add_document")
    clean_title = _required_text(title, "title", max_chars=240)
    clean_type = _keyish(document_type, "document_type")
    row = {
        "user": u,
        "created_ts": _now_iso(),
        "document_date": _parse_date(document_date, "document_date"),
        "document_type": clean_type,
        "title": clean_title,
        "source": _optional_text(source, "source", max_chars=200),
        "provider": _optional_text(provider, "provider", max_chars=160),
        "facility": _optional_text(facility, "facility", max_chars=160),
        "tags": _optional_text(tags, "tags", max_chars=500),
        "summary": _optional_text(summary, "summary"),
        "content_text": _optional_text(content_text, "content_text"),
        "source_uri": _optional_text(source_uri, "source_uri", max_chars=1000),
    }
    _audit("add_document", f"{_audit_user(u)} type={clean_type} title_hash={_fingerprint(clean_title)}")
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO documents(user, created_ts, document_date, document_type, title, source, provider, facility, "
            "tags, summary, content_text, source_uri) "
            "VALUES (:user, :created_ts, :document_date, :document_type, :title, :source, :provider, :facility, "
            ":tags, :summary, :content_text, :source_uri)",
            row,
        )
        row["id"] = cur.lastrowid
    return row


@mcp.tool(annotations={"title": "List documents", "readOnlyHint": True, "idempotentHint": True})
def list_documents(
    document_type: str | None = None,
    since: str | None = None,
    until: str | None = None,
    user: str | None = None,
    limit: int = 100,
) -> dict:
    """List stored health documents/reports and extracted text metadata."""
    u = _tool_user(user, "list_documents")
    lo, hi = _range_bounds(since, until)
    lim = _limit(limit, default=100)
    sql = (
        "SELECT * FROM documents WHERE user=? "
        "AND substr(COALESCE(document_date, created_ts), 1, 10) BETWEEN ? AND ?"
    )
    args: list = [u, lo.split("T", 1)[0], hi.split("T", 1)[0]]
    if document_type:
        sql += " AND document_type=?"
        args.append(_keyish(document_type, "document_type"))
    sql += " ORDER BY COALESCE(document_date, created_ts) DESC, id DESC LIMIT ?"
    args.append(lim)
    _audit("list_documents", f"{_audit_user(u)} limit={lim}")
    with _db() as conn:
        rows = _rows(conn.execute(sql, args))
    return {"user": u, "count": len(rows), "documents": rows}


@mcp.tool(annotations={"title": "Add family history", "readOnlyHint": False, "idempotentHint": False})
def add_family_history(
    relation: str,
    condition_name: str,
    status: str | None = None,
    age_at_onset: float | None = None,
    relative_status: str | None = None,
    age_at_death: float | None = None,
    cause_of_death: str | None = None,
    notes: str | None = None,
    user: str | None = None,
) -> dict:
    """Store family history facts by relation."""
    u = _tool_user(user, "add_family_history")
    clean_relation = _required_text(relation, "relation", max_chars=120)
    clean_condition = _required_text(condition_name, "condition_name", max_chars=160)
    row = {
        "user": u,
        "created_ts": _now_iso(),
        "relation": clean_relation,
        "condition_name": clean_condition,
        "status": _optional_text(status, "status", max_chars=80),
        "age_at_onset": _optional_finite_float(age_at_onset, "age_at_onset"),
        "relative_status": _optional_text(relative_status, "relative_status", max_chars=80),
        "age_at_death": _optional_finite_float(age_at_death, "age_at_death"),
        "cause_of_death": _optional_text(cause_of_death, "cause_of_death", max_chars=200),
        "notes": _optional_text(notes, "notes"),
    }
    _audit("add_family_history", f"{_audit_user(u)} relation_hash={_fingerprint(clean_relation)} condition_hash={_fingerprint(clean_condition)}")
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO family_history(user, created_ts, relation, condition_name, status, age_at_onset, "
            "relative_status, age_at_death, cause_of_death, notes) "
            "VALUES (:user, :created_ts, :relation, :condition_name, :status, :age_at_onset, "
            ":relative_status, :age_at_death, :cause_of_death, :notes)",
            row,
        )
        row["id"] = cur.lastrowid
    return row


@mcp.tool(annotations={"title": "List family history", "readOnlyHint": True, "idempotentHint": True})
def list_family_history(relation: str | None = None, user: str | None = None, limit: int = 200) -> dict:
    """List family history records, optionally filtered by relation."""
    u = _tool_user(user, "list_family_history")
    lim = _limit(limit, default=200)
    sql = "SELECT * FROM family_history WHERE user=?"
    args: list = [u]
    if relation:
        sql += " AND relation=?"
        args.append(_required_text(relation, "relation", max_chars=120))
    sql += " ORDER BY relation ASC, condition_name ASC, id ASC LIMIT ?"
    args.append(lim)
    _audit("list_family_history", f"{_audit_user(u)} limit={lim}")
    with _db() as conn:
        rows = _rows(conn.execute(sql, args))
    return {"user": u, "count": len(rows), "family_history": rows}


@mcp.tool(annotations={"title": "Add generic health record", "readOnlyHint": False, "idempotentHint": False})
def add_health_record(
    record_type: str,
    title: str,
    record_date: str | None = None,
    body: str | None = None,
    source: str | None = None,
    tags: str | None = None,
    extra_json: str | None = None,
    user: str | None = None,
) -> dict:
    """Store any health datum that does not fit a dedicated table yet."""
    u = _tool_user(user, "add_health_record")
    clean_type = _keyish(record_type, "record_type")
    clean_title = _required_text(title, "title", max_chars=240)
    row = {
        "user": u,
        "created_ts": _now_iso(),
        "record_date": _parse_date(record_date, "record_date"),
        "record_type": clean_type,
        "title": clean_title,
        "body": _optional_text(body, "body"),
        "source": _optional_text(source, "source", max_chars=200),
        "tags": _optional_text(tags, "tags", max_chars=500),
        "extra_json": _json_text(extra_json, "extra_json"),
    }
    _audit("add_health_record", f"{_audit_user(u)} type={clean_type} title_hash={_fingerprint(clean_title)}")
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO health_records(user, created_ts, record_date, record_type, title, body, source, tags, extra_json) "
            "VALUES (:user, :created_ts, :record_date, :record_type, :title, :body, :source, :tags, :extra_json)",
            row,
        )
        row["id"] = cur.lastrowid
    return row


@mcp.tool(annotations={"title": "List generic health records", "readOnlyHint": True, "idempotentHint": True})
def list_health_records(
    record_type: str | None = None,
    since: str | None = None,
    until: str | None = None,
    user: str | None = None,
    limit: int = 100,
) -> dict:
    """List generic/catch-all health records, optionally filtered by type/date."""
    u = _tool_user(user, "list_health_records")
    lo, hi = _range_bounds(since, until)
    lim = _limit(limit, default=100)
    sql = (
        "SELECT * FROM health_records WHERE user=? "
        "AND substr(COALESCE(record_date, created_ts), 1, 10) BETWEEN ? AND ?"
    )
    args: list = [u, lo.split("T", 1)[0], hi.split("T", 1)[0]]
    if record_type:
        sql += " AND record_type=?"
        args.append(_keyish(record_type, "record_type"))
    sql += " ORDER BY COALESCE(record_date, created_ts) DESC, id DESC LIMIT ?"
    args.append(lim)
    _audit("list_health_records", f"{_audit_user(u)} limit={lim}")
    with _db() as conn:
        rows = _rows(conn.execute(sql, args))
    return {"user": u, "count": len(rows), "health_records": rows}
