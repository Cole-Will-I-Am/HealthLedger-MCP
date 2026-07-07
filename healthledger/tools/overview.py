"""Whole-record views & operations: summary, agenda, care gaps, search,
delete, export, and status."""
from healthledger.runtime import *  # noqa: F401,F403


@mcp.tool(annotations={"title": "Summarize health record", "readOnlyHint": True, "idempotentHint": True})
def summarize_health(
    since: str | None = None,
    until: str | None = None,
    user: str | None = None,
) -> dict:
    """Produce a compact, analysis-ready digest of a user's whole record over a window:
    profile, per-metric statistics + trend, recent events grouped by category, and
    recent notes. This is the primary tool for an LLM to reason over someone's health;
    it returns computed descriptive data only, never a diagnosis.

    Args:
        since / until: ISO8601 or 'YYYY-MM-DD' bounds. Defaults to the last 90 days
            if `since` is omitted.
        user: which person; defaults to the primary user.
    """
    u = _tool_user(user, "summarize_health")
    if not since:
        since = (datetime.now(timezone.utc) - timedelta(days=90)).date().isoformat()
    lo, hi = _range_bounds(since, until)
    lo_date, hi_date = lo.split("T", 1)[0], hi.split("T", 1)[0]
    _audit("summarize_health", f"{_audit_user(u)} since={lo} until={hi}")

    with _db() as conn:
        profile = {r["key"]: r["value"] for r in conn.execute(
            "SELECT key, value FROM profile WHERE user=?", (u,)).fetchall()}

        metric_names = [r["metric"] for r in conn.execute(
            "SELECT DISTINCT metric FROM metrics WHERE user=? AND ts BETWEEN ? AND ?",
            (u, lo, hi)).fetchall()]

        metric_stats = {}
        for m in sorted(metric_names):
            pts = [(r["ts"], r["value"]) for r in conn.execute(
                "SELECT ts, value FROM metrics WHERE user=? AND metric=? "
                "AND ts BETWEEN ? AND ? ORDER BY ts ASC", (u, m, lo, hi)).fetchall()]
            vals = [v for _, v in pts]
            unit_row = conn.execute(
                "SELECT unit FROM metrics WHERE user=? AND metric=? "
                "AND ts BETWEEN ? AND ? ORDER BY ts DESC, id DESC LIMIT 1",
                (u, m, lo, hi)).fetchone()
            metric_stats[m] = {
                "count": len(vals),
                "unit": unit_row["unit"] if unit_row else None,
                "latest": vals[-1],
                "min": min(vals),
                "max": max(vals),
                "mean": round(statistics.fmean(vals), 4),
                "trend": _linreg_per_day(pts),
            }

        events_by_cat = {}
        for r in conn.execute(
            "SELECT category, COUNT(*) n FROM events WHERE user=? AND ts BETWEEN ? AND ? "
            "GROUP BY category", (u, lo, hi)).fetchall():
            events_by_cat[r["category"]] = r["n"]

        recent_events = _rows(conn.execute(
            "SELECT ts, category, name, detail, severity FROM events "
            "WHERE user=? AND ts BETWEEN ? AND ? ORDER BY ts DESC LIMIT 20", (u, lo, hi)))

        recent_notes = _rows(conn.execute(
            "SELECT ts, title, body, tags FROM notes "
            "WHERE user=? AND ts BETWEEN ? AND ? ORDER BY ts DESC LIMIT 10", (u, lo, hi)))

        active_conditions = _rows(conn.execute(
            "SELECT id, name, status, onset_date, severity, body_site FROM conditions "
            "WHERE user=? AND status='active' ORDER BY name LIMIT 50", (u,)))
        active_medications = _rows(conn.execute(
            "SELECT id, name, dose, route, frequency, schedule, refill_due_date FROM medications "
            "WHERE user=? AND status='active' ORDER BY name LIMIT 50", (u,)))
        active_allergies = _rows(conn.execute(
            "SELECT id, allergen, reaction, severity FROM allergies "
            "WHERE user=? AND status='active' ORDER BY allergen LIMIT 50", (u,)))
        recent_labs = _rows(conn.execute(
            "SELECT result_date, analyte, value_text, unit, ref_text, flag FROM lab_results "
            "WHERE user=? AND substr(COALESCE(result_date, created_ts), 1, 10) BETWEEN ? AND ? "
            "ORDER BY COALESCE(result_date, created_ts) DESC, id DESC LIMIT 20",
            (u, lo_date, hi_date),
        ))
        recent_biomarkers = _rows(conn.execute(
            "SELECT measured_date, biomarker, category, value_text, unit, ref_text, flag FROM biomarkers "
            "WHERE user=? AND substr(COALESCE(measured_date, created_ts), 1, 10) BETWEEN ? AND ? "
            "ORDER BY COALESCE(measured_date, created_ts) DESC, id DESC LIMIT 20",
            (u, lo_date, hi_date),
        ))
        tumor_records = _rows(conn.execute(
            "SELECT id, cancer_type, tumor_name, body_site, diagnosis_date, status, stage, grade, treatment_status "
            "FROM tumors WHERE user=? ORDER BY COALESCE(diagnosis_date, created_ts) DESC, id DESC LIMIT 20",
            (u,),
        ))
        recent_encounters = _rows(conn.execute(
            "SELECT id, encounter_date, encounter_type, provider, facility, reason, follow_up_date FROM encounters "
            "WHERE user=? AND substr(COALESCE(encounter_date, created_ts), 1, 10) BETWEEN ? AND ? "
            "ORDER BY COALESCE(encounter_date, created_ts) DESC, id DESC LIMIT 20",
            (u, lo_date, hi_date),
        ))
        recent_procedures = _rows(conn.execute(
            "SELECT id, procedure_date, name, body_site, provider, outcome, follow_up_date FROM procedures "
            "WHERE user=? AND substr(COALESCE(procedure_date, created_ts), 1, 10) BETWEEN ? AND ? "
            "ORDER BY COALESCE(procedure_date, created_ts) DESC, id DESC LIMIT 20",
            (u, lo_date, hi_date),
        ))
        recent_imaging = _rows(conn.execute(
            "SELECT id, imaging_date, modality, body_site, impression, follow_up_date FROM imaging_reports "
            "WHERE user=? AND substr(COALESCE(imaging_date, created_ts), 1, 10) BETWEEN ? AND ? "
            "ORDER BY COALESCE(imaging_date, created_ts) DESC, id DESC LIMIT 20",
            (u, lo_date, hi_date),
        ))
        immunizations_due = _rows(conn.execute(
            "SELECT id, vaccine, immunization_date, next_due_date FROM immunizations "
            "WHERE user=? AND next_due_date IS NOT NULL ORDER BY next_due_date ASC LIMIT 20",
            (u,),
        ))
        recent_documents = _rows(conn.execute(
            "SELECT id, document_date, document_type, title, source, provider, tags FROM documents "
            "WHERE user=? AND substr(COALESCE(document_date, created_ts), 1, 10) BETWEEN ? AND ? "
            "ORDER BY COALESCE(document_date, created_ts) DESC, id DESC LIMIT 20",
            (u, lo_date, hi_date),
        ))
        family_history = _rows(conn.execute(
            "SELECT id, relation, condition_name, status, age_at_onset, relative_status, age_at_death, cause_of_death FROM family_history "
            "WHERE user=? ORDER BY relation ASC, condition_name ASC LIMIT 50",
            (u,),
        ))
        recent_genomic_records = _rows(conn.execute(
            "SELECT id, record_type, test_date, report_date, gene, rsid, clinical_significance, "
            "pgx_phenotype, pgx_drug, polygenic_trait FROM genomic_records "
            "WHERE user=? AND substr(COALESCE(test_date, report_date, created_ts), 1, 10) BETWEEN ? AND ? "
            "ORDER BY COALESCE(test_date, report_date, created_ts) DESC, id DESC LIMIT 20",
            (u, lo_date, hi_date),
        ))
        recent_health_records = _rows(conn.execute(
            "SELECT id, record_date, record_type, title, source, tags FROM health_records "
            "WHERE user=? AND substr(COALESCE(record_date, created_ts), 1, 10) BETWEEN ? AND ? "
            "ORDER BY COALESCE(record_date, created_ts) DESC, id DESC LIMIT 20",
            (u, lo_date, hi_date),
        ))
        recent_reproductive_records = _rows(conn.execute(
            "SELECT id, record_type, start_date, end_date, due_date, method, replacement_due_date, outcome FROM reproductive_records "
            "WHERE user=? AND substr(COALESCE(start_date, due_date, replacement_due_date, created_ts), 1, 10) BETWEEN ? AND ? "
            "ORDER BY COALESCE(start_date, due_date, replacement_due_date, created_ts) DESC, id DESC LIMIT 20",
            (u, lo_date, hi_date),
        ))
        recent_substance_use = _rows(conn.execute(
            "SELECT id, timestamp, substance, amount, unit, frequency FROM substance_use_logs "
            "WHERE user=? AND timestamp BETWEEN ? AND ? ORDER BY timestamp DESC, id DESC LIMIT 20",
            (u, lo, hi),
        ))
        recent_wearable_samples = _rows(conn.execute(
            "SELECT id, source_name, sample_type, start_ts, end_ts, value, unit, aggregation FROM wearable_samples "
            "WHERE user=? AND start_ts BETWEEN ? AND ? ORDER BY start_ts DESC, id DESC LIMIT 20",
            (u, lo, hi),
        ))
        wearable_types = _rows(conn.execute(
            "SELECT sample_type, COUNT(*) AS n, MAX(start_ts) AS latest_ts FROM wearable_samples "
            "WHERE user=? AND start_ts BETWEEN ? AND ? GROUP BY sample_type ORDER BY sample_type LIMIT 50",
            (u, lo, hi),
        ))
        due_tasks = _rows(conn.execute(
            "SELECT id, task_type, title, due_date, status, priority FROM care_tasks "
            "WHERE user=? AND status NOT IN ('done','completed','cancelled') "
            "AND due_date IS NOT NULL ORDER BY due_date ASC LIMIT 20",
            (u,),
        ))
        domain_counts = _table_counts(conn, u)

    return {
        "user": u,
        "window": {"since": lo, "until": hi},
        "profile": profile,
        "active_conditions": active_conditions,
        "active_medications": active_medications,
        "active_allergies": active_allergies,
        "metrics": metric_stats,
        "event_counts": events_by_cat,
        "recent_labs": recent_labs,
        "recent_biomarkers": recent_biomarkers,
        "tumor_records": tumor_records,
        "recent_encounters": recent_encounters,
        "recent_procedures": recent_procedures,
        "recent_imaging": recent_imaging,
        "immunizations_due": immunizations_due,
        "recent_documents": recent_documents,
        "family_history": family_history,
        "recent_genomic_records": recent_genomic_records,
        "recent_health_records": recent_health_records,
        "recent_reproductive_records": recent_reproductive_records,
        "recent_substance_use": recent_substance_use,
        "recent_wearable_samples": recent_wearable_samples,
        "wearable_types": wearable_types,
        "due_tasks": due_tasks,
        "domain_counts": domain_counts,
        "recent_events": recent_events,
        "recent_notes": recent_notes,
        "disclaimer": "Descriptive data only; not medical advice. Consult a professional for clinical decisions.",
    }


@mcp.tool(annotations={"title": "Health agenda", "readOnlyHint": True, "idempotentHint": True})
def health_agenda(days: int = 30, user: str | None = None) -> dict:
    """Return a deterministic agenda: due tasks, refills, follow-ups, immunizations, and medication schedule."""
    u = _tool_user(user, "health_agenda")
    horizon_days = max(0, min(int(days), 3650))
    today = datetime.now(timezone.utc).date()
    end = (today + timedelta(days=horizon_days)).isoformat()
    today_s = today.isoformat()
    _audit("health_agenda", f"{_audit_user(u)} days={horizon_days}")
    with _db() as conn:
        tasks = _rows(conn.execute(
            "SELECT id, task_type, title, due_date, status, priority, related_table, related_id FROM care_tasks "
            "WHERE user=? AND status NOT IN ('done','completed','cancelled') AND due_date IS NOT NULL "
            "AND due_date <= ? ORDER BY due_date ASC, priority DESC LIMIT 100",
            (u, end),
        ))
        refills = _rows(conn.execute(
            "SELECT id, name, dose, refill_due_date, prescriber FROM medications "
            "WHERE user=? AND status='active' AND refill_due_date IS NOT NULL AND refill_due_date <= ? "
            "ORDER BY refill_due_date ASC LIMIT 50",
            (u, end),
        ))
        followups = []
        for table, date_col, title_cols in [
            ("encounters", "follow_up_date", "encounter_type || COALESCE(': ' || provider, '')"),
            ("procedures", "follow_up_date", "name"),
            ("imaging_reports", "follow_up_date", "modality || COALESCE(': ' || body_site, '')"),
        ]:
            followups.extend(_rows(conn.execute(
                f"SELECT '{table}' AS source_table, id, {date_col} AS due_date, {title_cols} AS title "
                f"FROM {table} WHERE user=? AND {date_col} IS NOT NULL AND {date_col} <= ? "
                f"ORDER BY {date_col} ASC LIMIT 50",
                (u, end),
            )))
        immunizations = _rows(conn.execute(
            "SELECT id, vaccine, next_due_date FROM immunizations "
            "WHERE user=? AND next_due_date IS NOT NULL AND next_due_date <= ? ORDER BY next_due_date ASC LIMIT 50",
            (u, end),
        ))
        reproductive_dates = _rows(conn.execute(
            "SELECT id, record_type, method, due_date, replacement_due_date FROM reproductive_records "
            "WHERE user=? AND ((due_date IS NOT NULL AND due_date <= ?) "
            "OR (replacement_due_date IS NOT NULL AND replacement_due_date <= ?)) "
            "ORDER BY COALESCE(due_date, replacement_due_date) ASC LIMIT 50",
            (u, end, end),
        ))
        meds = _rows(conn.execute(
            "SELECT id, name, dose, route, frequency, schedule, instructions FROM medications "
            "WHERE user=? AND status='active' ORDER BY name LIMIT 100",
            (u,),
        ))
    overdue_tasks = [t for t in tasks if t.get("due_date") and t["due_date"] < today_s]
    return {
        "user": u,
        "today": today_s,
        "through": end,
        "medication_schedule": meds,
        "tasks": tasks,
        "overdue_tasks": overdue_tasks,
        "refills": refills,
        "followups": followups,
        "immunizations_due": immunizations,
        "reproductive_dates": reproductive_dates,
        "disclaimer": "Agenda organizes stored dates only; verify clinical timing with a licensed professional.",
    }


@mcp.tool(annotations={"title": "Care gap report", "readOnlyHint": True, "idempotentHint": True})
def care_gap_report(user: str | None = None) -> dict:
    """Report missing/stale data and unresolved stored follow-ups. This is organizational, not clinical guidance."""
    u = _tool_user(user, "care_gap_report")
    today = datetime.now(timezone.utc).date().isoformat()
    _audit("care_gap_report", _audit_user(u))
    gaps = []
    with _db() as conn:
        counts = _table_counts(conn, u)
        active_meds_missing_schedule = _rows(conn.execute(
            "SELECT id, name FROM medications WHERE user=? AND status='active' "
            "AND (schedule IS NULL OR schedule='') ORDER BY name LIMIT 50",
            (u,),
        ))
        open_tasks_without_due = _rows(conn.execute(
            "SELECT id, task_type, title FROM care_tasks WHERE user=? "
            "AND status NOT IN ('done','completed','cancelled') AND due_date IS NULL ORDER BY created_ts DESC LIMIT 50",
            (u,),
        ))
        due_followups = []
        for table, date_col, label_sql in [
            ("encounters", "follow_up_date", "encounter_type || COALESCE(': ' || provider, '')"),
            ("procedures", "follow_up_date", "name"),
            ("imaging_reports", "follow_up_date", "modality || COALESCE(': ' || body_site, '')"),
        ]:
            due_followups.extend(_rows(conn.execute(
                f"SELECT '{table}' AS source_table, id, {label_sql} AS title, {date_col} AS follow_up_date "
                f"FROM {table} WHERE user=? AND {date_col} IS NOT NULL AND {date_col} <= ? "
                f"ORDER BY {date_col} LIMIT 50",
                (u, today),
            )))
        latest_lab = conn.execute(
            "SELECT MAX(result_date) AS latest FROM lab_results WHERE user=?", (u,)
        ).fetchone()["latest"]
        latest_encounter = conn.execute(
            "SELECT MAX(encounter_date) AS latest FROM encounters WHERE user=?", (u,)
        ).fetchone()["latest"]
        latest_physical = conn.execute(
            "SELECT MAX(encounter_date) AS latest FROM encounters WHERE user=? "
            "AND (encounter_type LIKE '%physical%' OR encounter_type IN ('annual', 'wellness', 'preventive'))",
            (u,),
        ).fetchone()["latest"]
        latest_substance_log = conn.execute(
            "SELECT MAX(timestamp) AS latest FROM substance_use_logs WHERE user=?", (u,)
        ).fetchone()["latest"]
        latest_wearable_sample = conn.execute(
            "SELECT MAX(start_ts) AS latest FROM wearable_samples WHERE user=?", (u,)
        ).fetchone()["latest"]
    for table in ("conditions", "medications", "allergies", "lab_results", "encounters", "immunizations"):
        if counts.get(table, 0) == 0:
            gaps.append({"kind": "missing_domain", "table": table, "message": f"no stored {table}"})
    for med in active_meds_missing_schedule:
        gaps.append({"kind": "medication_missing_schedule", "medication_id": med["id"], "name": med["name"]})
    for task in open_tasks_without_due:
        gaps.append({"kind": "task_missing_due_date", "task_id": task["id"], "title": task["title"]})
    if latest_physical is None:
        gaps.append({
            "kind": "missing_physical_record",
            "message": "no stored annual/wellness/physical encounter",
        })
    return {
        "user": u,
        "checked_at": _now_iso(),
        "counts": counts,
        "latest_lab_result_date": latest_lab,
        "latest_encounter_date": latest_encounter,
        "latest_physical_date": latest_physical,
        "latest_substance_log_ts": latest_substance_log,
        "latest_wearable_sample_ts": latest_wearable_sample,
        "overdue_followups": due_followups,
        "gaps": gaps,
        "disclaimer": "Care gaps are based only on stored data completeness/dates; this is not clinical screening advice.",
    }


@mcp.tool(annotations={"title": "Search records", "readOnlyHint": True, "idempotentHint": True})
def search_records(query: str, user: str | None = None, limit: int = 50) -> dict:
    """Full-text-ish search across stored health domains (case-insensitive substring)."""
    u = _tool_user(user, "search_records")
    lim = _limit(limit, default=50)
    clean_query = _required_text(query, "query", max_chars=256)
    q = _like_pattern(clean_query)
    _audit("search_records", f"{_audit_user(u)} query_hash={_fingerprint(clean_query)} limit={lim}")
    with _db() as conn:
        events = _rows(conn.execute(
            "SELECT id, ts, category, name, detail FROM events WHERE user=? AND "
            "(name LIKE ? ESCAPE '\\' OR detail LIKE ? ESCAPE '\\') ORDER BY ts DESC LIMIT ?",
            (u, q, q, lim)))
        notes = _rows(conn.execute(
            "SELECT id, ts, title, body, tags FROM notes WHERE user=? AND "
            "(title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\') "
            "ORDER BY ts DESC LIMIT ?",
            (u, q, q, q, lim)))
        medications = _rows(conn.execute(
            "SELECT id, name, dose, frequency, status FROM medications WHERE user=? AND "
            "(name LIKE ? ESCAPE '\\' OR generic_name LIKE ? ESCAPE '\\' OR indication LIKE ? ESCAPE '\\') "
            "ORDER BY name LIMIT ?",
            (u, q, q, q, lim)))
        medication_logs = _rows(conn.execute(
            "SELECT id, medication_id, medication_name, scheduled_ts, taken_ts, status, dose_taken FROM medication_logs "
            "WHERE user=? AND (medication_name LIKE ? ESCAPE '\\' OR status LIKE ? ESCAPE '\\' "
            "OR dose_taken LIKE ? ESCAPE '\\' OR note LIKE ? ESCAPE '\\') "
            "ORDER BY COALESCE(taken_ts, scheduled_ts, created_ts) DESC LIMIT ?",
            (u, q, q, q, q, lim)))
        conditions = _rows(conn.execute(
            "SELECT id, name, status, onset_date, body_site FROM conditions WHERE user=? AND "
            "(name LIKE ? ESCAPE '\\' OR body_site LIKE ? ESCAPE '\\' OR notes LIKE ? ESCAPE '\\') "
            "ORDER BY name LIMIT ?",
            (u, q, q, q, lim)))
        allergies = _rows(conn.execute(
            "SELECT id, allergen, reaction, severity, status, noted_date FROM allergies WHERE user=? AND "
            "(allergen LIKE ? ESCAPE '\\' OR reaction LIKE ? ESCAPE '\\' OR notes LIKE ? ESCAPE '\\') "
            "ORDER BY allergen LIMIT ?",
            (u, q, q, q, lim)))
        lab_reports = _rows(conn.execute(
            "SELECT id, report_date, collection_date, lab_name, title, source FROM lab_reports WHERE user=? AND "
            "(title LIKE ? ESCAPE '\\' OR summary LIKE ? ESCAPE '\\' OR lab_name LIKE ? ESCAPE '\\' "
            "OR ordering_provider LIKE ? ESCAPE '\\') ORDER BY COALESCE(collection_date, report_date, created_ts) DESC LIMIT ?",
            (u, q, q, q, q, lim)))
        labs = _rows(conn.execute(
            "SELECT id, result_date, analyte, value_text, unit, flag FROM lab_results WHERE user=? AND "
            "(analyte LIKE ? ESCAPE '\\' OR value_text LIKE ? ESCAPE '\\' OR flag LIKE ? ESCAPE '\\') "
            "ORDER BY COALESCE(result_date, created_ts) DESC LIMIT ?",
            (u, q, q, q, lim)))
        biomarkers = _rows(conn.execute(
            "SELECT id, measured_date, biomarker, value_text, unit, flag FROM biomarkers WHERE user=? AND "
            "(biomarker LIKE ? ESCAPE '\\' OR value_text LIKE ? ESCAPE '\\' OR category LIKE ? ESCAPE '\\') "
            "ORDER BY COALESCE(measured_date, created_ts) DESC LIMIT ?",
            (u, q, q, q, lim)))
        tumors = _rows(conn.execute(
            "SELECT id, cancer_type, tumor_name, body_site, status, diagnosis_date FROM tumors WHERE user=? AND "
            "(tumor_name LIKE ? ESCAPE '\\' OR cancer_type LIKE ? ESCAPE '\\' OR body_site LIKE ? ESCAPE '\\' "
            "OR biomarker_summary LIKE ? ESCAPE '\\') ORDER BY COALESCE(diagnosis_date, created_ts) DESC LIMIT ?",
            (u, q, q, q, q, lim)))
        genomic_records = _rows(conn.execute(
            "SELECT id, record_type, test_date, report_date, gene, rsid, clinical_significance, "
            "pgx_phenotype, pgx_drug, polygenic_trait FROM genomic_records WHERE user=? AND "
            "(record_type LIKE ? ESCAPE '\\' OR gene LIKE ? ESCAPE '\\' OR hgvs_c LIKE ? ESCAPE '\\' "
            "OR hgvs_p LIKE ? ESCAPE '\\' OR rsid LIKE ? ESCAPE '\\' OR clinical_significance LIKE ? ESCAPE '\\' "
            "OR associated_condition LIKE ? ESCAPE '\\' OR pgx_phenotype LIKE ? ESCAPE '\\' "
            "OR pgx_drug LIKE ? ESCAPE '\\' OR pgx_guideline_source LIKE ? ESCAPE '\\' "
            "OR polygenic_trait LIKE ? ESCAPE '\\' OR source LIKE ? ESCAPE '\\' OR notes LIKE ? ESCAPE '\\' "
            "OR extra_json LIKE ? ESCAPE '\\') ORDER BY COALESCE(test_date, report_date, created_ts) DESC LIMIT ?",
            (u, q, q, q, q, q, q, q, q, q, q, q, q, q, q, lim)))
        encounters = _rows(conn.execute(
            "SELECT id, encounter_date, encounter_type, provider, facility, reason FROM encounters WHERE user=? AND "
            "(encounter_type LIKE ? ESCAPE '\\' OR provider LIKE ? ESCAPE '\\' OR facility LIKE ? ESCAPE '\\' "
            "OR reason LIKE ? ESCAPE '\\') ORDER BY COALESCE(encounter_date, created_ts) DESC LIMIT ?",
            (u, q, q, q, q, lim)))
        procedures = _rows(conn.execute(
            "SELECT id, procedure_date, name, body_site, provider, facility FROM procedures WHERE user=? AND "
            "(name LIKE ? ESCAPE '\\' OR body_site LIKE ? ESCAPE '\\' OR provider LIKE ? ESCAPE '\\' "
            "OR outcome LIKE ? ESCAPE '\\' OR notes LIKE ? ESCAPE '\\') "
            "ORDER BY COALESCE(procedure_date, created_ts) DESC LIMIT ?",
            (u, q, q, q, q, q, lim)))
        imaging_reports = _rows(conn.execute(
            "SELECT id, imaging_date, modality, body_site, facility, impression FROM imaging_reports WHERE user=? AND "
            "(modality LIKE ? ESCAPE '\\' OR body_site LIKE ? ESCAPE '\\' OR facility LIKE ? ESCAPE '\\' "
            "OR findings LIKE ? ESCAPE '\\' OR impression LIKE ? ESCAPE '\\') "
            "ORDER BY COALESCE(imaging_date, created_ts) DESC LIMIT ?",
            (u, q, q, q, q, q, lim)))
        immunizations = _rows(conn.execute(
            "SELECT id, vaccine, immunization_date, dose, next_due_date FROM immunizations WHERE user=? AND "
            "(vaccine LIKE ? ESCAPE '\\' OR dose LIKE ? ESCAPE '\\' OR provider LIKE ? ESCAPE '\\' "
            "OR facility LIKE ? ESCAPE '\\' OR notes LIKE ? ESCAPE '\\') "
            "ORDER BY COALESCE(immunization_date, next_due_date, created_ts) DESC LIMIT ?",
            (u, q, q, q, q, q, lim)))
        documents = _rows(conn.execute(
            "SELECT id, document_date, document_type, title, source, tags FROM documents WHERE user=? AND "
            "(title LIKE ? ESCAPE '\\' OR summary LIKE ? ESCAPE '\\' OR content_text LIKE ? ESCAPE '\\' "
            "OR tags LIKE ? ESCAPE '\\') ORDER BY COALESCE(document_date, created_ts) DESC LIMIT ?",
            (u, q, q, q, q, lim)))
        family_history = _rows(conn.execute(
            "SELECT id, relation, condition_name, status, age_at_onset, relative_status, age_at_death, cause_of_death FROM family_history WHERE user=? AND "
            "(relation LIKE ? ESCAPE '\\' OR condition_name LIKE ? ESCAPE '\\' OR status LIKE ? ESCAPE '\\' "
            "OR relative_status LIKE ? ESCAPE '\\' OR cause_of_death LIKE ? ESCAPE '\\' OR notes LIKE ? ESCAPE '\\') "
            "ORDER BY relation, condition_name LIMIT ?",
            (u, q, q, q, q, q, q, lim)))
        tasks = _rows(conn.execute(
            "SELECT id, task_type, title, due_date, status, priority FROM care_tasks WHERE user=? AND "
            "(title LIKE ? ESCAPE '\\' OR task_type LIKE ? ESCAPE '\\' OR notes LIKE ? ESCAPE '\\') "
            "ORDER BY COALESCE(due_date, created_ts) ASC LIMIT ?",
            (u, q, q, q, lim)))
        health_records = _rows(conn.execute(
            "SELECT id, record_date, record_type, title, source, tags FROM health_records WHERE user=? AND "
            "(title LIKE ? ESCAPE '\\' OR body LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\' OR extra_json LIKE ? ESCAPE '\\') "
            "ORDER BY COALESCE(record_date, created_ts) DESC LIMIT ?",
            (u, q, q, q, q, lim)))
        reproductive_records = _rows(conn.execute(
            "SELECT id, record_type, start_date, end_date, due_date, method, replacement_due_date, outcome FROM reproductive_records "
            "WHERE user=? AND (record_type LIKE ? ESCAPE '\\' OR method LIKE ? ESCAPE '\\' OR outcome LIKE ? ESCAPE '\\' "
            "OR source LIKE ? ESCAPE '\\' OR notes LIKE ? ESCAPE '\\' OR extra_json LIKE ? ESCAPE '\\') "
            "ORDER BY COALESCE(start_date, due_date, replacement_due_date, created_ts) DESC LIMIT ?",
            (u, q, q, q, q, q, q, lim)))
        substance_use = _rows(conn.execute(
            "SELECT id, timestamp, substance, amount, unit, frequency, route, context FROM substance_use_logs "
            "WHERE user=? AND (substance LIKE ? ESCAPE '\\' OR unit LIKE ? ESCAPE '\\' OR frequency LIKE ? ESCAPE '\\' "
            "OR route LIKE ? ESCAPE '\\' OR context LIKE ? ESCAPE '\\' OR notes LIKE ? ESCAPE '\\') "
            "ORDER BY timestamp DESC LIMIT ?",
            (u, q, q, q, q, q, q, lim)))
        wearable_sources = _rows(conn.execute(
            "SELECT id, name, source_type, manufacturer, model, last_sync_ts FROM wearable_sources WHERE user=? AND "
            "(name LIKE ? ESCAPE '\\' OR source_type LIKE ? ESCAPE '\\' OR manufacturer LIKE ? ESCAPE '\\' "
            "OR model LIKE ? ESCAPE '\\' OR external_id LIKE ? ESCAPE '\\') ORDER BY name ASC LIMIT ?",
            (u, q, q, q, q, q, lim)))
        wearable_samples = _rows(conn.execute(
            "SELECT id, source_name, sample_type, start_ts, end_ts, value, unit, aggregation FROM wearable_samples WHERE user=? AND "
            "(source_name LIKE ? ESCAPE '\\' OR sample_type LIKE ? ESCAPE '\\' OR unit LIKE ? ESCAPE '\\' "
            "OR aggregation LIKE ? ESCAPE '\\' OR metadata_json LIKE ? ESCAPE '\\' OR notes LIKE ? ESCAPE '\\') "
            "ORDER BY start_ts DESC LIMIT ?",
            (u, q, q, q, q, q, q, lim)))
    return {
        "user": u,
        "query": clean_query,
        "events": events,
        "notes": notes,
        "medications": medications,
        "medication_logs": medication_logs,
        "conditions": conditions,
        "allergies": allergies,
        "lab_reports": lab_reports,
        "lab_results": labs,
        "biomarkers": biomarkers,
        "tumors": tumors,
        "genomic_records": genomic_records,
        "encounters": encounters,
        "procedures": procedures,
        "imaging_reports": imaging_reports,
        "immunizations": immunizations,
        "documents": documents,
        "family_history": family_history,
        "care_tasks": tasks,
        "health_records": health_records,
        "reproductive_records": reproductive_records,
        "substance_use_logs": substance_use,
        "wearable_sources": wearable_sources,
        "wearable_samples": wearable_samples,
    }


@mcp.tool(annotations={"title": "Delete a record", "readOnlyHint": False, "destructiveHint": True, "idempotentHint": True})
def delete_record(table: str, record_id: int, user: str | None = None) -> dict:
    """Delete one record by id (for corrections). DESTRUCTIVE.

    Args:
        table: one of the health data tables with integer ids.
        record_id: the row id to delete.
        user: which person owns the row; defaults to the primary user (guards against
            deleting another user's row by id).
    """
    u = _tool_user(user, "delete_record")
    t = table.strip().lower()
    if t not in EXPORT_TABLES:
        allowed = "|".join(sorted(EXPORT_TABLES))
        return {"deleted": False, "error": f"table must be one of {allowed}, got {table!r}"}
    rid = int(record_id)
    if rid < 1:
        raise ValueError("record_id must be a positive integer")
    _audit("delete_record", f"{_audit_user(u)} table={t} id={rid}")
    with _db() as conn:
        cur = conn.execute(f"DELETE FROM {t} WHERE id=? AND user=?", (rid, u))
        n = cur.rowcount
    return {"deleted": bool(n), "table": t, "id": rid, "rows_affected": n}


@mcp.tool(annotations={"title": "Export data page", "readOnlyHint": True, "idempotentHint": True})
def export_data(
    user: str | None = None,
    table: str = "all",
    limit: int = MAX_EXPORT_ROWS,
    offset: int = 0,
    include_profile: bool = True,
) -> dict:
    """Export a bounded page of a user's record as structured JSON.

    Args:
        user: which person; defaults to the primary user.
        table: 'all', 'metrics', 'events', or 'notes'. 'all' returns one page per
            record table.
        limit: max rows per exported table. Capped by HEALTH_MCP_MAX_EXPORT_ROWS.
        offset: zero-based row offset for paginating metrics/events/notes.
        include_profile: include profile key/value facts. Profile rows are small and
            always returned in full when included.
    """
    u = _tool_user(user, "export_data")
    requested_table = _required_text(table, "table", max_chars=32).lower()
    lim = _export_limit(limit)
    off = _offset(offset)
    if requested_table != "all" and requested_table not in EXPORT_TABLES:
        allowed = "all|" + "|".join(sorted(EXPORT_TABLES))
        raise ValueError(f"table must be one of {allowed}")
    _audit("export_data", f"{_audit_user(u)} table={requested_table} limit={lim} offset={off}")
    with _db() as conn:
        profile = {}
        if include_profile:
            profile = {r["key"]: r["value"] for r in conn.execute(
                "SELECT key, value FROM profile WHERE user=? ORDER BY key", (u,)).fetchall()}
        pages = {}
        if requested_table == "all":
            for export_table in sorted(EXPORT_TABLES):
                pages[export_table] = _export_page(conn, u, export_table, lim, off)
        else:
            pages[requested_table] = _export_page(conn, u, requested_table, lim, off)
    return {
        "user": u,
        "exported_at": _now_iso(),
        "limit": lim,
        "offset": off,
        "profile": profile,
        "pages": pages,
    }

@mcp.tool(annotations={"title": "Health MCP status", "readOnlyHint": True, "idempotentHint": True})
def health_status(user: str | None = None) -> dict:
    """Return non-secret operational status and per-user record counts."""
    u = _tool_user(user, "health_status")
    _audit("health_status", _audit_user(u))
    audit_parent = AUDIT_LOG.parent
    with _db() as conn:
        meta = _schema_meta(conn)
        counts = _table_counts(conn, u)
        all_counts = _table_counts(conn)
        journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    return {
        "status": "ok",
        "checked_at": _now_iso(),
        "user": u,
        "schema": {
            "expected_version": SCHEMA_VERSION,
            "current_version": int(meta.get("schema_version", "0")),
            "created_at": meta.get("created_at"),
        },
        "database": {
            "path": str(DB_PATH),
            "exists": DB_PATH.exists(),
            "size_bytes": DB_PATH.stat().st_size if DB_PATH.exists() else 0,
            "journal_mode": journal_mode,
        },
        "audit_log": {
            "path": str(AUDIT_LOG),
            "exists": AUDIT_LOG.exists(),
            "parent_writable": os.access(audit_parent, os.W_OK),
            "size_bytes": AUDIT_LOG.stat().st_size if AUDIT_LOG.exists() else 0,
        },
        "config": {
            "transport": TRANSPORT,
            "public_url": PUBLIC_URL,
            "mcp_path": MCP_PATH,
            "bind_host": HOST,
            "port": PORT,
            "max_rows": MAX_ROWS,
            "max_export_rows": MAX_EXPORT_ROWS,
            "max_bulk_json_chars": MAX_BULK_JSON_CHARS,
            "max_wearable_import_rows": MAX_WEARABLE_IMPORT_ROWS,
            "rate_limit_enabled": RATE_LIMIT_ENABLED,
            "rate_limit_calls": RATE_LIMIT_CALLS,
            "rate_limit_window_seconds": RATE_LIMIT_WINDOW_SECONDS,
        },
        "counts": counts,
        "total_counts": all_counts,
    }
