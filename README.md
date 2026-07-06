# health-mcp

A remote MCP server that stores a person's **personal health data** and returns
**analysis-ready views** of it, so an LLM (via a claude.ai custom connector) can log,
retrieve, and reason over that record on demand.

Sibling of `vps-mcp`; same security model (Cloudflare Tunnel + GitHub-OAuth allow-list).

> Not a medical device. It stores and summarises what you record. The analysis tools
> return descriptive statistics and trends, not diagnosis. For clinical decisions,
> consult a licensed professional.

## Endpoint
- Public MCP URL: `https://health-mcp.manticthink.com/mcp`
- Binds `127.0.0.1:8800`, reached only through the Cloudflare Tunnel `health-mcp`
  (id `93c6f3dd-3b3e-440a-9fe2-626e17ed5edb`).

## Storage
- SQLite at `/srv/health-mcp/health.db` (mode 0600, WAL).
- Schema v3 stores quantitative metrics, events, notes, profile facts, conditions,
  allergies, medications and dose logs, lab reports/results, biomarkers, tumor/
  cancer records, encounters/physicals, procedures, imaging, immunizations, care
  tasks, documents, enriched family history, reproductive health records,
  substance-use logs, wearable/app sources, wearable samples, and generic
  `health_records` for anything that does not fit a dedicated table yet.
- Every tool takes an optional `user` label, so one server can hold several people
  (defaults to `HEALTH_MCP_DEFAULT_USER`, currently `me`).
- `audit.log` records every tool call.
- `health-mcp-backup.timer` writes daily SQLite backups to
  `/srv/health-mcp/backups` by default.

## Tools (67)
Core capture/retrieval:
`log_metric`, `get_metrics`, `list_metrics`, `analyze_metric`, `log_event`,
`get_events`, `log_note`, `get_notes`, `set_profile`, `get_profile`,
`delete_profile`.

Structured clinical history:
`add_condition`, `list_conditions`, `add_allergy`, `list_allergies`,
`add_medication`, `list_medications`, `log_medication_taken`,
`list_medication_schedule`, `list_medication_logs`, `add_encounter`,
`list_encounters`, `add_procedure`, `list_procedures`, `add_imaging_report`,
`list_imaging_reports`, `add_immunization`, `list_immunizations`.

Labs, biomarkers, oncology, and documents:
`add_lab_report`, `list_lab_reports`, `add_lab_result`, `list_lab_results`,
`analyze_lab_trend`, `add_biomarker`, `list_biomarkers`,
`analyze_biomarker_trend`, `add_tumor_record`, `list_tumor_records`,
`add_document`, `list_documents`, `add_family_history`, `list_family_history`,
`add_health_record`, `list_health_records`.

Reproductive, substance, and wearable data:
`add_reproductive_record`, `list_reproductive_records`,
`analyze_reproductive_trend`, `add_substance_use_log`,
`list_substance_use_logs`, `analyze_substance_trend`, `add_wearable_source`,
`list_wearable_sources`, `add_wearable_sample`, `import_wearable_samples`,
`list_wearable_samples`, `analyze_wearable_trend`.

Planning, whole-record views, and operations:
`add_care_task`, `complete_care_task`, `list_care_tasks`, `list_due_tasks`,
`health_agenda`, `care_gap_report`, `summarize_health`, `search_records`,
`delete_record`, `export_data`, `health_status`.

Trend tools compute count/min/max/mean/median plus least-squares trend where
numeric dated values exist. `summarize_health` returns a compact cross-domain
digest; `health_agenda` returns stored upcoming tasks/refills/follow-ups/
immunizations/reproductive due dates; `care_gap_report` reports missing/stale
stored data and unresolved stored follow-ups without making clinical screening
claims.

Wearable imports are intentionally separated from ordinary metrics:
`wearable_sources` identifies the device/app/feed, and `wearable_samples` stores
high-volume typed samples such as steps, HRV, resting heart rate, workouts, sleep,
SpO2, calories, temperature, and similar device measurements. Use
`import_wearable_samples` for repeated samples; by default each call accepts up to
`HEALTH_MCP_MAX_WEARABLE_IMPORT_ROWS=500` samples and
`HEALTH_MCP_MAX_BULK_JSON_CHARS=200000` bytes of JSON.

Recommended profile keys for clients: `birth_date`, `sex`, `gender`,
`height_cm`, `blood_type`, `emergency_contact`, `primary_care_provider`,
`preferred_pharmacy`, `insurance`, `advance_directive_on_file`, and stable
preferences/goals. Time-varying data should go in the dedicated tables instead
of profile keys.

`export_data` is paginated and capped. Use `table`, `limit`, and `offset`; `table=all`
returns one capped page for each record table.

Tool calls are rate-limited in-process by authenticated principal when available,
falling back to the `user` label for local/stdio calls. Defaults:
`HEALTH_MCP_RATE_LIMIT_CALLS=240` per `HEALTH_MCP_RATE_LIMIT_WINDOW_SECONDS=60`.

## One remaining setup step: GitHub OAuth App
The service is installed and **fail-closed** — it will not start until real OAuth
credentials replace the `PASTE_...` placeholders in `.env`.

1. Go to https://github.com/settings/developers -> **New OAuth App**
   - Homepage URL:               `https://health-mcp.manticthink.com`
   - Authorization callback URL: `https://health-mcp.manticthink.com/auth/callback`
2. **Generate a new client secret.**
3. Put both values in `/srv/health-mcp/.env`:
   ```
   HEALTH_MCP_GITHUB_CLIENT_ID=...
   HEALTH_MCP_GITHUB_CLIENT_SECRET=...
   ```
4. Start it:
   ```
   systemctl start health-mcp
   systemctl status health-mcp --no-pager
   ```

Only GitHub logins in `HEALTH_MCP_ALLOWED_LOGINS` (currently `Cole-Will-I-Am`) may connect.

## Connect from claude.ai
Settings -> Connectors -> **Add custom connector** -> URL `https://health-mcp.manticthink.com/mcp`
-> authorize with GitHub.

## Services
- `health-mcp.service` — the server (enabled; starts once creds are set).
- `health-mcp-tunnel.service` — the Cloudflare Tunnel (enabled, running).
- `health-mcp-backup.timer` — daily SQLite backup.
- The app exits with status `2` when required OAuth config is missing. systemd
  treats that as a non-restartable fail-closed state; after filling `.env`, run
  `systemctl reset-failed health-mcp && systemctl start health-mcp`.

## Operations
```
journalctl -u health-mcp -f            # app logs
journalctl -u health-mcp-tunnel -f     # tunnel logs
journalctl -u health-mcp-backup -n 50  # backup logs
sqlite3 /srv/health-mcp/health.db .tables
systemctl list-timers health-mcp-backup.timer
/srv/health-mcp/bin/backup.sh          # run a manual SQLite backup
```

## Offline tests
These do not touch the production SQLite database or require real GitHub OAuth
credentials:
```
cd /srv/health-mcp
./.venv/bin/python test_wiring.py
./.venv/bin/python test_tools.py
```

## Health check
With the app running, an unauthenticated request returns 401 (up + guarded):
```
curl -s -o /dev/null -w '%{http_code}\n' https://health-mcp.manticthink.com/mcp   # 401
```
