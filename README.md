<div align="center">

# 🩺 HealthLedger&nbsp;MCP

**Own your health record. Let an LLM read and reason over it — on your terms.**

A remote [Model Context Protocol](https://modelcontextprotocol.io) server that stores your
**personal health data** and hands back **analysis-ready views**, so a model (via a
claude.ai custom connector) can log, retrieve, and reason over that record on demand.

[![Python](https://img.shields.io/badge/python-3.12-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/protocol-MCP-6E56CF)](https://modelcontextprotocol.io)
[![Tools](https://img.shields.io/badge/tools-71-0EA5E9)](#-tool-catalog)
[![Auth](https://img.shields.io/badge/auth-GitHub%20OAuth-181717?logo=github&logoColor=white)](#-security-model)
[![Storage](https://img.shields.io/badge/storage-SQLite%20(WAL)-003B57?logo=sqlite&logoColor=white)](#-storage)
[![Status](https://img.shields.io/badge/status-fail--closed-16A34A)](#-security-model)

</div>

> [!WARNING]
> **Not a medical device.** HealthLedger stores and summarizes what *you* record. Its
> analysis tools return descriptive statistics and trends — **not diagnosis**. For any
> clinical decision, consult a licensed professional.

---

## ✨ What it does

HealthLedger is a single-tenant-friendly, multi-user-capable health ledger that lives
behind a Cloudflare Tunnel and a GitHub-OAuth allow-list. It is the sibling of `vps-mcp`
and shares its security model.

| | |
|---|---|
| 🔐 **Private by construction** | Binds `127.0.0.1` only; reachable exclusively through your Cloudflare Tunnel + OAuth allow-list. |
| 🧱 **Structured clinical schema** | 20+ dedicated tables (conditions, meds, labs, biomarkers, oncology, imaging, wearables, …) rather than a bag of notes. |
| 📈 **Analysis-ready** | Trend tools compute count / min / max / mean / median plus a least-squares slope over dated numeric values. |
| 🔗 **Cross-signal reasoning** | Correlate two signals, estimate before/after change around an event, align many signals onto one time grid, and reconcile units & reference ranges — across metrics, wearables, labs, biomarkers, and substances. |
| 🧑‍🤝‍🧑 **Multi-person** | Every tool takes an optional `user` label, so one server can hold a whole household. |
| 🧾 **Auditable** | Every tool call is appended to `audit.log`; daily SQLite backups run on a timer. |
| 🚪 **Fail-closed** | Refuses to start without real OAuth credentials — no accidental open endpoint. |

---

## 🚀 Quick start

> The service ships **installed but fail-closed**. One setup step stands between you and a
> running server: creating a GitHub OAuth App.

### 1 · Create a GitHub OAuth App

Go to **[github.com/settings/developers](https://github.com/settings/developers) → New OAuth App**:

| Field | Value |
|---|---|
| Homepage URL | `https://health-mcp.manticthink.com` |
| Authorization callback URL | `https://health-mcp.manticthink.com/auth/callback` |

Then **Generate a new client secret**.

### 2 · Drop the credentials into `.env`

```dotenv
# /srv/health-mcp/.env
HEALTH_MCP_GITHUB_CLIENT_ID=...
HEALTH_MCP_GITHUB_CLIENT_SECRET=...
```

### 3 · Start it

```bash
systemctl reset-failed health-mcp   # clear the fail-closed state
systemctl start health-mcp
systemctl status health-mcp --no-pager
```

> Only GitHub logins listed in `HEALTH_MCP_ALLOWED_LOGINS` (currently **`Cole-Will-I-Am`**) may connect.

### 4 · Connect from claude.ai

**Settings → Connectors → Add custom connector**, URL:

```
https://health-mcp.manticthink.com/mcp
```

…then authorize with GitHub. Done.

---

## 🌐 Endpoint

| | |
|---|---|
| **Public MCP URL** | `https://health-mcp.manticthink.com/mcp` |
| **Local bind** | `127.0.0.1:8800` (never exposed directly) |
| **Reachability** | via Cloudflare Tunnel `health-mcp` — id `93c6f3dd-3b3e-440a-9fe2-626e17ed5edb` |

**Health check** — an unauthenticated request should return `401` (server is *up and guarded*):

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://health-mcp.manticthink.com/mcp   # → 401
```

---

## 🔒 Security model

```
claude.ai ──HTTPS──► Cloudflare Tunnel ──► 127.0.0.1:8800  (health-mcp)
                          │                        │
                    GitHub OAuth            SQLite 0600 / WAL
                   (allow-list only)         + audit.log
```

- The security boundary is the **GitHub-OAuth allow-list** in `server.py` plus the
  **loopback bind** reached only through the tunnel — *not* systemd sandboxing (though
  the unit is sandboxed too).
- The app **exits with status `2`** when required OAuth config is missing. systemd treats
  that as a non-restartable, deliberate fail-closed state.
- Tool calls are **rate-limited in-process** per authenticated principal (falling back to
  the `user` label for local/stdio calls).

---

## 🗄️ Storage

- **SQLite** at `/srv/health-mcp/health.db` — mode `0600`, WAL journaling.
- **Schema v3** covers quantitative metrics, events, notes, profile facts, conditions,
  allergies, medications & dose logs, lab reports/results, biomarkers, tumor/cancer
  records, encounters/physicals, procedures, imaging, immunizations, care tasks,
  documents, enriched family history, reproductive-health records, substance-use logs,
  wearable/app sources, wearable samples, and a generic `health_records` catch-all.
- **`audit.log`** records every tool call.
- **`health-mcp-backup.timer`** writes daily SQLite backups to `/srv/health-mcp/backups`.

### Profile keys (stable facts only)

Recommended keys for clients: `birth_date`, `sex`, `gender`, `height_cm`, `blood_type`,
`emergency_contact`, `primary_care_provider`, `preferred_pharmacy`, `insurance`,
`advance_directive_on_file`, plus stable preferences/goals.

> ⏳ **Time-varying data belongs in the dedicated tables**, not in profile keys.

---

## 🧰 Tool catalog

**71 tools**, grouped by purpose. Every tool accepts an optional `user` label (default
`me`, from `HEALTH_MCP_DEFAULT_USER`).

<details open>
<summary><b>📥 Core capture &amp; retrieval</b></summary>

`log_metric` · `get_metrics` · `list_metrics` · `analyze_metric` · `log_event` ·
`get_events` · `log_note` · `get_notes` · `set_profile` · `get_profile` · `delete_profile`
</details>

<details>
<summary><b>🏥 Structured clinical history</b></summary>

`add_condition` · `list_conditions` · `add_allergy` · `list_allergies` ·
`add_medication` · `list_medications` · `log_medication_taken` ·
`list_medication_schedule` · `list_medication_logs` · `add_encounter` ·
`list_encounters` · `add_procedure` · `list_procedures` · `add_imaging_report` ·
`list_imaging_reports` · `add_immunization` · `list_immunizations`
</details>

<details>
<summary><b>🧪 Labs, biomarkers, oncology &amp; documents</b></summary>

`add_lab_report` · `list_lab_reports` · `add_lab_result` · `list_lab_results` ·
`analyze_lab_trend` · `add_biomarker` · `list_biomarkers` · `analyze_biomarker_trend` ·
`add_tumor_record` · `list_tumor_records` · `add_document` · `list_documents` ·
`add_family_history` · `list_family_history` · `add_health_record` · `list_health_records`
</details>

<details>
<summary><b>🔬 Reproductive, substance &amp; wearable data</b></summary>

`add_reproductive_record` · `list_reproductive_records` · `analyze_reproductive_trend` ·
`add_substance_use_log` · `list_substance_use_logs` · `analyze_substance_trend` ·
`add_wearable_source` · `list_wearable_sources` · `add_wearable_sample` ·
`import_wearable_samples` · `list_wearable_samples` · `analyze_wearable_trend`
</details>

<details>
<summary><b>🔗 Cross-signal reasoning</b></summary>

`correlate_metrics` · `analyze_event_impact` · `align_series` · `normalize_series`
</details>

<details>
<summary><b>🗓️ Planning, whole-record views &amp; operations</b></summary>

`add_care_task` · `complete_care_task` · `list_care_tasks` · `list_due_tasks` ·
`health_agenda` · `care_gap_report` · `summarize_health` · `search_records` ·
`delete_record` · `export_data` · `health_status`
</details>

### How the "smart" tools behave

| Tool | What it returns |
|---|---|
| `analyze_*_trend` / `analyze_metric` | count · min · max · mean · median · least-squares slope over dated numeric values |
| `correlate_metrics` | Pearson & Spearman between two signals aligned on a common time grid, with paired sample size, a two-sided p-value, and significance caveats |
| `analyze_event_impact` | before/after descriptive stats around a discrete event (med start, procedure) plus the difference in means and a Welch t-test |
| `align_series` | 2+ signals resampled onto one shared day/week/month grid — one row per bucket, one column per signal (inner or outer join) |
| `normalize_series` | one signal's readings converted to a common unit (incl. mg/dL↔mmol/L via analyte molar mass) with reference ranges reconciled and a unitless in-range position |
| `summarize_health` | compact cross-domain digest of the record |
| `health_agenda` | stored upcoming tasks, refills, follow-ups, immunizations, reproductive due dates |
| `care_gap_report` | missing/stale stored data and unresolved follow-ups — **without** clinical screening claims |
| `export_data` | paginated & capped; use `table`, `limit`, `offset` (`table=all` → one capped page per table) |

### Wearables, on purpose

Wearable imports are kept **separate** from ordinary metrics:

- **`wearable_sources`** identify the device / app / feed.
- **`wearable_samples`** store high-volume typed samples — steps, HRV, resting HR,
  workouts, sleep, SpO₂, calories, temperature, and similar.
- Use **`import_wearable_samples`** for bulk. Per call: up to
  `HEALTH_MCP_MAX_WEARABLE_IMPORT_ROWS=500` samples and
  `HEALTH_MCP_MAX_BULK_JSON_CHARS=200000` bytes of JSON.

---

## ⚙️ Configuration

All settings are environment variables (set in `/srv/health-mcp/.env`). Defaults shown.

| Variable | Default | Purpose |
|---|---|---|
| `HEALTH_MCP_GITHUB_CLIENT_ID` | *(required)* | GitHub OAuth App client id |
| `HEALTH_MCP_GITHUB_CLIENT_SECRET` | *(required)* | GitHub OAuth App client secret |
| `HEALTH_MCP_ALLOWED_LOGINS` | `Cole-Will-I-Am` | comma-separated GitHub logins allowed to connect |
| `HEALTH_MCP_PUBLIC_URL` | `https://health-mcp.manticthink.com` | public base URL |
| `HEALTH_MCP_DEFAULT_USER` | `me` | default `user` label when none is passed |
| `HEALTH_MCP_HOST` | `127.0.0.1` | bind address |
| `HEALTH_MCP_PORT` | `8800` | bind port |
| `HEALTH_MCP_PATH` | `/mcp` | MCP mount path |
| `HEALTH_MCP_MAX_ROWS` | `1000` | max rows returned by a list query |
| `HEALTH_MCP_MAX_EXPORT_ROWS` | `500` | max rows per export page |
| `HEALTH_MCP_MAX_TEXT_CHARS` | `20000` | max chars per free-text field |
| `HEALTH_MCP_MAX_WEARABLE_IMPORT_ROWS` | `500` | max wearable samples per import call |
| `HEALTH_MCP_MAX_BULK_JSON_CHARS` | `200000` | max JSON payload size for bulk import |
| `HEALTH_MCP_RATE_LIMIT_CALLS` | `240` | calls allowed per window |
| `HEALTH_MCP_RATE_LIMIT_WINDOW_SECONDS` | `60` | rate-limit window length |

---

## 🧩 Services

| Unit | Role | State |
|---|---|---|
| `health-mcp.service` | the MCP server | enabled — starts once creds are set |
| `health-mcp-tunnel.service` | Cloudflare Tunnel | enabled, running |
| `health-mcp-backup.timer` | daily SQLite backup | enabled |

> If the app is in the fail-closed state after you fill `.env`, run
> `systemctl reset-failed health-mcp && systemctl start health-mcp`.

---

## 🛠️ Operations

```bash
journalctl -u health-mcp -f            # app logs
journalctl -u health-mcp-tunnel -f     # tunnel logs
journalctl -u health-mcp-backup -n 50  # backup logs

sqlite3 /srv/health-mcp/health.db .tables
systemctl list-timers health-mcp-backup.timer
/srv/health-mcp/bin/backup.sh          # run a manual SQLite backup
```

---

## ✅ Offline tests

These touch **neither** the production SQLite database **nor** real GitHub OAuth
credentials:

```bash
cd /srv/health-mcp
./.venv/bin/python test_wiring.py
./.venv/bin/python test_tools.py
```

---

<div align="center">
<sub>Built on the Model Context Protocol · Cloudflare Tunnel · GitHub OAuth · SQLite.
Sibling project of <code>vps-mcp</code>.</sub>
</div>
