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

Operations uses tabs with **Scheduling assistant** selected first. In `/test`,
**WhatsApp & attendance**, **Record an absence**, and **Needs rescheduling** each
have their own tab. Recent absences is alongside the absence form. Switching
tabs preserves entered form values; arrow keys, Home and End navigate the tabs.
The calendar's WhatsApp offers shortcut opens the WhatsApp tab for its selected
date. This layout does not change scheduling or messaging behavior.

For the separate fictional coworker test (daily draft → admin publication →
WhatsApp → replies → replacement review → attendance → weekly progress), see
[`flow_test/README.md`](flow_test/README.md). Run `python -m flow_test.app` for
preview, or configure its ignored recipient file and use `--live` for actual
coworker messages. Its database is isolated. The authorized test Zoho importer and
separate manual Scheduler export are documented in `flow_test/README.md` and
`flow_test/EXPORT.md`; default preview does not call Zoho.

The existing **Operations → Needs rescheduling → Possible replacements** drawer
supports admin reservations. **Choose patient** opens the full searchable directory
by default; **Suggested patients** retains the existing candidate list. Select a
patient, review warnings, and use **Confirm without messaging** (or **Reserve anyway
without messaging**) to reserve the slot immediately. **Confirm with messaging** is
displayed but disabled. These controls also open when selecting a released calendar
appointment or after recording a cancellation. This is part of the main UI served by
`python app.py`, not only the separate response-demo screen. Reservations are stored
in the configured application database; use a separate local test database when
trying fictional patients.

To open the main UI against the original configured database for local review, use
`python app.py --local-ui`. This mode creates only the two additive admin reservation
tables if missing; it skips source imports, unrelated startup migrations, and the
background WhatsApp sender. Existing patients, therapy plans, and appointments are
not modified on startup. Clicking Confirm/Reserve in this mode does save to that
configured original database.

Double-click a blank Day or Mon–Sat calendar cell to add an appointment directly.
The drawer prefills its date/time and the doctor when selected from a doctor column.
Choose the doctor, therapy, branch, and patient; review warnings and confirm without
messaging. Enter/Space on a focused blank cell opens the same form. Existing bookings
are preserved, including ones hidden by filters; any conflicts are shown for explicit
admin override. Previewing an empty cell does not create any database records.

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

## Docker

Build the image from the repository root. Secrets are deliberately excluded from
the image, so pass the ignored local `.env` file only when running the container.

```powershell
docker build -t therapy-scheduler:latest .
docker run --rm -p 8765:8765 --env-file .env therapy-scheduler:latest
```

This uses `THERAPY_SCHEDULER_DATABASE_URL` from `.env`, which is suitable for
the configured Azure PostgreSQL database. To use a persistent local SQLite
database instead, override that variable and mount a Docker-managed volume:

```powershell
docker run --rm -p 8765:8765 --env-file .env `
  -e THERAPY_SCHEDULER_DATABASE_URL=sqlite:////app/data/eic_scheduler.db `
  -v therapy_scheduler_data:/app/data therapy-scheduler:latest
```

Open `http://127.0.0.1:8765`. The container listens on all interfaces internally;
the published port remains local to the Docker host unless you change the command.

## WhatsApp webhook and template test

Meta verifies `GET /webhooks/whatsapp` using `WHATSAPP_VERIFY_TOKEN`. Incoming
`POST /webhooks/whatsapp` events require Meta's `X-Hub-Signature-256` and are
acknowledged without changing appointments. Configure `WHATSAPP_VERIFY_TOKEN`
and `WHATSAPP_APP_SECRET` as deployed secrets before adding the callback URL in
Meta.

An authenticated template **Help** button tap now queues the fixed plain-text
reply: `Do you want to reschedule or contact staff?`. It replies to the tapping
user on the configured `WHATSAPP_PHONE_NUMBER_ID`, not the test recipient. The
HTTP handler acknowledges after saving the event; a background worker sends the
reply. Only fresh button events are handled, not ordinary text or delivery statuses.
The additive `whatsapp_help_replies` table stores a hashed inbound message ID to
deduplicate retries across restarts. Recipients are cleared after each attempt.
Pending events survive restarts; ambiguous/in-progress send attempts are not
automatically retried because Meta may already have accepted them. A new button
tap can request another reply. This question does not reschedule appointments or
notify staff; answering it has no automated follow-up yet.

Authenticated reply-button taps also persist in `whatsapp_button_clicks` in the
configured application database (Azure PostgreSQL in production). Each row keeps
`user_name` (Meta's WhatsApp profile name, nullable if absent), `phone_number`,
`clicked_button`, `button_payload`, `message_id`, `phone_number_id`, `clicked_at`,
and `recorded_at` (timezone-aware instants written/read with `Asia/Kolkata`). The
`time_zone` column records `Asia/Kolkata`; PostgreSQL also physically stores the
generated `clicked_at_ist` and `recorded_at_ist` columns as Indian local time, so
direct SQL reports need no conversion. PostgreSQL internally normalizes
`timestamptz` instants to UTC; the original `clicked_at`/`recorded_at` display in
the SQL session's timezone, while the `_ist` columns always display Indian time.
Startup migrates legacy naive UTC timestamps without shifting the actual instant.
Use the following query (do not apply the old UTC-to-IST conversion again):

```sql
SELECT user_name, phone_number, clicked_button,
       clicked_at_ist, recorded_at_ist, time_zone
FROM whatsapp_button_clicks
ORDER BY clicked_at DESC;
```

Names are not verified patient names. Template quick
replies and interactive reply buttons are supported; ordinary messages, delivery
statuses, and URL/call buttons are not recorded as reply-button taps. Retries
deduplicate by the receiving business phone ID and inbound message ID. Old taps
are stored even if they cannot trigger a fresh Help reply. These personal details
remain in the database, not application logs or a public read endpoint; retention
and authorized reporting should be agreed before wider production use. Existing
callbacks cannot be backfilled because their contact details were not retained.

The one-off template test is deliberately not exposed as a browser API. With
the sender credentials, approved template name, and test recipient configured
in a trusted shell, run:

```powershell
python -m services.whatsapp_test_sender
```

It uses `WHATSAPP_ACCESS_TOKEN`, `WHATSAPP_PHONE_NUMBER_ID`,
`WHATSAPP_TEMPLATE_NAME`, `WHATSAPP_TEMPLATE_LANGUAGE`, and
`WHATSAPP_TEST_RECIPIENT`. It prints only Meta's message ID after a successful
request. Do not use this command for patient messaging until guardian contact,
consent, and appointment workflow rules have been approved.

## Offline parent-response scheduling demo

For a clickable demo with a larger dataset, run:

```powershell
python app.py --response-demo
```

Open `http://127.0.0.1:8766` and click **Run scheduler**. The screen uses the real
reply-processing and scheduling service against a separate fictional SQLite database.
Tabs show the original and updated Monday appointments, who filled each released
opening, parent replies, weekly progress, and unsaved alternative suggestions.
After running, open **Released openings** and click a time to open **Choose patient**
by default, or click **Confirm suggestion**. Search the full fictional patient list,
review the selected visit and any warnings, then use **Confirm without messaging**
to reserve it immediately. If there are rule mismatches, the button becomes
**Reserve anyway without messaging** and saves the admin's explicit override.
**Confirm with messaging** is visible but disabled; no messages or Zoho calls occur.
Complete continuous visits are selected by default, with a single-slot option when
needed. Reservations persist when the page is refreshed and update the weekly
counts; superseded suggestions remain in the appointment history. An off-plan
reservation does not count toward another therapy's target. The separate reservation
and audit tables record the chosen patient and the warnings the local admin accepted.
The original unsaved alternatives are hidden after manual changes to avoid showing
stale suggestions. Automatic solver constraints are unchanged by this admin override.
**Reset demo** creates a new fake database so the same scenario can be replayed;
previous run files remain under `outputs/response_demo_live/`.

The deterministic large input is `demo/parent_responses_large.json`: 80 patients,
12 therapists, 2 branches, 3 therapies, 120 therapy requirements, 96 original
appointments, and 24 assumed replies (12 Yes, 12 No, 8 other Monday visits awaiting
a reply). Monday starts with 48 sessions across 32 visits. The example releases 16
session slots, proposes 15 replacement sessions across 11 visits, and leaves one
opening unfilled because eligible candidates are unavailable. No covers the entire
day explicitly in this fixture. Some candidates already have their weekly target
booked, some have a plan starting next week, and some are inactive.

The large demo uses the deterministic `RESPONSE_DEMO_SOLVER_SETTINGS` in `config.py`
with a 30-second maximum per solve so it exercises CP-SAT rather than the production
quick-preview fallback for 100 or more requirements. It does not initialize the
configured application database or start any WhatsApp/Zoho integration. To regenerate
the fictional JSON, run `python -m importers.parent_response_demo_data`.

Test the scheduling response to assumed parent Yes/No replies without connecting
WhatsApp or Zoho or using the configured application database:

```powershell
python -m services.parent_response_demo
```

The fictional input is `demo/parent_responses.json`. The command creates a separate
SQLite database at `outputs/parent_response_demo/parent_response_demo.sqlite` and a
before/after report at `outputs/parent_response_demo/report.md`. Existing output is
never overwritten; use `--output-dir outputs/another_demo` for another run.

The example confirms A's Monday visit, releases B's visit after an explicitly
whole-day No, leaves C's unanswered visit untouched, and proposes D for B's opening.
D's existing Tuesday booking remains fixed. E already has its weekly demand booked;
F needs another therapist. New replacements are tentative (`PROPOSED`), not confirmed.
Alternative times for B are returned as unsaved suggestions within the remaining week.

The `parent_availability_responses` table stores the event ID, linked appointment,
date/slot snapshot, YES/NO reply, explicit unavailability scope, source, and processing
time. The scheduling service deduplicates events and rejects conflicting or stale
replies before changing appointments. It records absences and audit events, preserves
continuous visits, and counts completed and reserved therapy separately against each
weekly target. It adds appointments only in the selected day's released openings;
the existing browser generator's rolling mode is unchanged. The core service is
`services/parent_response_service.py`; callers stage replies with
`record_parent_response`, commit the input batch, then call `process_daily_responses`.

```powershell
python -m pytest tests/test_parent_responses.py -q
```

## Source refresh

The import is intentionally idempotent. To deliberately reload an updated workbook, use a new empty
database or clear the selected database only after taking an approved backup. Do not replace or
modify the source workbook in place while the app is running.
