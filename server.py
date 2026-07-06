#!/usr/bin/env python3
"""health-mcp — a remote MCP server that stores a person's health data and returns
analysis-ready views of it, so an LLM (via a claude.ai custom connector) can log,
retrieve, and reason over that record on demand.

Not a medical device. It stores and summarizes what the user records; it does not
diagnose or prescribe. The analysis tools return descriptive statistics and trends
(counts, min/max, mean, median, spread, least-squares slope) — inputs for a human
or model to interpret, not medical conclusions.

Security model (mirrors the sibling vps-mcp server)
---------------------------------------------------
* Transport: Streamable HTTP bound to 127.0.0.1 only. Reached from the internet
  exclusively through a Cloudflare Tunnel (public TLS hostname -> 127.0.0.1:PORT).
  It never binds publicly.
* Auth: OAuth 2.1 via FastMCP's GitHub OAuth proxy. claude.ai runs the flow; the
  user authorizes with GitHub; FastMCP issues/validates the token.
* Authorization: the GitHub token verifier is subclassed to an ALLOW-LIST. Only the
  logins in HEALTH_MCP_ALLOWED_LOGINS may use the connector; any other authenticated
  GitHub account is rejected (verify_token -> None -> 401).
* Fail-closed: the process refuses to start without client id/secret AND at least
  one allow-listed login. There is no "open" mode.
* Data at rest: SQLite at HEALTH_MCP_DB (file mode 0600), journal_mode=WAL.

Health data is sensitive. The GitHub OAuth-app secret and the allow-list are the
only things between the internet and this person's health record.
"""

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

from fastmcp import FastMCP
from fastmcp.server.auth.auth import AccessToken
from fastmcp.server.auth.oauth_proxy import OAuthProxy
from fastmcp.server.auth.providers.github import GitHubTokenVerifier
from mcp.server.auth.middleware.auth_context import get_access_token


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

# Transport: "http" (remote, OAuth-protected — the default) or "stdio" (local, e.g.
# Claude Desktop on a laptop). A stdio server is launched as a trusted subprocess of
# the client and never touches the network, so it needs no tunnel and no OAuth.
TRANSPORT = os.environ.get("HEALTH_MCP_TRANSPORT", "http").strip().lower()

DB_PATH = Path(os.path.expanduser(os.environ.get("HEALTH_MCP_DB", "/srv/health-mcp/health.db")))
AUDIT_LOG = Path(os.path.expanduser(os.environ.get("HEALTH_MCP_AUDIT_LOG", "/srv/health-mcp/audit.log")))
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


def _audit(event: str, detail: str) -> None:
    try:
        ts = datetime.now(timezone.utc).isoformat()
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(AUDIT_LOG, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.chmod(AUDIT_LOG, 0o600)
        except OSError:
            pass
        with os.fdopen(fd, "a", encoding="utf-8") as fh:
            fh.write(f"{ts}\t{event}\t{detail}\n")
    except Exception:
        pass  # auditing must never break a tool call


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _audit_user(user: str) -> str:
    return f"user_hash={_fingerprint(user)}"


def _required_text(value: str | None, field: str, *, max_chars: int = MAX_TEXT_CHARS) -> str:
    if value is None:
        raise ValueError(f"{field} is required")
    text = str(value).strip()
    if not text:
        raise ValueError(f"{field} cannot be empty")
    if len(text) > max_chars:
        raise ValueError(f"{field} is too long; max {max_chars} characters")
    return text


def _optional_text(value: str | None, field: str, *, max_chars: int = MAX_TEXT_CHARS) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > max_chars:
        raise ValueError(f"{field} is too long; max {max_chars} characters")
    return text


def _keyish(value: str, field: str) -> str:
    text = re.sub(r"\s+", "_", _required_text(value, field, max_chars=80).lower())
    if not SAFE_KEY_RE.fullmatch(text):
        raise ValueError(
            f"{field} must start with a letter/number and contain only letters, "
            "numbers, underscore, dot, colon, slash, or hyphen"
        )
    return text


def _user(value: str | None) -> str:
    candidate = str(value).strip() if value is not None else ""
    return _required_text(candidate or DEFAULT_USER, "user", max_chars=80)


def _caller_key(user: str) -> str:
    try:
        token = get_access_token()
    except Exception:
        token = None
    if token is not None:
        claims = getattr(token, "claims", None) or {}
        principal = (
            claims.get("login")
            or getattr(token, "subject", None)
            or getattr(token, "client_id", None)
        )
        if principal:
            return f"auth:{str(principal).lower()}"
    return f"local:{_fingerprint(user)}"


def _rate_limit(tool: str, user: str) -> None:
    if not RATE_LIMIT_ENABLED:
        return
    now = time.monotonic()
    cutoff = now - RATE_LIMIT_WINDOW_SECONDS
    key = _caller_key(user)
    bucket = [stamp for stamp in _RATE_BUCKETS.get(key, []) if stamp >= cutoff]
    if len(bucket) >= RATE_LIMIT_CALLS:
        _RATE_BUCKETS[key] = bucket
        _audit("rate_limited", f"caller_hash={_fingerprint(key)} tool={tool}")
        raise RuntimeError(
            f"rate limit exceeded: {RATE_LIMIT_CALLS} calls per "
            f"{RATE_LIMIT_WINDOW_SECONDS} seconds"
        )
    bucket.append(now)
    _RATE_BUCKETS[key] = bucket


def _tool_user(value: str | None, tool: str) -> str:
    user = _user(value)
    _rate_limit(tool, user)
    return user


def _metric(value: str) -> str:
    return _keyish(value, "metric")


def _profile_key(value: str) -> str:
    return _keyish(value, "key")


def _status(value: str | None, *, default: str = "active", max_chars: int = 40) -> str:
    return _optional_text(value, "status", max_chars=max_chars) or default


def _category(value: str) -> str:
    cat = _required_text(value, "category", max_chars=32).lower()
    if cat not in EVENT_CATEGORIES:
        allowed = "|".join(sorted(EVENT_CATEGORIES))
        raise ValueError(f"category must be one of {allowed}")
    return cat


def _finite_float(value: float, field: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field} must be a finite number")
    return number


def _optional_finite_float(value: float | None, field: str) -> float | None:
    if value is None:
        return None
    return _finite_float(value, field)


def _limit(value: int, *, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(parsed, MAX_ROWS))


def _export_limit(value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = MAX_EXPORT_ROWS
    return max(1, min(parsed, MAX_EXPORT_ROWS))


def _offset(value: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 0
    return max(0, parsed)


def _try_numeric(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return _finite_float(str(value).replace(",", "").strip(), "numeric_value")
    except (TypeError, ValueError):
        return None


def _json_text(value: str | None, field: str = "extra_json") -> str | None:
    text = _optional_text(value, field)
    if text is None:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"{field} must be valid JSON") from e
    return json.dumps(parsed, separators=(",", ":"), sort_keys=True)


def _json_list(value: str, field: str, *, max_chars: int = MAX_BULK_JSON_CHARS) -> list:
    text = _required_text(value, field, max_chars=max_chars)
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"{field} must be valid JSON") from e
    if not isinstance(parsed, list):
        raise ValueError(f"{field} must be a JSON array")
    return parsed


def _like_pattern(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


# --------------------------------------------------------------------------- storage
@contextmanager
def _db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        yield conn
        conn.commit()
    finally:
        conn.close()


def _ensure_columns(conn: sqlite3.Connection, table: str, columns: list[tuple[str, str]]) -> None:
    existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
    for name, ddl in columns:
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _db() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS metrics (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                user    TEXT NOT NULL,
                ts      TEXT NOT NULL,          -- ISO8601 UTC
                metric  TEXT NOT NULL,          -- e.g. weight_kg, systolic_bp, glucose_mgdl, sleep_hours, mood
                value   REAL NOT NULL,
                unit    TEXT,
                note    TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_metrics_user_metric_ts
                ON metrics(user, metric, ts);

            CREATE TABLE IF NOT EXISTS events (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                user      TEXT NOT NULL,
                ts        TEXT NOT NULL,
                category  TEXT NOT NULL,        -- symptom | medication | meal | activity | other
                name      TEXT NOT NULL,        -- e.g. "headache", "ibuprofen", "5k run"
                detail    TEXT,                 -- dose, food detail, distance, etc.
                severity  REAL,                 -- optional 0-10 or numeric magnitude
                note      TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_events_user_cat_ts
                ON events(user, category, ts);

            CREATE TABLE IF NOT EXISTS notes (
                id     INTEGER PRIMARY KEY AUTOINCREMENT,
                user   TEXT NOT NULL,
                ts     TEXT NOT NULL,
                title  TEXT,
                body   TEXT NOT NULL,
                tags   TEXT                     -- comma-separated
            );
            CREATE INDEX IF NOT EXISTS ix_notes_user_ts ON notes(user, ts);

            CREATE TABLE IF NOT EXISTS profile (
                user   TEXT NOT NULL,
                key    TEXT NOT NULL,           -- e.g. age, height_cm, conditions, allergies, medications, blood_type
                value  TEXT NOT NULL,
                PRIMARY KEY (user, key)
            );

            CREATE TABLE IF NOT EXISTS schema_meta (
                key    TEXT PRIMARY KEY,
                value  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS conditions (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user          TEXT NOT NULL,
                created_ts    TEXT NOT NULL,
                name          TEXT NOT NULL,
                status        TEXT NOT NULL,
                onset_date    TEXT,
                resolved_date TEXT,
                severity      TEXT,
                body_site     TEXT,
                notes         TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_conditions_user_status
                ON conditions(user, status, name);

            CREATE TABLE IF NOT EXISTS allergies (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user        TEXT NOT NULL,
                created_ts  TEXT NOT NULL,
                allergen    TEXT NOT NULL,
                reaction    TEXT,
                severity    TEXT,
                status      TEXT NOT NULL,
                noted_date  TEXT,
                notes       TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_allergies_user_status
                ON allergies(user, status, allergen);

            CREATE TABLE IF NOT EXISTS medications (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user            TEXT NOT NULL,
                created_ts      TEXT NOT NULL,
                name            TEXT NOT NULL,
                generic_name    TEXT,
                dose            TEXT,
                route           TEXT,
                frequency       TEXT,
                schedule        TEXT,
                status          TEXT NOT NULL,
                start_date      TEXT,
                end_date        TEXT,
                prescriber      TEXT,
                indication      TEXT,
                refill_due_date TEXT,
                instructions    TEXT,
                notes           TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_medications_user_status
                ON medications(user, status, name);
            CREATE INDEX IF NOT EXISTS ix_medications_user_refill
                ON medications(user, refill_due_date);

            CREATE TABLE IF NOT EXISTS medication_logs (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user            TEXT NOT NULL,
                created_ts      TEXT NOT NULL,
                medication_id   INTEGER,
                medication_name TEXT NOT NULL,
                scheduled_ts    TEXT,
                taken_ts        TEXT,
                status          TEXT NOT NULL,
                dose_taken      TEXT,
                note            TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_medication_logs_user_med_ts
                ON medication_logs(user, medication_name, COALESCE(taken_ts, scheduled_ts, created_ts));

            CREATE TABLE IF NOT EXISTS lab_reports (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                user              TEXT NOT NULL,
                created_ts        TEXT NOT NULL,
                report_date       TEXT,
                collection_date   TEXT,
                lab_name          TEXT,
                ordering_provider TEXT,
                title             TEXT NOT NULL,
                summary           TEXT,
                source            TEXT,
                document_id       INTEGER,
                notes             TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_lab_reports_user_date
                ON lab_reports(user, collection_date, report_date);

            CREATE TABLE IF NOT EXISTS lab_results (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user          TEXT NOT NULL,
                created_ts    TEXT NOT NULL,
                report_id     INTEGER,
                result_date   TEXT,
                analyte       TEXT NOT NULL,
                value_text    TEXT NOT NULL,
                numeric_value REAL,
                unit          TEXT,
                ref_low       REAL,
                ref_high      REAL,
                ref_text      TEXT,
                flag          TEXT,
                specimen      TEXT,
                notes         TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_lab_results_user_analyte_date
                ON lab_results(user, analyte, result_date);

            CREATE TABLE IF NOT EXISTS biomarkers (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user          TEXT NOT NULL,
                created_ts    TEXT NOT NULL,
                biomarker     TEXT NOT NULL,
                category      TEXT,
                measured_date TEXT,
                value_text    TEXT NOT NULL,
                numeric_value REAL,
                unit          TEXT,
                ref_low       REAL,
                ref_high      REAL,
                ref_text      TEXT,
                flag          TEXT,
                source        TEXT,
                notes         TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_biomarkers_user_marker_date
                ON biomarkers(user, biomarker, measured_date);

            CREATE TABLE IF NOT EXISTS tumors (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                user              TEXT NOT NULL,
                created_ts        TEXT NOT NULL,
                cancer_type       TEXT,
                tumor_name        TEXT NOT NULL,
                body_site         TEXT,
                diagnosis_date    TEXT,
                status            TEXT NOT NULL,
                stage             TEXT,
                grade             TEXT,
                size_value        REAL,
                size_unit         TEXT,
                biomarker_summary TEXT,
                treatment_status  TEXT,
                source            TEXT,
                notes             TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_tumors_user_status_date
                ON tumors(user, status, diagnosis_date);

            CREATE TABLE IF NOT EXISTS encounters (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                user            TEXT NOT NULL,
                created_ts      TEXT NOT NULL,
                encounter_date  TEXT,
                encounter_type  TEXT NOT NULL,
                provider        TEXT,
                facility        TEXT,
                reason          TEXT,
                vitals_summary  TEXT,
                assessment      TEXT,
                plan            TEXT,
                follow_up_date  TEXT,
                notes           TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_encounters_user_date_type
                ON encounters(user, encounter_date, encounter_type);

            CREATE TABLE IF NOT EXISTS procedures (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                user           TEXT NOT NULL,
                created_ts     TEXT NOT NULL,
                procedure_date TEXT,
                name           TEXT NOT NULL,
                body_site      TEXT,
                provider       TEXT,
                facility       TEXT,
                outcome        TEXT,
                follow_up_date TEXT,
                notes          TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_procedures_user_date
                ON procedures(user, procedure_date, name);

            CREATE TABLE IF NOT EXISTS imaging_reports (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                user              TEXT NOT NULL,
                created_ts        TEXT NOT NULL,
                imaging_date      TEXT,
                modality          TEXT,
                body_site         TEXT,
                facility          TEXT,
                ordering_provider TEXT,
                findings          TEXT,
                impression        TEXT,
                follow_up_date    TEXT,
                document_id       INTEGER,
                notes             TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_imaging_user_date
                ON imaging_reports(user, imaging_date, modality);

            CREATE TABLE IF NOT EXISTS immunizations (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                user              TEXT NOT NULL,
                created_ts        TEXT NOT NULL,
                vaccine           TEXT NOT NULL,
                immunization_date TEXT,
                dose              TEXT,
                lot               TEXT,
                provider          TEXT,
                facility          TEXT,
                next_due_date     TEXT,
                notes             TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_immunizations_user_due
                ON immunizations(user, next_due_date, vaccine);

            CREATE TABLE IF NOT EXISTS care_tasks (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user          TEXT NOT NULL,
                created_ts    TEXT NOT NULL,
                task_type     TEXT NOT NULL,
                title         TEXT NOT NULL,
                due_date      TEXT,
                status        TEXT NOT NULL,
                priority      TEXT,
                related_table TEXT,
                related_id    INTEGER,
                recurrence    TEXT,
                completed_ts  TEXT,
                notes         TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_care_tasks_user_due_status
                ON care_tasks(user, status, due_date);

            CREATE TABLE IF NOT EXISTS documents (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user          TEXT NOT NULL,
                created_ts    TEXT NOT NULL,
                document_date TEXT,
                document_type TEXT NOT NULL,
                title         TEXT NOT NULL,
                source        TEXT,
                provider      TEXT,
                facility      TEXT,
                tags          TEXT,
                summary       TEXT,
                content_text  TEXT,
                source_uri    TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_documents_user_date_type
                ON documents(user, document_date, document_type);

            CREATE TABLE IF NOT EXISTS family_history (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                user           TEXT NOT NULL,
                created_ts     TEXT NOT NULL,
                relation       TEXT NOT NULL,
                condition_name TEXT NOT NULL,
                status         TEXT,
                age_at_onset   REAL,
                relative_status TEXT,
                age_at_death   REAL,
                cause_of_death TEXT,
                notes          TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_family_history_user_relation
                ON family_history(user, relation, condition_name);

            CREATE TABLE IF NOT EXISTS health_records (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user        TEXT NOT NULL,
                created_ts  TEXT NOT NULL,
                record_date TEXT,
                record_type TEXT NOT NULL,
                title       TEXT NOT NULL,
                body        TEXT,
                source      TEXT,
                tags        TEXT,
                extra_json  TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_health_records_user_type_date
                ON health_records(user, record_type, record_date);

            CREATE TABLE IF NOT EXISTS reproductive_records (
                id                       INTEGER PRIMARY KEY AUTOINCREMENT,
                user                     TEXT NOT NULL,
                created_ts               TEXT NOT NULL,
                record_type              TEXT NOT NULL,
                start_date               TEXT,
                end_date                 TEXT,
                flow_intensity           TEXT,
                pain_level               REAL,
                cervical_mucus           TEXT,
                ovulation_predicted_date TEXT,
                gestational_age_weeks    REAL,
                due_date                 TEXT,
                outcome                  TEXT,
                method                   TEXT,
                insertion_date           TEXT,
                removal_date             TEXT,
                replacement_due_date     TEXT,
                source                   TEXT,
                notes                    TEXT,
                extra_json               TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_reproductive_user_type_date
                ON reproductive_records(user, record_type, start_date);
            CREATE INDEX IF NOT EXISTS ix_reproductive_user_due
                ON reproductive_records(user, due_date, replacement_due_date);

            CREATE TABLE IF NOT EXISTS substance_use_logs (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                user        TEXT NOT NULL,
                created_ts  TEXT NOT NULL,
                timestamp   TEXT NOT NULL,
                substance   TEXT NOT NULL,
                amount      REAL,
                unit        TEXT,
                frequency   TEXT,
                route       TEXT,
                context     TEXT,
                notes       TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_substance_user_substance_ts
                ON substance_use_logs(user, substance, timestamp);

            CREATE TABLE IF NOT EXISTS wearable_sources (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user          TEXT NOT NULL,
                created_ts    TEXT NOT NULL,
                name          TEXT NOT NULL,
                source_type   TEXT,
                manufacturer  TEXT,
                model         TEXT,
                external_id   TEXT,
                first_seen_ts TEXT,
                last_sync_ts  TEXT,
                notes         TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_wearable_sources_user_name
                ON wearable_sources(user, name);

            CREATE TABLE IF NOT EXISTS wearable_samples (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user          TEXT NOT NULL,
                created_ts    TEXT NOT NULL,
                source_id     INTEGER,
                source_name   TEXT,
                sample_type   TEXT NOT NULL,
                start_ts      TEXT NOT NULL,
                end_ts        TEXT,
                value         REAL NOT NULL,
                unit          TEXT,
                aggregation   TEXT,
                confidence    REAL,
                metadata_json TEXT,
                notes         TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_wearable_samples_user_type_start
                ON wearable_samples(user, sample_type, start_ts);
            CREATE INDEX IF NOT EXISTS ix_wearable_samples_user_source_start
                ON wearable_samples(user, source_id, start_ts);
            """
        )
        _ensure_columns(conn, "family_history", [
            ("relative_status", "relative_status TEXT"),
            ("age_at_death", "age_at_death REAL"),
            ("cause_of_death", "cause_of_death TEXT"),
        ])
        conn.execute(
            "INSERT OR IGNORE INTO schema_meta(key, value) VALUES (?, ?)",
            ("created_at", _now_iso()),
        )
        conn.execute(
            "INSERT INTO schema_meta(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            ("schema_version", str(SCHEMA_VERSION)),
        )
    try:
        os.chmod(DB_PATH, 0o600)
    except OSError:
        pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _parse_ts(value: str | None, *, end_of_day: bool = False) -> str:
    """Normalise a timestamp to ISO8601 UTC. Accepts None/'now', full ISO strings,
    or 'YYYY-MM-DD'. Naive inputs are assumed UTC."""
    if value is None or str(value).strip().lower() in ("", "now"):
        return _now_iso()
    raw = str(value).strip()
    try:
        if DATE_ONLY_RE.fullmatch(raw):
            dt = datetime.strptime(raw, "%Y-%m-%d")
            if end_of_day:
                dt = dt.replace(hour=23, minute=59, second=59)
        else:
            dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(
            f"unrecognised timestamp {value!r}; use ISO8601 (2026-07-06T08:30) "
            f"or 'YYYY-MM-DD' or 'now'"
        )
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _range_bounds(since: str | None, until: str | None) -> tuple[str, str]:
    lo = _parse_ts(since) if since else "0000-01-01T00:00:00+00:00"
    hi = _parse_ts(until, end_of_day=True) if until else "9999-12-31T23:59:59+00:00"
    return lo, hi


def _parse_date(value: str | None, field: str, *, default_today: bool = False) -> str | None:
    if value is None or str(value).strip() == "":
        return datetime.now(timezone.utc).date().isoformat() if default_today else None
    return _parse_ts(str(value)).split("T", 1)[0]


def _date_or_now(value: str | None, field: str) -> str:
    parsed = _parse_date(value, field, default_today=True)
    if parsed is None:
        raise ValueError(f"{field} is required")
    return parsed


def _rows(cur) -> list[dict]:
    return [dict(r) for r in cur.fetchall()]


def _schema_meta(conn: sqlite3.Connection) -> dict:
    rows = conn.execute("SELECT key, value FROM schema_meta ORDER BY key").fetchall()
    return {r["key"]: r["value"] for r in rows}


def _table_counts(conn: sqlite3.Connection, user: str | None = None) -> dict:
    counts = {}
    for table in DATA_TABLES:
        if user is None:
            counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        else:
            counts[table] = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE user=?", (user,)).fetchone()[0]
    if user is None:
        counts["profile"] = conn.execute("SELECT COUNT(*) FROM profile").fetchone()[0]
    else:
        counts["profile"] = conn.execute("SELECT COUNT(*) FROM profile WHERE user=?", (user,)).fetchone()[0]
    return counts


def _export_page(conn: sqlite3.Connection, user: str, table: str, limit: int, offset: int) -> dict:
    if table not in EXPORT_TABLES:
        allowed = "|".join(sorted(EXPORT_TABLES))
        raise ValueError(f"table must be one of {allowed}")
    total = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE user=?", (user,)).fetchone()[0]
    rows = _rows(conn.execute(EXPORT_SELECT_SQL[table], (user, limit, offset)))
    next_offset = offset + len(rows) if offset + len(rows) < total else None
    return {
        "table": table,
        "limit": limit,
        "offset": offset,
        "next_offset": next_offset,
        "total": total,
        "count": len(rows),
        "rows": rows,
    }


def _ols(xs: list[float], ys: list[float]) -> dict | None:
    """Ordinary least squares of ys on xs, with slope uncertainty.

    Returns slope, intercept, R^2 and — when n>2 — the slope's standard error,
    two-sided p-value (H0: slope=0) and 95% confidence interval, so a real trend
    can be told apart from noise. None if the regressor has no spread. (Depends
    on _t_two_sided_p / _t_crit, resolved at call time.)"""
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return None
    syy = sum((y - my) ** 2 for y in ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    slope = sxy / sxx
    intercept = my - slope * mx
    sse = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
    fit = {
        "slope": slope,
        "intercept": intercept,
        "r_squared": (1.0 - sse / syy) if syy > 0 else None,
        "sse": sse,
        "n": n,
    }
    if n > 2:
        s2 = sse / (n - 2)
        se = math.sqrt(s2 / sxx) if s2 > 0 else 0.0
        fit["slope_stderr"] = se
        if se > 0:
            t = slope / se
            fit["slope_t"] = t
            fit["slope_p_value"] = _t_two_sided_p(t, n - 2)
            tc = _t_crit(n - 2)
            if tc is not None:
                fit["slope_ci95"] = [slope - tc * se, slope + tc * se]
        else:
            fit["slope_p_value"] = 0.0 if slope != 0 else None
            fit["slope_ci95"] = [slope, slope]
    return fit


def _t_crit(df: float, alpha: float = 0.05) -> float | None:
    """Two-sided critical t value: the |t| whose two-sided p equals alpha.
    Found by bisection on _t_two_sided_p (monotone-decreasing in |t|)."""
    if df <= 0:
        return None
    lo, hi = 0.0, 1000.0
    for _ in range(64):
        mid = (lo + hi) / 2.0
        if (_t_two_sided_p(mid, df) or 0.0) > alpha:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def _quartiles(values: list[float]) -> list[float] | None:
    """[Q1, Q3] via linear interpolation between order statistics."""
    s = sorted(values)
    n = len(s)
    if n < 2:
        return None

    def _pct(p: float) -> float:
        idx = p * (n - 1)
        lo = int(idx)
        frac = idx - lo
        return s[lo] if lo + 1 >= n else s[lo] * (1 - frac) + s[lo + 1] * frac

    return [_pct(0.25), _pct(0.75)]


def _linreg_per_day(points: list[tuple[str, float]]) -> dict | None:
    """Least-squares slope of value vs. time (in days since the first sample),
    now carrying slope uncertainty so 'trending up' can be told from noise."""
    if len(points) < 2:
        return None
    t0 = datetime.fromisoformat(points[0][0])
    xs = [(datetime.fromisoformat(ts) - t0).total_seconds() / 86400.0 for ts, _ in points]
    ys = [v for _, v in points]
    fit = _ols(xs, ys)
    if fit is None:
        return None
    slope = fit["slope"]
    intercept = fit["intercept"]
    span_days = xs[-1] - xs[0]
    result = {
        "slope_per_day": round(slope, 6),
        "change_over_span": round(slope * span_days, 4),
        "span_days": round(span_days, 3),
        "direction": "rising" if slope > 0 else ("falling" if slope < 0 else "flat"),
        "projected_next_day": round(intercept + slope * (xs[-1] + 1), 4),
        "r_squared": round(fit["r_squared"], 4) if fit["r_squared"] is not None else None,
    }
    if "slope_stderr" in fit:
        result["slope_stderr"] = round(fit["slope_stderr"], 6)
        ci = fit.get("slope_ci95")
        if ci is not None:
            result["slope_ci95_per_day"] = [round(ci[0], 6), round(ci[1], 6)]
        p = fit.get("slope_p_value")
        if p is not None:
            result["slope_p_value"] = p
            result["significant"] = p < 0.05
        if ci is not None:
            result["confidence"] = (
                "slope is distinguishable from flat (95% CI excludes zero)"
                if (ci[0] > 0 or ci[1] < 0) else
                "slope is NOT distinguishable from flat (95% CI includes zero) — treat as noise"
            )
    return result


# --------------------------------------------------------------------------- auth
class AllowlistGitHubTokenVerifier(GitHubTokenVerifier):
    """GitHub token verifier restricted to an allow-list of logins (defence in depth):
    GitHub validates the token, then the resolved `login` must be in ALLOWED_LOGINS."""

    def __init__(self, *, allowed_logins: set[str], **kwargs):
        super().__init__(**kwargs)
        self._allowed_logins = {l.lower() for l in allowed_logins}

    async def verify_token(self, token: str) -> AccessToken | None:
        result = await super().verify_token(token)
        if result is None:
            return None
        login = (result.claims or {}).get("login")
        if not login or login.lower() not in self._allowed_logins:
            _audit("auth.denied", f"login={login!r} not in allow-list")
            return None
        return result


def build_auth() -> OAuthProxy:
    verifier = AllowlistGitHubTokenVerifier(
        allowed_logins=ALLOWED_LOGINS,
        required_scopes=["user"],
        cache_ttl_seconds=300,
    )
    return OAuthProxy(
        upstream_authorization_endpoint="https://github.com/login/oauth/authorize",
        upstream_token_endpoint="https://github.com/login/oauth/access_token",
        upstream_client_id=CLIENT_ID,
        upstream_client_secret=CLIENT_SECRET,
        token_verifier=verifier,
        base_url=PUBLIC_URL,
        redirect_path="/auth/callback",
        issuer_url=PUBLIC_URL,
        require_authorization_consent=True,
    )


# --------------------------------------------------------------------------- server
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
        "change-point. The simpler analyze_*_trend tools now also carry slope uncertainty.\n\n"
        "Every tool takes an optional `user` label so one server can hold several people "
        "(e.g. family members); it defaults to the configured primary user.\n\n"
        "This is a data store, not a clinician. Summary and trend tools return "
        "descriptive organization/statistics only - never present them as diagnosis. For anything "
        "clinical, advise the user to consult a licensed professional."
    ),
    # OAuth only in remote (http) mode; local stdio runs unauthenticated by design.
    auth=(build_auth() if TRANSPORT == "http" else None),
)


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


@mcp.tool(annotations={"title": "Analyze lab trend", "readOnlyHint": True, "idempotentHint": True})
def analyze_lab_trend(analyte: str, since: str | None = None, until: str | None = None, user: str | None = None) -> dict:
    """Return descriptive trend stats for numeric lab results for one analyte."""
    u = _tool_user(user, "analyze_lab_trend")
    lo, hi = _range_bounds(since, until)
    clean_analyte = _keyish(analyte, "analyte")
    _audit("analyze_lab_trend", f"{_audit_user(u)} analyte_hash={_fingerprint(clean_analyte)}")
    with _db() as conn:
        rows = _rows(conn.execute(
            "SELECT result_date, numeric_value, unit, ref_low, ref_high, flag FROM lab_results "
            "WHERE user=? AND analyte=? AND numeric_value IS NOT NULL "
            "AND substr(COALESCE(result_date, created_ts), 1, 10) BETWEEN ? AND ? "
            "ORDER BY COALESCE(result_date, created_ts) ASC",
            (u, clean_analyte, lo.split("T", 1)[0], hi.split("T", 1)[0]),
        ))
    if not rows:
        return {"user": u, "analyte": clean_analyte, "count": 0}
    values = [r["numeric_value"] for r in rows]
    points = [(r["result_date"] + "T00:00:00+00:00", r["numeric_value"]) for r in rows if r["result_date"]]
    return {
        "user": u,
        "analyte": clean_analyte,
        "count": len(rows),
        "unit": rows[-1]["unit"],
        "latest": rows[-1],
        "min": min(values),
        "max": max(values),
        "mean": round(statistics.fmean(values), 4),
        "median": round(statistics.median(values), 4),
        "trend": _linreg_per_day(points) if len(points) >= 2 else None,
        "disclaimer": "Descriptive lab trend only; not diagnosis or medical advice.",
    }


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


@mcp.tool(annotations={"title": "Analyze biomarker trend", "readOnlyHint": True, "idempotentHint": True})
def analyze_biomarker_trend(biomarker: str, since: str | None = None, until: str | None = None, user: str | None = None) -> dict:
    """Return descriptive trend stats for numeric biomarker observations."""
    u = _tool_user(user, "analyze_biomarker_trend")
    lo, hi = _range_bounds(since, until)
    clean_marker = _keyish(biomarker, "biomarker")
    _audit("analyze_biomarker_trend", f"{_audit_user(u)} biomarker_hash={_fingerprint(clean_marker)}")
    with _db() as conn:
        rows = _rows(conn.execute(
            "SELECT measured_date, numeric_value, unit, ref_low, ref_high, flag FROM biomarkers "
            "WHERE user=? AND biomarker=? AND numeric_value IS NOT NULL "
            "AND substr(COALESCE(measured_date, created_ts), 1, 10) BETWEEN ? AND ? "
            "ORDER BY COALESCE(measured_date, created_ts) ASC",
            (u, clean_marker, lo.split("T", 1)[0], hi.split("T", 1)[0]),
        ))
    if not rows:
        return {"user": u, "biomarker": clean_marker, "count": 0}
    values = [r["numeric_value"] for r in rows]
    points = [(r["measured_date"] + "T00:00:00+00:00", r["numeric_value"]) for r in rows if r["measured_date"]]
    return {
        "user": u,
        "biomarker": clean_marker,
        "count": len(rows),
        "unit": rows[-1]["unit"],
        "latest": rows[-1],
        "min": min(values),
        "max": max(values),
        "mean": round(statistics.fmean(values), 4),
        "median": round(statistics.median(values), 4),
        "trend": _linreg_per_day(points) if len(points) >= 2 else None,
        "disclaimer": "Descriptive biomarker trend only; not diagnosis or medical advice.",
    }


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


@mcp.tool(annotations={"title": "Analyze reproductive trend", "readOnlyHint": True, "idempotentHint": True})
def analyze_reproductive_trend(user: str | None = None, limit: int = 24) -> dict:
    """Return descriptive cycle length/duration stats from stored cycle records only."""
    u = _tool_user(user, "analyze_reproductive_trend")
    lim = _limit(limit, default=24)
    _audit("analyze_reproductive_trend", f"{_audit_user(u)} limit={lim}")
    with _db() as conn:
        rows = _rows(conn.execute(
            "SELECT start_date, end_date, flow_intensity, pain_level FROM reproductive_records "
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
    return {
        "user": u,
        "cycle_records": len(rows),
        "cycle_length_days": {
            "count": len(cycle_lengths),
            "min": min(cycle_lengths) if cycle_lengths else None,
            "max": max(cycle_lengths) if cycle_lengths else None,
            "mean": round(statistics.fmean(cycle_lengths), 2) if cycle_lengths else None,
            "median": round(statistics.median(cycle_lengths), 2) if cycle_lengths else None,
        },
        "period_length_days": {
            "count": len(period_lengths),
            "min": min(period_lengths) if period_lengths else None,
            "max": max(period_lengths) if period_lengths else None,
            "mean": round(statistics.fmean(period_lengths), 2) if period_lengths else None,
            "median": round(statistics.median(period_lengths), 2) if period_lengths else None,
        },
        "upcoming_reproductive_dates": upcoming,
        "disclaimer": "Descriptive reproductive tracking only; not fertility, contraception, or medical advice.",
    }


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


@mcp.tool(annotations={"title": "Analyze substance trend", "readOnlyHint": True, "idempotentHint": True})
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
            "SELECT timestamp, amount, unit FROM substance_use_logs "
            "WHERE user=? AND substance=? AND amount IS NOT NULL AND timestamp BETWEEN ? AND ? "
            "ORDER BY timestamp ASC",
            (u, clean_substance, lo, hi),
        ))
    if not rows:
        return {"user": u, "substance": clean_substance, "count": 0}
    daily: dict[str, float] = {}
    for r in rows:
        day = r["timestamp"].split("T", 1)[0]
        daily[day] = daily.get(day, 0.0) + float(r["amount"])
    points = [(f"{day}T00:00:00+00:00", value) for day, value in sorted(daily.items())]
    values = list(daily.values())
    return {
        "user": u,
        "substance": clean_substance,
        "count": len(rows),
        "logged_days": len(daily),
        "unit": rows[-1]["unit"],
        "total_amount": round(sum(values), 4),
        "mean_per_logged_day": round(statistics.fmean(values), 4),
        "median_per_logged_day": round(statistics.median(values), 4),
        "daily_totals": [{"date": day, "total": total} for day, total in sorted(daily.items())][-30:],
        "trend": _linreg_per_day(points) if len(points) >= 2 else None,
        "disclaimer": "Descriptive substance-use tracking only; not treatment or medical advice.",
    }


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


@mcp.tool(annotations={"title": "Analyze wearable trend", "readOnlyHint": True, "idempotentHint": True})
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
        "SELECT start_ts, value, unit, aggregation, source_name FROM wearable_samples "
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
        return {"user": u, "sample_type": clean_type, "count": 0}
    values = [float(r["value"]) for r in rows]
    points = [(r["start_ts"], float(r["value"])) for r in rows]
    return {
        "user": u,
        "sample_type": clean_type,
        "count": len(rows),
        "unit": rows[-1]["unit"],
        "latest": rows[-1],
        "min": min(values),
        "max": max(values),
        "mean": round(statistics.fmean(values), 4),
        "median": round(statistics.median(values), 4),
        "trend": _linreg_per_day(points) if len(points) >= 2 else None,
        "disclaimer": "Descriptive wearable-data trend only; not diagnosis or medical advice.",
    }


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


# --------------------------------------------------------------- cross-signal
# Health insight lives in RELATIONSHIPS. Every analyze_* tool above looks at one
# series in isolation; the tools below align two or more signals — drawn from
# metrics, wearable samples, labs, biomarkers, or substance logs — onto a common
# footing so a model can compare them without hand-aligning timestamps or units.
# They stay strictly descriptive: correlation is not causation, and none of this
# is diagnosis.

# source key -> how to pull a dated numeric series from its table.
_SERIES_SOURCES = {
    "metric": {
        "table": "metrics", "name_col": "metric", "ts_col": "ts",
        "value_col": "value", "unit_col": "unit",
        "date_mode": "full", "coalesce_created": False, "has_ref": False,
    },
    "wearable": {
        "table": "wearable_samples", "name_col": "sample_type", "ts_col": "start_ts",
        "value_col": "value", "unit_col": "unit",
        "date_mode": "full", "coalesce_created": False, "has_ref": False,
    },
    "lab": {
        "table": "lab_results", "name_col": "analyte", "ts_col": "result_date",
        "value_col": "numeric_value", "unit_col": "unit",
        "date_mode": "date", "coalesce_created": True, "has_ref": True,
    },
    "biomarker": {
        "table": "biomarkers", "name_col": "biomarker", "ts_col": "measured_date",
        "value_col": "numeric_value", "unit_col": "unit",
        "date_mode": "date", "coalesce_created": True, "has_ref": True,
    },
    "substance": {
        "table": "substance_use_logs", "name_col": "substance", "ts_col": "timestamp",
        "value_col": "amount", "unit_col": "unit",
        "date_mode": "full", "coalesce_created": False, "has_ref": False,
    },
}

_RESAMPLE_BUCKETS = ("day", "week", "month")
_AGG_FUNCS = ("mean", "median", "sum", "min", "max", "first", "last", "count")


def _series_source(source: str) -> tuple[str, dict]:
    key = _required_text(source, "source", max_chars=40).strip().lower()
    if key not in _SERIES_SOURCES:
        allowed = "|".join(sorted(_SERIES_SOURCES))
        raise ValueError(f"source must be one of {allowed}")
    return key, _SERIES_SOURCES[key]


def _to_full_ts(ts: str) -> str:
    """Promote a date-only stamp to an ISO datetime; leave datetimes untouched."""
    if ts and "T" not in ts and len(ts) >= 10:
        return ts[:10] + "T00:00:00+00:00"
    return ts


def _resolve_series(conn, user: str, source: str | None, name: str | None,
                    lo: str, hi: str) -> tuple[str, str, dict, list[dict]]:
    """Pull one dated numeric series from any supported source, ascending by time.

    Returns (source_key, normalized_name, source_config, items) where each item is
    {ts, value, unit[, ref_low, ref_high]} and non-numeric rows are dropped."""
    key, cfg = _series_source(source)
    clean = _keyish(_required_text(name, "name"), "name")
    ts_expr = f"COALESCE({cfg['ts_col']}, created_ts)" if cfg["coalesce_created"] else cfg["ts_col"]
    cols = [f"{ts_expr} AS ts", f"{cfg['value_col']} AS value", f"{cfg['unit_col']} AS unit"]
    if cfg["has_ref"]:
        cols += ["ref_low AS ref_low", "ref_high AS ref_high"]
    sql = (
        f"SELECT {', '.join(cols)} FROM {cfg['table']} "
        f"WHERE user=? AND {cfg['name_col']}=? AND {cfg['value_col']} IS NOT NULL"
    )
    args: list = [user, clean]
    if cfg["date_mode"] == "date":
        sql += f" AND substr({ts_expr}, 1, 10) BETWEEN ? AND ?"
        args += [lo.split("T", 1)[0], hi.split("T", 1)[0]]
    else:
        sql += f" AND {cfg['ts_col']} BETWEEN ? AND ?"
        args += [lo, hi]
    sql += f" ORDER BY {ts_expr} ASC"
    items: list[dict] = []
    for r in _rows(conn.execute(sql, args)):
        ts = r.get("ts")
        if ts is None:
            continue
        item = {"ts": _to_full_ts(ts), "value": float(r["value"]), "unit": r.get("unit")}
        if cfg["has_ref"]:
            item["ref_low"] = r.get("ref_low")
            item["ref_high"] = r.get("ref_high")
        items.append(item)
    return key, clean, cfg, items


def _bucket_key(ts: str, bucket: str) -> str:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if bucket == "day":
        return dt.date().isoformat()
    if bucket == "week":
        return dt.strftime("%G-W%V")
    if bucket == "month":
        return dt.strftime("%Y-%m")
    raise ValueError(f"resample must be one of {'|'.join(_RESAMPLE_BUCKETS)}")


def _agg_values(values: list[float], agg: str) -> float:
    if agg == "mean":
        return statistics.fmean(values)
    if agg == "median":
        return statistics.median(values)
    if agg == "sum":
        return float(sum(values))
    if agg == "min":
        return min(values)
    if agg == "max":
        return max(values)
    if agg == "first":
        return values[0]
    if agg == "last":
        return values[-1]
    if agg == "count":
        return float(len(values))
    raise ValueError(f"agg must be one of {'|'.join(_AGG_FUNCS)}")


def _resample_series(items: list[dict], bucket: str, agg: str) -> dict[str, float]:
    """Collapse resolved items to one value per time bucket (items are time-ordered)."""
    grouped: dict[str, list[float]] = {}
    for it in items:
        grouped.setdefault(_bucket_key(it["ts"], bucket), []).append(it["value"])
    return {k: round(_agg_values(v, agg), 6) for k, v in grouped.items()}


def _rankdata(values: list[float]) -> list[float]:
    """Average ranks (ties share the mean rank), 1-based — for Spearman."""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg_rank = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg_rank
        i = j + 1
    return ranks


def _pearson_r(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 2:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    syy = sum((y - my) ** 2 for y in ys)
    if sxx == 0 or syy == 0:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    return sxy / math.sqrt(sxx * syy)


def _betacf(a: float, b: float, x: float) -> float:
    """Continued fraction for the incomplete beta function (Numerical Recipes)."""
    fpmin = 1e-300
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < fpmin:
        d = fpmin
    d = 1.0 / d
    h = d
    for m in range(1, 300):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < fpmin:
            d = fpmin
        c = 1.0 + aa / c
        if abs(c) < fpmin:
            c = fpmin
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < 3e-16:
            break
    return h


def _betai(a: float, b: float, x: float) -> float:
    """Regularized incomplete beta I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(lbeta + a * math.log(x) + b * math.log(1.0 - x))
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def _t_two_sided_p(t: float, df: float) -> float | None:
    """Two-sided p-value for a Student-t statistic with df degrees of freedom."""
    if df <= 0:
        return None
    if not math.isfinite(t):
        return 0.0
    return round(_betai(df / 2.0, 0.5, df / (df + t * t)), 6)


def _corr_p_value(r: float, n: int) -> float | None:
    df = n - 2
    if df <= 0:
        return None
    if abs(r) >= 1.0:
        return 0.0
    t = r * math.sqrt(df / (1.0 - r * r))
    return _t_two_sided_p(t, df)


def _corr_strength(r: float) -> str:
    a = abs(r)
    if a < 0.1:
        return "negligible"
    if a < 0.3:
        return "weak"
    if a < 0.5:
        return "moderate"
    if a < 0.7:
        return "strong"
    return "very strong"


def _group_stats(items: list[dict]) -> dict:
    values = [it["value"] for it in items]
    n = len(values)
    if n == 0:
        return {"count": 0}
    stats = {
        "count": n,
        "first_ts": items[0]["ts"],
        "last_ts": items[-1]["ts"],
        "min": round(min(values), 4),
        "max": round(max(values), 4),
        "mean": round(statistics.fmean(values), 4),
        "median": round(statistics.median(values), 4),
        "stdev": round(statistics.stdev(values), 4) if n > 1 else 0.0,
        "trend": _linreg_per_day([(it["ts"], it["value"]) for it in items]) if n >= 2 else None,
    }
    return stats


def _welch_t(a_vals: list[float], b_vals: list[float]) -> dict | None:
    """Welch's unequal-variance t-test for a difference in means."""
    na, nb = len(a_vals), len(b_vals)
    if na < 2 or nb < 2:
        return None
    va, vb = statistics.variance(a_vals), statistics.variance(b_vals)
    se2 = va / na + vb / nb
    if se2 <= 0:
        return None
    t = (statistics.fmean(b_vals) - statistics.fmean(a_vals)) / math.sqrt(se2)
    df_den = (va / na) ** 2 / (na - 1) + (vb / nb) ** 2 / (nb - 1)
    df = (se2 ** 2) / df_den if df_den > 0 else float(na + nb - 2)
    return {"t": round(t, 4), "df": round(df, 2), "p_value": _t_two_sided_p(t, df)}


# --- unit + reference normalization -----------------------------------------
_UNIT_ALIASES = {
    "mgdl": "mg/dl", "mg/dl": "mg/dl", "mg/dL": "mg/dl",
    "gdl": "g/dl", "g/dl": "g/dl", "gl": "g/l", "g/l": "g/l",
    "mgl": "mg/l", "mg/l": "mg/l",
    "ug/ml": "ug/ml", "mcg/ml": "ug/ml", "ug/dl": "ug/dl", "mcg/dl": "ug/dl",
    "ug/l": "ug/l", "mcg/l": "ug/l", "ng/ml": "ng/ml", "ng/dl": "ng/dl", "pg/ml": "pg/ml",
    "mmol/l": "mmol/l", "mmoll": "mmol/l", "umol/l": "umol/l", "mcmol/l": "umol/l",
    "nmol/l": "nmol/l", "pmol/l": "pmol/l", "mol/l": "mol/l",
    "percent": "%", "%": "%", "pct": "%",
    "kg": "kg", "kgs": "kg", "g": "g", "gram": "g", "grams": "g",
    "mg": "mg", "ug": "ug", "mcg": "ug", "lb": "lb", "lbs": "lb", "pound": "lb", "pounds": "lb",
    "oz": "oz", "ounce": "oz", "ounces": "oz",
    "cm": "cm", "m": "m", "mm": "mm", "in": "in", "inch": "in", "inches": "in", "ft": "ft",
    "l": "l", "liter": "l", "litre": "l", "dl": "dl", "ml": "ml",
    "c": "c", "celsius": "c", "centigrade": "c", "f": "f", "fahrenheit": "f", "k": "k", "kelvin": "k",
    "bpm": "bpm", "mmhg": "mmhg", "count": "count", "steps": "count",
}

# linear-conversion families: unit -> multiplier to the family's base unit.
_DIMENSIONS = {
    "mass": {"kg": 1000.0, "g": 1.0, "mg": 1e-3, "ug": 1e-6, "lb": 453.59237, "oz": 28.349523125},
    "length": {"m": 1.0, "cm": 0.01, "mm": 1e-3, "in": 0.0254, "ft": 0.3048},
    "volume": {"l": 1.0, "dl": 0.1, "ml": 1e-3},
    # mass concentration, base g/L
    "mass_conc": {"g/l": 1.0, "g/dl": 10.0, "mg/dl": 0.01, "mg/l": 1e-3,
                  "ug/ml": 1e-3, "ug/dl": 1e-5, "ug/l": 1e-6,
                  "ng/ml": 1e-6, "ng/dl": 1e-8, "pg/ml": 1e-9},
    # molar concentration, base mol/L
    "molar_conc": {"mol/l": 1.0, "mmol/l": 1e-3, "umol/l": 1e-6, "nmol/l": 1e-9, "pmol/l": 1e-12},
}
_MASS_CONC = _DIMENSIONS["mass_conc"]
_MOLAR_CONC = _DIMENSIONS["molar_conc"]

# molar masses (g/mol) that enable mass<->molar concentration bridging per analyte.
_ANALYTE_MW = {
    "glucose": 180.16,
    "cholesterol": 386.65, "total_cholesterol": 386.65,
    "hdl": 386.65, "hdl_cholesterol": 386.65, "ldl": 386.65, "ldl_cholesterol": 386.65,
    "triglycerides": 885.4, "triglyceride": 885.4,
    "creatinine": 113.12, "uric_acid": 168.11, "calcium": 40.08,
    "urea": 60.06, "bilirubin": 584.66, "total_bilirubin": 584.66,
    "testosterone": 288.42, "cortisol": 362.46,
}
_TEMP_UNITS = ("c", "f", "k")


def _norm_unit(unit: str | None) -> str | None:
    if unit is None:
        return None
    u = str(unit).strip().lower().replace("µ", "u").replace("μ", "u").replace("°", "")
    u = u.replace(" ", "")
    if not u:
        return None
    return _UNIT_ALIASES.get(u, u)


def _temp_convert(value: float, frm: str, to: str) -> float | None:
    if frm == "c":
        c = value
    elif frm == "f":
        c = (value - 32.0) * 5.0 / 9.0
    elif frm == "k":
        c = value - 273.15
    else:
        return None
    return {"c": c, "f": c * 9.0 / 5.0 + 32.0, "k": c + 273.15}.get(to)


def _convert_value(value: float | None, from_unit: str | None, to_unit: str | None,
                   analyte: str | None = None) -> tuple[float | None, bool, str]:
    """Convert a value between units. Returns (converted, ok, method_tag).

    Handles same-unit identity, temperature, linear families (mass, length,
    volume, mass/molar concentration), and analyte-aware mass<->molar bridging.
    Never guesses: an unknown pairing returns (None, False, reason)."""
    if value is None:
        return None, False, "no_value"
    frm, to = _norm_unit(from_unit), _norm_unit(to_unit)
    if frm == to:
        return value, True, "identity"
    if frm is None or to is None:
        return None, False, "unknown_unit"
    if frm in _TEMP_UNITS and to in _TEMP_UNITS:
        r = _temp_convert(value, frm, to)
        return (r, r is not None, "temperature")
    for dim, table in _DIMENSIONS.items():
        if frm in table and to in table:
            return value * table[frm] / table[to], True, dim
    mw = _ANALYTE_MW.get(analyte) if analyte else None
    if mw:
        if frm in _MASS_CONC and to in _MOLAR_CONC:
            mol_per_l = (value * _MASS_CONC[frm]) / mw
            return mol_per_l / _MOLAR_CONC[to], True, "molar_from_mass"
        if frm in _MOLAR_CONC and to in _MASS_CONC:
            g_per_l = (value * _MOLAR_CONC[frm]) * mw
            return g_per_l / _MASS_CONC[to], True, "mass_from_molar"
    return None, False, "no_conversion"


@mcp.tool(annotations={"title": "Correlate two signals", "readOnlyHint": True, "idempotentHint": True})
def correlate_metrics(
    source_a: str,
    name_a: str,
    source_b: str,
    name_b: str,
    since: str | None = None,
    until: str | None = None,
    resample: str = "day",
    agg: str = "mean",
    method: str = "both",
    lag_days: int = 0,
    user: str | None = None,
) -> dict:
    """Correlate two health signals aligned onto a common time grid.

    Resamples each signal to one value per `resample` bucket (day/week/month)
    with `agg`, inner-joins the buckets they share, then computes Pearson and/or
    Spearman correlation with a two-sided p-value and the paired sample size.
    Either signal may come from any source: metric, wearable, lab, biomarker,
    substance.

    Args:
        source_a / name_a: first signal, e.g. source='metric' name='weight_kg'.
        source_b / name_b: second signal, e.g. source='lab' name='a1c_percent'.
        since / until: ISO8601 or 'YYYY-MM-DD' bounds (inclusive).
        resample: 'day' | 'week' | 'month' bucket granularity.
        agg: how to collapse multiple readings in a bucket
             ('mean','median','sum','min','max','first','last','count').
        method: 'pearson' | 'spearman' | 'both'.
        lag_days: only with resample='day'. Positive values pair each A bucket
            with the B bucket `lag_days` days earlier (tests whether B leads A).
        user: which person; defaults to the primary user.

    Correlation is descriptive association, never causation or diagnosis.
    """
    u = _tool_user(user, "correlate_metrics")
    if resample not in _RESAMPLE_BUCKETS:
        raise ValueError(f"resample must be one of {'|'.join(_RESAMPLE_BUCKETS)}")
    if method not in ("pearson", "spearman", "both"):
        raise ValueError("method must be pearson|spearman|both")
    if agg not in _AGG_FUNCS:
        raise ValueError(f"agg must be one of {'|'.join(_AGG_FUNCS)}")
    lag = int(lag_days)
    if lag and resample != "day":
        raise ValueError("lag_days is only supported with resample='day'")
    lo, hi = _range_bounds(since, until)
    with _db() as conn:
        skey_a, clean_a, _, items_a = _resolve_series(conn, u, source_a, name_a, lo, hi)
        skey_b, clean_b, _, items_b = _resolve_series(conn, u, source_b, name_b, lo, hi)
    _audit("correlate_metrics",
           f"{_audit_user(u)} a_hash={_fingerprint(clean_a)} b_hash={_fingerprint(clean_b)} "
           f"resample={resample} agg={agg} lag={lag}")
    grid_a = _resample_series(items_a, resample, agg)
    grid_b = _resample_series(items_b, resample, agg)

    def _shift(key: str) -> str:
        if not lag:
            return key
        return (datetime.fromisoformat(key).date() - timedelta(days=lag)).isoformat()

    paired = [(k, grid_a[k], grid_b[_shift(k)]) for k in sorted(grid_a) if _shift(k) in grid_b]
    xs = [p[1] for p in paired]
    ys = [p[2] for p in paired]
    result = {
        "user": u,
        "series_a": {"source": skey_a, "name": clean_a,
                     "unit": items_a[-1]["unit"] if items_a else None,
                     "n_readings": len(items_a), "n_buckets": len(grid_a)},
        "series_b": {"source": skey_b, "name": clean_b,
                     "unit": items_b[-1]["unit"] if items_b else None,
                     "n_readings": len(items_b), "n_buckets": len(grid_b)},
        "resample": resample, "agg": agg, "lag_days": lag,
        "paired_n": len(paired),
        "overlap": ({"first_bucket": paired[0][0], "last_bucket": paired[-1][0]}
                    if paired else None),
        "disclaimer": "Descriptive association only; correlation is not causation or diagnosis.",
    }
    caveats: list[str] = []
    if len(paired) < 3:
        result["message"] = "need at least 3 shared buckets to correlate"
        caveats.append("Too few overlapping points for a meaningful correlation.")
        result["caveats"] = caveats
        return result
    if len(paired) < 10:
        caveats.append(f"Only {len(paired)} paired points; the estimate is unstable and "
                       "p-values are rough.")
    caveats.append("Time-series points are autocorrelated, so true significance is weaker "
                   "than the p-value suggests.")
    if method in ("pearson", "both"):
        r = _pearson_r(xs, ys)
        if r is None:
            result["pearson"] = None
            caveats.append("Pearson undefined: a series had zero variance over the shared buckets.")
        else:
            result["pearson"] = {
                "r": round(r, 4), "p_value": _corr_p_value(r, len(paired)),
                "df": len(paired) - 2, "strength": _corr_strength(r),
                "direction": "positive" if r > 0 else ("negative" if r < 0 else "none"),
            }
    if method in ("spearman", "both"):
        rho = _pearson_r(_rankdata(xs), _rankdata(ys))
        if rho is None:
            result["spearman"] = None
        else:
            result["spearman"] = {
                "rho": round(rho, 4), "p_value": _corr_p_value(rho, len(paired)),
                "strength": _corr_strength(rho),
                "direction": "positive" if rho > 0 else ("negative" if rho < 0 else "none"),
            }
    result["caveats"] = caveats
    return result


@mcp.tool(annotations={"title": "Analyze event impact", "readOnlyHint": True, "idempotentHint": True})
def analyze_event_impact(
    name: str,
    event_date: str,
    source: str = "metric",
    event_label: str | None = None,
    window_days: int | None = None,
    washout_days: int = 0,
    user: str | None = None,
) -> dict:
    """Estimate a signal's before/after change around a discrete event.

    Splits one signal at an anchor date (e.g. a medication start, procedure, or
    regimen change) into 'before' and 'after' groups, reports descriptive stats
    for each, and adds the difference in means plus a Welch t-test.

    Args:
        name: the signal name, e.g. 'resting_heart_rate' or 'a1c_percent'.
        event_date: the anchor date (ISO8601 or 'YYYY-MM-DD').
        source: 'metric' | 'wearable' | 'lab' | 'biomarker' | 'substance'.
        event_label: optional description of the event, echoed in the response.
        window_days: if set, only include readings within this many days on each
            side of the event; omit to use all available history.
        washout_days: exclude readings within this many days of the event on both
            sides (a washout gap) to skip transition-period noise.
        user: which person; defaults to the primary user.

    A before/after difference is descriptive, never proof the event caused it.
    """
    u = _tool_user(user, "analyze_event_impact")
    anchor = _parse_ts(event_date)
    anchor_dt = datetime.fromisoformat(anchor)
    wd = int(window_days) if window_days is not None else None
    wash = max(0, int(washout_days))
    if wd is not None:
        lo = (anchor_dt - timedelta(days=wd)).isoformat()
        hi = (anchor_dt + timedelta(days=wd)).replace(hour=23, minute=59, second=59).isoformat()
    else:
        lo, hi = _range_bounds(None, None)
    with _db() as conn:
        skey, clean, cfg, items = _resolve_series(conn, u, source, name, lo, hi)
    _audit("analyze_event_impact",
           f"{_audit_user(u)} source={skey} name_hash={_fingerprint(clean)} washout={wash}")
    before, after = [], []
    for it in items:
        it_dt = datetime.fromisoformat(it["ts"])
        if wash and abs((it_dt - anchor_dt).total_seconds()) / 86400.0 < wash:
            continue
        (before if it_dt < anchor_dt else after).append(it)
    out = {
        "user": u,
        "source": skey,
        "name": clean,
        "event": {"date": anchor, "label": event_label},
        "unit": items[-1]["unit"] if items else None,
        "window_days": wd,
        "washout_days": wash,
        "before": _group_stats(before),
        "after": _group_stats(after),
        "disclaimer": "Descriptive before/after comparison; not proof of causation or medical advice.",
    }
    if before and after:
        mb, ma = out["before"]["mean"], out["after"]["mean"]
        change = round(ma - mb, 4)
        out["change"] = {
            "mean_before": mb,
            "mean_after": ma,
            "absolute_change": change,
            "percent_change": round((change / mb) * 100.0, 2) if mb else None,
            "direction": "increase" if change > 0 else ("decrease" if change < 0 else "no change"),
        }
        welch = _welch_t([it["value"] for it in before], [it["value"] for it in after])
        if welch:
            out["change"]["welch_t_test"] = welch
    else:
        out["message"] = "need readings on both sides of the event to compare"
    return out


@mcp.tool(annotations={"title": "Align multiple signals", "readOnlyHint": True, "idempotentHint": True})
def align_series(
    series_json: str,
    since: str | None = None,
    until: str | None = None,
    resample: str = "day",
    agg: str = "mean",
    join: str = "outer",
    limit: int = 500,
    user: str | None = None,
) -> dict:
    """Resample 2+ signals onto one shared time grid for side-by-side comparison.

    Takes a JSON array of signal specs and returns a single aligned table — one
    row per time bucket, one column per signal — so signals can be compared
    without hand-matching timestamps.

    Args:
        series_json: JSON array of specs, each {"source","name"} with optional
            "label" and per-series "agg". Example:
            '[{"source":"metric","name":"weight_kg"},
              {"source":"lab","name":"a1c_percent","agg":"last"}]'
        since / until: ISO8601 or 'YYYY-MM-DD' bounds (inclusive).
        resample: 'day' | 'week' | 'month' bucket granularity.
        agg: default bucket aggregation ('mean','median','sum','min','max',
             'first','last','count'); a spec's own "agg" overrides it.
        join: 'outer' (every bucket any signal has, missing entries null) or
              'inner' (only buckets every signal shares).
        limit: max rows returned (the most recent are kept if exceeded).
        user: which person; defaults to the primary user.

    Aligned descriptive values only; not diagnosis or medical advice.
    """
    u = _tool_user(user, "align_series")
    if resample not in _RESAMPLE_BUCKETS:
        raise ValueError(f"resample must be one of {'|'.join(_RESAMPLE_BUCKETS)}")
    if join not in ("outer", "inner"):
        raise ValueError("join must be outer|inner")
    if agg not in _AGG_FUNCS:
        raise ValueError(f"agg must be one of {'|'.join(_AGG_FUNCS)}")
    specs = _json_list(series_json, "series_json")
    if not 1 <= len(specs) <= 8:
        raise ValueError("series_json must contain between 1 and 8 signal specs")
    lim = _limit(limit, default=500)
    lo, hi = _range_bounds(since, until)
    grids: list[tuple[str, dict]] = []
    meta: list[dict] = []
    used_labels: set[str] = set()
    with _db() as conn:
        for spec in specs:
            if not isinstance(spec, dict):
                raise ValueError("each series spec must be a JSON object")
            skey, clean, cfg, items = _resolve_series(
                conn, u, spec.get("source"), spec.get("name"), lo, hi)
            spec_agg = spec.get("agg", agg)
            if spec_agg not in _AGG_FUNCS:
                raise ValueError(f"agg must be one of {'|'.join(_AGG_FUNCS)}")
            label = _optional_text(spec.get("label"), "label", max_chars=60) or f"{skey}:{clean}"
            base, n = label, 2
            while label in used_labels:
                label = f"{base}#{n}"
                n += 1
            used_labels.add(label)
            grid = _resample_series(items, resample, spec_agg)
            grids.append((label, grid))
            meta.append({"label": label, "source": skey, "name": clean,
                         "unit": items[-1]["unit"] if items else None, "agg": spec_agg,
                         "n_readings": len(items), "n_buckets": len(grid)})
    _audit("align_series", f"{_audit_user(u)} series={len(specs)} resample={resample} join={join}")
    if join == "inner":
        keys: set | None = None
        for _, g in grids:
            keys = set(g) if keys is None else (keys & set(g))
        ordered = sorted(keys or set())
    else:
        allk: set = set()
        for _, g in grids:
            allk |= set(g)
        ordered = sorted(allk)
    total = len(ordered)
    truncated = total > lim
    if truncated:
        ordered = ordered[-lim:]
    grid_rows = [{"bucket": k, **{label: g.get(k) for label, g in grids}} for k in ordered]
    return {
        "user": u,
        "resample": resample,
        "default_agg": agg,
        "join": join,
        "series": meta,
        "bucket_count": total,
        "returned": len(grid_rows),
        "truncated": truncated,
        "grid": grid_rows,
        "disclaimer": "Aligned descriptive values only; not diagnosis or medical advice.",
    }


@mcp.tool(annotations={"title": "Normalize units & ranges", "readOnlyHint": True, "idempotentHint": True})
def normalize_series(
    name: str,
    source: str = "lab",
    to_unit: str | None = None,
    analyte: str | None = None,
    since: str | None = None,
    until: str | None = None,
    limit: int = 200,
    user: str | None = None,
) -> dict:
    """Reconcile mixed units and reference ranges within one signal.

    Pulls a signal's readings, converts every value (and its reference range, for
    labs/biomarkers) to a single common unit, and adds a unitless 'reference
    position' so readings taken with different units or reference ranges become
    directly comparable.

    Args:
        name: the signal name, e.g. 'glucose' or 'a1c_percent'.
        source: 'lab' | 'biomarker' | 'metric' | 'wearable' | 'substance'.
        to_unit: target unit for all values; omit to use the most common unit
            already present in the series.
        analyte: analyte hint (e.g. 'glucose', 'cholesterol', 'creatinine') that
            unlocks mass<->molar conversions like mg/dL<->mmol/L; defaults to the
            signal name.
        since / until: ISO8601 or 'YYYY-MM-DD' bounds (inclusive).
        limit: max readings to return.
        user: which person; defaults to the primary user.

    'reference position' is 0 at the lower reference bound and 1 at the upper.
    Descriptive normalization only; not interpretation or diagnosis.
    """
    u = _tool_user(user, "normalize_series")
    lim = _limit(limit, default=200)
    lo, hi = _range_bounds(since, until)
    with _db() as conn:
        skey, clean, cfg, items = _resolve_series(conn, u, source, name, lo, hi)
    _audit("normalize_series", f"{_audit_user(u)} source={skey} name_hash={_fingerprint(clean)}")
    if not items:
        return {"user": u, "source": skey, "name": clean, "count": 0}
    analyte_key = _keyish(analyte, "analyte") if analyte else clean
    units_seen: dict = {}
    for it in items:
        nu = _norm_unit(it["unit"])
        units_seen[nu] = units_seen.get(nu, 0) + 1
    if to_unit is not None:
        target = _norm_unit(to_unit)
    else:
        ranked = sorted(units_seen.items(), key=lambda kv: (kv[0] is None, -kv[1]))
        target = ranked[0][0] if ranked else None
    rows: list[dict] = []
    converted_values: list[float] = []
    unconverted: set[str] = set()
    for it in items[-lim:]:
        conv_val, ok, tag = _convert_value(it["value"], it["unit"], target, analyte_key)
        row = {
            "ts": it["ts"],
            "original_value": round(it["value"], 6),
            "original_unit": it["unit"],
            "value": round(conv_val, 6) if (ok and conv_val is not None) else None,
            "unit": target,
            "converted": bool(ok and conv_val is not None),
            "conversion": tag,
        }
        if cfg["has_ref"]:
            rl, rh = it.get("ref_low"), it.get("ref_high")
            rl_c = _convert_value(rl, it["unit"], target, analyte_key)[0] if rl is not None else None
            rh_c = _convert_value(rh, it["unit"], target, analyte_key)[0] if rh is not None else None
            row["ref_low"] = round(rl_c, 6) if rl_c is not None else None
            row["ref_high"] = round(rh_c, 6) if rh_c is not None else None
            base_val = row["value"] if row["value"] is not None else it["value"]
            base_lo = rl_c if rl_c is not None else rl
            base_hi = rh_c if rh_c is not None else rh
            if base_lo is not None and base_hi is not None and base_hi != base_lo:
                row["reference_position"] = round((base_val - base_lo) / (base_hi - base_lo), 4)
                row["in_range"] = bool(base_lo <= base_val <= base_hi)
        if row["converted"]:
            converted_values.append(row["value"])
        else:
            unconverted.add(it["unit"] or "unitless")
        rows.append(row)
    summary = {
        "user": u,
        "source": skey,
        "name": clean,
        "analyte_hint": analyte_key,
        "target_unit": target,
        "units_seen": {(k or "unitless"): v for k, v in units_seen.items()},
        "count": len(rows),
        "converted_count": len(converted_values),
        "unconverted_units": sorted(unconverted),
        "rows": rows,
        "disclaimer": "Unit/reference normalization only; not interpretation or diagnosis.",
    }
    if converted_values:
        summary["normalized_stats"] = {
            "unit": target,
            "min": round(min(converted_values), 4),
            "max": round(max(converted_values), 4),
            "mean": round(statistics.fmean(converted_values), 4),
            "median": round(statistics.median(converted_values), 4),
        }
    return summary


# ------------------------------------------------------ trend intelligence
# analyze_trend goes beyond a single least-squares line. The slope tools above
# now report their own uncertainty (see _ols); this tool adds robust outlier
# flags, baseline framing (latest vs your own median), a linear-vs-nonlinear
# shape check, and single change-point detection. All descriptive, no diagnosis.

def _det3(m: list[list[float]]) -> float:
    return (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
            - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
            + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))


def _quad_r2(xs: list[float], ys: list[float]) -> float | None:
    """R^2 of a quadratic least-squares fit (normal equations via Cramer's rule),
    used only to detect curvature a straight line would miss."""
    n = len(xs)
    if n < 4:
        return None
    s1 = sum(xs)
    s2 = sum(x * x for x in xs)
    s3 = sum(x ** 3 for x in xs)
    s4 = sum(x ** 4 for x in xs)
    sy = sum(ys)
    sxy = sum(x * y for x, y in zip(xs, ys))
    sx2y = sum(x * x * y for x, y in zip(xs, ys))
    base = [[s4, s3, s2], [s3, s2, s1], [s2, s1, float(n)]]
    det = _det3(base)
    if det == 0:
        return None
    rhs = [sx2y, sxy, sy]
    a = _det3([[rhs[0], s3, s2], [rhs[1], s2, s1], [rhs[2], s1, float(n)]]) / det
    b = _det3([[s4, rhs[0], s2], [s3, rhs[1], s1], [s2, rhs[2], float(n)]]) / det
    c = _det3([[s4, s3, rhs[0]], [s3, s2, rhs[1]], [s2, s1, rhs[2]]]) / det
    my = sy / n
    syy = sum((y - my) ** 2 for y in ys)
    if syy == 0:
        return None
    sse = sum((y - (a * x * x + b * x + c)) ** 2 for x, y in zip(xs, ys))
    return 1.0 - sse / syy


def _mad_outliers(values: list[float], ts_list: list[str], thresh: float = 3.5) -> dict:
    """Flag points by modified z-score (median/MAD) — robust to the outliers
    themselves, unlike a mean/stdev rule that they distort."""
    n = len(values)
    if n < 4:
        return {"method": "modified z-score (median/MAD)", "threshold": thresh,
                "count": 0, "points": [], "note": "need >=4 points to judge outliers"}
    med = statistics.median(values)
    mad = statistics.median([abs(v - med) for v in values])
    if mad > 0:
        scores = [0.6745 * (v - med) / mad for v in values]
        method = "modified z-score (median/MAD)"
    else:
        sd = statistics.pstdev(values)
        if sd == 0:
            return {"method": "modified z-score (median/MAD)", "threshold": thresh,
                    "count": 0, "points": [], "note": "no variance"}
        scores = [(v - med) / sd for v in values]
        method = "z-score (MAD=0, fell back to stdev)"
    pts = [{"ts": ts_list[i], "value": round(values[i], 4), "robust_z": round(scores[i], 2)}
           for i in range(n) if abs(scores[i]) > thresh]
    pts.sort(key=lambda p: abs(p["robust_z"]), reverse=True)
    return {"method": method, "threshold": thresh, "count": len(pts), "points": pts[:20]}


def _baseline_frame(points: list[tuple[str, float]], lookback_days: int) -> dict:
    """Latest reading vs the user's own recent median and typical range — how a
    clinician reads a value, rather than latest-vs-fitted-line."""
    latest_ts, latest_val = points[-1]
    cutoff = datetime.fromisoformat(latest_ts) - timedelta(days=max(1, lookback_days))
    vals = [v for ts, v in points if datetime.fromisoformat(ts) >= cutoff]
    if len(vals) < 2:
        return {"lookback_days": lookback_days, "n": len(vals),
                "note": "not enough history in the baseline window"}
    med = statistics.median(vals)
    q = _quartiles(vals)
    mad = statistics.median([abs(v - med) for v in vals])
    delta = latest_val - med
    frame = {
        "lookback_days": lookback_days,
        "n": len(vals),
        "median": round(med, 4),
        "iqr": [round(q[0], 4), round(q[1], 4)] if q else None,
        "latest": {"ts": latest_ts, "value": round(latest_val, 4)},
        "delta_vs_median": round(delta, 4),
        "percent_vs_median": round(delta / med * 100.0, 2) if med != 0 else None,
        "robust_z": round(0.6745 * delta / mad, 2) if mad > 0 else None,
    }
    if q:
        frame["position"] = ("above your typical range (>Q3)" if latest_val > q[1]
                             else "below your typical range (<Q1)" if latest_val < q[0]
                             else "within your typical range (Q1-Q3)")
    return frame


def _rate_of_change(points: list[tuple[str, float]]) -> dict:
    """Recent vs earlier slope, to expose acceleration a single global line hides."""
    n = len(points)
    if n < 4:
        return {"note": "need >=4 points to compare recent vs earlier rate"}

    def _seg_slope(seg):
        if len(seg) < 2:
            return None
        t0 = datetime.fromisoformat(seg[0][0])
        xs = [(datetime.fromisoformat(ts) - t0).total_seconds() / 86400.0 for ts, _ in seg]
        fit = _ols(xs, [v for _, v in seg])
        return fit["slope"] if fit else None

    mid = n // 2
    earlier = _seg_slope(points[:mid + 1])
    recent = _seg_slope(points[mid:])
    out = {
        "earlier_slope_per_day": round(earlier, 6) if earlier is not None else None,
        "recent_slope_per_day": round(recent, 6) if recent is not None else None,
    }
    if earlier is not None and recent is not None:
        out["acceleration_per_day"] = round(recent - earlier, 6)
        out["recent_change_per_30d"] = round(recent * 30.0, 4)
        out["direction_shift"] = (earlier <= 0 < recent) or (earlier >= 0 > recent)
    return out


def _trend_shape(xs: list[float], ys: list[float], linear_r2: float | None) -> dict:
    """Warn when a straight line is the wrong model: weak fit, residual runs
    (unmodeled structure), or a materially better quadratic (curvature)."""
    n = len(xs)
    warnings: list[str] = []
    advised = True
    fit = _ols(xs, ys)
    runs = None
    if fit and n >= 8:
        resid = [y - (fit["intercept"] + fit["slope"] * x) for x, y in zip(xs, ys)]
        signs = [1 if r >= 0 else -1 for r in resid if r != 0]
        n1 = sum(1 for s in signs if s > 0)
        n2 = sum(1 for s in signs if s < 0)
        if n1 > 0 and n2 > 0:
            obs = 1 + sum(1 for i in range(1, len(signs)) if signs[i] != signs[i - 1])
            exp = 1 + 2.0 * n1 * n2 / (n1 + n2)
            var = (2.0 * n1 * n2 * (2.0 * n1 * n2 - n1 - n2)) / (((n1 + n2) ** 2) * (n1 + n2 - 1))
            z = (obs - exp) / math.sqrt(var) if var > 0 else 0.0
            runs = {"observed_runs": obs, "expected_runs": round(exp, 2), "z": round(z, 2)}
            if z < -1.96:
                warnings.append("residuals cluster in runs (z<-1.96): the signal has structure "
                                "a straight line misses — consider a curve or cycle")
                advised = False
    curvature = None
    quad_r2 = _quad_r2(xs, ys)
    if quad_r2 is not None and linear_r2 is not None:
        gain = quad_r2 - linear_r2
        curvature = {"linear_r2": round(linear_r2, 4), "quadratic_r2": round(quad_r2, 4),
                     "r2_gain": round(gain, 4), "curved": gain > 0.1}
        if gain > 0.1:
            warnings.append("a quadratic fits materially better (R2 gain>0.10): the trend is "
                            "likely non-linear, so linear extrapolation may mislead")
            advised = False
    if linear_r2 is not None and linear_r2 < 0.3:
        warnings.append("weak linear fit (R2<0.30): the straight-line slope explains little "
                        "of the variation")
        advised = False
    return {"linear_r2": round(linear_r2, 4) if linear_r2 is not None else None,
            "residual_runs_test": runs, "curvature": curvature,
            "linear_extrapolation_advised": advised, "warnings": warnings}


def _change_point(points: list[tuple[str, float]], min_seg: int = 3,
                  min_reduction: float = 0.3) -> dict:
    """Single change-point by the split that best reduces within-segment variance
    (piecewise-constant mean). Heuristic and descriptive, not a formal test."""
    ys = [v for _, v in points]
    n = len(ys)
    if n < 2 * min_seg:
        return {"detected": False, "note": f"need >={2 * min_seg} points"}

    def _sse(seg):
        if not seg:
            return 0.0
        m = sum(seg) / len(seg)
        return sum((v - m) ** 2 for v in seg)

    total = _sse(ys)
    if total == 0:
        return {"detected": False, "note": "no variance"}
    best_k, best_sse = None, None
    for k in range(min_seg, n - min_seg + 1):
        s = _sse(ys[:k]) + _sse(ys[k:])
        if best_sse is None or s < best_sse:
            best_k, best_sse = k, s
    reduction = 1.0 - best_sse / total
    if reduction < min_reduction:
        return {"detected": False, "best_variance_reduction": round(reduction, 3),
                "note": "no clear regime change"}
    left, right = ys[:best_k], ys[best_k:]
    return {
        "detected": True,
        "date": points[best_k][0],
        "index": best_k,
        "variance_reduction": round(reduction, 3),
        "before": {"n": len(left), "mean": round(statistics.fmean(left), 4)},
        "after": {"n": len(right), "mean": round(statistics.fmean(right), 4)},
        "mean_shift": round(statistics.fmean(right) - statistics.fmean(left), 4),
        "note": f"heuristic single change-point (variance-reduction >= {min_reduction}); descriptive only",
    }


@mcp.tool(annotations={"title": "Analyze trend (advanced)", "readOnlyHint": True, "idempotentHint": True})
def analyze_trend(
    name: str,
    source: str = "metric",
    since: str | None = None,
    until: str | None = None,
    baseline_window_days: int = 180,
    outlier_threshold: float = 3.5,
    user: str | None = None,
) -> dict:
    """Trend intelligence for one signal — beyond a single straight line.

    Pulls a signal's dated numeric readings and returns, in one call:
      * trend — least-squares slope with a standard error, 95% CI, p-value, and
        an honest 'distinguishable from flat / treat as noise' verdict;
      * baseline — the latest reading framed against your own recent median and
        typical range (Q1-Q3), the way a clinician reads a value;
      * rate_of_change — recent vs earlier slope, exposing acceleration;
      * outliers — points flagged by a robust median/MAD z-score, not silently
        averaged into the mean;
      * shape — whether a straight line is the right model at all (weak fit,
        residual runs, or a better-fitting quadratic) and whether linear
        extrapolation is advisable for bounded or cyclical signals;
      * change_point — the single most likely regime shift, if any.

    Args:
        name: the signal name, e.g. 'weight_kg', 'a1c_percent', 'resting_heart_rate'.
        source: 'metric' | 'wearable' | 'lab' | 'biomarker' | 'substance'.
        since / until: ISO8601 or 'YYYY-MM-DD' bounds (inclusive).
        baseline_window_days: lookback for the baseline median/range (default 180).
        outlier_threshold: modified z-score cutoff for outliers (default 3.5).
        user: which person; defaults to the primary user.

    Everything here is descriptive statistics — not diagnosis or medical advice.
    """
    u = _tool_user(user, "analyze_trend")
    lo, hi = _range_bounds(since, until)
    with _db() as conn:
        skey, clean, cfg, items = _resolve_series(conn, u, source, name, lo, hi)
    _audit("analyze_trend", f"{_audit_user(u)} source={skey} name_hash={_fingerprint(clean)}")
    if len(items) < 2:
        return {"user": u, "source": skey, "name": clean, "count": len(items),
                "message": "need at least 2 numeric readings to analyze a trend"}
    points = [(it["ts"], it["value"]) for it in items]
    values = [it["value"] for it in items]
    ts_list = [it["ts"] for it in items]
    t0 = datetime.fromisoformat(points[0][0])
    xs = [(datetime.fromisoformat(ts) - t0).total_seconds() / 86400.0 for ts, _ in points]
    trend = _linreg_per_day(points)
    linear_r2 = trend.get("r_squared") if trend else None
    return {
        "user": u,
        "source": skey,
        "name": clean,
        "unit": items[-1]["unit"],
        "count": len(items),
        "window": {"since": lo, "until": hi},
        "first": {"ts": items[0]["ts"], "value": round(items[0]["value"], 4)},
        "latest": {"ts": items[-1]["ts"], "value": round(items[-1]["value"], 4)},
        "mean": round(statistics.fmean(values), 4),
        "median": round(statistics.median(values), 4),
        "stdev": round(statistics.stdev(values), 4) if len(values) > 1 else 0.0,
        "trend": trend,
        "baseline": _baseline_frame(points, int(baseline_window_days)),
        "rate_of_change": _rate_of_change(points),
        "outliers": _mad_outliers(values, ts_list, float(outlier_threshold)),
        "shape": _trend_shape(xs, values, linear_r2),
        "change_point": _change_point(points),
        "disclaimer": "Descriptive trend analysis with uncertainty, outlier, baseline, "
                      "shape, and change-point framing; not diagnosis or medical advice.",
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


if __name__ == "__main__":
    if TRANSPORT == "stdio":
        # Local mode (e.g. Claude Desktop on a MacBook). No OAuth: the process is a
        # trusted local subprocess of the client and never binds to the network.
        _init_db()
        sys.stderr.write(
            f"health-mcp starting (stdio, local): db={DB_PATH} default_user={DEFAULT_USER}\n"
        )
        mcp.run()  # stdio transport (the FastMCP default)
    else:
        _fail_closed()
        _init_db()
        sys.stderr.write(
            f"health-mcp starting: {PUBLIC_URL}{MCP_PATH} -> {HOST}:{PORT} "
            f"(db={DB_PATH}, allow-list: {', '.join(sorted(ALLOWED_LOGINS))})\n"
        )
        mcp.run(transport="http", host=HOST, port=PORT, path=MCP_PATH)
