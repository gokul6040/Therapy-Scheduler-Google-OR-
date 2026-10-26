# EIC branches: isolated daily WhatsApp flow test

This is a coworker test using fictional children, not a clinical deployment.
The test reuses the real OR-Tools scheduler, independent validation and parent
response repair. The optional read-only Zoho importer is pinned to the authorized
`nidhin.kumar_turnb/test-gokul` reports. A separate user-authorized manual output
export now writes only to that test app's Scheduler form; see [EXPORT.md](EXPORT.md).
Neither path uses the main application's session factory or initializes its existing tables.

## Zoho test reports and daily job (22 October 2026)

Read source: `Plan_Report` and `All_Consultants` in `test-gokul` only. The live
reports were verified to contain eight fictional children, two consultants, and
both Added Time and Modified Time. The parser accepts `;` or newlines between
Schedule entries, with `therapy, weekly frequency, duration in months, consultant`
in each entry. Each latest plan starts on the India calendar date of its source
Added Time (already IST); Modified Time is also IST and detects edits. The original two therapies are Psychology Therapy
Session and IEP Session, not the original Speech/OT seed.

The existing saved OAuth connection can read these reports. The separately pasted
short-lived token could not reconnect with the saved client, so it is not used.
No credentials belong in fixtures, generated examples, logs or source control.

Validate a fresh read without changing the test database:

```powershell
.\.venv\Scripts\python.exe -m flow_test.zoho_daily
```

Deploy the updated test endpoints with `& .\flow_test\deploy_azure.ps1`, then:

```powershell
.\.venv\Scripts\python.exe -m flow_test.zoho_daily --apply
```

Import refuses to switch from the old fixture while fixture appointments exist.
Only if an explicit fresh test is wanted, run the existing reset command first.
Import itself never deletes history or sends WhatsApp. After an import, open
**Schedule generator** in `/test`: tomorrow's refreshed unsaved draft loads
automatically. Review and save using the existing controls. Send START first
when using session messaging. Sunday target dates receive no draft.

The first scheduled/`--apply` run bootstraps all records before its run cutoff.
Later runs ask Zoho for records whose Added Time OR Modified Time is at or after
the saved checkpoint and strictly before the new cutoff, using the source
field's own time zone: Added Time and Modified Time both use Asia/Kolkata.
The saved checkpoint remains a UTC instant. Clinic dates and the 03:00 daily trigger
use Asia/Kolkata. All pages from both reports must succeed. Changes merge into
the cached source by Creator ID, then the complete current dataset is converted.
The checkpoint commits in the SAME transaction as source and scheduler updates.
Failures preserve it; concurrent stale runs are rejected and can retry.

On 23 October the user confirmed only Added Time had changed to IST; Modified
Time remains UTC. The earlier implementation interpreted both as UTC. Deploy the
app first, then reread both reports once to migrate the saved source contract
and recover changes without resetting data:

```powershell
.\.venv\Scripts\python.exe -m flow_test.zoho_daily --apply --reconcile
```

This rereads both reports before a fixed cutoff, merges by stable IDs, preserves
saved bookings/history, and atomically records the per-field source-timezone marker
with the new UTC checkpoint. Legacy cached rows absent from the full read are
converted from their previous zones before merging; this preserves their original
instants and India treatment dates. It never deletes missing rows. A pre-fix
checkpoint/cache requires this one-time reconciliation before incremental runs.
Old job clients without the per-field contract are rejected before any import/checkpoint update.
After reconciliation, rerun `deploy_zoho_job.ps1` to update the existing job to
the fixed app image and verify a manual execution. Leave the trigger Manual until requested.

An empty change window is valid: it keeps earlier records and advances the
checkpoint without duplicating them. Missing rows never mean deletion. A new
source plan row for an existing Registration ID selects the complete latest plan
by Modified Time, with Added Time breaking a tie. If both times match across
different latest rows, staff review is required. The latest Schedule is the whole
current plan, not a union of old plans. Unchanged therapy requirements keep their
IDs and progress when another therapy is added. Existing source IDs
cannot silently change Registration ID. The former shared `--plan-start` option
is deprecated and does not override the date derived from each plan's Added Time.

There are no A–H, patient-count, two-consultant or `TEST CONSULTANT` name limits.
New valid Registration IDs and consultants in the authorized reports can be
added. All branches in the authorized reports are supported in isolated test
storage. Consultant matching includes branch; spaces, tabs, case differences and
common invisible paste markers are normalized. Each branch's capacity follows
its active consultant count, matching original OR rule S-03. The initial source must contain at
least one valid plan and consultant; an empty bootstrap cannot activate old seed
data accidentally. Frequency/date changes update the requirement in place and
warn staff when appointment history exists: two bookings under a 2/week target
still count when the latest target becomes 3/week, leaving one session to plan.
Removed therapies have zero future demand while existing appointments remain.
Warnings appear in the import response and the existing staff inbox. A changed
doctor/branch on a booked requirement remains pending staff review, with other
records imported. Consultant capability/status and identity conflicts still stop
for review. Parent numbers come directly from validated Zoho Parent No. fields; no
extra configured number pool or local entry is required. Adding a new parent
number updates transport and callback routing after the import commits. Bindings
restore on restart without local contacts. Unsent queued or waiting offers follow
the new number, including its contact-window requirement. Sent and uncertain
offers retain their original recipient and are not automatically resent. Failed
validation does not activate proposed contacts. Only current
source contacts and recipients of existing test offers enter the test flow.
See `RESTRICTIONS.md` for all current test boundaries, defaults and operational limits.

After one successful live TEST import, provision the **manual** Azure job
`orscheduler-dailyjob`:

```powershell
& .\flow_test\deploy_zoho_job.ps1
az containerapp job start --name orscheduler-dailyjob --resource-group TherapyScheduler
```

Only if the user later requests daily automation, enable its trigger:

```powershell
& .\flow_test\deploy_zoho_job.ps1 -EnableDailySchedule
```

The schedule is 03:00 IST / 21:30 UTC. The job reuses the deployed app image but
overrides its command with `python -m flow_test.zoho_daily --apply`. It needs
only OAuth and test-admin secrets, not database or WhatsApp credentials. It imports
through the running test app so the existing process lock coordinates it with
replies and saves. The job launcher reuses the app's existing registry-password
configuration; managed-identity-only registry setups need separate job registry
configuration. It never runs the web-server command or production importer.
The launcher writes the temporary CLI configuration as ASCII JSON with Unicode
escapes (valid YAML), without a BOM. This also works with Windows PowerShell 5
and Azure CLI's locale-based YAML reader. A launcher encoding fix does not
require redeploying the app or resetting/reimporting the test data.

Source snapshots, the imported scenario, sync metadata, checkpoint and draft date are stored
in namespaced test settings, retaining the same 19-table schema. Restart preserves
the imported scenario. An explicit reset reseeds that imported scenario if it
is already installed. Existing seed therapist/requirement records may remain
inactive to avoid destroying references.

Local HTTP verification uses `postman/simulations/zoho-test.sim.yaml` and the
failure variant. The mock contract comes from masked actual test responses and
official Creator v2.1 report-reading documentation. No owner-published mock was
used; local fixtures contain dummy phones. Run the real daily command against
the simulation with dummy OAuth settings, never real credentials. Neither mock
is deployed or published to a Postman workspace.

The incremental HTTP check exercises an initial eight-plan/two-consultant read,
a subsequent zero-row window and a changed-only one-plan/one-consultant read.
It finishes with nine patients, three consultants and eleven requirements while
preserving previous IDs. The mock's `/__test__/add-patient` endpoint is local
fixture control, not a Zoho API endpoint; it contains dummy contacts only.
UTC recovery was also exercised over real local HTTP: bootstrap 8/2, reject a
legacy checkpoint, reconcile a missed source row to 9/3, then fetch 0/0 on the
next incremental run. It produced 11 draft sessions, zero saved appointments
and zero offers. Both temporary servers were stopped after verification.

## Start with preview

The existing application also serves **`/test`**: run `python app.py` and open
`http://127.0.0.1:8765/test`. It serves the **same `web/index.html`, `web/app.js`
and `web/styles.css` as the main application**, including the calendar, filters,
patient/doctor perspectives, appointment drawers, preferences, directories,
operations and schedule generator. There is no separate test screen layout.
The test banner and admin-key dialog identify the workspace. WhatsApp actions
extend the same appointment drawer and Operations page. Shared UI API calls go to
`/test/api/...` and use a request-scoped test session and test-only preview store.
The original UI handlers operate on fictional records; normal requests retain
the original session and preview store. The shared **test** generator reuses the
standalone daily flow's one-day input builder and shared core solver. Bounded daily
and replacement searches retain all valid starts; the clinical constraints and
unbounded weekly candidate shortcut are unchanged.
The admin selects one Monday–Saturday date, reviews the unsaved preview, and saves
proposed sessions only for that date. All fixed bookings/completed therapies in
the same week count toward demand; other dates remain untouched. The UI separates
weekly demand from selected-day sessions. Main-app rolling planning is unchanged.
When the selected date is **tomorrow in India time**, saving the shared calendar
automatically queues one WhatsApp offer per still-proposed visit. Live mode labels
the action **Save & send WhatsApp offers**; preview mode queues simulated offers
without contacting WhatsApp. Other dates save as proposed without auto-publication.
Existing offers are not resent. Save, offers and queue jobs commit together; if
any recipient is missing, the whole save rolls back and the same preview can be
retried after correction. A closed session window instead leaves only that
parent's offer waiting for contact while other offers proceed. Repeating a successful save returns
the saved result. A date rollover that changes whether saving sends requires a
fresh preview. There is no scheduled task that later publishes an earlier save.
The standalone daily-flow launcher and its existing daily API also remain available.
Zoho/source import routes are blocked and the external assistant is disabled in
the test workspace. Unknown test routes cannot fall through to production.

For proposals not already offered by saving tomorrow's calendar, open a saved
appointment to **Publish preview offer** (or **Publish WhatsApp offer** in live
mode). Publication covers the child's complete visit.
The drawer shows sending, delivery, the validated parent reply and the message.
Use **WhatsApp offers** on the calendar, or **Operations**, to select a date,
publish proposed visits, inspect replacements, resolve Help requests, and review
queue history and weekly progress. Once confirmed, record **Completed** or
**No-show** per therapy in the drawer or Operations.

In preview, choose a reply and click **Simulate reply**. The preview queue runs
automatically. Cannot attend immediately means unavailable for the whole appointment day; replacements still
need publication. Live mode has no simulation controls. Status panels refresh
every five seconds while open. Reading/refreshing never sends, and preview status
does not invent real delivery receipts. Live publication rechecks recipients and
the session window. An ambiguous send remains for staff review, without a retry
button or automatic resend.

Enter `FLOW_TEST_ADMIN_TOKEN` at the test login. If unset, a private key is created
in the ignored `flow_test/data/admin.token` file. Preview is the default and uses
its separate local SQLite file. For Azure live testing, configure a persistent
admin key, `FLOW_TEST_LIVE=true`, the existing restricted PostgreSQL test settings,
and valid parent numbers in the Zoho test source (or optional seeded-fixture contacts).
Deploy these code changes to the existing
container app to make `/test` available at its public URL; no new app is required.
Keep a single running replica while using this single-process test worker.

In mounted live mode the existing, signature-verified `/webhooks/whatsapp` routes
events for imported parent numbers/existing offer recipients and business phone ID to test storage.
Other numbers retain the existing webhook flow. Coworker numbers should therefore
be dedicated to this test while live mode is enabled. The callback URL in Meta
does not need to change. The test-only tables use separate SQLAlchemy metadata,
so importing the mounted workspace cannot add them to production migrations.

From the repository root:

```powershell
.\.venv\Scripts\python.exe -m flow_test.app
```

Open the private admin URL printed by the launcher. The default port is **8767**.
The URL's fragment contains a random admin token; the page removes the fragment
and keeps the token in that tab's session storage. Admin APIs require that token.
The only unauthenticated application routes are the static dashboard assets and
the separately verified WhatsApp webhook. No contacts or secrets are served as files.

Preview makes **no WhatsApp requests**, even if the root `.env` has sender credentials.
It has reply buttons to test the same incoming job processor. Publishing does not
call Zoho. Starting the server does not create appointment offers.

## Fictional input and expected flow

`scenario.json` is the input. It contains eight fictional children A–H, their assigned
therapists and weekly therapy frequencies, recurring slot availability, the test
centre, and its capacity. All test plans are active with no expiry. No workbook or
existing patient database is read. The fixture uses the application's existing
**40-minute slots**, not the earlier illustrative 30-minute example.

| Child | Weekly requirement |
|---|---|
| A | Speech 3, OT 2 |
| B / D / F / H | Speech 2 each |
| C / G | OT 2 each |
| E | Speech 2, OT 2 |

Anu is assigned Speech; Ravi is assigned OT. Both use the existing configured
working-day slots, 09:00–16:50 with breaks. Children have no preassigned times or
artificial availability windows. OR chooses actual appointment times; a future
genuine family restriction can still be supplied explicitly.

1. Select a Monday–Saturday date. All eight children A–H are eligible by default.
2. **Generate daily draft** creates ten proposed therapy sessions across eight
   children on a fresh date/week. A and E each have a continuous two-therapy visit.
   Existing booked/completed work may reduce this count. Rerunning preserves bookings.
3. Review and save. In the shared `/test` UI, saving tomorrow automatically queues
   eight visit offers for a fresh roster. The standalone dashboard still requires
   **Publish proposed visits**. The worker processes at most one job per second.
4. A chooses **YES**: both sessions become confirmed. C can stay unanswered.
5. B chooses **NO / Cannot attend**: B is immediately marked unavailable for the
   entire appointment date. Every unfinished booking for B on that date is released;
   there is no visit/day follow-up question.
6. The real solver proposes an eligible replacement if one exists. With all eight
   children already booked, the correct outcome can be no eligible same-day
   replacement. Other children's saved visits and other dates stay fixed. To test
   an explicit standby case, deliberately omit a child from the generation API's
   `patients` list; no hidden time restrictions or standby defaults are applied.
7. Review any replacement and click **Offer this visit**. It stays pending until accepted.
8. The replacement chooses **YES**. Mark its session completed. Mark A's therapies individually
   completed, allowing partial attendance. A no-show restores that session's demand.
9. Open the next working date and generate again. Booked and completed sessions
   anywhere in the same Monday–Saturday week reduce the remaining weekly demand.

**HELP** and **RESCHEDULE** create admin inbox items without cancelling a visit.
Staff can mark inbox items resolved. The queue and audit history are visible.
The test does not implement clinical priority ranking, automatic week-to-week
carry-forward, automatic appointment moves, cascading replacements, or Zoho sync.
Its daily generation does not replace the production rolling scheduler.

The eight weekly plans are A: Speech 3 + OT 2; B/D/F/H: Speech 2 each;
C/G: OT 2 each; E: Speech 2 + OT 2. This totals 21 sessions/week, not 21
sessions every day. Both therapists use the configured working day Monday–Saturday.

Existing v1/v2 test databases upgrade once to v3 on the updated app's next startup.
The upgrade adds missing E–H and removes old fixture availability restrictions. It
preserves A–D's records, attendance, absences, offers, queues and audit history.
It does not generate bookings or send offers. Conflicting E–H codes require staff
review; there is no database reset or silent record replacement. Upgrading the
already provisioned schema requires no rerun of the PostgreSQL provisioner.

## Fresh start in the existing live test workspace

The user has explicitly authorized clearing the old fictional live test history.
From the project root in PowerShell, run:

```powershell
& .\flow_test\start_fresh_live.ps1
```

This deploys the current app to the existing Azure Container App, verifies scenario
`malaparamba-fictional-v3`, then calls the authenticated reset API. Only the existing
PostgreSQL `flow_test` schema is cleared and reseeded. Appointments, offers, queues,
parent replies, attendance, absences, contact windows, manual reservations and old
audit/inbox entries are removed. Inputs become eight fictional children, two
therapists and ten weekly requirements totaling 21 sessions; a fresh reset audit
and cutoff are recorded. No appointments are generated and no messages are sent.
The app serializes reset with its worker and clears cached schedule previews. A
failed reseed rolls back the whole reset. Pre-reset callbacks cannot restore the
erased history; receipts for deleted outbound messages are ignored.

Production/public records, Azure credentials and the configured recipient numbers
are outside the reset. Already-delivered messages remain in WhatsApp on the phone.
Refresh `/test` after the command completes, send a new START from the test number,
then generate, review and save tomorrow's calendar. Repeat this command only when
another deliberate fresh start is wanted; it erases the latest test run too.

If the current v3 app is already deployed and only a new reset is required:

```powershell
.\.venv\Scripts\python.exe -m flow_test.reset_live --confirm RESET-flow-test
```

## Parent numbers and optional fixture contacts

For the Zoho-backed test, edit Parent No. in Zoho and run the manual sync job.
Any valid international parent number in that authorized source is accepted;
no local file edit or separate number approval is needed. The contact must still
message first in session mode; template mode uses its approved message template.

The following file applies to the original standalone seeded fixture, not to
Zoho-imported parent numbers:

`recipients.local.json` is created for you and ignored by Git. Fill in international
numbers and mark consent only for coworkers who agreed to receive test messages:

```json
{
  "TEST-A": {"name": "Asha", "phone": "<country code and coworker number>", "consent": true},
  "TEST-B": {"phone": "", "consent": false},
  "TEST-C": {"phone": "", "consent": false},
  "TEST-D": {"phone": "", "consent": false},
  "TEST-E": {"phone": "", "consent": false},
  "TEST-F": {"phone": "", "consent": false},
  "TEST-G": {"phone": "", "consent": false},
  "TEST-H": {"phone": "", "consent": false}
}
```

Blank entries receive no live messages. For the full default scenario configure
all eight entries. One coworker may represent multiple fictional children; offer
tokens keep their replies separate. Restart after editing recipient settings.
The optional `name` field supplies the greeting (for example, "Hi Asha,"). When
blank or absent, the message uses that fictional child's configured name. It
never guesses a name from the WhatsApp profile.
Only registered source/fixture contacts, existing offer recipients and the configured receiving business number
are processed. WhatsApp profile names are not used as patient identities.

## Configure live WhatsApp

The default **session mode needs no approved template**. Copy
`flow_test/.env.example` to `flow_test/.env` and leave
`FLOW_TEST_MESSAGE_MODE=session` (or set that environment variable in Azure).

1. Import the parent numbers from Zoho (or configure optional fixture contacts), sender credentials and reachable webhook below.
2. Each coworker sends **START** to the connected test WhatsApp number.
3. The authenticated webhook records a 24-hour response window for that coworker
   and receiving business number. The dashboard shows when they are ready.
4. Generate and publish the daily draft. The app sends an ordinary interactive
   message with **Yes / Cannot attend / Help** buttons, without a template.
5. Continue the same confirmation, decline, replacement and attendance test.
   D's coworker must also have an open window before receiving a replacement offer.

The app creates the real buttons through WhatsApp Cloud API; you do not type or
create buttons in the WhatsApp app. An example published continuous visit reads:

```text
TEST ONLY — fictional appointment

Hi Asha,
You have Speech and OT scheduled on Monday, 26 October 2026 at Malaparamba EIC — FICTIONAL TEST.

Speech: 9:00 AM–9:40 AM IST
OT: 9:40 AM–10:20 AM IST

Can you attend?
Reference: <visit reference>

[Yes] [Cannot attend] [Help]
```

Name comes from the local recipient binding (or fictional patient); therapy,
date, time and centre come from the published appointments. A continuous visit
lists all its therapies in one message. The reply buttons carry the visit's
reference internally so its response updates the correct booking.

Publishing saves all valid recipients' offers; closed windows create visible
WAITING_FOR_CONTACT jobs without blocking other parents. Sending checks again:
if the window closed while a job waited, no send is attempted
and its state becomes `WAITING_FOR_CONTACT`. A new coworker message reopens the
window and resumes these unsent jobs. Replayed old callbacks and delivery receipts
cannot reopen a window. Each new user message resets the window; an outbound
message does not. START itself does not create an appointment or send an offer.

Saving rechecks that the reviewed scheduling inputs and bookings are unchanged.
A stale preview must be generated again. Reply deadlines use the parent's event
time, so worker delays cannot invalidate an on-time reply to a retained offer.
Uncertain sends create a staff inbox item and are not automatically resent.
The deployment helper preserves the existing messaging mode; use `-MessageMode`
only when deliberately switching between session and approved-template sending.

This follows [WhatsApp's messaging policy](https://whatsappbusiness.com/policy/):
replies within 24 hours of the user's last message can be sent without a template.
Outside that window, proactive messages still need an approved template.

### Optional proactive template mode

Use `FLOW_TEST_MESSAGE_MODE=template` only after a suitable template is approved.
This retains the original template path. Configure:

- `FLOW_TEST_TEMPLATE_NAME`: an approved appointment **test** template.
- `FLOW_TEST_TEMPLATE_LANGUAGE`: its exact approved language code.
- `FLOW_TEST_TEMPLATE_PARAMETERS`: a JSON string array matching the template's
  body parameter count and order. Default: `["{patient}", "{visit_details}"]`.
- The template must have three quick-reply buttons in this order: **Confirm
  attendance**, **Cannot attend**, **Help**. Their dynamic payloads identify the
  offer. The template text must clearly identify this as a fictional test.
- Supported body placeholders: `{patient}`, `{visit_details}`, `{reference}`,
  `{centre}`, `{date}`, `{time}`, `{greeting_name}`, `{therapy}`, `{date_display}`,
  `{schedule}`. Keep `{reference}` or `{visit_details}` in the
  message so the coworker can copy its reference into a text reply.

The existing root `.env` can supply `WHATSAPP_ACCESS_TOKEN`,
`WHATSAPP_PHONE_NUMBER_ID`, `WHATSAPP_APP_SECRET`, `WHATSAPP_VERIFY_TOKEN`, and
`WHATSAPP_GRAPH_API_VERSION`. You can set these through the process environment
instead. Flow-specific `.env` settings do not modify the root file. If needed,
set `FLOW_TEST_VERIFY_TOKEN` to a different verify token for this callback.

Run:

```powershell
.\.venv\Scripts\python.exe -m flow_test.app --live
```

The standalone launcher requires explicit admin publication. In the shared
`/test` UI, saving tomorrow's calendar also publishes its proposed visits.
Configure a current/future planning date. Simulated responses are disabled in live mode.

### Receive real replies

Meta must reach this exact application's endpoint through a public HTTPS test
deployment or a tunnel to the running local server:

```text
https://<your-test-host>/webhooks/whatsapp-flow-test
```

Subscribe the test Meta app/number to message events and complete its verify-token
challenge. Incoming POSTs require Meta's `X-Hub-Signature-256` with the configured
App Secret. Receipt is acknowledged after committing durable incoming jobs; the
worker applies them separately. Preview deliberately ignores real callback events.
Do not replace an existing production callback to test this workflow; use a
dedicated test configuration or explicitly arrange routing to this endpoint.
This repository does not provision HTTPS hosting or a tunnel automatically.

Button replies are preferred. Text commands include the offer's printed reference:

```text
YES <reference>
NO <reference>
VISIT <reference>
DAY <reference>
HELP <reference>
RESCHEDULE <reference>
```

Replying directly to the original offer message with one of these words also
works when WhatsApp supplies its reply context. Unlinked/ambiguous text and
unsupported message types go to the admin inbox. NO now means whole-day
unavailability under the confirmed management decision. DAY is an explicit alias;
VISIT remains supported for older explicit commands, but the normal button flow
never asks for this extra choice. All affected open offers become declined.
Completed therapy is retained; a conflicting whole-day declaration requires staff
review under the unchanged shared response-service validation.

## Persistence, expiry and failure behavior

- Preview: `data/flow_preview.sqlite`. Live: `data/flow_live.sqlite`. Both are
  ignored. Live and preview cannot open the same test database.
- Shared PostgreSQL: use the existing database with
  `FLOW_TEST_DATABASE_SCHEMA=flow_test` and a restricted test DB login. Per-engine
  schema mapping directs all 19 required tables, queries and foreign keys to that
  namespace, with no `public` search-path fallback or shared-model modifications.
- `python -m flow_test.provision_postgres` creates the namespace and restricted
  `flow_test_app` login, checks that the login cannot access existing tables, and
  saves only the test login in the ignored `flow_test/.env`. Supply the admin URL
  through `FLOW_TEST_ADMIN_DATABASE_URL` for that provisioning process only.
  The command leaves existing WhatsApp settings, public tables and solver files
  unchanged. Before source import it seeds eight fictional patients, two therapists and ten therapy
  requirements; it creates no appointments or messages.
- The provisioning report is saved to `outputs/flow_test_storage_report.json`,
  containing counts and isolation checks, with no credentials or patient details.
- Local test DB credentials, coworker bindings and SQLite files are excluded from
  Docker build contexts as well as Git. Inject deployment credentials as secrets.
- Live storage settings are read only with `--live`. Default preview keeps its
  local SQLite file. Optional PostgreSQL preview uses
  `FLOW_TEST_PREVIEW_DATABASE_URL` and `FLOW_TEST_PREVIEW_DATABASE_SCHEMA=flow_test_preview`;
  provision it separately with `--schema flow_test_preview`.
- To check/initialize configured live storage without a sender, webhook worker or
  messages, run `python -m flow_test.app --live --init-db`.
- Without an explicit test schema, PostgreSQL still requires an empty dedicated
  database ending in `_flow_test`. Unmarked populated namespaces are refused.
- Use **one server process** per test database/schema.
  Restart recovery assumes this single-process ownership.
- Publishing and saving its outgoing jobs is atomic. Response processing and its
  follow-up jobs are atomic too. Callback IDs are deduplicated across restarts.
- Offer reply deadline defaults to 600 minutes / 10 hours **after sending**; change
  `FLOW_TEST_OFFER_MINUTES` if needed. On expiry, the booking stays reserved and
  an admin task appears. A late YES is rejected. **Admin release** explicitly
  releases the booking; run the daily generator to fill it afterward. This test
  setting is not an approved production no-response/cancellation policy.
- Outgoing sends that fail or are interrupted become `UNKNOWN`; they are never
  blindly retried, because Meta may have accepted them. Review delivery externally,
  then explicitly release/re-offer if appropriate. This test has no manual retry UI.
- Live workers send up to five different numbers concurrently. Set
  `FLOW_TEST_SEND_BATCH_SIZE` between 1 and 20 to change the batch size. Messages
  sharing one phone remain ordered and are paced six seconds apart. All A–H
  sharing one test phone therefore still receive separate messages in order.
  This preserves per-parent contact-window checks and atomic save validation.
- Obsolete offers are suppressed. Old, mismatched, or closed-offer replies cannot
  change a booking. Delivery/read receipts never confirm attendance.
- Free-text acknowledgments are only queued from incoming messages and expire
  after their response window. Session-mode appointment offers require a verified
  open window; template-mode offers use the configured approved template.
- The admin screen masks coworker numbers. The ignored database still contains
  test contacts and incoming text; retain it only as long as needed for the test.

To replay local SQLite from scratch, stop the server and **rename** the relevant
ignored DB file as an archive. Restart to create a new test DB. PostgreSQL test
schemas persist across restarts; the app has no destructive reset/drop command.
Changing `scenario.json` takes effect only in fresh test storage.

## Verification

For the repeatable HTTP flow check, start these in two separate terminals:

```powershell
postman simulation run postman/simulations/zoho-test.sim.yaml --port 4913 --output ndjson
postman simulation run postman/simulations/zoho-test-failure.sim.yaml --port 4914 --output ndjson
```

Then run `.\.venv\Scripts\python.exe postman/checks/verify_daily_flow.py` in a
third terminal. It uses dummy OAuth, temporary SQLite and simulated WhatsApp;
it covers save/reply/replacement/attendance and a source outage without contacting
Azure, Meta or Zoho. Stop both simulations afterward. See `READINESS_AUDIT.md`
for the remaining deployed-environment checks.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_flow_test.py -q
.\.venv\Scripts\python.exe -m pytest -q
```

Automated tests use temporary databases and fake transports, including signed
webhook requests through the real HTTP handler. They never send to Meta or Zoho.
Real delivery requires your recipient file, valid credentials, and the reachable
webhook described above. In default session mode, coworkers message first and no
template is needed. Optional proactive template mode requires its matching
approved template.

### Sending an offer from the replacement drawer

In the shared `/test` calendar or Operations, open a released appointment, choose
any patient or a suggested patient, and review the warnings. **Send offer & wait
for confirmation** reserves the selected visit as pending and queues its WhatsApp
offer atomically. Warnings shown in the review are recorded as admin overrides.
The appointment details then show the offer, sending status and reply controls.
Preview mode simulates delivery; live mode uses the same configured credentials,
consented recipients and existing verified webhook. A coworker must first send
START when using session messages. Yes confirms; Cannot attend immediately releases
that child's unfinished bookings for the whole date; Help enters Operations.
Retrying the same request does not resend the offer. A missing recipient saves
no booking; a closed session window saves a pending booking and an offer waiting
for contact. **Confirm without messaging** still confirms immediately.
This connection applies to released-slot replacements; new empty-cell bookings
and the production drawer retain their existing behavior. No new tables or solver
changes are required.

### Deploy the shared test workspace to the existing Azure app

From a PowerShell terminal in the project root, sign in to the intended Azure
subscription with `az login`, then run `& .\flow_test\deploy_azure.ps1`.
The script targets the existing `orscheduler` app in `TherapyScheduler` and builds
through the existing `orscheduler0488dadb` registry. It reads the restricted test
DB settings from the ignored `flow_test/.env`, the existing admin token, and the
configured recipients. It validates the app configuration and runs tests before
building or making cloud changes. This deploys the current project source.

The script adds three Azure secrets and updates only the test environment variables,
image and replica limits. Existing WhatsApp credentials and main database settings
are retained. `FLOW_TEST_RECIPIENTS_B64` contains base64-encoded recipients JSON
through a secret reference; it overrides the local recipients file and keeps that
file outside the build image. Base64 is for Windows command quoting, not encryption.
The script requires Single revision mode and sets one replica for the test worker.
It checks the authenticated test state after deployment without publishing offers.
The final check retries while the new revision starts and rejects HTML fallback
pages, incomplete JSON, preview mode and HTTP errors without reading missing fields.
It does not change the existing Meta callback configuration. After deployment,
send START from a configured coworker number and publish one test visit in `/test`.

Changing the local recipient file later requires updating the Azure recipient
secret and restarting the app, or rerunning the deployment script. Editing the
local file alone does not update a deployed app. If the final state check fails,
inspect the new revision's status/logs before publishing: an Azure update may have
succeeded even if the subsequent HTTP check failed.


### Whole-day decline policy change

The Cannot attend button keeps its existing NO payload, so valid existing offers
can use the new behavior after deployment. Already-processed NO events are not
silently replayed: any earlier offer still awaiting a scope answer needs an explicit
DAY reply or staff follow-up. The UI distinguishes those older retained bookings
from the new completed whole-day declines. No scheduler files or database tables
change. Replacement proposals still need admin review and publication.
