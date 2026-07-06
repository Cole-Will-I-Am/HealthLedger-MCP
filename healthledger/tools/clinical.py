"""Clinical history: conditions, allergies, medications, encounters,
procedures, imaging, immunizations, and care tasks."""
from healthledger.runtime import *  # noqa: F401,F403


@mcp.tool(annotations={"title": "Add a condition", "readOnlyHint": False, "idempotentHint": False})
def add_condition(
    name: str,
    status: str = "active",
    onset_date: str | None = None,
    resolved_date: str | None = None,
    severity: str | None = None,
    body_site: str | None = None,
    notes: str | None = None,
    user: str | None = None,
) -> dict:
    """Store a structured problem-list condition. Descriptive record only."""
    u = _tool_user(user, "add_condition")
    clean_name = _required_text(name, "name", max_chars=160)
    clean_status = _status(status)
    row = {
        "user": u,
        "created_ts": _now_iso(),
        "name": clean_name,
        "status": clean_status,
        "onset_date": _parse_date(onset_date, "onset_date"),
        "resolved_date": _parse_date(resolved_date, "resolved_date"),
        "severity": _optional_text(severity, "severity", max_chars=80),
        "body_site": _optional_text(body_site, "body_site", max_chars=160),
        "notes": _optional_text(notes, "notes"),
    }
    _audit("add_condition", f"{_audit_user(u)} name_hash={_fingerprint(clean_name)} status={clean_status}")
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO conditions(user, created_ts, name, status, onset_date, resolved_date, severity, body_site, notes) "
            "VALUES (:user, :created_ts, :name, :status, :onset_date, :resolved_date, :severity, :body_site, :notes)",
            row,
        )
        row["id"] = cur.lastrowid
    return row


@mcp.tool(annotations={"title": "List conditions", "readOnlyHint": True, "idempotentHint": True})
def list_conditions(status: str | None = None, user: str | None = None, limit: int = 200) -> dict:
    """List stored conditions, optionally filtered by status."""
    u = _tool_user(user, "list_conditions")
    lim = _limit(limit, default=200)
    sql = "SELECT * FROM conditions WHERE user=?"
    args: list = [u]
    if status:
        sql += " AND status=?"
        args.append(_status(status))
    sql += " ORDER BY COALESCE(onset_date, created_ts) DESC, id DESC LIMIT ?"
    args.append(lim)
    _audit("list_conditions", f"{_audit_user(u)} limit={lim}")
    with _db() as conn:
        rows = _rows(conn.execute(sql, args))
    return {"user": u, "count": len(rows), "conditions": rows}


@mcp.tool(annotations={"title": "Add an allergy", "readOnlyHint": False, "idempotentHint": False})
def add_allergy(
    allergen: str,
    reaction: str | None = None,
    severity: str | None = None,
    status: str = "active",
    noted_date: str | None = None,
    notes: str | None = None,
    user: str | None = None,
) -> dict:
    """Store a structured allergy or intolerance record."""
    u = _tool_user(user, "add_allergy")
    clean_allergen = _required_text(allergen, "allergen", max_chars=160)
    clean_status = _status(status)
    row = {
        "user": u,
        "created_ts": _now_iso(),
        "allergen": clean_allergen,
        "reaction": _optional_text(reaction, "reaction", max_chars=500),
        "severity": _optional_text(severity, "severity", max_chars=80),
        "status": clean_status,
        "noted_date": _parse_date(noted_date, "noted_date"),
        "notes": _optional_text(notes, "notes"),
    }
    _audit("add_allergy", f"{_audit_user(u)} allergen_hash={_fingerprint(clean_allergen)} status={clean_status}")
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO allergies(user, created_ts, allergen, reaction, severity, status, noted_date, notes) "
            "VALUES (:user, :created_ts, :allergen, :reaction, :severity, :status, :noted_date, :notes)",
            row,
        )
        row["id"] = cur.lastrowid
    return row


@mcp.tool(annotations={"title": "List allergies", "readOnlyHint": True, "idempotentHint": True})
def list_allergies(status: str | None = None, user: str | None = None, limit: int = 200) -> dict:
    """List stored allergies/intolerances, optionally filtered by status."""
    u = _tool_user(user, "list_allergies")
    lim = _limit(limit, default=200)
    sql = "SELECT * FROM allergies WHERE user=?"
    args: list = [u]
    if status:
        sql += " AND status=?"
        args.append(_status(status))
    sql += " ORDER BY allergen ASC LIMIT ?"
    args.append(lim)
    _audit("list_allergies", f"{_audit_user(u)} limit={lim}")
    with _db() as conn:
        rows = _rows(conn.execute(sql, args))
    return {"user": u, "count": len(rows), "allergies": rows}


@mcp.tool(annotations={"title": "Add a medication", "readOnlyHint": False, "idempotentHint": False})
def add_medication(
    name: str,
    dose: str | None = None,
    frequency: str | None = None,
    schedule: str | None = None,
    generic_name: str | None = None,
    route: str | None = None,
    status: str = "active",
    start_date: str | None = None,
    end_date: str | None = None,
    prescriber: str | None = None,
    indication: str | None = None,
    refill_due_date: str | None = None,
    instructions: str | None = None,
    notes: str | None = None,
    user: str | None = None,
) -> dict:
    """Store a structured medication with schedule/refill metadata."""
    u = _tool_user(user, "add_medication")
    clean_name = _required_text(name, "name", max_chars=160)
    clean_status = _status(status)
    row = {
        "user": u,
        "created_ts": _now_iso(),
        "name": clean_name,
        "generic_name": _optional_text(generic_name, "generic_name", max_chars=160),
        "dose": _optional_text(dose, "dose", max_chars=120),
        "route": _optional_text(route, "route", max_chars=80),
        "frequency": _optional_text(frequency, "frequency", max_chars=160),
        "schedule": _optional_text(schedule, "schedule", max_chars=500),
        "status": clean_status,
        "start_date": _parse_date(start_date, "start_date"),
        "end_date": _parse_date(end_date, "end_date"),
        "prescriber": _optional_text(prescriber, "prescriber", max_chars=160),
        "indication": _optional_text(indication, "indication", max_chars=300),
        "refill_due_date": _parse_date(refill_due_date, "refill_due_date"),
        "instructions": _optional_text(instructions, "instructions"),
        "notes": _optional_text(notes, "notes"),
    }
    _audit("add_medication", f"{_audit_user(u)} med_hash={_fingerprint(clean_name)} status={clean_status}")
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO medications(user, created_ts, name, generic_name, dose, route, frequency, schedule, status, "
            "start_date, end_date, prescriber, indication, refill_due_date, instructions, notes) "
            "VALUES (:user, :created_ts, :name, :generic_name, :dose, :route, :frequency, :schedule, :status, "
            ":start_date, :end_date, :prescriber, :indication, :refill_due_date, :instructions, :notes)",
            row,
        )
        row["id"] = cur.lastrowid
    return row


@mcp.tool(annotations={"title": "List medications", "readOnlyHint": True, "idempotentHint": True})
def list_medications(status: str | None = "active", user: str | None = None, limit: int = 200) -> dict:
    """List medications, defaulting to active/current medications."""
    u = _tool_user(user, "list_medications")
    lim = _limit(limit, default=200)
    sql = "SELECT * FROM medications WHERE user=?"
    args: list = [u]
    if status:
        sql += " AND status=?"
        args.append(_status(status))
    sql += " ORDER BY name ASC LIMIT ?"
    args.append(lim)
    _audit("list_medications", f"{_audit_user(u)} limit={lim}")
    with _db() as conn:
        rows = _rows(conn.execute(sql, args))
    return {"user": u, "count": len(rows), "medications": rows}


@mcp.tool(annotations={"title": "Log medication taken", "readOnlyHint": False, "idempotentHint": False})
def log_medication_taken(
    medication_name: str,
    medication_id: int | None = None,
    scheduled_ts: str | None = None,
    taken_ts: str | None = None,
    status: str = "taken",
    dose_taken: str | None = None,
    note: str | None = None,
    user: str | None = None,
) -> dict:
    """Log an adherence event: taken, missed, skipped, delayed, or other status."""
    u = _tool_user(user, "log_medication_taken")
    clean_name = _required_text(medication_name, "medication_name", max_chars=160)
    row = {
        "user": u,
        "created_ts": _now_iso(),
        "medication_id": int(medication_id) if medication_id else None,
        "medication_name": clean_name,
        "scheduled_ts": _parse_ts(scheduled_ts) if scheduled_ts else None,
        "taken_ts": _parse_ts(taken_ts) if taken_ts else _now_iso(),
        "status": _status(status, default="taken"),
        "dose_taken": _optional_text(dose_taken, "dose_taken", max_chars=120),
        "note": _optional_text(note, "note"),
    }
    _audit("log_medication_taken", f"{_audit_user(u)} med_hash={_fingerprint(clean_name)} status={row['status']}")
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO medication_logs(user, created_ts, medication_id, medication_name, scheduled_ts, taken_ts, status, dose_taken, note) "
            "VALUES (:user, :created_ts, :medication_id, :medication_name, :scheduled_ts, :taken_ts, :status, :dose_taken, :note)",
            row,
        )
        row["id"] = cur.lastrowid
    return row


@mcp.tool(annotations={"title": "List medication schedule", "readOnlyHint": True, "idempotentHint": True})
def list_medication_schedule(user: str | None = None) -> dict:
    """Return active medication schedules and refill dates."""
    u = _tool_user(user, "list_medication_schedule")
    today = datetime.now(timezone.utc).date().isoformat()
    _audit("list_medication_schedule", _audit_user(u))
    with _db() as conn:
        meds = _rows(conn.execute(
            "SELECT id, name, dose, route, frequency, schedule, refill_due_date, instructions, prescriber "
            "FROM medications WHERE user=? AND status='active' ORDER BY name",
            (u,),
        ))
        refill_due = [m for m in meds if m.get("refill_due_date") and m["refill_due_date"] <= today]
    return {"user": u, "date": today, "medications": meds, "refills_due": refill_due}


@mcp.tool(annotations={"title": "List medication logs", "readOnlyHint": True, "idempotentHint": True})
def list_medication_logs(
    medication_name: str | None = None,
    since: str | None = None,
    until: str | None = None,
    user: str | None = None,
    limit: int = 200,
) -> dict:
    """List medication adherence/dose logs, optionally filtered by medication/date."""
    u = _tool_user(user, "list_medication_logs")
    lo, hi = _range_bounds(since, until)
    lim = _limit(limit, default=200)
    sql = (
        "SELECT * FROM medication_logs WHERE user=? "
        "AND COALESCE(taken_ts, scheduled_ts, created_ts) BETWEEN ? AND ?"
    )
    args: list = [u, lo, hi]
    clean_name = None
    if medication_name:
        clean_name = _required_text(medication_name, "medication_name", max_chars=160)
        sql += " AND medication_name=?"
        args.append(clean_name)
    sql += " ORDER BY COALESCE(taken_ts, scheduled_ts, created_ts) DESC, id DESC LIMIT ?"
    args.append(lim)
    med_part = f" med_hash={_fingerprint(clean_name)}" if clean_name else ""
    _audit("list_medication_logs", f"{_audit_user(u)}{med_part} limit={lim}")
    with _db() as conn:
        rows = _rows(conn.execute(sql, args))
    return {"user": u, "count": len(rows), "medication_logs": rows}


@mcp.tool(annotations={"title": "Add encounter", "readOnlyHint": False, "idempotentHint": False})
def add_encounter(
    encounter_type: str,
    encounter_date: str | None = None,
    provider: str | None = None,
    facility: str | None = None,
    reason: str | None = None,
    vitals_summary: str | None = None,
    assessment: str | None = None,
    plan: str | None = None,
    follow_up_date: str | None = None,
    notes: str | None = None,
    user: str | None = None,
) -> dict:
    """Store a visit/encounter such as annual physical, specialist visit, ER visit, therapy, dental, or vision."""
    u = _tool_user(user, "add_encounter")
    clean_type = _keyish(encounter_type, "encounter_type")
    row = {
        "user": u,
        "created_ts": _now_iso(),
        "encounter_date": _parse_date(encounter_date, "encounter_date", default_today=True),
        "encounter_type": clean_type,
        "provider": _optional_text(provider, "provider", max_chars=160),
        "facility": _optional_text(facility, "facility", max_chars=160),
        "reason": _optional_text(reason, "reason", max_chars=500),
        "vitals_summary": _optional_text(vitals_summary, "vitals_summary"),
        "assessment": _optional_text(assessment, "assessment"),
        "plan": _optional_text(plan, "plan"),
        "follow_up_date": _parse_date(follow_up_date, "follow_up_date"),
        "notes": _optional_text(notes, "notes"),
    }
    _audit("add_encounter", f"{_audit_user(u)} type={clean_type}")
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO encounters(user, created_ts, encounter_date, encounter_type, provider, facility, reason, "
            "vitals_summary, assessment, plan, follow_up_date, notes) "
            "VALUES (:user, :created_ts, :encounter_date, :encounter_type, :provider, :facility, :reason, "
            ":vitals_summary, :assessment, :plan, :follow_up_date, :notes)",
            row,
        )
        row["id"] = cur.lastrowid
    return row


@mcp.tool(annotations={"title": "List encounters", "readOnlyHint": True, "idempotentHint": True})
def list_encounters(encounter_type: str | None = None, user: str | None = None, limit: int = 100) -> dict:
    """List visits/encounters, optionally filtered by type."""
    u = _tool_user(user, "list_encounters")
    lim = _limit(limit, default=100)
    sql = "SELECT * FROM encounters WHERE user=?"
    args: list = [u]
    if encounter_type:
        sql += " AND encounter_type=?"
        args.append(_keyish(encounter_type, "encounter_type"))
    sql += " ORDER BY COALESCE(encounter_date, created_ts) DESC, id DESC LIMIT ?"
    args.append(lim)
    _audit("list_encounters", f"{_audit_user(u)} limit={lim}")
    with _db() as conn:
        rows = _rows(conn.execute(sql, args))
    return {"user": u, "count": len(rows), "encounters": rows}


@mcp.tool(annotations={"title": "Add procedure", "readOnlyHint": False, "idempotentHint": False})
def add_procedure(
    name: str,
    procedure_date: str | None = None,
    body_site: str | None = None,
    provider: str | None = None,
    facility: str | None = None,
    outcome: str | None = None,
    follow_up_date: str | None = None,
    notes: str | None = None,
    user: str | None = None,
) -> dict:
    """Store a procedure/surgery/test record and any follow-up date."""
    u = _tool_user(user, "add_procedure")
    clean_name = _required_text(name, "name", max_chars=200)
    row = {
        "user": u,
        "created_ts": _now_iso(),
        "procedure_date": _parse_date(procedure_date, "procedure_date"),
        "name": clean_name,
        "body_site": _optional_text(body_site, "body_site", max_chars=160),
        "provider": _optional_text(provider, "provider", max_chars=160),
        "facility": _optional_text(facility, "facility", max_chars=160),
        "outcome": _optional_text(outcome, "outcome"),
        "follow_up_date": _parse_date(follow_up_date, "follow_up_date"),
        "notes": _optional_text(notes, "notes"),
    }
    _audit("add_procedure", f"{_audit_user(u)} name_hash={_fingerprint(clean_name)}")
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO procedures(user, created_ts, procedure_date, name, body_site, provider, facility, outcome, follow_up_date, notes) "
            "VALUES (:user, :created_ts, :procedure_date, :name, :body_site, :provider, :facility, :outcome, :follow_up_date, :notes)",
            row,
        )
        row["id"] = cur.lastrowid
    return row


@mcp.tool(annotations={"title": "List procedures", "readOnlyHint": True, "idempotentHint": True})
def list_procedures(
    since: str | None = None,
    until: str | None = None,
    user: str | None = None,
    limit: int = 100,
) -> dict:
    """List procedures, surgeries, and tests with outcomes/follow-up dates."""
    u = _tool_user(user, "list_procedures")
    lo, hi = _range_bounds(since, until)
    lim = _limit(limit, default=100)
    _audit("list_procedures", f"{_audit_user(u)} limit={lim}")
    with _db() as conn:
        rows = _rows(conn.execute(
            "SELECT * FROM procedures WHERE user=? "
            "AND substr(COALESCE(procedure_date, created_ts), 1, 10) BETWEEN ? AND ? "
            "ORDER BY COALESCE(procedure_date, created_ts) DESC, id DESC LIMIT ?",
            (u, lo.split("T", 1)[0], hi.split("T", 1)[0], lim),
        ))
    return {"user": u, "count": len(rows), "procedures": rows}


@mcp.tool(annotations={"title": "Add imaging report", "readOnlyHint": False, "idempotentHint": False})
def add_imaging_report(
    modality: str,
    imaging_date: str | None = None,
    body_site: str | None = None,
    facility: str | None = None,
    ordering_provider: str | None = None,
    findings: str | None = None,
    impression: str | None = None,
    follow_up_date: str | None = None,
    document_id: int | None = None,
    notes: str | None = None,
    user: str | None = None,
) -> dict:
    """Store imaging/radiology report metadata and text findings."""
    u = _tool_user(user, "add_imaging_report")
    clean_modality = _keyish(modality, "modality")
    row = {
        "user": u,
        "created_ts": _now_iso(),
        "imaging_date": _parse_date(imaging_date, "imaging_date"),
        "modality": clean_modality,
        "body_site": _optional_text(body_site, "body_site", max_chars=160),
        "facility": _optional_text(facility, "facility", max_chars=160),
        "ordering_provider": _optional_text(ordering_provider, "ordering_provider", max_chars=160),
        "findings": _optional_text(findings, "findings"),
        "impression": _optional_text(impression, "impression"),
        "follow_up_date": _parse_date(follow_up_date, "follow_up_date"),
        "document_id": int(document_id) if document_id else None,
        "notes": _optional_text(notes, "notes"),
    }
    _audit("add_imaging_report", f"{_audit_user(u)} modality={clean_modality}")
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO imaging_reports(user, created_ts, imaging_date, modality, body_site, facility, ordering_provider, "
            "findings, impression, follow_up_date, document_id, notes) "
            "VALUES (:user, :created_ts, :imaging_date, :modality, :body_site, :facility, :ordering_provider, "
            ":findings, :impression, :follow_up_date, :document_id, :notes)",
            row,
        )
        row["id"] = cur.lastrowid
    return row


@mcp.tool(annotations={"title": "List imaging reports", "readOnlyHint": True, "idempotentHint": True})
def list_imaging_reports(
    modality: str | None = None,
    since: str | None = None,
    until: str | None = None,
    user: str | None = None,
    limit: int = 100,
) -> dict:
    """List imaging/radiology reports, optionally filtered by modality/date."""
    u = _tool_user(user, "list_imaging_reports")
    lo, hi = _range_bounds(since, until)
    lim = _limit(limit, default=100)
    sql = (
        "SELECT * FROM imaging_reports WHERE user=? "
        "AND substr(COALESCE(imaging_date, created_ts), 1, 10) BETWEEN ? AND ?"
    )
    args: list = [u, lo.split("T", 1)[0], hi.split("T", 1)[0]]
    if modality:
        sql += " AND modality=?"
        args.append(_keyish(modality, "modality"))
    sql += " ORDER BY COALESCE(imaging_date, created_ts) DESC, id DESC LIMIT ?"
    args.append(lim)
    _audit("list_imaging_reports", f"{_audit_user(u)} limit={lim}")
    with _db() as conn:
        rows = _rows(conn.execute(sql, args))
    return {"user": u, "count": len(rows), "imaging_reports": rows}


@mcp.tool(annotations={"title": "Add immunization", "readOnlyHint": False, "idempotentHint": False})
def add_immunization(
    vaccine: str,
    immunization_date: str | None = None,
    dose: str | None = None,
    lot: str | None = None,
    provider: str | None = None,
    facility: str | None = None,
    next_due_date: str | None = None,
    notes: str | None = None,
    user: str | None = None,
) -> dict:
    """Store an immunization/vaccine record and optional next due date."""
    u = _tool_user(user, "add_immunization")
    clean_vaccine = _required_text(vaccine, "vaccine", max_chars=200)
    row = {
        "user": u,
        "created_ts": _now_iso(),
        "vaccine": clean_vaccine,
        "immunization_date": _parse_date(immunization_date, "immunization_date"),
        "dose": _optional_text(dose, "dose", max_chars=120),
        "lot": _optional_text(lot, "lot", max_chars=120),
        "provider": _optional_text(provider, "provider", max_chars=160),
        "facility": _optional_text(facility, "facility", max_chars=160),
        "next_due_date": _parse_date(next_due_date, "next_due_date"),
        "notes": _optional_text(notes, "notes"),
    }
    _audit("add_immunization", f"{_audit_user(u)} vaccine_hash={_fingerprint(clean_vaccine)}")
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO immunizations(user, created_ts, vaccine, immunization_date, dose, lot, provider, facility, next_due_date, notes) "
            "VALUES (:user, :created_ts, :vaccine, :immunization_date, :dose, :lot, :provider, :facility, :next_due_date, :notes)",
            row,
        )
        row["id"] = cur.lastrowid
    return row


@mcp.tool(annotations={"title": "List immunizations", "readOnlyHint": True, "idempotentHint": True})
def list_immunizations(
    due_within_days: int | None = None,
    user: str | None = None,
    limit: int = 200,
) -> dict:
    """List immunizations, optionally limited to vaccines due within N days."""
    u = _tool_user(user, "list_immunizations")
    lim = _limit(limit, default=200)
    today = datetime.now(timezone.utc).date()
    sql = "SELECT * FROM immunizations WHERE user=?"
    args: list = [u]
    through = None
    if due_within_days is not None:
        days = max(0, min(int(due_within_days), 3650))
        through = (today + timedelta(days=days)).isoformat()
        sql += " AND next_due_date IS NOT NULL AND next_due_date <= ?"
        args.append(through)
    sql += " ORDER BY COALESCE(immunization_date, next_due_date, created_ts) DESC, id DESC LIMIT ?"
    args.append(lim)
    _audit("list_immunizations", f"{_audit_user(u)} limit={lim}")
    with _db() as conn:
        rows = _rows(conn.execute(sql, args))
    return {
        "user": u,
        "today": today.isoformat(),
        "through": through,
        "count": len(rows),
        "immunizations": rows,
    }


@mcp.tool(annotations={"title": "Add care task", "readOnlyHint": False, "idempotentHint": False})
def add_care_task(
    title: str,
    due_date: str | None = None,
    task_type: str = "general",
    status: str = "open",
    priority: str | None = None,
    related_table: str | None = None,
    related_id: int | None = None,
    recurrence: str | None = None,
    notes: str | None = None,
    user: str | None = None,
) -> dict:
    """Store an actionable health task: appointment, refill, lab, screening, follow-up, upload, call, etc."""
    u = _tool_user(user, "add_care_task")
    clean_title = _required_text(title, "title", max_chars=240)
    clean_type = _keyish(task_type, "task_type")
    clean_status = _status(status, default="open")
    row = {
        "user": u,
        "created_ts": _now_iso(),
        "task_type": clean_type,
        "title": clean_title,
        "due_date": _parse_date(due_date, "due_date"),
        "status": clean_status,
        "priority": _optional_text(priority, "priority", max_chars=60),
        "related_table": _optional_text(related_table, "related_table", max_chars=80),
        "related_id": int(related_id) if related_id else None,
        "recurrence": _optional_text(recurrence, "recurrence", max_chars=200),
        "completed_ts": _now_iso() if clean_status in {"done", "completed"} else None,
        "notes": _optional_text(notes, "notes"),
    }
    _audit("add_care_task", f"{_audit_user(u)} task_type={clean_type} status={clean_status}")
    with _db() as conn:
        cur = conn.execute(
            "INSERT INTO care_tasks(user, created_ts, task_type, title, due_date, status, priority, related_table, "
            "related_id, recurrence, completed_ts, notes) "
            "VALUES (:user, :created_ts, :task_type, :title, :due_date, :status, :priority, :related_table, "
            ":related_id, :recurrence, :completed_ts, :notes)",
            row,
        )
        row["id"] = cur.lastrowid
    return row


@mcp.tool(annotations={"title": "Complete care task", "readOnlyHint": False, "idempotentHint": True})
def complete_care_task(task_id: int, user: str | None = None, notes: str | None = None) -> dict:
    """Mark a care task completed."""
    u = _tool_user(user, "complete_care_task")
    rid = int(task_id)
    if rid < 1:
        raise ValueError("task_id must be a positive integer")
    done_ts = _now_iso()
    _audit("complete_care_task", f"{_audit_user(u)} id={rid}")
    with _db() as conn:
        cur = conn.execute(
            "UPDATE care_tasks SET status='completed', completed_ts=?, notes=COALESCE(?, notes) WHERE id=? AND user=?",
            (done_ts, _optional_text(notes, "notes"), rid, u),
        )
        n = cur.rowcount
    return {"completed": bool(n), "id": rid, "user": u, "completed_ts": done_ts, "rows_affected": n}


@mcp.tool(annotations={"title": "List care tasks", "readOnlyHint": True, "idempotentHint": True})
def list_care_tasks(
    status: str | None = None,
    task_type: str | None = None,
    user: str | None = None,
    limit: int = 200,
) -> dict:
    """List care tasks, optionally filtered by status or task type."""
    u = _tool_user(user, "list_care_tasks")
    lim = _limit(limit, default=200)
    sql = "SELECT * FROM care_tasks WHERE user=?"
    args: list = [u]
    if status:
        sql += " AND status=?"
        args.append(_status(status, default="open"))
    if task_type:
        sql += " AND task_type=?"
        args.append(_keyish(task_type, "task_type"))
    sql += " ORDER BY COALESCE(due_date, created_ts) ASC, id DESC LIMIT ?"
    args.append(lim)
    _audit("list_care_tasks", f"{_audit_user(u)} limit={lim}")
    with _db() as conn:
        rows = _rows(conn.execute(sql, args))
    return {"user": u, "count": len(rows), "care_tasks": rows}


@mcp.tool(annotations={"title": "List due tasks", "readOnlyHint": True, "idempotentHint": True})
def list_due_tasks(days: int = 30, user: str | None = None, include_overdue: bool = True, limit: int = 200) -> dict:
    """List open care tasks due within N days, optionally including overdue tasks."""
    u = _tool_user(user, "list_due_tasks")
    horizon_days = max(0, min(int(days), 3650))
    today = datetime.now(timezone.utc).date()
    end = (today + timedelta(days=horizon_days)).isoformat()
    start = "0000-01-01" if include_overdue else today.isoformat()
    lim = _limit(limit, default=200)
    _audit("list_due_tasks", f"{_audit_user(u)} days={horizon_days} limit={lim}")
    with _db() as conn:
        rows = _rows(conn.execute(
            "SELECT * FROM care_tasks WHERE user=? AND status NOT IN ('done','completed','cancelled') "
            "AND due_date IS NOT NULL AND due_date BETWEEN ? AND ? ORDER BY due_date ASC, priority DESC LIMIT ?",
            (u, start, end, lim),
        ))
    return {"user": u, "today": today.isoformat(), "through": end, "count": len(rows), "tasks": rows}
