#!/usr/bin/env python3
"""Offline tool smoke tests against a temporary database.

Run: ./.venv/bin/python test_tools.py
"""
import os
import json
import tempfile

failures = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + (f"  {extra}" if extra else ""))
    if not cond:
        failures.append(name)


with tempfile.TemporaryDirectory() as tmp:
    os.environ["HEALTH_MCP_TRANSPORT"] = "stdio"
    os.environ["HEALTH_MCP_DB"] = os.path.join(tmp, "health.db")
    os.environ["HEALTH_MCP_AUDIT_LOG"] = os.path.join(tmp, "audit.log")
    os.environ["HEALTH_MCP_DEFAULT_USER"] = "me"

    import server  # noqa: E402

    server._init_db()

    first = server.log_metric(" Weight Kg ", 180, "lb", timestamp="2026-07-06T08:00:00Z", user="  ")
    second = server.log_metric("weight kg", 181, "lb", timestamp="2026-07-06T20:00:00Z")
    check("blank user falls back to default", first["user"] == "me")
    check("metric names normalize", first["metric"] == "weight_kg" and second["metric"] == "weight_kg")

    day = server.get_metrics("weight_kg", since="2026-07-06", until="2026-07-06")
    check("date-only until includes the full day", day["count"] == 2, str(day))

    analysis = server.analyze_metric("weight kg", since="2026-07-06", until="2026-07-06")
    stats = analysis.get("stats", {})
    check("analysis sees normalized metric", stats.get("count") == 2, str(analysis))
    check("analysis trend rises", (stats.get("trend") or {}).get("direction") == "rising", str(stats))

    event = server.log_event("symptom", "Headache", detail="after lunch", severity=4)
    check("event severity stored as float", event["severity"] == 4.0, str(event))

    try:
        server.log_event("invalid", "bad category")
    except ValueError:
        check("invalid event category rejected", True)
    else:
        check("invalid event category rejected", False)

    server.log_note("felt 100% recovered", title="Recovery", tags="sleep,recovery")
    server.log_note("plain note", title="Other", tags="misc")
    literal_percent = server.search_records("%")
    check("search treats % as a literal", len(literal_percent["notes"]) == 1, str(literal_percent))

    server.set_profile("Condition", "example")
    profile = server.get_profile()
    check("profile stores normalized keys", profile["profile"].get("condition") == "example", str(profile))
    deleted_profile = server.delete_profile("condition")
    check("delete_profile removes profile keys", deleted_profile["deleted"] is True, str(deleted_profile))

    export_page = server.export_data(table="metrics", limit=1)
    metrics_page = export_page["pages"]["metrics"]
    check("export_data is paged", metrics_page["count"] == 1 and metrics_page["next_offset"] == 1,
          str(export_page))
    export_all = server.export_data(limit=1)
    check("export_data all returns pages", {"events", "metrics", "notes", "medications", "lab_results",
                                            "reproductive_records", "wearable_samples"}.issubset(set(export_all["pages"])),
          str(export_all))

    med = server.add_medication(
        "Metformin",
        dose="500 mg",
        frequency="twice daily",
        schedule="morning and evening with meals",
        refill_due_date="2026-07-06",
    )
    med_schedule = server.list_medication_schedule()
    check("medication schedule includes active med", med_schedule["medications"][0]["name"] == "Metformin",
          str(med_schedule))
    check("refills_due includes due med", med_schedule["refills_due"][0]["name"] == "Metformin",
          str(med_schedule))
    med_log = server.log_medication_taken("Metformin", medication_id=med["id"], dose_taken="500 mg")
    check("medication log stores dose", med_log["dose_taken"] == "500 mg", str(med_log))
    med_logs = server.list_medication_logs("Metformin")
    check("medication logs list", med_logs["count"] == 1 and med_logs["medication_logs"][0]["dose_taken"] == "500 mg",
          str(med_logs))

    condition = server.add_condition("Hypertension", onset_date="2024-01-01", severity="mild")
    allergy = server.add_allergy("Penicillin", reaction="rash", severity="moderate")
    check("condition stored", condition["name"] == "Hypertension", str(condition))
    check("allergy stored", allergy["allergen"] == "Penicillin", str(allergy))

    report = server.add_lab_report("Annual bloodwork", collection_date="2026-07-06", lab_name="Example Lab")
    lab_reports = server.list_lab_reports(since="2026-07-06", until="2026-07-06")
    check("lab reports list", lab_reports["count"] == 1 and lab_reports["lab_reports"][0]["title"] == "Annual bloodwork",
          str(lab_reports))
    server.add_lab_result("A1C Percent", "5.7", unit="%", result_date="2026-01-01", report_id=report["id"])
    server.add_lab_result("A1C Percent", "5.9", unit="%", result_date="2026-07-06", report_id=report["id"])
    lab_trend = server.analyze_lab_trend("a1c percent")
    check("lab trend sees numeric results", lab_trend["count"] == 2 and lab_trend["trend"]["direction"] == "rising",
          str(lab_trend))

    server.add_biomarker("CRP", "2.0", unit="mg/L", measured_date="2026-01-01", category="inflammation")
    server.add_biomarker("CRP", "1.0", unit="mg/L", measured_date="2026-07-06", category="inflammation")
    biomarkers = server.list_biomarkers("crp", category="inflammation")
    check("biomarkers list", biomarkers["count"] == 2, str(biomarkers))
    biomarker_trend = server.analyze_biomarker_trend("crp")
    check("biomarker trend works", biomarker_trend["count"] == 2 and biomarker_trend["trend"]["direction"] == "falling",
          str(biomarker_trend))

    tumor = server.add_tumor_record(
        "Left lung nodule",
        cancer_type="unknown",
        body_site="left lung",
        status="monitoring",
        size_value=8,
        size_unit="mm",
        biomarker_summary="pathology pending",
    )
    check("tumor record stored", tumor["tumor_name"] == "Left lung nodule", str(tumor))

    encounter = server.add_encounter(
        "physical",
        encounter_date="2026-07-06",
        provider="Dr. Example",
        vitals_summary="BP 120/80",
        follow_up_date="2026-07-06",
    )
    procedure = server.add_procedure("Colonoscopy", procedure_date="2026-07-06", outcome="normal", follow_up_date="2026-07-06")
    imaging = server.add_imaging_report(
        "MRI",
        imaging_date="2026-07-06",
        body_site="brain",
        impression="normal",
        follow_up_date="2026-07-06",
    )
    server.add_immunization("Tetanus", immunization_date="2016-07-06", next_due_date="2026-07-06")
    task = server.add_care_task("Schedule follow-up", due_date="2026-07-06", task_type="follow_up")
    tasks = server.list_care_tasks(status="open")
    check("care tasks list", tasks["count"] == 1 and tasks["care_tasks"][0]["id"] == task["id"], str(tasks))
    procedures = server.list_procedures(since="2026-07-06", until="2026-07-06")
    imaging_reports = server.list_imaging_reports("mri")
    immunizations = server.list_immunizations(due_within_days=1)
    check("procedures list", procedures["count"] == 1 and procedures["procedures"][0]["id"] == procedure["id"],
          str(procedures))
    check("imaging reports list", imaging_reports["count"] == 1 and imaging_reports["imaging_reports"][0]["id"] == imaging["id"],
          str(imaging_reports))
    check("immunizations list", immunizations["count"] == 1 and immunizations["immunizations"][0]["vaccine"] == "Tetanus",
          str(immunizations))
    agenda = server.health_agenda(days=1)
    check("agenda includes care task", any(t["id"] == task["id"] for t in agenda["tasks"]), str(agenda))
    check("agenda includes refill", any(r["id"] == med["id"] for r in agenda["refills"]), str(agenda))
    check("agenda includes follow-up", any(f["id"] == encounter["id"] for f in agenda["followups"]), str(agenda))
    check("agenda includes procedure follow-up", any(f["source_table"] == "procedures" for f in agenda["followups"]),
          str(agenda))
    check("agenda includes imaging follow-up", any(f["source_table"] == "imaging_reports" for f in agenda["followups"]),
          str(agenda))
    check("agenda includes immunization due", agenda["immunizations_due"][0]["vaccine"] == "Tetanus", str(agenda))

    doc = server.add_document("Pathology report", "pathology", document_date="2026-07-06", summary="pathology pending")
    fam = server.add_family_history(
        "mother",
        "diabetes",
        age_at_onset=55,
        relative_status="deceased",
        age_at_death=78,
        cause_of_death="stroke",
    )
    generic = server.add_health_record("genetic_test", "Carrier screen", extra_json='{"result":"negative"}')
    documents = server.list_documents("pathology")
    family_history = server.list_family_history("mother")
    health_records = server.list_health_records("genetic_test")
    check("document stored", doc["document_type"] == "pathology", str(doc))
    check("family history stored", fam["relation"] == "mother", str(fam))
    check("family history stores mortality detail", fam["relative_status"] == "deceased" and fam["age_at_death"] == 78.0,
          str(fam))
    check("generic health record stores JSON", generic["extra_json"] == '{"result":"negative"}', str(generic))
    check("documents list", documents["count"] == 1 and documents["documents"][0]["id"] == doc["id"],
          str(documents))
    check("family history list", family_history["count"] == 1 and family_history["family_history"][0]["id"] == fam["id"],
          str(family_history))
    check("health records list", health_records["count"] == 1 and health_records["health_records"][0]["id"] == generic["id"],
          str(health_records))

    cycle1 = server.add_reproductive_record("cycle", start_date="2026-06-01", end_date="2026-06-05", flow_intensity="medium", pain_level=3)
    cycle2 = server.add_reproductive_record("cycle", start_date="2026-06-29", end_date="2026-07-03", flow_intensity="light", pain_level=2)
    contraception = server.add_reproductive_record(
        "contraception",
        method="iud",
        insertion_date="2024-07-06",
        replacement_due_date="2026-07-06",
        source="clinic note",
    )
    reproductive = server.list_reproductive_records("cycle", since="2026-06-01", until="2026-07-06")
    reproductive_trend = server.analyze_reproductive_trend()
    check("reproductive records list", reproductive["count"] == 2 and reproductive["reproductive_records"][0]["id"] == cycle2["id"],
          str(reproductive))
    check("reproductive trend computes cycle length",
          reproductive_trend["cycle_length_days"]["mean"] == 28.0 and reproductive_trend["period_length_days"]["mean"] == 5.0,
          str(reproductive_trend))

    server.add_substance_use_log("caffeine", amount=100, unit="mg", timestamp="2026-07-05T08:00:00Z", context="coffee")
    server.add_substance_use_log("caffeine", amount=150, unit="mg", timestamp="2026-07-06T08:00:00Z", context="coffee")
    substance_logs = server.list_substance_use_logs("caffeine")
    substance_trend = server.analyze_substance_trend("caffeine")
    check("substance logs list", substance_logs["count"] == 2, str(substance_logs))
    check("substance trend works",
          substance_trend["logged_days"] == 2 and substance_trend["total_amount"] == 250.0
          and substance_trend["trend"]["direction"] == "rising",
          str(substance_trend))

    source = server.add_wearable_source("Apple Health", source_type="phone", manufacturer="Apple", model="HealthKit")
    wearable_sources = server.list_wearable_sources()
    check("wearable source stored", wearable_sources["count"] == 1 and wearable_sources["wearable_sources"][0]["id"] == source["id"],
          str(wearable_sources))
    sample = server.add_wearable_sample(
        "steps",
        8000,
        start_ts="2026-07-05T00:00:00Z",
        end_ts="2026-07-05T23:59:59Z",
        unit="count",
        source_id=source["id"],
        source_name="Apple Health",
        aggregation="daily_total",
        metadata_json='{"import":"manual"}',
    )
    bulk = server.import_wearable_samples(json.dumps([
        {
            "sample_type": "steps",
            "value": 9000,
            "unit": "count",
            "start_ts": "2026-07-06T00:00:00Z",
            "end_ts": "2026-07-06T23:59:59Z",
            "source_id": source["id"],
            "source_name": "Apple Health",
            "aggregation": "daily_total",
            "metadata": {"import": "batch"},
        },
        {
            "sample_type": "resting_heart_rate",
            "value": 62,
            "unit": "bpm",
            "start_ts": "2026-07-06T06:00:00Z",
            "source_name": "Apple Health",
            "aggregation": "daily_average",
        },
    ]))
    wearable_samples = server.list_wearable_samples("steps", since="2026-07-05", until="2026-07-06")
    wearable_trend = server.analyze_wearable_trend("steps")
    check("wearable sample stored", sample["sample_type"] == "steps" and sample["metadata_json"] == '{"import":"manual"}',
          str(sample))
    check("wearable batch import", bulk["inserted"] == 2, str(bulk))
    check("wearable samples list", wearable_samples["count"] == 2, str(wearable_samples))
    check("wearable trend works", wearable_trend["count"] == 2 and wearable_trend["trend"]["direction"] == "rising",
          str(wearable_trend))

    # --- cross-signal reasoning -------------------------------------------
    for i, (w, g) in enumerate([(80.0, 5.4), (81.0, 5.6), (82.0, 5.9), (83.0, 6.1), (84.5, 6.4)]):
        d = f"2026-0{i + 1}-15"
        server.log_metric("weight_kg", w, "kg", timestamp=f"{d}T08:00:00Z", user="xsig")
        server.add_lab_result("A1C Percent", str(g), unit="%", result_date=d,
                              ref_low=4.0, ref_high=5.6, user="xsig")
    corr = server.correlate_metrics("metric", "weight_kg", "lab", "a1c percent",
                                    resample="month", user="xsig")
    check("correlate pairs monthly buckets", corr["paired_n"] == 5, str(corr))
    check("correlate finds strong positive Pearson",
          corr["pearson"]["r"] > 0.9 and corr["pearson"]["direction"] == "positive", str(corr))
    check("correlate reports a numeric p-value", isinstance(corr["pearson"]["p_value"], float), str(corr))
    check("correlate computes Spearman too", corr["spearman"]["rho"] > 0.9, str(corr))
    check("correlate carries autocorrelation caveat",
          any("autocorrelated" in c for c in corr["caveats"]), str(corr))

    impact = server.analyze_event_impact("weight_kg", "2026-03-20", source="metric",
                                         event_label="regimen change", user="xsig")
    check("event impact splits before/after",
          impact["before"]["count"] == 3 and impact["after"]["count"] == 2, str(impact))
    check("event impact reports a mean increase",
          impact["change"]["direction"] == "increase" and impact["change"]["absolute_change"] > 0,
          str(impact))

    aligned = server.align_series(json.dumps([
        {"source": "metric", "name": "weight_kg"},
        {"source": "lab", "name": "a1c percent", "agg": "last", "label": "a1c"},
    ]), resample="month", join="inner", user="xsig")
    check("align_series inner-joins shared buckets", aligned["returned"] == 5, str(aligned))
    check("align_series exposes both signals",
          all("a1c" in r and "metric:weight_kg" in r for r in aligned["grid"]), str(aligned))

    server.add_lab_result("glucose", "90", unit="mg/dL", result_date="2026-01-10",
                          ref_low=70, ref_high=99, user="xsig")
    server.add_lab_result("glucose", "5.5", unit="mmol/L", result_date="2026-02-10",
                          ref_low=3.9, ref_high=5.5, user="xsig")
    norm = server.normalize_series("glucose", source="lab", to_unit="mg/dL", user="xsig")
    check("normalize collapses to one unit",
          norm["target_unit"] == "mg/dl" and norm["converted_count"] == 2, str(norm))
    mmol_row = [r for r in norm["rows"] if r["original_unit"] == "mmol/L"][0]
    check("normalize mmol/L -> mg/dL via molar mass", abs(mmol_row["value"] - 99.09) < 1.5, str(mmol_row))
    check("normalize computes reference position",
          all("reference_position" in r for r in norm["rows"]), str(norm))

    search = server.search_records("pathology")
    check("search includes new domains", search["documents"] and search["tumors"], str(search))
    search_mri = server.search_records("mri")
    check("search includes imaging", search_mri["imaging_reports"], str(search_mri))
    search_family = server.search_records("diabetes")
    check("search includes family history", search_family["family_history"], str(search_family))
    search_wearable = server.search_records("apple")
    check("search includes wearables", search_wearable["wearable_sources"] and search_wearable["wearable_samples"],
          str(search_wearable))
    search_reproductive = server.search_records("iud")
    check("search includes reproductive records", search_reproductive["reproductive_records"], str(search_reproductive))

    summary = server.summarize_health(since="2026-01-01", until="2026-07-06")
    check("summary includes full domains",
          summary["recent_biomarkers"] and summary["recent_procedures"] and summary["recent_imaging"]
          and summary["recent_documents"] and summary["family_history"]
          and summary["recent_reproductive_records"] and summary["recent_substance_use"]
          and summary["recent_wearable_samples"] and summary["wearable_types"],
          str(summary))

    status = server.health_status()
    check("health_status reports schema version", status["schema"]["current_version"] == server.SCHEMA_VERSION,
          str(status))
    check("health_status reports counts", status["counts"]["metrics"] == 2 and status["counts"]["medications"] == 1
          and status["counts"]["wearable_samples"] == 3,
          str(status))

    gaps = server.care_gap_report()
    check("care_gap_report returns stored counts", gaps["counts"]["lab_results"] == 2, str(gaps))
    check("agenda includes reproductive due dates",
          any(r["id"] == contraception["id"] for r in server.health_agenda(days=1)["reproductive_dates"]),
          str(server.health_agenda(days=1)))

    audit = open(os.environ["HEALTH_MCP_AUDIT_LOG"], encoding="utf-8").read()
    check("audit avoids metric values", "value=180" not in audit and "value=181" not in audit, audit)
    check("audit avoids event/note text", "Headache" not in audit and "100%" not in audit, audit)

print()
print("RESULT:", "ALL PASS" if not failures else f"{len(failures)} FAILURE(S): {failures}")
raise SystemExit(1 if failures else 0)
