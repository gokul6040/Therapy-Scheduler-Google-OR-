# EIC Therapy Scheduler

This repository pairs a professional browser-based clinical scheduling workspace with a
deterministic, rule-based scheduling engine. Scheduling decisions are made by Google OR-Tools
CP-SAT; no LLM or generative model is involved.

## Run

```powershell
python -m pip install -r requirements.txt
python -m pytest -q
python app.py
```

Open `http://127.0.0.1:8765` and use **Schedule generator**. By default, the app uses the local
`eic_scheduler_sanitized.db`. The workbook is first copied into the visible `plan_detail`,
`therapist_master`, and `specialty_master` source tables. The scheduler builds its planning data
from those authoritative tables: `plan_detail` supplies treatment demand, `therapist_master`
supplies therapy capabilities and branch eligibility, and `specialty_master` standardizes therapy
names.
Generated schedules cover one Monday–Saturday calendar week. Existing active appointments are
kept in place, new needs fill remaining capacity, and every result is independently validated
before the manager can persist new `PROPOSED` appointments.

## Prototype boundaries

- Session duration and consecutive-break behavior live in `scheduling/slot_config.py`.
- `app.py` launches the browser UI; `web_app.py` exposes the local JSON API and serves `web/`.
- SQLAlchemy supports the local SQLite fallback and PostgreSQL. Production credentials stay outside
  the repository in the `THERAPY_SCHEDULER_DATABASE_URL` environment variable.
- For local development, copy `.env.example` to `.env` and enter the required database or Zoho
  OAuth values there. `.env` is ignored by Git; never add real credentials to source files.
- Location capacities and generated patient availability are explicitly marked as demo data.
- Every workbook therapist is treated as active across the manager-confirmed Monday-Saturday
  operating hours. Capability and branch eligibility come only from `Therapist_Master`; source
  inconsistencies are stored as import warnings rather than silently reassigned.
- The original Streamlit proof of concept is preserved in `app_legacy.py` and is not imported
  by the active application.
- Manager-facing forms support cancellations, replacement suggestions, patient and therapist
  absences, and validated manual moves. Automatic replacement chains remain a later increment.
- Creator sync creates appointment rows only in the configured `All_Therapy_Schedules` form. It
  writes the appointment date plus patient, therapist, therapy type, branch, and therapy plan as
  plain text; it never creates or changes patient, therapist, therapy, branch, or therapy-plan records. Each
  local appointment has a one-time sync record so a retry cannot duplicate it in Creator. The
  manager approved skipping the obsolete Creator form workflow for these API-created rows; this
  bypass does not modify the workflow itself.
- The first Zoho connection stores its refresh token only in the ignored local `.env` secret store.
  Later restarts reconnect automatically with the minimum permissions to create schedule rows and
  read the configured `Plan_Detail_Report`. A manual Plan Detail pull stores Creator data in the
  separate PostgreSQL `zoho_plan_detail_records` staging table; it does not overwrite live
  scheduler source data.

## PostgreSQL setup

After Azure provisioning is complete, set the connection string for the current PowerShell session
and load the source workbook. Replace the placeholders and URL-encode a password containing URL
special characters.

```powershell
$env:THERAPY_SCHEDULER_DATABASE_URL = "postgresql+psycopg://<admin-user>:<password>@therapy-scheduler-poc-gokul.postgres.database.azure.com:5432/postgres?sslmode=require"
python initialize_database.py
python app.py
```

The initializer creates the schema, stores the three approved workbook sheets as visible source
tables, then builds the scheduler projection only when the target database has no patients. It never
overwrites an existing schedule database.

## Source refresh

The import is intentionally idempotent. To deliberately reload an updated workbook, use a new empty
database or clear the selected database only after taking an approved backup. Do not replace or
modify the source workbook in place while the app is running.
