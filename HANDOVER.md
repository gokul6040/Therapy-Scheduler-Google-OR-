# Thanal EIC scheduling and WhatsApp test — continuation handover

## Setup update — Scheduler fields added, 23 October 2026

The user confirmed adding `Scheduler_Key` and `Status`. A fresh read-only check
confirmed the saved connection still refreshes, but lacks
`ZohoCreator.report.UPDATE` and `ZohoCreator.meta.form.READ`. Therefore API field
definitions/unique-key enforcement could not yet be verified. No records changed.
Next: user runs `python -m flow_test.zoho_authorize`, opens its printed link and
approves the requested permissions, then deploys using `deploy_azure.ps1`.
After consent, verify actual field link names/types and Scheduler_Key uniqueness
before the first live export. Earlier pending-field statements below are historical.

## Latest work — calendar Push to Zoho button, 23 October 2026

Implemented locally at the user's request; **not deployed and no live export was
performed**. The `/test` calendar now has **Push to Zoho**, beside WhatsApp offers.
It previews the selected date and sent unanswered visits, then runs the same manual
cutoff/export as the CLI. Week/agenda views still push only the stated selected
date. All saved appointments on that date are considered across calendar filters;
unsaved generated previews are excluded. The dialog shows success counts, staff
review count and last successful IST push, or a retryable actionable error.

- New authenticated POST `/test/api/zoho/export/push` accepts `{date: YYYY-MM-DD}`.
  `flow_test/zoho_calendar.py` calls the existing shared `zoho_push.run_push` via
  in-process state helpers, avoiding self-HTTP deadlock. Scheduling locks are held
  only for state changes, not external HTTP. Overlapping calendar pushes reject.
- OAuth stays server-side. `deploy_azure.ps1` now reads saved test OAuth credentials
  and deploys additive `flow-test-zoho-*` secrets and test-prefixed env references.
  No credentials go into browser configuration or images. Main settings remain.
- Existing unique keys, preflight, frozen batches, per-row acknowledgements,
  unknown-send recovery and CLI remain. Missing fields/scopes block before cutoff.
  Source job remains manual and unchanged; no new Azure resource or timer.
- Test workspace banner now correctly states that writes go to `test-gokul` only.

Validation: **401 tests passed** in 219.63s. The real calendar HTTP endpoint also
passed the existing Postman simulation: it resumed an uncertain CLI create without
duplicates, skipped unchanged repeats, and patched a declined row/added its replacement.
The real browser button/preview/confirmation passed using temporary SQLite and dummy
OAuth against the local simulation. No real messages or live Zoho writes were sent.

External setup from the previous section is still pending: Scheduler_Key (unique),
Status, and one-time `python -m flow_test.zoho_authorize` consent for update and
form-definition scopes. Then the user runs `& .\flow_test\deploy_azure.ps1` and uses
the calendar button. No database reset or source-job redeployment is required for
this button. See `flow_test/EXPORT.md`.

## Latest work — manual final-schedule export, 23 October 2026

The user has now authorized Zoho output **only in the existing test app**:
`nidhin.kumar_turnb/test-gokul`, form `Scheduler`, report `Scheduler_Report`.
Form URL was supplied explicitly after the report URL. Production remains excluded.
See `flow_test/EXPORT.md` for the complete user guide and commands.

The user clarified that the final schedule should be pushed after a daily cutoff:
confirmed people remain, declined people are replaced, and no reply means assumed
Yes. They chose **manual runs for now**, with a fixed IST daily time to be chosen
later. The new `zoho_push --date YYYY-MM-DD --apply` command is that manual cutoff;
it is NOT added to `orscheduler-dailyjob` or an automatic timer.

Implemented locally:

- `flow_test/zoho_export.py`: authenticated preview/prepare/ack/finish, finalisation,
  durable batches/fingerprints/row mappings/IST last-push time in existing settings.
- `flow_test/zoho_push.py`: preview default, explicit apply; pins form/report/owner,
  verifies form schema/unique key, creates new rows or updates owned existing rows.
  No workflows are skipped and no existing unrelated rows or history are deleted.
- Already-persisted queued parent replies are processed before cutoff. Sent silent
  offers become CONFIRMED locally, with `flow_assumed_yes` audit and distinct
  `ASSUMED_CONFIRMED` output status. This never invents a parent response or sends
  an acknowledgement for silence. Help/unsent/waiting/unknown/failed-delivery/changed
  offers stay for staff. Late/stale/ambiguous replies are not silence, even when
  their inbox item has been resolved; an explicit booking decision is required.
- The ten-hour expiry worker still alerts staff/retains bookings. Manual finalisation
  is an explicit separate action and does not activate daily scheduling.
- The first completed push sends finalised rows. Next pushes compare fingerprints
  (including edits to older appointments), not Zoho Added Time alone. Released or
  cancelled previously exported rows get an updated status; replacements get their
  own row. Date moves update the original key even when pushing its former date.
- Unchanged rows are skipped. Partial/uncertain batches survive restart, retaining
  the old successful checkpoint. Rerun reconciles by the unique key, finishes the
  frozen batch, then checks intervening changes. Completed/cutoff times display IST.
- `flow_test/zoho_authorize.py`: one-time user consent using existing client and
  registered loopback callback. Atomically stores only the test-prefixed refresh
  token in ignored `flow_test/.env`; main token/settings remain. Callback logs do
  not print code/token, one-time state checked. The existing job deployment helper
  now honours test-prefixed OAuth settings too; the source job still only reads.
- The original report's actual fields/formats were read: Reg_ID, PatientName,
  Therapy_Date (`24-10-2026`), Doctor, TherapyTime (`10:40 AM`), Added_Time
  (`23-Oct-2026 11:05:33`). Zoho sets Added Time; exporter never assigns it.

**External setup remains pending. Do not claim a live push succeeded.**
The last read-only check still found one fictional sample and no Scheduler_Key/Status.
The user was asked to add Single Line `Scheduler_Key` (No duplicate values) and
Single Line `Status`, then asked why. Explained: one appointment key prevents
duplicates/retries; status preserves cancelled/replaced history. They have not yet
confirmed adding the fields. They need to save these form changes before preflight.

The saved token's actual scopes are only `ZohoCreator.form.CREATE` and
`ZohoCreator.report.READ`; metadata requests returned 401/code2945 invalid scope.
Updating an existing row needs `report.UPDATE`; validating the field/unique-key
definition needs `meta.form.READ`. Run the new authorize helper for all four using
existing client credentials, with user completing the Zoho consent themselves.
The helper is prepared/tested but has NOT been run against the real OAuth account.
There is no need for the user to repeat client secrets. No root .env token was changed.

Validation: **397 tests passed in 217.09 seconds**, including 15 focused export
tests and the late/ambiguous reply guard. Real CLI and
shared HTTP calls against the local Postman simulation passed: OAuth state rejection,
code exchange/test-token storage, unique-key preflight, lost response after accepted
create without duplicate, zero-change repeat and decline/replacement updates.
Postman request logs show actual POST/PATCH/metadata calls. All contacts/OAuth are
dummy and storage temporary. Official Zoho Get Fields/Add Records/PATCH contracts
and the inspected report supplied the mock; Orbit discovery was unavailable.

No live database reset/import, OAuth consent, schema modification, app deployment,
new cloud resource, real WhatsApp send or actual Scheduler write was performed in
this export work. Core OR is untouched by it. Storage remains 19 tables.

Next steps after fields are added:

```powershell
.\.venv\Scripts\python.exe -m flow_test.zoho_authorize
& .\flow_test\deploy_azure.ps1
# If the preceding source time-zone migration has not been applied:
.\.venv\Scripts\python.exe -m flow_test.zoho_daily --apply --reconcile
& .\flow_test\deploy_zoho_job.ps1
.\.venv\Scripts\python.exe -m flow_test.zoho_push --date 2026-10-24
.\.venv\Scripts\python.exe -m flow_test.zoho_push --date 2026-10-24 --apply
```

Run one at a time, stop on error. The preview does not finalise or write. Apply
changes local pending offers as described and writes only the authorized test form.
No automatic daily trigger is enabled. The full CLI/HTTP check is
`postman/checks/verify_scheduler_push.py` with the existing simulation on port4913.

## Latest source timestamp change — 23 October 2026

The user initially said Added Time is now IST, then explicitly clarified that
**only Added Time changed**. Modified Time remains UTC. This section supersedes
older statements below that both fields use UTC.

- `SOURCE_TIMEZONES` declares Added_Time=Asia/Kolkata, Modified_Time=UTC.
  Added Time's date supplies treatment start directly; no second +05:30 conversion.
  Modified Time still selects the latest plan. Checkpoints stay UTC instants.
- Every read formats Added Time bounds in IST and Modified Time bounds in UTC.
  Import compares both as instants, including chronology and half-open boundaries.
- `/test/api/zoho/status` now returns `sourceTimezones` (a field map), replacing
  the older singular `sourceTimezone`. Incremental clients must send the same map.
  Deploy checks and the job client require it, preventing old clients from skipping
  records or advancing the checkpoint under the obsolete interpretation.
- An existing old cache/checkpoint requires one `--apply --reconcile` run after
  app deployment. Retained cached rows are converted from their previous zones
  before merging. Absent rows still are not deletions. Saved appointment IDs,
  statuses and requirement references are preserved; no reset is required.
- Tests cover midnight dates, mixed-field inclusive/exclusive boundaries, actual
  chronology, obsolete client rejection, legacy cache recovery and retained rows.
  Postman fixtures now use IST Added Time with UTC Modified Time and evaluate
  each field's criteria independently.
- The real CLI/shared HTTP flow passed against local Postman simulations: legacy
  contract blocks incremental sync, explicit reconciliation succeeds, then an empty
  delta returns 0/0 without duplicates. Help/replacement/attendance and source-503
  checkpoint preservation still pass. No actual messages or live database writes.
- A fresh read-only Zoho fetch validated 13 patients and two consultants, with no
  Modified-before-Added instant errors under the user-confirmed contract.
  A real bounded API pull with the new per-field criteria also returned 13/2 and
  passed import-window validation without writing to the live database.
- Full regression: **382 passed in 224.14 seconds**. All changes remain local; the user
  runs deployment commands. The agent has not reset/imported live data, deployed
  or changed the manual trigger.

Required order for this code update (stop at any failure):

```powershell
& .\flow_test\deploy_azure.ps1
.\.venv\Scripts\python.exe -m flow_test.zoho_daily --apply --reconcile
& .\flow_test\deploy_zoho_job.ps1
az containerapp job start --name orscheduler-dailyjob --resource-group TherapyScheduler
az containerapp job execution list --name orscheduler-dailyjob --resource-group TherapyScheduler --output table
```

## Latest readiness audit — 23 October 2026, local changes awaiting deployment

This section supersedes older status statements below. The user explicitly
approved the narrow shared OR search fix: bounded one-day/replacement runs now
consider all valid candidate times instead of retaining two starts per day.
Unbounded weekly search retains its shortcut. Clinical constraints, assigned
doctors, breaks, capacity and booking/progress preservation remain in force.
The 100-patient/10-consultant experiment improved from 40 to 100 sessions; blocking
one doctor-slot gives 99. Both quick and CP-SAT paths have regression coverage.

The wider audit fixed stale-preview saves, per-parent closed contact windows,
delayed on-time replies, callback persistence during slow sends, long complete-visit
messages, invalid source entries/durations, uncertain-send staff warnings,
deployment mode preservation and exact-image readiness checks. Manual empty-cell
booking now works without querying absent optional mapping tables in the existing
19-table test schema; absent mappings grant no additional capabilities.

One closed contact window no longer blocks the entire schedule save. That parent's
offer waits visibly; other parents proceed. Verified fresh inbound contact resumes
the waiting offer. A timely delayed reply can resolve a retained expired offer,
but an explicitly released/closed offer never reopens.

See `READINESS_AUDIT.md` for evidence, limitations and production gates. The
reusable local HTTP check is `postman/checks/verify_daily_flow.py`; start the normal
Postman simulation on 4913 and the failure simulation on 4914 as documented in
`flow_test/README.md`. It passed after the final functional changes: eight source
patients, seven initial offers, repeated-save deduplication, Help/staff resolution,
Yes/completed attendance, No/replacement/Yes/no-show and weekly progress. A simulated
503 preserves the checkpoint and existing appointments. No real message was sent.
UI verification used temporary fictional SQLite and a network-free dummy sender;
the screenshot is `outputs/audit_waiting_offer.png`. JavaScript syntax and the
scoped design detector pass; no design suppressions were added.

Final full regression: **373 passed in 227.74 seconds**, outside the Windows
sandbox for temporary PowerShell scripts, file writes and loopback checks. An
earlier run found a test-clock failure after midnight: its fixed 22-Oct appointment
became a past date, correctly blocked by the live send guard. The test now fixes
that clock; application past-date protection was retained. All 12 focused
readiness tests also pass.

No live reset/import, deployment, real WhatsApp send, Zoho write or trigger change
was performed. User runs deployment commands; no reset is required:

```powershell
& .\flow_test\deploy_azure.ps1
& .\flow_test\deploy_zoho_job.ps1
az containerapp job start --name orscheduler-dailyjob --resource-group TherapyScheduler
az containerapp job execution list --name orscheduler-dailyjob --resource-group TherapyScheduler --output table
```

Last read-only Azure observation: app `orscheduler`, ready revision
`orscheduler--0000020`, image `therapy-scheduler:test-flow-20261022-153956`, messaging
mode `session`; these local changes are newer. Job `orscheduler-dailyjob` remains
Manual. Source conversion succeeded, but a direct PostgreSQL source-identity check
timed out: earlier TEST-K/L corrections were not freshly verified. Recheck rather
than assuming they are resolved; do not change firewall rules or reset data.

Before production: deploy/retest actual Meta and Azure PostgreSQL behavior, configure
and verify the approved proactive template, add staff authentication/permissions and
attributed audit identities, prove alerts/freshness/backup restoration/recovery,
agree source deactivation and staff conflict-resolution workflows, and retain one
process/replica until durable multi-process coordination exists. Daily activation,
Zoho write-back, scheduled reminders and the test assistant remain deferred.

Snapshot verified on **21 October 2026, Asia/Kolkata**. Recheck live state before
making changes: the user can deploy or send replies between chats.
This document intentionally contains no passwords, admin keys or access tokens.

## Latest local update — 22 October 2026, awaiting deployment

The user requested expanding the test beyond Malaparamba, using Added Time as
treatment start, following original OR capacity, warning on booked plan changes,
ten-hour offer replies, parallel message batches and resilient preprocessing.
These changes are implemented LOCALLY and have **351 passing tests**. They have
not been deployed by the agent. No live import, reset or WhatsApp send was run.
The existing main OR files and the 19-table schema are unchanged.

- `flow_test/zoho_import.py` accepts branches from the two authorized test reports,
  matches consultants by branch/name/consultation type, and creates separate
  locations. Capacity follows active consultant count, matching original S-03;
  three active consultants can have three simultaneous sessions when otherwise valid.
- Each latest plan starts on Added Time's Asia/Kolkata date, interpreting the
  source timestamp as UTC. Modified Time still chooses the latest record and
  drives incremental reads. The former shared start parameter is compatibility
  only and cannot override Added Time. Some existing 21-Oct source plans will
  therefore start on 21 October instead of the old shared 22 October on next import.
- Text cleanup handles leading/trailing/repeated spaces, tabs, Unicode width,
  nonbreaking spaces and common invisible paste markers. Branch/consultant/
  therapy matching ignores case; Registration ID case is preserved. Missing
  data, ambiguous matches and unsupported formats still fail atomically.
- The user confirmed the 2→3 example: apply the new weekly target, retain two
  bookings and completed progress under the same requirement ID, and schedule
  one more session. Frequency/date/duration/order changes with history now
  produce warnings in the import response and existing staff inbox, without
  rejecting the whole import. Removal sets historical therapy demand to zero,
  preserving appointments. A changed doctor/branch on a requirement with history
  stays pending staff review while other records import; it does not silently
  rewrite the saved assignment. Consultant identity/capability/status history
  guards remain. Old source rows still cannot change Registration ID.
- The default reply window is 600 minutes / ten hours after sending. Existing
  sent offers retain their recorded expiry. Local ignored test settings and the
  deployment helper now explicitly use 600 minutes.
- `FlowTest.process_batch` sends up to five different parent numbers concurrently
  (`FLOW_TEST_SEND_BATCH_SIZE`, 1–20); at most one message per phone per batch.
  Live repeated sends to a shared number are ordered and paced six seconds apart.
  Thus A–H on the SAME phone will still be sequential. HTTP threads never share
  sessions; the workflow lock coordinates reset/import/replies and send results.
  Existing validation, per-offer expiry and UNKNOWN/no-blind-retry behavior remain.
  The old single-job processor/drain remains for deterministic preview tests.
- Job `orscheduler-dailyjob` must stay MANUAL. Its launcher now uses
  `python -m flow_test.zoho_daily --apply`, without a shared start date. Status
  includes `planStartSource: Added_Time`; local clients and deployment readiness
  check this before proceeding. Existing old job arguments are accepted by the
  new server for compatibility.

Validation: full suite **351 passed** outside Windows sandbox (PowerShell temp
scripts, atomic temp-file writes and loopback checks are restricted inside it).
New tests cover branch/reset/message identity, Unicode cleanup, three simultaneous
sessions, the user's 2→3 example, reductions/removals, warnings, batch concurrency,
same-phone order/cooldown, reset during a send and ten-hour expiry. The actual sync
CLI ran against the existing local Postman mock and temporary preview DB with
dummy credentials: bootstrap, checkpoint recovery, 9 patients / 3 consultants /
11 requirements and a subsequent empty incremental window; no real messages.
The command without `--plan-start` also passed validation against the simulation.
All temporary HTTP/simulation servers were stopped.

User runs deployment commands themselves; no reset is needed:

```powershell
& .\flow_test\deploy_azure.ps1
& .\flow_test\deploy_zoho_job.ps1
az containerapp job start --name orscheduler-dailyjob --resource-group TherapyScheduler
az containerapp job execution list --name orscheduler-dailyjob --resource-group TherapyScheduler --output table
```

The last read-only live check before this change found 12 imported patients and
checkpoint `2026-10-22T10:20:30Z`. Later failures were identity edits on existing
Creator rows: TEST-L→TEST-M and TEST-K→TEST-L. User was told to restore original
IDs and add M as a new row, then rerun. Recheck live/source state rather than
assuming those edits are already corrected. No current source data was changed
by the agent. The contact-window whole-save rule and tomorrow-only publication
are unchanged; adding concurrency does not bypass either.

Older snapshots below are historical and may be superseded by this section.

## Latest continuation — incremental Zoho sync and variable records

This section supersedes the fixed-count/full-snapshot limitations in the initial
Zoho implementation described below. User requested these changes explicitly.

LATEST USER INSTRUCTION: imported Parent No. values should work without a second
manually approved number list. A new TEST-K phone ending 3767 caused two manual
jobs to fail after reset; only the old phone ending 7484 was in that old pool.
The failure was validation, not Zoho/Azure downtime; live sync/checkpoint remained
null after the reset. The original static phone restriction has now been removed
LOCALLY for Zoho imports. `FlowTest.use_source_contacts` refreshes parent bindings,
transport and webhook routing; source contacts survive restart without local
entries. Existing offers retain their original phone/reply routing when a source
phone changes. Unknown arbitrary contacts cannot send/receive through an offer,
and another family's reply cannot confirm it. Optional fixture contacts remain.

Verification: **334 tests passed**. New tests cover import/send/reply with a new
number and no file entry, phone edit with old-offer routing, restart, wrong-family
reply rejection, unknown contacts, and failed-import contact rollback. Actual CLI
through the local Postman HTTP simulation accepted a new dummy source phone with
no local entry and created only the unsaved draft; 0 messages/offers were sent.
Temporary servers stopped after verification. No live import/message/deployment
was performed by the agent. Deploy the updated app, then run the existing manual
job to import TEST-K; no reset/reconciliation is required for this contact change.
Job DTO/command is unchanged, so the existing UTC-fixed job client is compatible.

The user asked what other restrictions exist. `flow_test/RESTRICTIONS.md` now
documents the current boundaries/defaults: test app/branch, one shared start
date, capacity 2, default Mon-Sat availability, tomorrow-only offer-on-save,
session-window whole-batch requirement, offer expiry, history review guards,
atomic import, disabled features and unchanged OR rules. Do not silently remove
these unrelated rules. Daily trigger is still manual per the user's instruction.

CURRENT USER PREFERENCE: keep `orscheduler-dailyjob` MANUAL. The user explicitly
does not want the daily trigger enabled yet and has more questions to clarify.
Do not enable it based on the successful tests or earlier setup instructions.

Latest live verification after the user's UTC fix deployment/reconciliation:
job image `therapy-scheduler:test-flow-20261022-124547`, trigger Manual, corrected
execution `orscheduler-dailyjob-s2vxv5f` Succeeded. Live test sync retains
9 patients, 2 consultants, 11 requirements, 23 weekly sessions; checkpoint
`2026-10-22T07:19:46Z`, sourceTimezone UTC, reconciliationRequired false,
changed false (no changes after the user's successful reconciliation).
The reconciliation produced 11 draft sessions for 2026-10-23 and sent 0 messages.
This supersedes the earlier local-only UTC fix/deployment-needed notes below.
No reset, import, deployment or trigger changes were performed by the agent
during this verification; only live read-only checks and this handover update.

LATEST: `orscheduler-dailyjob` was created by the user with Manual trigger. Its
first execution `orscheduler-dailyjob-xsb6hjj` is Succeeded (verified in Azure),
but the job downloaded 0/0 and left live projection at 8 patients/2 consultants,
10 requirements/21 weekly demand. The full authorized Zoho read has 9 patients:
new TEST-I has Added/Modified Time `22-Oct-2026 06:54:24`. A read-only query for
the actual UTC interval 06:50:36–07:01:19 returned TEST-I; the old IST-converted
query returned none. The test source timestamps are UTC; clinic dates stay IST.

Fixed LOCALLY: source UTC criteria/validation, a persisted
`zoho_test_source_timezone` marker, old-client rejection, and explicit
`--apply --reconcile` recovery. Reconciliation rereads bounded full source,
preserves IDs/bookings/history, and atomically saves source/timezone/checkpoint.
Deploy the updated app, run `zoho_daily --plan-start 2026-10-22 --apply --reconcile`,
then rerun `deploy_zoho_job.ps1` to update the EXISTING job image and test it
manually. Enable daily only after success and correct source totals. Do NOT
reset the database. Expected current Zoho totals: 9 patients, 2 consultants,
11 requirements, 23 weekly sessions. No live writes/deployments by the agent.

Verification for UTC fix: **331 tests passed**; real read-only Zoho window
returned missed TEST-I; actual CLI through local Postman simulation recovered
a backdated row (8/2 bootstrap -> 9/3 recovery -> 0/0 next incremental), with
11 draft sessions and no saved appointments/offers/messages. Temporary HTTP
app/server and Postman simulation stopped after verification. UTC changes still
need deployment; existing Azure app/job image remains test-flow-20261022-121346.

Latest user terminal evidence: the user deliberately reset `/test`, then the
first Zoho import succeeded at 22 October 2026 06:50 UTC. Imported 8 patients,
2 consultants, 10 requirements and 21 weekly sessions; draft date 2026-10-23,
10 draft sessions, 0 messages sent, checkpoint `2026-10-22T06:50:36Z`.
This supersedes the earlier fixture-history blocker below; do NOT reset again.
Job creation then failed locally in Azure CLI's YAML parser, so
`orscheduler-dailyjob` was not created. Windows PowerShell 5's UTF-8 BOM was read
using the CLI's default Windows encoding. Fixed the local job launcher to write
ASCII JSON with Unicode escapes and no BOM (valid YAML). Reproduced the exact
old parser error and verified the fixed bytes with Azure CLI's bundled PyYAML;
two Windows PowerShell compatibility tests passed with dummy values, including
Malayalam/emoji and escaped values. This is a launcher-only fix: no app rebuild,
reset or repeat import is needed. User should rerun `deploy_zoho_job.ps1`, then
start/check the manual execution before enabling the 03:00 IST schedule.

Live deployment checked on 22 October after the user's deployment: image
`therapy-scheduler:test-flow-20261022-121346`, revision `orscheduler--0000018`,
latest and ready match, provisioning Succeeded, 100% traffic to latest. Initial
sync command returned `Not found`; a later authenticated `/test/api/zoho/status`
read succeeded with sync/checkpoint both null. Consistent with revision rollout.
The deployment helper now also waits for the incremental Zoho status contract;
this readiness-only script change needs no app rebuild. Live source is still the
fixture, with 10 appointments on 22 October, 8 offers, 58 queue jobs and no inbox
items. First Zoho import is blocked by fixture history until the user deliberately
resets the isolated test workspace. No reset/import was performed by the agent.

The user selected Azure job name **`orscheduler-dailyjob`**. This is the default
in `flow_test/deploy_zoho_job.ps1` and the name used in manual-run commands.
The name change is local; creating/enabling the Azure job is still pending.

- `zoho_daily --apply` obtains `/test/api/zoho/status` and its saved checkpoint,
  freezes a UTC cutoff, then reads both approved Zoho reports using Added_Time
  OR Modified_Time in a half-open interval. First run has no lower bound.
  Criteria are formatted in source UTC (corrected above). Main read helper now accepts optional
  criteria, handles real empty-window code 9280 (and legacy 3100), and rejects
  repeated pagination cursors. Existing unfiltered calls remain compatible.
- Test import merges rows by Creator ID with `zoho_test_source`; it does not
  replace the dataset with that day's subset. Source/projection/checkpoint
  (`zoho_test_checkpoint`) commit in one transaction. Failed imports/pulls keep
  the checkpoint. Stale concurrent executions fail rather than advancing it.
  Empty deltas are valid after bootstrap; an empty initial source is refused.
- Removed fixed A–H IDs, exact eight/two counts, and TEST CONSULTANT name prefix.
  New valid Registration IDs and any consultant count are supported within the
  pinned test app and Malaparamba branch. New patient phones can use any number
  in the explicitly consented test-number pool, with no per-patient config entry.
  Bindings restore on restart. Unapproved numbers stop import/checkpoint advance.
- Cached source retains old plan rows. For repeated Registration ID, the complete
  latest Modified_Time plan is used, with Added_Time breaking ties. Distinct
  winning rows tied on both timestamps require review. Older schedules are not
  unioned; unchanged requirements keep IDs/progress when a new therapy is added.
  Changing a source row's Registration ID is refused. Booked-plan and consultant
  status changes still require review. Treatment start remains explicitly
  2026-10-22, never Added Time. Room capacity remains independently configured.
- Reset verification derives expected patient/consultant/weekly totals from the
  current source scenario. Deployment contact validation no longer requires
  exactly the original A–H keys. Azure job readiness checks checkpoint support.

Verification: **324 tests passed** (322 in the sandboxed full run; the two
Windows permission-blocked checks passed when rerun outside the sandbox).
Latest-plan tests cover edits to older rows, Added Time tie-breaking, ambiguous
latest versions, and X-to-X-plus-Y updates preserving completed X progress for
both edited rows and new source rows. Real Zoho TEST reads returned 8/2 for bootstrap
and 0/0 for a later no-change window. The full real job client through local
Postman HTTP simulation returned initial downloads 8/2, next downloads 0/0, then
only 1 new plan and 1 new consultant after fixture insertion. Result: 9 patients,
3 consultants, 11 requirements, 22 weekly demand, 11 draft sessions, 0 messages.
A simulated upstream 503 preserved the last successful checkpoint and source.
Tests cover timestamp boundaries, empty windows, rollback, new names/IDs,
consent, restart, newer plan rows and dynamic reset counts.

The user has deployed the importer, incremental sync and latest-plan code in the
image above. The later deployment-readiness check is local only. No Azure job
was created/enabled and no live TEST data was reset/imported by the agent.
The fixture-to-Zoho source-switch protection still applies to the first import.
Temporary simulation/app servers were stopped after verification.

## Latest work — Zoho TEST source and daily import, 22 October 2026

This section supersedes earlier statements that all Zoho reads are excluded.
The user explicitly authorized reading only these two reports:
`nidhin.kumar_turnb/test-gokul/Plan_Report` and `All_Consultants`.
No production source reads or Zoho writes were performed. Both live test reports
were read through the existing saved OAuth connection: eight A–H plans, two
consultants, Added_Time and Modified_Time. Supplied pasted tokens were not usable
with the saved client; the existing saved connection worked, so no new credential
is needed. No credentials are kept in committed fixtures or docs.

Confirmed by user: Schedule fields are therapy, sessions/week, calendar months,
consultant. Semicolon/newline separates requirements. Added Time is NOT treatment
start. **2026-10-22** is the separately confirmed start for these eight test plans.

Implemented locally:

- `flow_test/zoho_import.py`: parser, validation, consent checks and repeatable
  atomic projection. Pin to the test app and A–H. Preserve IDs/history; reject
  switching old fixture appointments to new therapies; reject booked-plan and
  consultant-status changes that require staff review. Same 19-table schema;
  source/scenario/provenance use namespaced test settings.
- `flow_test/zoho_daily.py`: read-only Zoho pulls, optional validation-only mode,
  authenticated import via the existing app's `/test/api/zoho/import`. No direct
  DB access, appointment save, messaging, or Zoho write from this command.
- `/test/api/zoho/status` and `/test/api/zoho/draft`: authenticated metadata and
  tomorrow's unsaved OR preview. The generator loads that draft and retains the
  shared review/save path. Sunday targets are skipped. Reopening refreshes the
  draft against current bookings. Main scheduling/core OR files were unchanged.
- `flow_test/deploy_zoho_job.ps1`: reuse deployed image/environment/registry and
  stored OAuth settings. Manual job by default, `-EnableDailySchedule` sets
  03:00 IST (21:30 UTC). It requires a successful live TEST import first.
- The initial small-test job reads FULL snapshots and compares them; it does not
  implement incremental timestamp-window fetching. This catches edits and avoids
  a missed-day cursor gap. Exactly eight patients/two consultants are supported.

Verification: **303 tests passed**; real authorized Zoho reads validated the
contract; the complete real Zoho read -> temporary local app -> OR path produced
10 sessions, 21 weekly demand, 0 messages. Local Postman simulation additionally
exercised OAuth, two-page reports, and upstream failure. After failure there were
0 saved appointments, 0 offers and only the prior import audit. UI draft reviewed
in browser; screenshot `outputs/zoho-import-calendar-preview.png`. Two standalone
dashboard text contrast/size findings were fixed; no new design ignores added.

**NOT DEPLOYED; no Azure job created or enabled.** Live `/test` still uses the old
fixture. Latest read showed ten appointments on 22 October and parent reply audit
activity. Nothing was reset, overwritten or messaged by this work. User prefers
deployment commands to run themselves. To deliberately start fresh with the new
source, deploy/reset with `& .\flow_test\start_fresh_live.ps1`, then run:
` .\.venv\Scripts\python.exe -m flow_test.zoho_daily --plan-start 2026-10-22 --apply`.
Send a fresh START after reset before staff save. Open Schedule generator in
`/test`, review tomorrow's automatic draft and save. The existing reset helper
remains compatible with imported v3 scenarios and reseeds the imported source
when present. See `flow_test/README.md` for manual/daily job commands and limits.

Temporary preview and Postman servers were stopped after verification. Preserve
all pre-existing dirty changes; do not attribute unrelated scheduling diffs to
this addition. Do not auto-reset on continuation or enable a schedule merely
because the deployment helper exists.

## Current instruction — fresh start in live `/test`, unrestricted A–H (v3)

**Read this before the historical sections below.** The user explicitly requested
the eight children in the live test database and removal of old schedules,
WhatsApp replies and the rest of the test run. This supersedes the earlier
no-reset instruction **only for the isolated live `flow_test` schema**. The user
asked for commands to run; the command below is prepared and tested, not executed
by the agent. No live reset or deployment has run in this change.

```powershell
& .\flow_test\start_fresh_live.ps1
```

This wrapper first runs the existing Azure deployment, then runs:

```powershell
.\.venv\Scripts\python.exe -m flow_test.reset_live --confirm RESET-flow-test
```

The second command can also be used alone for an intentionally new test run once
v3 is deployed. Do not rerun it as a routine refresh: it erases the latest test run.

- Default scenario is now **`malaparamba-fictional-v3`**. A–H are all eligible for
  initial generation; there are no artificial standby defaults or child-time
  restrictions. Inputs remain child, therapy, weekly requirement and assigned
  therapist. Anu handles Speech and Ravi OT across the existing 09:00–16:50 slots
  with configured breaks. OR chooses actual appointment times. A/E each need
  Speech and OT; B/D/F/H Speech; C/G OT. Weekly target = 21 across ten requirements.
  A fresh daily preview produces ten sessions across eight visits, with one
  combined message for A and one for E. Genuine optional availability remains
  supported. Old fixed-time regression fixtures are retained only in tests.
- App startup upgrades v1/v2 inputs to v3 without erasing history. The **separate,
  explicitly confirmed reset** then deletes/reseeds the 19 allowlisted test tables
  in one transaction: appointments, offers, incoming/outgoing jobs, parent
  responses, attendance, absences, inbox, manual reservations, contact windows,
  old audit rows and old fictional inputs. It inserts the eight current children,
  two therapists, their plans, scenario/mode/reset-cutoff settings and one fresh
  `flow_test_reset` audit. No table drop, sequence reset or main/public SQL occurs.
- Reset uses the authenticated `/test/api/reset` endpoint under the same lock as
  the live worker. Live mode additionally requires PostgreSQL and exact schema
  `flow_test`, recognized fictional settings, known patient codes and exact
  confirmation `RESET flow_test`. Cached test previews are cleared after success.
  A failed reseed rolls back all deletions. Old callback timestamps cannot restore
  erased messages or contact readiness; delivery receipts for deleted messages
  are ignored. New parent clicks on old offers still cannot confirm new bookings.
- The client is pinned to the existing Azure app and reads the existing ignored
  admin key. It checks LIVE mode, v3 and exactly A–H before the reset, and checks
  zero appointments/offers/jobs/inbox/completed/booked sessions and remaining 21
  afterwards. It does not retry a reset automatically. Unexpected new activity
  after reset requires inspection, not a blind repeat. Existing credentials and
  configured recipient numbers are unchanged. All eight still use the existing
  consented test number ending 7484.
- After the command succeeds: refresh `/test`, send a **fresh START** from that
  test number, then generate/review/save tomorrow's calendar. No message is sent
  by deployment or reset. Messages already on the phone remain in WhatsApp;
  this clears database history, not phone chat history.
- Validation: **287 tests passed**, including complete reset, rollback, auth,
  cache invalidation, schema isolation, v1/v2 upgrades, no fixed child times,
  eight-offer save, replay cutoff and client protocol tests. Both PowerShell
  scripts parsed successfully. No core scheduler files were edited.
- Last read-only live check before handing over commands: A–D only; 22 October
  has five saved session rows, four offers and three inbox rows; no active queued
  sends. These live counts have not yet been reset. Older eight-child preview
  screenshots and earlier preservation-only instructions below are historical.

## Newest change — eight fictional children, same two therapists (local only)

The user requested eight children named A–H with two doctors. Default
`flow_test/scenario.json` is now `malaparamba-fictional-v2`:

| Child | Weekly requirement | Available time | Initial role |
|---|---|---|---|
| A | Speech 3, OT 2 | 09:00–10:20 | Planned |
| B | Speech 2 | 09:40–10:20 | Planned |
| C | OT 2 | 09:00–09:40 | Planned |
| D | Speech 2 | 09:40–10:20 | Standby for early Speech |
| E | Speech 2, OT 2 | 10:35–11:55 | Planned |
| F | Speech 2 | 11:15–11:55 | Planned |
| G | OT 2 | 10:35–11:15 | Planned |
| H | Speech 2 | 11:15–11:55 | Standby for later Speech |

Anu remains Speech-only; Ravi remains OT-only. Each now has four morning slots,
09:00–11:55, using the unchanged 40-minute sessions and existing break. Capacity
remains two. Fresh default generation produces eight therapy sessions across six
children, with D/H available for replacement scenarios. Total weekly demand for
all eight children is 21; the six-child initial roster has 17.

- **Not deployed yet.** No live DB records or Azure settings were changed for
  this expansion. Run `& .\flow_test\deploy_azure.ps1` to deploy both this dataset
  and the automatic-tomorrow-offer save change described next.
- Startup of the updated app upgrades a marked v1 test store once: adds E–H,
  their five requirements, opens therapist slots 2/3, and audits
  `flow_scenario_expanded`. It preserves all A–D records, bookings, offers, replies,
  absences, attendance, jobs and existing audit entries. It sends no messages or
  generates appointments. No schema change/provisioner rerun is needed. New-code
  collisions and unexpected scenario versions fail for review, without reset.
- All eight local recipient mappings now reuse the existing consented test number
  ending 7484. A–D bindings were preserved; only E–H were added. These ignored
  bindings take effect online when the deployment updates the recipient secret.
  Deployment validation now derives required codes from the scenario instead of
  assuming four children. Readiness also requires the new scenario version, so
  an older four-child revision cannot falsely pass the post-deploy check. Its
  PowerShell syntax and old/new response checks passed without Azure requests.
  No credential or actual phone value is in this document.
- Original four-child regression cases use the explicit fixture
  `tests/fixtures/flow_scenario_v1.json`. New expansion tests cover fresh eight-child
  generation, A/E combined messages, D/H replacement selection, transactional
  upgrade rollback/collisions, unchanged history across all 19 tables, safe
  restart, and the same test-schema isolation. Shared UI save coverage verifies
  eight appointments generate six queued offers, with no standby messaging.
- Validation: **276 tests passed**, followed by the final expansion/storage checks
  (**13 passed**). JS syntax checks passed. Browser verified A–H in the directory,
  two therapist columns, eight sessions and six automatically queued preview offers.
  Screenshots: `outputs/eight_children_directory.jpg` and
  `outputs/eight_children_calendar.jpg`. These are isolated preview records;
  the live 22 October results below remain historical, not overwritten.

## Latest requested change — automatic offers when saving tomorrow (local only)

After completing the live scenario below, the user requested: saving the calendar
should immediately message everyone scheduled for the next day only. Implemented
locally in the shared `/test` calendar save path; **not yet deployed**.

- Generate and review one day as before. If that date is tomorrow in Asia/Kolkata,
  the live save button reads **Save & send WhatsApp offers**. Saving automatically
  publishes all still-proposed visits on that date. Other dates save proposals
  without auto-publication. Nothing runs later merely because a saved future date
  becomes tomorrow; this is triggered by Save, not a scheduled job.
- Each continuous visit still receives one offer covering its therapies. Existing
  pending/confirmed/completed appointments are not republished. Same-preview retries
  return the original response; regeneration/save does not resend existing offers.
- Appointment save, offer creation and queue jobs use one transaction. Missing
  recipients/windows or a queue write failure rolls everything back. The existing
  preview can be retried after correction. If midnight changes whether saving would
  send, a fresh preview is required. Existing worker delivery rules still apply.
- Preview saves queue simulated offers only; the shared UI processes that preview
  queue automatically. Verified in a separate SQLite preview: four appointments
  became pending and three visit offers became SIMULATED, without clicking Publish.
- Standalone daily APIs and replacement offers retain explicit publication. The
  normal production calendar and core solver were not changed.
- Files changed for this request: `flow_test/service.py`, `flow_test/shared_ui.py`,
  `web/app.js`, `tests/test_flow_test_mount.py`, `rules.md`, `flow_test/README.md`,
  and this handover. No schema changes, new secrets or live data mutations.
- Validation: **268 tests passed**; both JavaScript files passed syntax checks;
  Impeccable detector reported no findings for the changed UI. Browser preview
  verified save-to-offer processing. Screenshot:
  `outputs/save_tomorrow_offers.jpg` (ignored, fictional local records).
- Next deployment, for the user to run: `& .\flow_test\deploy_azure.ps1`.
  Existing Azure revision from the last live check remains recorded below; do not
  claim this save policy is already online. Recheck readiness/assets after deploy.

## Continuation update — 21 October 2026, checked through approximately 14:20 IST

Read this update before the older snapshot below. Live state was checked again
through the authenticated `/test` API and Azure CLI; no deployment was run in
this continuation.

- Azure is already running `therapy-scheduler:test-flow-20261021-130055`, revision
  `orscheduler--0000014`. Latest and ready revisions agree; provisioning succeeded;
  Single revision mode and one replica remain configured.
- The deployed WhatsApp controls exactly match local `web/whatsapp_controls.js`.
  The server-generated offer preview also includes the new whole-day decline
  wording. The earlier "needs deployment" instruction is superseded by this check.
  A fresh real NO round trip under this revision has not been performed here.
- Full local regression suite rerun: **258 passed**. Both shared JavaScript files
  passed `node --check`. An initial sandbox run passed 256 and encountered two
  environment permission failures; the full rerun with required access passed.
- TEST-B's offer had expired at **13:32:32 IST**, while appointment **8** remained
  `PENDING_CONFIRMATION`. A late DAY reply was therefore not used.
- Staff deliberately reconciled B through existing authenticated test endpoints:
  `/api/release` closed the expired offer and released appointment 8, then
  `/api/absences` recorded whole-day unavailability for 22 October with an explicit
  reconciliation reason. Audit contains `flow_admin_release` and
  `patient_marked_absent`. This is a staff action, not a replayed parent response.
- Inbox **1** was an unmatched `Day` without a usable offer reference; inbox **2**
  was B's expiry notice. Both were reviewed and marked resolved. The unmatched
  message was not treated as proof of a particular child's identity.
- Reviewed TEST-D for B's **22 October, 09:40–10:20 Speech** opening: no warnings.
  Reserved through the same released-slot `with_messaging` endpoint used by the
  drawer. New appointment **10** initially became `PENDING_CONFIRMATION`; one replacement offer
  was **SENT** and **delivered** to the existing consented test recipient. No
  duplicate publication was performed. Request ID:
  `live-20261022-testb8-testd-replacement`.
- At publication, D's offer had a **15:13:01 IST on 21 October** deadline. Its
  later Yes cleared that expiry, and attendance subsequently closed the offer,
  as recorded below. At that initial checkpoint A and C were confirmed, B had
  zero booked Speech sessions and two remaining, and D had one booked and one remaining.

**Help verified live at approximately 14:17 IST:** D's real Help reply created
inbox **3**, `Parent requested help`, and audit `flow_reply_help`. Acknowledgement
job **42** was SENT and read. Staff resolved inbox 3; appointment 10 remained
`PENDING_CONFIRMATION` throughout. No cancellation or replacement occurred.

**Replacement confirmation verified live at approximately 14:18 IST:** the user
tapped Yes on D's original offer. The offer and appointment **10** became
`CONFIRMED`, `last_reply=YES`, expiry cleared, with `flow_reply_yes` and
`parent_response_applied` audit entries. Confirmation acknowledgement job **47**
was SENT and delivered. No live callback was simulated.

**Attendance and weekly progress verified at approximately 14:19 IST:** recorded
the following explicitly fictional outcomes for 22 October, using authenticated
`/test/api/attendance`. These are scenario results, not actual delivered care.

| Child / therapy | Appointment | Final scenario status | Weekly completed | Booked | Remaining |
|---|---|---|---|---|---|
| A / Speech | 6 | COMPLETED | 1 | 0 | 2 |
| A / OT | 7 | NO_SHOW | 0 | 0 | 2 |
| B / Speech | 8 | NEEDS_RESCHEDULING | 0 | 0 | 2 |
| C / OT | 9 | COMPLETED | 1 | 0 | 1 |
| D / Speech | 10 | COMPLETED | 1 | 0 | 1 |

All four offers are now CLOSED; their original reply history is retained. Each
attendance action has an audit entry. A's OT no-show restored remaining demand
from one to two. Three sessions are completed, eight remain, none remain booked,
and there are zero open staff inbox items.

**Next-day planning verified at approximately 14:20 IST:** generated an unsaved
23 October preview through `/test/api/schedules/generate`, selecting A/B/C/D and
the supported 20-second search setting. It returned OPTIMAL, one-day scope,
`canSave=true`, and zero validation violations. Weekly requested = 11; scheduled
= 7 (three prior completed sessions plus four new proposed sessions); unmet = 4,
with explicit capacity/daily-visit reasons. Only the four 23 October proposals
were returned for review. Saved 22 October records and weekly progress were
unchanged, and 23 October still has zero saved appointments. No preview was saved
or published, and no further WhatsApp offer was sent.

The remaining replacement, Help, attendance and updated weekly-progress live
scenario checks are complete. A fresh real NO round trip under revision 14 remains
separate from B's audited staff reconciliation; its new behavior is covered by
the passing local suite. Zoho remains excluded. No scheduler code, credentials,
recipient mappings or existing unrelated working changes were edited.

## 1. Objective and boundaries

Build and test the full one-day therapy scheduling workflow for the first pilot at
Malaparamba EIC Centre: draft appointments, admin review, WhatsApp offers, real
parent replies, replacement proposals, Help requests, attendance and weekly progress.
Use fictional children and consented coworker recipients. **Zoho input/output is
excluded from this test.** The broader future production architecture includes
Zoho Creator sync, PostgreSQL, OR-Tools and PostgreSQL-backed workers.

The user explicitly requires:

- Reuse the existing Azure Container App and WhatsApp credentials.
- Reuse the existing PostgreSQL database through isolated test tables/schema.
- Serve `/test` using the same browser UI as the main site; do not create another UI.
- Plan **one selected day**, Monday–Saturday, considering the whole week's demand
  and existing bookings. Do not run a 14-day plan for `/test`.
- Preserve the established OR Scheduler. Do not edit solver logic to implement
  test messaging or UI changes. Keep coordination in test/services/UI adapters.
- Currently all four fictional patients use the same consented test phone ending
  **7484**, configured in the ignored local recipients file. Offer tokens identify
  the patient/visit; never infer a child just from this shared phone.
- Latest confirmed policy: **Cannot attend / NO means unavailable for the whole
  appointment date**, without asking VISIT versus DAY.

## 2. Verified live environment

| Item | Value/status |
|---|---|
| Workspace | `C:\Users\GokulDRaj\OneDrive - turnb.com\Desktop\Therapy Scheduler` |
| Test URL | `https://orscheduler.agreeablemeadow-8055ebc2.eastus2.azurecontainerapps.io/test` |
| Webhook | Same host, `/webhooks/whatsapp` |
| Container App / resource group | `orscheduler` / `TherapyScheduler` |
| Registry | `orscheduler0488dadb.azurecr.io` |
| Currently deployed image | `therapy-scheduler:test-flow-20261021-110245` |
| Latest and latest-ready revision | Both `orscheduler--0000013`; provisioning `Succeeded` |
| Revision mode / replicas | Single; minimum 1, maximum 1 |
| Live state | `LIVE — real coworker messages`; message mode `session` |
| PostgreSQL server / database | `therapy-scheduler-poc-gokul.postgres.database.azure.com` / `postgres` |
| Isolated test schema / restricted role | `flow_test` / `flow_test_app` |
| Test tables | 19: 14 shared workflow dependencies plus 5 flow-specific tables |
| WhatsApp phone-number ID / Graph API version | `1265029893366600` / `v26.0` |

The business WhatsApp display number and name were verified through the Meta API
in this chat; use the existing business chat. Actual recipient numbers live only
in the ignored configuration, not in this document.

**Real sending, delivery/read receipts and incoming YES/NO have already worked.**
The test contact window is currently open for all four patient mappings. They share
one phone, so one inbound START opens the same contact window for all four.

## 3. Current deployed versus local status — critical

| Feature | Status |
|---|---|
| Shared `/test` UI, one-day preview/save | Deployed; working |
| Live WhatsApp offers, worker, signed webhook routing | Deployed; real round trip observed |
| Publishing, delivery/reply tracking, Help inbox, attendance controls | Deployed; local regression coverage; finish remaining live scenarios |
| Released-slot replacement picker: confirm immediately or message and wait | Deployed; preview/integration tested; complete live replacement test |
| Operations tabs | **Already deployed**; remote `/test/shared.js` exactly matches local `web/app.js` |
| New immediate whole-day NO rule | **Local only; not yet deployed** |
| Updated whole-day reply label/message copy | **Local only**; remote WhatsApp component still has the old scope-question label |
| Deployment-script readiness fix | Fixed locally and checked against live endpoint; does not require rebuilding just to verify an already running revision |

Do not repeat the earlier claim that the Operations tabs still need deployment:
the live asset comparison confirms they are online. The next deployment is needed
for the **whole-day decline change** and any subsequent work.

## 4. Current real test records

For **22 October 2026**:

| Fictional child | Sessions | Appointment / latest reply |
|---|---|---|
| TEST-A | Speech and OT | Both CONFIRMED; YES received; offer sent/read |
| TEST-B | Speech | PENDING_CONFIRMATION; NO received under the old policy; offer sent/read |
| TEST-C | OT | CONFIRMED; YES received; offer sent/read |
| TEST-D | Standby Speech | Not yet proposed in this live case because B's booking has not been released |

No appointments are on 21 October. Recent queue history shows 20 DONE jobs and
6 SENT jobs, with one open staff inbox item. This is the API's recent/global job
history, not an independent count per selected date. No failed/unknown jobs were
present in that response. Inspect the open inbox item before deciding what to do.

**Important:** already-processed NO replies are not automatically replayed when the
new policy is deployed. Existing Test B still needs the original explicit DAY
reply or a deliberately audited staff reconciliation. Do not forge a parent
callback, mark it confirmed, silently replay history, or reset the live test DB.
The original follow-up/reference is in the WhatsApp conversation. A stale/expired
offer requires staff review rather than bypassing its validation.

## 5. Normal workflow and exact behavior

1. Admin opens **Schedule generator**, selects a date, generates a one-day preview,
   reviews validation, and clicks **Save as proposed**. Saving sends nothing.
2. Admin opens **WhatsApp & attendance** or the appointment drawer and publishes
   one complete visit or the proposed batch. Appointment becomes PENDING_CONFIRMATION;
   the outgoing queue worker sends the offer.
3. YES confirms the offered continuous visit. Delivery/read alone never confirms.
4. **After the next deployment**, Cannot attend / NO immediately stages explicit
   day-scoped responses for every unfinished visit for that child on that date,
   releases them as NEEDS_RESCHEDULING, closes affected open offers as DECLINED,
   and invokes the unchanged response-repair scheduler. Other dates/children stay fixed.
5. Replacements remain PROPOSED until admin review/publication. No automatic
   replacement messaging and no automatic family confirmation.
6. In the released-slot drawer, admin can search any patient or use suggestions,
   review warnings and explicitly override them, then choose:
   **Confirm without messaging** (CONFIRMED immediately), or
   **Send offer & wait for confirmation** (pending until YES).
7. Replacement reservation, offer and job commit in one transaction. Missing
   recipient/window or stale preview rolls back the booking. Retrying the same
   request returns the same booking/offer without resending or resetting status.
8. HELP/RESCHEDULE enters the staff inbox and acknowledges the request; booking
   remains reserved. Staff can mark the inbox item resolved.
9. Staff marks COMPLETED or NO_SHOW per confirmed therapy. Weekly progress tracks
   completed versus booked sessions; future daily runs retain existing bookings.

Unmatched, stale, expired or changed-offer replies require staff review. No
response does not cancel a visit. Offer expiry defaults to 60 minutes after send,
with booking retained for admin follow-up. Ambiguous outgoing failures become
UNKNOWN without blind automatic resending. Do not promise general automatic retries.

The new day-decline adapter handles multiple unfinished visit bundles, closes
related offers and rejects duplicate/reversed replies. Completed therapy is never
undone; a conflicting day declaration goes to staff review under the unchanged
shared response-service validation. Explicit legacy VISIT commands still retain
their supplied scope; the normal NO button no longer asks for that choice.

Generation and publication are **manual admin actions**. Workers automate queued
sends/reply processing; there is no automatic daily cron planner or Sunday summary.

## 6. Operations UI

Four tabs in `/test`, with Scheduling assistant selected first:

1. Scheduling assistant.
2. WhatsApp & attendance.
3. Record an absence (with recent absences beside the form).
4. Needs rescheduling (current count in the tab).

Tabs preserve unsaved forms while switching and while WhatsApp updates refresh
the released-appointments panel. Arrow keys/Home/End work. Tab navigation stays
visible while scrolling. Calendar **WhatsApp offers** opens the WhatsApp tab for
the selected calendar date. The main site has the same applicable tab layout,
but live WhatsApp controls remain scoped to the test workspace.

**Scheduling assistant is intentionally disabled in `/test` at present.** Its
first-tab placement does not mean the Azure OpenAI assistant is connected to test
data. Safely enabling it would require a test-scoped adapter and tests; do not
route it into production sessions. It is not required to run OR-Tools generation.

## 7. Files a new chat should read

Paths below are relative to the workspace root:

| File | Purpose |
|---|---|
| `AGENTS.md`, `rules.md`, `questions.md` | Repository instructions, confirmed rules, unresolved policies |
| `flow_test/README.md` | Detailed setup, live/preview limitations and behavior |
| `flow_test/service.py` | One-day adapter, atomic replacement offers, queues, replies, latest day-decline change |
| `flow_test/mount.py` | Shared `/test` mount, runtime/worker, recipient configuration and webhook split |
| `flow_test/shared_ui.py` | Authenticated allowlist into existing UI handlers with test-scoped sessions |
| `flow_test/storage.py`, `flow_test/models.py` | Isolated schema/engine mapping and five flow-specific models |
| `flow_test/scenario.json` | Fictional data; no Zoho dependency |
| `flow_test/provision_postgres.py` | Existing restricted-schema provisioning; do not rerun unnecessarily |
| `flow_test/deploy_azure.ps1` | Build, additive Azure secrets/config, existing-app update, readiness polling |
| `web_app.py` | Main HTTP/API coordination, workspace context and signed webhook |
| `web/app.js`, `web/styles.css`, `web/index.html` | Shared UI and Operations tabs |
| `web/whatsapp_controls.js` | Publishing/status/Help/attendance and new whole-day label |
| `services/admin_reservation_service.py` | Preview/warnings, idempotent reservations; pending-offer opt-in for test flow |
| `services/parent_response_service.py` | Existing explicit-scope response processing and weekly progress; reused unchanged by latest policy |
| `tests/test_flow_test.py`, `tests/test_flow_test_mount.py` | Main flow/policy/HTTP/isolation/integration tests |
| `tests/test_admin_reservations.py`, `tests/test_empty_slot_reservations.py` | Existing manual-booking constraints and compatibility |

`flow_test/web/*` includes an older standalone dashboard. Do not replace the shared
mounted UI with it; the user explicitly requested the same main UI at `/test`.

## 8. Configuration and secrets — locations, not values

- `flow_test/.env` (ignored): restricted FLOW_TEST_DATABASE_URL and FLOW_TEST_DATABASE_SCHEMA.
- `flow_test/recipients.local.json` (ignored): TEST-A through TEST-D, consent true,
  all mapped to the same currently approved coworker number; optional greeting names blank.
- `flow_test/data/admin.token` (ignored): existing test admin key, also deployed
  through FLOW_TEST_ADMIN_TOKEN. Read locally only when needed; do not paste it into docs.
- Existing WhatsApp access token, app secret and verify token are Azure Container
  App secrets. Preserve/reuse them; do not rotate, print or bake into the image.
- Azure test secrets: `flow-test-db-url`, `flow-test-admin-key`, `flow-test-recipients`.
- Test env: FLOW_TEST_LIVE=true, FLOW_TEST_MESSAGE_MODE=session,
  FLOW_TEST_DATABASE_SCHEMA=flow_test, and secret references for DB/admin/recipients.
- FLOW_TEST_RECIPIENTS_B64 is base64 recipients JSON injected as an Azure secret.
  Encoding avoids Windows CLI quoting problems; it is not encryption. It overrides
  the local file. Editing the local file alone does not change the live recipient.
- `.dockerignore` excludes local .env, recipients, data, outputs and Git/venv files.

Live uses the isolated Azure PostgreSQL schema. Default local preview uses a
separate SQLite DB and never calls WhatsApp, even with sender credentials present.
The test-specific ORM metadata does not add test tables to production migrations.

## 9. Fictional scenario

Malaparamba EIC fictional centre; capacity 2; Anu handles Speech, Ravi handles OT.
Initial roster: A/B/C. D is held as standby in initial daily planning.
A needs Speech 3/week + OT 2/week; B Speech 2/week; C OT 2/week; D Speech 2/week.
Initial day normally has A Speech 09:00 and OT 09:40, C OT 09:00, B Speech 09:40.
A's two sessions form one continuous visit/offer. B's decline can propose D in
the same Speech opening if constraints remain satisfied.

## 10. Tests and deployment

Last full suite after the whole-day rule: **258 passed**. JS syntax check passed.
Browser preview verified one NO click changes B to NEEDS_RESCHEDULING, its offer
to DECLINED with `Unavailable for the whole day`, and proposes D without messaging D.
Local screenshots are in ignored `outputs/`, including `operations_tabs.jpg`,
`whole_day_decline.jpg`, `replacement_offer_drawer.jpg` and `replacement_offer_confirmed.jpg`.

From the workspace in PowerShell:

```powershell
.\.venv\Scripts\python.exe -m pytest -q
node --check web/app.js
node --check web/whatsapp_controls.js
az login
& .\flow_test\deploy_azure.ps1
```

Run deployment only when deployment is the authorized next action; never deploy
merely to answer a status question. The user has previously preferred running the
provided deployment commands themselves. The script builds the current working
source in ACR (no local Docker required), adds test secrets, preserves existing
WhatsApp/main DB settings, updates the existing app and keeps one replica. It
does not generate or publish offers. It checks live test state with the admin key.

A previous deployment built/pushed/updated successfully but its final check
tried `.mode` on a response without that property. `Wait-TestWorkspace` now
handles HTML fallback/incomplete JSON/startup errors/preview mode with bounded
retries. The fix was tested using fake responses and the actual live endpoint.
A failed final check does not prove deployment failed; inspect ready revision and
authenticated state before needlessly building another image.

For safe shared-UI preview without invoking main production initialization, use
an unused local port and an explicitly separate preview database:

```powershell
$env:FLOW_TEST_LIVE='false'
$env:FLOW_TEST_PREVIEW_DATABASE_URL='sqlite:///flow_test/data/handover_preview.sqlite'
.\.venv\Scripts\python.exe -c "from http.server import ThreadingHTTPServer; from web_app import TherapySchedulerHandler; ThreadingHTTPServer(('127.0.0.1',8776),TherapySchedulerHandler).serve_forever()"
```

Then open `http://127.0.0.1:8776/test`. Do not publish real messages or mutate the
live schema to verify UI layout. There may be earlier local preview servers on
ports 8772–8775; those are not the Azure app.

## 11. Remaining work in priority order

1. Deploy the local whole-day Cannot attend rule and its updated copy; verify
   readiness and the new assets. Operations tabs are already live.
2. Deliberately resolve the earlier live Test B decline under the confirmed policy;
   inspect offer expiry and the original follow-up. Do not claim deployment alone
   replays the earlier NO. Review the one open staff inbox item.
3. Finish the real live scenario: release B, review D, send D an offer from the
   replacement drawer, receive YES, verify confirmation; test Help resolution and
   Completed/No-show with fictional sessions. These exist and are preview/test
   covered, but the full live replacement/attendance path is not yet verified.
4. If wanted, connect the Scheduling assistant safely to test data. This is not
   implemented merely because it is now the first tab.
5. Replace the shared recipient with separate coworkers later. Update Azure
   recipient secret/restart or redeploy, and each new coworker sends START.
6. Production expansion remains separate: Zoho input/output, approved proactive
   templates/reminders, scheduled daily runs, Sunday summaries, operational
   monitoring and decisions still open in questions.md. Do not present these as done.

Known limits: one process/replica; no automatic cascades or week-to-week clinical
carry-forward; no automatic replacement acceptance; empty-calendar-cell messaging
is not connected. The empty-cell manual booking path references therapist
capability/location mapping tables not in the 19-table test dependency set: inspect
and test before declaring that path fully supported in `/test`; do not add more
tables casually. Released-slot replacement messaging is the verified target path.

## 12. Working-tree precautions

The checkout has many pre-existing modified and untracked files. No clean commit
or PR was created in this conversation. In particular, `scheduling/*`, config,
models, main app and tests already show earlier modifications: **do not reset,
overwrite, revert or claim all dirty diffs were authored by the latest change**.
The daily test adapter and new NO policy reuse the existing scheduler; latest work
did not edit the core scheduler files. Preserve existing live test records.

Follow AGENTS.md: scheduling behavior changes update rules.md with tests; answered
policies leave questions.md. The No-scope question was removed after the user's
explicit whole-day decision. Keep patient/therapist information and credentials
out of commits. Do not ask the user to repeat credentials already available in
the ignored configuration or existing Azure secrets.

## 13. Lunch display correction — local, awaiting deployment

The user reconfirmed lunch is **12:35–13:15**. The shared OR slot configuration
already enforces this; 13:15–13:55 is a valid afternoon session. The calendar had
incorrectly shaded slot index 5 as lunch. The shared day/week calendar now shows
a separate, non-bookable lunch row from schedule API metadata derived from the
existing slot times and consecutive-block boundary. No solver slots, appointment
indexes, live data or scheduling policy changed. R-03 now states the exact times.

Validation: **288 tests passed** and both views were checked in an isolated local
preview. Screenshot: `outputs/lunch-calendar-preview.png`. To publish this display
fix, run `& .\flow_test\deploy_azure.ps1`; no database reset is needed.
