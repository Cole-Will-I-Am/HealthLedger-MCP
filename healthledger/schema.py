"""Database schema: DDL, initialization/migrations, and schema-derived helpers."""
from __future__ import annotations

from healthledger.config import *  # noqa: F401,F403
from healthledger.db import _db, _rows
from healthledger.timeutil import _now_iso


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

            -- Discrete genomic/PGx findings stay separate from the generic
            -- health_records catch-all, matching tumors and reproductive_records.
            CREATE TABLE IF NOT EXISTS genomic_records (
                id                    INTEGER PRIMARY KEY AUTOINCREMENT,
                user                  TEXT NOT NULL,
                created_ts            TEXT NOT NULL,
                record_type           TEXT NOT NULL,   -- variant | pgx | carrier_screen | polygenic_risk | wgs_summary | other
                test_date             TEXT,
                report_date           TEXT,
                lab_name              TEXT,
                ordering_provider     TEXT,
                methodology           TEXT,            -- targeted_panel | WES | WGS | array | other
                gene                  TEXT,
                transcript            TEXT,
                hgvs_c                TEXT,
                hgvs_p                TEXT,
                rsid                  TEXT,
                zygosity              TEXT,            -- heterozygous | homozygous | hemizygous
                clinical_significance TEXT,            -- pathogenic | likely_pathogenic | vus | likely_benign | benign
                inheritance_pattern   TEXT,
                associated_condition  TEXT,
                pgx_phenotype         TEXT,            -- e.g. poor/intermediate/normal/rapid/ultrarapid metabolizer
                pgx_drug              TEXT,
                pgx_guideline_source  TEXT,            -- e.g. CPIC, FDA label
                polygenic_trait       TEXT,
                polygenic_score       REAL,
                polygenic_percentile  REAL,
                document_id           INTEGER,         -- FK-by-convention to documents.id (source report)
                source                TEXT,
                notes                 TEXT,
                extra_json            TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_genomic_records_user_type_date
                ON genomic_records(user, record_type, test_date);

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

        # Cryptographic integrity: add chain columns + tips table
        from healthledger.integrity import _migrate_chain_schema
        _migrate_chain_schema(conn)

    try:
        os.chmod(DB_PATH, 0o600)
    except OSError:
        pass

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
