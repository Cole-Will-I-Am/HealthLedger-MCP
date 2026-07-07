"""The shared FastMCP application instance every tool registers on."""
from __future__ import annotations

from healthledger.config import *  # noqa: F401,F403
from healthledger.auth import build_auth
from fastmcp import FastMCP


mcp = FastMCP(
    name="health-mcp",
    instructions=(
        "Personal health record for a user. STORE readings and events as they are "
        "reported, RETRIEVE history on request, and use the analysis tools to get "
        "computed, analysis-ready views (statistics and trends) that you can then "
        "interpret for the user.\n\n"
        "Data model:\n"
        "  * metrics — quantitative time series (weight_kg, systolic_bp, diastolic_bp, "
        "heart_rate, glucose_mgdl, sleep_hours, steps, spo2, temperature_c, mood 0-10 …). "
        "Use log_metric; snake_case metric names, include a unit.\n"
        "  * events — discrete happenings: symptoms, medications, meals, activities. "
        "Use log_event with category in {symptom, medication, meal, activity, other}.\n"
        "  * notes — free-form journal entries (log_note).\n"
        "  * profile — durable facts: age, height_cm, blood_type, goals, preferences "
        "(set_profile / get_profile).\n"
        "  * structured history — conditions, allergies, medications, medication logs, "
        "lab reports/results, biomarkers, tumor/cancer records, encounters/annual "
        "physicals, procedures/surgeries, imaging, immunizations, documents, family "
        "history, care tasks, and generic health_records for anything that does not "
        "fit a dedicated table yet.\n"
        "  * genomic_records — discrete genomic and pharmacogenomic findings, carrier "
        "screens, polygenic risk, and WGS summaries. clinical_significance and "
        "pgx_phenotype are lab-reported classifications; store them as given, never "
        "infer or upgrade them.\n"
        "  * reproductive health — cycles, pregnancy, contraception, fertility signs, "
        "due dates, and replacement dates.\n"
        "  * substance-use logs — alcohol, nicotine, caffeine, cannabis, and other "
        "time-varying exposures.\n"
        "  * wearable data — source/device metadata plus high-volume samples such as "
        "steps, HRV, resting heart rate, sleep stages, workouts, calories, temperature, "
        "SpO2, and similar device/app measurements. Use batch import for repeated "
        "samples rather than individual note rows.\n"
        "  * dates and needs — use health_agenda for upcoming tasks/refills/follow-ups/"
        "immunizations, and care_gap_report for missing or stale stored data.\n"
        "  * cross-signal reasoning — the single-series analyze_* tools look at one "
        "signal in isolation; when a question is about a RELATIONSHIP, use the "
        "cross-signal tools instead. correlate_metrics measures association between two "
        "signals (e.g. weight vs A1c, sleep vs resting HR); analyze_event_impact "
        "estimates a before/after change around a discrete event (medication start, "
        "procedure); align_series resamples several signals onto one shared time grid so "
        "you never hand-align timestamps; normalize_series reconciles differing units and "
        "reference ranges so values from different labs/devices are comparable. These "
        "signals may come from metrics, wearable samples, labs, biomarkers, or substance "
        "logs. Association is not causation, and none of it is diagnosis.\n"
        "  * trend intelligence — analyze_trend goes beyond a single fitted line for one "
        "signal: it reports the slope with a confidence interval and p-value (so you can "
        "tell a real trend from noise), frames the latest reading against the user's own "
        "recent median and typical range, flags robust outliers, warns when a straight "
        "line is the wrong model for a bounded or cyclical signal, and detects a regime "
        "change-point. The simpler analyze_*_trend tools now also carry slope uncertainty.\n"
        "  * retrieval and grounding — do not confabulate around missing data. Use "
        "semantic_search to find free-text history by meaning/keywords (ranked full-text, "
        "not exact-key); use data_coverage to check what actually exists, what is absent, "
        "and how stale it is BEFORE asserting anything; and ground every computed claim in "
        "specific rows — the analysis tools return source_ids and the latest value's "
        "recency, and get_record(table, id) fetches the exact cited row.\n\n"
        "Every tool takes an optional `user` label so one server can hold several people "
        "(e.g. family members); it defaults to the configured primary user.\n\n"
        "This is a data store, not a clinician. Summary and trend tools return "
        "descriptive organization/statistics only - never present them as diagnosis. For anything "
        "clinical, advise the user to consult a licensed professional."
    ),
    # OAuth only in remote (http) mode; local stdio runs unauthenticated by design.
    auth=(build_auth() if TRANSPORT == "http" else None),
)
