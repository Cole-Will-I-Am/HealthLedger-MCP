<div align="center">

# 🩺 HealthLedger&nbsp;MCP

**Your health record, on your machine. Let any AI read and reason over it — on your terms.**

A **local-first, model-agnostic** [Model Context Protocol](https://modelcontextprotocol.io)
server that stores your **personal health data** in a local SQLite file and hands back
**analysis-ready views**, so **any MCP client — and any LLM behind it** — can log, retrieve,
and reason over that record on demand.

[![Python](https://img.shields.io/badge/python-3.11+-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![MCP](https://img.shields.io/badge/protocol-MCP-6E56CF)](https://modelcontextprotocol.io)
[![Tools](https://img.shields.io/badge/tools-72-0EA5E9)](#-tool-catalog)
[![Local-first](https://img.shields.io/badge/runs-local--first-16A34A)](#-quick-start)
[![Model-agnostic](https://img.shields.io/badge/works%20with-any%20LLM-8B5CF6)](#-use-it-with-any-client--any-model)
[![Storage](https://img.shields.io/badge/storage-SQLite%20(WAL)-003B57?logo=sqlite&logoColor=white)](#-storage)

</div>

> [!WARNING]
> **Not a medical device.** HealthLedger stores and summarizes what *you* record. Its
> analysis tools return descriptive statistics, trends, and associations — **not diagnosis**.
> For any clinical decision, consult a licensed professional.

---

## ✨ What it does

HealthLedger is a personal health ledger you run yourself. Install it wherever you want,
point your AI client at it, and start adding your data. By default it runs as a **local
stdio server** — nothing binds to the network and the record never leaves your machine.

| | |
|---|---|
| 🏠 **Local-first** | Runs as a stdio subprocess of your MCP client. No server to host, no account, no network — your data lives in a local SQLite file. |
| 🤖 **Model-agnostic** | It's a plain MCP server. Works with any MCP-capable client (Claude Desktop, Cline, Cursor, Zed, Continue, LibreChat, custom agents…) and any model behind it. |
| 🧱 **Structured clinical schema** | 20+ dedicated tables (conditions, meds, labs, biomarkers, oncology, imaging, wearables, …) rather than a bag of notes. |
| 📈 **Analysis-ready** | Trend tools compute count / min / max / mean / median plus a slope **with uncertainty** over dated numeric values. |
| 🔗 **Cross-signal reasoning** | Correlate two signals, estimate before/after change around an event, align many signals onto one time grid, and reconcile units & reference ranges. |
| 📐 **Trend intelligence** | Slopes with a confidence interval and p-value (real trend vs noise), robust outlier flags, latest-vs-baseline framing, non-linear/cyclical warnings, and change-point detection. |
| 🧑‍🤝‍🧑 **Multi-person** | Every tool takes an optional `user` label, so one instance can hold a whole household. |
| 🌐 **Optional remote mode** | If you *want* a shared instance, it can run as an OAuth-protected HTTP server behind a tunnel. Entirely opt-in — see [Remote mode](#-remote-mode-optional). |

---

## 🚀 Quick start

You need Python 3.11+. The fastest path uses [`uv`](https://docs.astral.sh/uv/) (`uvx` runs it with zero install):

```bash
# Try it directly from the repo — no clone, no install:
uvx --from git+https://github.com/Cole-Will-I-Am/HealthLedger-MCP healthledger-mcp
```

Prefer a persistent install?

```bash
pipx install git+https://github.com/Cole-Will-I-Am/HealthLedger-MCP
# or, from a clone:
git clone https://github.com/Cole-Will-I-Am/HealthLedger-MCP && cd HealthLedger-MCP
pip install .
healthledger-mcp        # starts a local stdio server
```

That's it — no OAuth, no tunnel, no account. Your data is written to
`~/.healthledger/health.db` (override with `HEALTH_MCP_DB`).

---

## 🔌 Use it with any client / any model

HealthLedger speaks stdio MCP, so it drops into the standard `mcpServers` config that
virtually every MCP client uses. Point your client at it and you're done — the model on
the other side can be Claude, a local Llama, GPT-something, whatever your client runs.

**Zero-install (uvx):**

```jsonc
{
  "mcpServers": {
    "healthledger": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/Cole-Will-I-Am/HealthLedger-MCP", "healthledger-mcp"]
    }
  }
}
```

**After `pipx`/`pip install`:**

```jsonc
{
  "mcpServers": {
    "healthledger": { "command": "healthledger-mcp" }
  }
}
```

- **Claude Desktop** → `claude_desktop_config.json`
- **Cursor** → `.cursor/mcp.json` · **Zed** → `context_servers` · **Cline / Continue / LibreChat** → their MCP settings
- Any other MCP client → the same `command`/`args` shape

> Want it to hold more than one person? Pass a `user` label on any tool call (defaults to
> `me`, from `HEALTH_MCP_DEFAULT_USER`).

---

## 🗄️ Storage

- **SQLite** at `~/.healthledger/health.db` — mode `0600`, WAL journaling. Override with `HEALTH_MCP_DB`.
- **Schema v3** covers quantitative metrics, events, notes, profile facts, conditions,
  allergies, medications & dose logs, lab reports/results, biomarkers, tumor/cancer
  records, encounters/physicals, procedures, imaging, immunizations, care tasks,
  documents, enriched family history, reproductive-health records, substance-use logs,
  wearable/app sources, wearable samples, and a generic `health_records` catch-all.
- **`~/.healthledger/audit.log`** records every tool call (override with `HEALTH_MCP_AUDIT_LOG`).
- It's just a SQLite file — back it up, sync it, or delete it like any other file.

### Profile keys (stable facts only)

Recommended keys for clients: `birth_date`, `sex`, `gender`, `height_cm`, `blood_type`,
`emergency_contact`, `primary_care_provider`, `preferred_pharmacy`, `insurance`,
`advance_directive_on_file`, plus stable preferences/goals.

> ⏳ **Time-varying data belongs in the dedicated tables**, not in profile keys.

---

## 🧰 Tool catalog

**72 tools**, grouped by purpose. Every tool accepts an optional `user` label (default
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
<summary><b>🔗 Cross-signal reasoning &amp; trend intelligence</b></summary>

`correlate_metrics` · `analyze_event_impact` · `align_series` · `normalize_series` ·
`analyze_trend`
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
| `analyze_*_trend` / `analyze_metric` | count · min · max · mean · median · least-squares slope over dated numeric values — the slope now carries a standard error, 95% CI, p-value, and R² so a trend can be told from noise |
| `analyze_trend` | full trend intelligence for one signal: slope **with uncertainty** (SE, 95% CI, p-value, "distinguishable from flat / treat as noise") · robust median/MAD **outlier** flags · **baseline framing** (latest vs your own median & Q1–Q3) · **rate-of-change** (recent vs earlier slope) · **shape check** warning when a straight line is the wrong model for a bounded/cyclical signal · single **change-point** detection |
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

Everything is environment variables. Defaults shown; the local-mode defaults need no setup.

| Variable | Default | Purpose |
|---|---|---|
| `HEALTH_MCP_TRANSPORT` | `stdio` | `stdio` (local) or `http` (remote, opt-in) |
| `HEALTH_MCP_DB` | `~/.healthledger/health.db` | SQLite database path |
| `HEALTH_MCP_AUDIT_LOG` | `~/.healthledger/audit.log` | audit log path |
| `HEALTH_MCP_DEFAULT_USER` | `me` | default `user` label when none is passed |
| `HEALTH_MCP_MAX_ROWS` | `1000` | max rows returned by a list query |
| `HEALTH_MCP_MAX_EXPORT_ROWS` | `500` | max rows per export page |
| `HEALTH_MCP_MAX_TEXT_CHARS` | `20000` | max chars per free-text field |
| `HEALTH_MCP_MAX_WEARABLE_IMPORT_ROWS` | `500` | max wearable samples per import call |
| `HEALTH_MCP_MAX_BULK_JSON_CHARS` | `200000` | max JSON payload size for bulk import |
| `HEALTH_MCP_RATE_LIMIT_CALLS` | `240` | calls allowed per window |
| `HEALTH_MCP_RATE_LIMIT_WINDOW_SECONDS` | `60` | rate-limit window length |

**Remote-mode-only** (`HEALTH_MCP_TRANSPORT=http`): `HEALTH_MCP_GITHUB_CLIENT_ID`,
`HEALTH_MCP_GITHUB_CLIENT_SECRET`, `HEALTH_MCP_ALLOWED_LOGINS`, `HEALTH_MCP_PUBLIC_URL`,
`HEALTH_MCP_HOST` (`127.0.0.1`), `HEALTH_MCP_PORT` (`8800`), `HEALTH_MCP_PATH` (`/mcp`).

---

## ✅ Offline tests

From a clone (these touch **neither** your real database **nor** real GitHub credentials —
they use a temp DB and dummy config):

```bash
python test_tools.py     # exercises the tools end-to-end
python test_wiring.py    # exercises the optional remote (OAuth) wiring
```

---

## 🌐 Remote mode (optional)

You don't need any of this to use HealthLedger — it's for people who want to reach one
instance from a networked client (e.g. a web-based assistant) instead of running it
locally. Set `HEALTH_MCP_TRANSPORT=http` and it becomes an OAuth-protected HTTP server:

```
client ──HTTPS──► reverse proxy / tunnel ──► 127.0.0.1:8800  (HealthLedger, http mode)
                        │                            │
                  GitHub OAuth                SQLite 0600 / WAL
                 (allow-list only)             + audit.log
```

- Binds `127.0.0.1` only; expose it via your own reverse proxy or a Cloudflare Tunnel.
- **Auth**: OAuth 2.1 via FastMCP's GitHub OAuth proxy. Only the GitHub logins in
  `HEALTH_MCP_ALLOWED_LOGINS` may connect — everyone else gets `401`.
- **Fail-closed**: in http mode the process refuses to start without client id/secret and
  at least one allow-listed login. There is no open networked mode.
- Health check: an unauthenticated request returns `401` (up and guarded).

**Live demo:** a single-tenant instance runs at `https://health-mcp.manticthink.com/mcp`
(allow-listed to the maintainer). It's there to show the remote path working — to actually
use HealthLedger, run your own local copy per [Quick start](#-quick-start).

---

<div align="center">
<sub>Local-first · model-agnostic · built on the Model Context Protocol &amp; SQLite.</sub>
</div>
