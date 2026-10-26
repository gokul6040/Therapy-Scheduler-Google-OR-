# Manual final schedule push to Zoho

The only write target is the user-authorized `nidhin.kumar_turnb/test-gokul`
**Scheduler** form, displayed in **Scheduler_Report**. Each therapy appointment
has one row. A child with two therapies can therefore have two rows. The source
importer and `orscheduler-dailyjob` still only read plans/consultants and draft;
they do not initiate this export. No new Azure resource or daily timer is added.

## What the manual cutoff means

The user selected a fixed daily cutoff eventually, but **manual execution for now**.
Running the push with `--apply` is that cutoff for the chosen therapy date:

- Already-received queued replies are processed first. Explicit No releases the
  child as before; the selected replacement is used instead of that child.
- Confirmed, completed and no-show appointments can be exported with their actual
  status. Sent unanswered offers become confirmed, with a distinct `flow_assumed_yes`
  audit and `ASSUMED_CONFIRMED` export status. No fake parent Yes event is recorded
  and no WhatsApp acknowledgement is sent for assumed acceptance.
- Unsent/waiting/uncertain/failed-delivery offers, changed offer snapshots and
  Help/Reschedule replies remain for staff review. They are not assumed accepted.
- Normal ten-hour expiry still alerts staff and retains bookings. The explicit
  manual cutoff can finalise unanswered sent offers independently of that deadline.
  Later actual replies/changes can affect the schedule and are reflected on the
  next manual push; this does not freeze appointment history forever.
- Preview never finalises or pushes. It shows changes already ready, pending IDs,
  how many unanswered visits would be assumed accepted, and the last push summary.

## Calendar button

After deployment, open **/test → Calendar**, choose the therapy date, then click
**Push to Zoho**. Review the date and unanswered offers, then confirm **Push to
Zoho**. This is the same manual cutoff as the CLI. It exports the saved schedule
for that one date across all filters; an unsaved generated preview is not exported.
Week/agenda view still exports only the selected calendar date stated in the dialog.

Success shows added/updated rows, assumed visits, remaining staff review and the
last successful push in IST. An interrupted push can be retried for the same date;
existing batches and unique keys prevent duplicates. If a batch belongs to another
date, return to that date and complete it first. No timer or new Azure resource is
created. Incoming replies can be stored and calendar reads continue during Zoho HTTP.

The button needs the two fields and OAuth consent below. The existing deployment
script now copies the saved test OAuth client/refresh token into additive Azure
secrets and test-prefixed environment references on the app. These values are never
sent to the browser. Redeploy after changing the local test refresh token:

```powershell
& .\flow_test\deploy_azure.ps1
```

The authenticated POST `/test/api/zoho/export/push` accepts `{ "date": "2026-10-24" }`.
It returns the same applied summary as the CLI; the existing `/export/preview`
remains read-only. Errors retain resumable export state and do not advance the
completed push checkpoint. A second concurrent calendar push returns HTTP 409.

## Two required fields

Add these Single Line fields to the Scheduler form:

| Display name | Field link name | Property |
|---|---|---|
| Scheduler Key | `Scheduler_Key` | Enable **No duplicate values**; default 255-character length is sufficient |
| Status | `Status` | Single Line |

Registration ID identifies a child, not a particular appointment. The Scheduler
Key identifies the local appointment across retries and edits. Status preserves
history when a previously exported appointment is released, cancelled or replaced.
Filter the report to `CONFIRMED`/`ASSUMED_CONFIRMED` for upcoming accepted sessions;
include `COMPLETED`/`NO_SHOW` when viewing attendance history. Old withdrawn rows
are updated, not deleted. Unrelated existing Zoho rows are never adopted or changed.

The verified original report fields are `Reg_ID`, `PatientName`, `Therapy_Date`,
`Doctor`, `TherapyTime`, and `Added_Time`. Its sample formats are `24-10-2026`,
`10:40 AM`, and `23-Oct-2026 11:05:33`. Zoho supplies Added Time; we do not assign
the provider's system field. New rows and later updates are selected from saved
scheduler state, not by filtering Zoho's Added Time, which cannot detect an edit.

## One-time connection update

The saved connection was verified to grant only `form.CREATE` and `report.READ`.
Updating existing schedule rows also needs `report.UPDATE`. Reading form definitions
lets the exporter validate fields and unique-key enforcement even when the report
is empty, and requires `meta.form.READ`.

```powershell
.\.venv\Scripts\python.exe -m flow_test.zoho_authorize
```

Open the printed Zoho URL and approve those permissions. The helper uses the
existing client and registered loopback callback, checks the one-time state, and
atomically saves only `FLOW_TEST_ZOHO_REFRESH_TOKEN` in ignored `flow_test/.env`.
It does not change the main refresh token or write schedule records. If a local
app occupies port 8765, stop that local process first. No credentials need retyping.
The deployment helper also honours these test-prefixed credentials for the existing
source job, which still only runs the source importer.

## Deploy, preview, then apply

Deploy the latest application code with `& .\flow_test\deploy_azure.ps1`. If the
Added Time IST / Modified Time IST migration has not been applied, run
`.\.venv\Scripts\python.exe -m flow_test.zoho_daily --apply --reconcile` once,
then update the source job with `& .\flow_test\deploy_zoho_job.ps1` (no daily switch).
Existing scheduling data does not need resetting.

For the 24 October 2026 schedule, first review:

```powershell
.\.venv\Scripts\python.exe -m flow_test.zoho_push --date 2026-10-24
```

Then make the manual cutoff and push:

```powershell
.\.venv\Scripts\python.exe -m flow_test.zoho_push --date 2026-10-24 --apply
```

Repeat the same command later to send only new/changed results. Unchanged rows
are skipped. Changed doctor/date/time/status/name data updates the existing keyed
Zoho row. A replacement has its own new key; the old row keeps its released status.

## Checkpoints and failures

State is namespaced in the existing `flow_test_settings` table; the schema remains
19 tables. A frozen batch contains the cutoff (IST), payload fingerprints and
per-row acknowledgements. `completedAtIST` advances only when that batch is fully
acknowledged. `capturedThroughIST` records its earlier snapshot boundary so changes
while HTTP requests are running are not mistaken for already exported changes.

The exporter compares saved fingerprints, including older edited appointments;
it does not rely on Added Time alone. Pending/unexported rows are reconsidered on
later runs. A lost response retains the batch; rerunning finds the unique key and
reconciles or updates the existing row. After resuming an older batch it checks
the latest schedule again, picking up intervening declines/changes. Failures leave
the old completed checkpoint intact. There are no automatic background retries.

Local test reset does not delete Zoho rows. It creates a fresh export namespace
for later appointments; old remote history requires a separate deliberate cleanup.
Current single-process/replica ownership remains required.

## Verification

Run the existing Postman simulation on port 4913, then:

```powershell
.\.venv\Scripts\python.exe postman/checks/verify_scheduler_push.py
```

This uses temporary SQLite, dummy credentials and real local HTTP/CLI calls. It
tests OAuth callback state rejection, code exchange/test-only token persistence,
schema uniqueness preflight, no-response finalisation, lost-response recovery,
zero-change repeat, decline/replacement and preservation of unrelated rows.
The simulation is based on the inspected test-report shape and official Creator
Add Records, Update Record by ID and Get Fields contracts. It is not an
owner-published mock and does not prove live scope grants, workflows or writes.
The full regression suite passed **397 tests**; 15 focused export tests cover
cutoff decisions, late/ambiguous replies, partial recovery, cancellations, moves,
authentication, destination restrictions and test-only credential persistence.

No live export, application deployment, OAuth consent or Zoho schema edit has been
performed by the agent. The user must complete the two fields and consent before
the first live push can be verified.
