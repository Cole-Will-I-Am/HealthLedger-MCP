"""Configuration, constants, and startup checks for HealthLedger MCP."""
from __future__ import annotations

import json
import hashlib
import ipaddress
import math
import os
import re
import sqlite3
import statistics
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path


def _int_env(name: str, default: int, *, min_value: int = 1, max_value: int | None = None) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}")
    if value < min_value:
        raise ValueError(f"{name} must be >= {min_value}, got {value}")
    if max_value is not None and value > max_value:
        raise ValueError(f"{name} must be <= {max_value}, got {value}")
    return value


# --------------------------------------------------------------------------- config
PUBLIC_URL = os.environ.get("HEALTH_MCP_PUBLIC_URL", "https://health-mcp.manticthink.com").rstrip("/")
HOST = os.environ.get("HEALTH_MCP_HOST", "127.0.0.1")
PORT = _int_env("HEALTH_MCP_PORT", 8800, min_value=1, max_value=65535)
MCP_PATH = os.environ.get("HEALTH_MCP_PATH", "/mcp")

CLIENT_ID = os.environ.get("HEALTH_MCP_GITHUB_CLIENT_ID", "").strip()
CLIENT_SECRET = os.environ.get("HEALTH_MCP_GITHUB_CLIENT_SECRET", "").strip()
ALLOWED_LOGINS = {
    s.strip().lower()
    for s in os.environ.get("HEALTH_MCP_ALLOWED_LOGINS", "").split(",")
    if s.strip()
}

# Transport: "stdio" (local, the default) or "http" (remote, OAuth-protected). In
# stdio mode any MCP client (Claude Desktop, Cline, Cursor, Zed, Continue, LibreChat,
# custom agents, …) launches this as a trusted subprocess; it never touches the
# network, so it needs no tunnel and no OAuth. Use "http" only to self-host a shared
# remote instance.
TRANSPORT = os.environ.get("HEALTH_MCP_TRANSPORT", "stdio").strip().lower()

# Data lives on the user's own machine. Defaults to ~/.healthledger; override with
# HEALTH_MCP_DB / HEALTH_MCP_AUDIT_LOG (the parent directory is created on startup).
DB_PATH = Path(os.path.expanduser(os.environ.get("HEALTH_MCP_DB", "~/.healthledger/health.db")))
AUDIT_LOG = Path(os.path.expanduser(os.environ.get("HEALTH_MCP_AUDIT_LOG", "~/.healthledger/audit.log")))
DEFAULT_USER = os.environ.get("HEALTH_MCP_DEFAULT_USER", "me").strip() or "me"
MAX_ROWS = _int_env("HEALTH_MCP_MAX_ROWS", 1000, min_value=1, max_value=10000)
MAX_TEXT_CHARS = _int_env("HEALTH_MCP_MAX_TEXT_CHARS", 20000, min_value=1, max_value=200000)
MAX_EXPORT_ROWS = _int_env("HEALTH_MCP_MAX_EXPORT_ROWS", 500, min_value=1, max_value=MAX_ROWS)
MAX_BULK_JSON_CHARS = _int_env("HEALTH_MCP_MAX_BULK_JSON_CHARS", 200000, min_value=1000, max_value=2000000)
MAX_WEARABLE_IMPORT_ROWS = _int_env("HEALTH_MCP_MAX_WEARABLE_IMPORT_ROWS", 500, min_value=1, max_value=5000)
RATE_LIMIT_ENABLED = os.environ.get("HEALTH_MCP_RATE_LIMIT_ENABLED", "1").strip().lower() not in {
    "0", "false", "no", "off"
}
RATE_LIMIT_CALLS = _int_env("HEALTH_MCP_RATE_LIMIT_CALLS", 240, min_value=1, max_value=100000)
RATE_LIMIT_WINDOW_SECONDS = _int_env("HEALTH_MCP_RATE_LIMIT_WINDOW_SECONDS", 60, min_value=1, max_value=3600)
SCHEMA_VERSION = 3

DATE_ONLY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SAFE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_.:/-]{0,79}$")
EVENT_CATEGORIES = {"symptom", "medication", "meal", "activity", "other"}
DATA_TABLES = (
    "metrics",
    "events",
    "notes",
    "conditions",
    "allergies",
    "medications",
    "medication_logs",
    "lab_reports",
    "lab_results",
    "biomarkers",
    "tumors",
    "encounters",
    "procedures",
    "imaging_reports",
    "immunizations",
    "care_tasks",
    "documents",
    "family_history",
    "health_records",
    "reproductive_records",
    "substance_use_logs",
    "wearable_sources",
    "wearable_samples",
)
EXPORT_TABLES = set(DATA_TABLES)
EXPORT_SELECT_SQL = {
    "metrics": "SELECT id, ts, metric, value, unit, note FROM metrics WHERE user=? ORDER BY ts ASC, id ASC LIMIT ? OFFSET ?",
    "events": "SELECT id, ts, category, name, detail, severity FROM events WHERE user=? ORDER BY ts ASC, id ASC LIMIT ? OFFSET ?",
    "notes": "SELECT id, ts, title, body, tags FROM notes WHERE user=? ORDER BY ts ASC, id ASC LIMIT ? OFFSET ?",
    "conditions": "SELECT id, name, status, onset_date, resolved_date, severity, body_site, notes FROM conditions WHERE user=? ORDER BY COALESCE(onset_date, created_ts) ASC, id ASC LIMIT ? OFFSET ?",
    "allergies": "SELECT id, allergen, reaction, severity, status, noted_date, notes FROM allergies WHERE user=? ORDER BY COALESCE(noted_date, created_ts) ASC, id ASC LIMIT ? OFFSET ?",
    "medications": "SELECT id, name, generic_name, dose, route, frequency, schedule, status, start_date, end_date, prescriber, indication, refill_due_date, instructions, notes FROM medications WHERE user=? ORDER BY COALESCE(start_date, created_ts) ASC, id ASC LIMIT ? OFFSET ?",
    "medication_logs": "SELECT id, medication_id, medication_name, scheduled_ts, taken_ts, status, dose_taken, note FROM medication_logs WHERE user=? ORDER BY COALESCE(taken_ts, scheduled_ts, created_ts) ASC, id ASC LIMIT ? OFFSET ?",
    "lab_reports": "SELECT id, report_date, collection_date, lab_name, ordering_provider, title, summary, source, document_id, notes FROM lab_reports WHERE user=? ORDER BY COALESCE(collection_date, report_date, created_ts) ASC, id ASC LIMIT ? OFFSET ?",
    "lab_results": "SELECT id, report_id, result_date, analyte, value_text, numeric_value, unit, ref_low, ref_high, ref_text, flag, specimen, notes FROM lab_results WHERE user=? ORDER BY COALESCE(result_date, created_ts) ASC, id ASC LIMIT ? OFFSET ?",
    "biomarkers": "SELECT id, biomarker, category, measured_date, value_text, numeric_value, unit, ref_low, ref_high, ref_text, flag, source, notes FROM biomarkers WHERE user=? ORDER BY COALESCE(measured_date, created_ts) ASC, id ASC LIMIT ? OFFSET ?",
    "tumors": "SELECT id, cancer_type, tumor_name, body_site, diagnosis_date, status, stage, grade, size_value, size_unit, biomarker_summary, treatment_status, source, notes FROM tumors WHERE user=? ORDER BY COALESCE(diagnosis_date, created_ts) ASC, id ASC LIMIT ? OFFSET ?",
    "encounters": "SELECT id, encounter_date, encounter_type, provider, facility, reason, vitals_summary, assessment, plan, follow_up_date, notes FROM encounters WHERE user=? ORDER BY COALESCE(encounter_date, created_ts) ASC, id ASC LIMIT ? OFFSET ?",
    "procedures": "SELECT id, procedure_date, name, body_site, provider, facility, outcome, follow_up_date, notes FROM procedures WHERE user=? ORDER BY COALESCE(procedure_date, created_ts) ASC, id ASC LIMIT ? OFFSET ?",
    "imaging_reports": "SELECT id, imaging_date, modality, body_site, facility, ordering_provider, findings, impression, follow_up_date, document_id, notes FROM imaging_reports WHERE user=? ORDER BY COALESCE(imaging_date, created_ts) ASC, id ASC LIMIT ? OFFSET ?",
    "immunizations": "SELECT id, vaccine, immunization_date, dose, lot, provider, facility, next_due_date, notes FROM immunizations WHERE user=? ORDER BY COALESCE(immunization_date, created_ts) ASC, id ASC LIMIT ? OFFSET ?",
    "care_tasks": "SELECT id, task_type, title, due_date, status, priority, related_table, related_id, recurrence, completed_ts, notes FROM care_tasks WHERE user=? ORDER BY COALESCE(due_date, created_ts) ASC, id ASC LIMIT ? OFFSET ?",
    "documents": "SELECT id, document_date, document_type, title, source, provider, facility, tags, summary, content_text, source_uri FROM documents WHERE user=? ORDER BY COALESCE(document_date, created_ts) ASC, id ASC LIMIT ? OFFSET ?",
    "family_history": "SELECT id, relation, condition_name, status, age_at_onset, relative_status, age_at_death, cause_of_death, notes FROM family_history WHERE user=? ORDER BY relation ASC, condition_name ASC, id ASC LIMIT ? OFFSET ?",
    "health_records": "SELECT id, record_date, record_type, title, body, source, tags, extra_json FROM health_records WHERE user=? ORDER BY COALESCE(record_date, created_ts) ASC, id ASC LIMIT ? OFFSET ?",
    "reproductive_records": "SELECT id, record_type, start_date, end_date, flow_intensity, pain_level, cervical_mucus, ovulation_predicted_date, gestational_age_weeks, due_date, outcome, method, insertion_date, removal_date, replacement_due_date, source, notes, extra_json FROM reproductive_records WHERE user=? ORDER BY COALESCE(start_date, due_date, replacement_due_date, created_ts) ASC, id ASC LIMIT ? OFFSET ?",
    "substance_use_logs": "SELECT id, timestamp, substance, amount, unit, frequency, route, context, notes FROM substance_use_logs WHERE user=? ORDER BY timestamp ASC, id ASC LIMIT ? OFFSET ?",
    "wearable_sources": "SELECT id, name, source_type, manufacturer, model, external_id, first_seen_ts, last_sync_ts, notes FROM wearable_sources WHERE user=? ORDER BY name ASC, id ASC LIMIT ? OFFSET ?",
    "wearable_samples": "SELECT id, source_id, source_name, sample_type, start_ts, end_ts, value, unit, aggregation, confidence, metadata_json, notes FROM wearable_samples WHERE user=? ORDER BY start_ts ASC, id ASC LIMIT ? OFFSET ?",
}
_RATE_BUCKETS: dict[str, list[float]] = {}


def _is_loopback_host(host: str) -> bool:
    cleaned = host.strip().strip("[]").lower()
    if cleaned == "localhost":
        return True
    try:
        return ipaddress.ip_address(cleaned).is_loopback
    except ValueError:
        return False


def _startup_problems() -> list[str]:
    problems = []
    if TRANSPORT not in {"http", "stdio"}:
        problems.append("HEALTH_MCP_TRANSPORT must be 'http' or 'stdio'.")
    if TRANSPORT == "http" and not _is_loopback_host(HOST):
        problems.append(
            f"HEALTH_MCP_HOST must be loopback-only in http mode; got {HOST!r}. "
            "Use 127.0.0.1 or ::1 behind the Cloudflare Tunnel."
        )
    if (not CLIENT_ID or not CLIENT_SECRET
            or CLIENT_ID.startswith("PASTE_") or CLIENT_SECRET.startswith("PASTE_")):
        problems.append(
            "HEALTH_MCP_GITHUB_CLIENT_ID / HEALTH_MCP_GITHUB_CLIENT_SECRET are not set "
            "(create a GitHub OAuth App — see README.md)."
        )
    if not ALLOWED_LOGINS:
        problems.append(
            "HEALTH_MCP_ALLOWED_LOGINS is empty — refusing to start an unrestricted "
            "health-data server. Set it to your GitHub login(s), comma-separated."
        )
    return problems


def _fail_closed() -> None:
    problems = _startup_problems()
    if problems:
        sys.stderr.write("health-mcp refuses to start (fail-closed):\n")
        for p in problems:
            sys.stderr.write(f"  - {p}\n")
        sys.exit(2)


if TRANSPORT == "http":
    early_problems = _startup_problems()
    if early_problems:
        sys.stderr.write("health-mcp refuses to start (fail-closed):\n")
        for problem in early_problems:
            sys.stderr.write(f"  - {problem}\n")
        sys.exit(2)
