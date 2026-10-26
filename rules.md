# Therapy Scheduler Rules

This is the plain-language source of truth for how the scheduler should behave.
Update a rule here before changing the scheduler.

## Status key

- **Implemented:** The current application enforces the rule.
- **Partly implemented:** Some supporting behavior exists, but the full workflow is not ready.
- **Not implemented:** This is agreed behavior that still needs to be built.
- **Manager decision needed:** The application should not assume an answer yet.

## Test final schedule export (R-31)

**Implemented locally**, covered by `tests/test_flow_test_zoho_export.py` and the
real CLI/HTTP Postman scenario `postman/checks/verify_scheduler_push.py`.
The user authorized writes only to `nidhin.kumar_turnb/test-gokul`, form `Scheduler`,
report `Scheduler_Report`. Main/prod Zoho targets remain inaccessible to this path.

- The user chose a fixed daily cutoff eventually and **manual execution for now**.
  Explicit `zoho_push --date YYYY-MM-DD --apply` finalises that chosen date; no
  timer, automatic publication on save, or new Azure resource is introduced.
- The `/test` calendar also offers **Push to Zoho**. Its preview shows the chosen
  calendar date, unanswered sent visits and last successful push before confirmation.
  Confirmation runs the same resumable exporter, for all saved appointments on that
  date regardless of visible calendar filters. Unsaved previews are excluded.
  OAuth remains on the server; authenticated push requests release the workflow
  lock while contacting Zoho and overlapping calendar pushes are rejected.
  Missing credentials, scopes or unique-key fields stop before finalisation.
- Already-persisted queued parent replies are handled first. Explicit No removes
  that child's visit as before. An offered replacement uses its own appointment.
  Only sent unanswered offers can be assumed accepted. Help/Reschedule, unsent,
  waiting-for-contact, uncertain/failed-delivery and changed offers remain for staff
  review. Unpublished proposals are not automatically confirmed.
- Assumed acceptance confirms the local appointments and records `flow_assumed_yes`;
  export Status is `ASSUMED_CONFIRMED`. It never fabricates a parent Yes, sends a
  WhatsApp acknowledgement for silence, or marks attendance completed. Actual Yes
  uses `CONFIRMED`. Manual cutoff is explicit and independent of the ten-hour expiry.
- One outgoing row represents one therapy appointment. It carries Registration ID,
  patient name, date (`dd-MM-yyyy`), doctor and time (`hh:mm AM/PM`, IST), together
  with `Scheduler_Key` and `Status`. Zoho sets its own Added Time in the configured
  report; that creation field is not assigned or used as the sole change detector.
- Form preflight validates required link names, Single Line Status and a unique
  Single Line Scheduler_Key. The existing client needs create/read/update plus
  read-form-definition scopes. One-time user consent saves only a test-prefixed
  refresh token in ignored `flow_test/.env`, preserving the main token/settings.
- Stable namespace/appointment keys and payload fingerprints live in the existing
  test settings table. Repeat pushes skip unchanged rows, include older edited
  appointments, update previously exported moved/cancelled/released rows, and add
  replacement appointments separately. Unrelated remote rows are never modified;
  withdrawn history is retained with its status rather than deleted.
- A frozen resumable batch persists acknowledgements per row. The successful-push
  time advances only when the batch is complete, and cutoff/completion are displayed
  in IST. Failed or uncertain writes preserve the old completed checkpoint; unique
  key reconciliation prevents creating a second row after a lost response. Resuming
  an older batch is followed by a fresh check for intervening schedule changes.
- Source imports/daily generation still never export results. Local reset does not
  erase Zoho history; a new namespace prevents local ID reuse overwriting old rows.
  Storage remains 19 tables and existing single-process ownership applies.

## Isolated daily coworker flow test (R-30)

**Implemented only in `flow_test/`; covered by `tests/test_flow_test.py` and
`tests/test_flow_test_storage.py`.**
The separate launcher seeds eight fictional patients and two fictional therapists
in an isolated test database. It reuses the existing 40-minute slot configuration
and deterministic `FLOW_TEST_SOLVER_SETTINGS` in `config.py`. Default preview
does not access Zoho. The separately authorized source importer and manual Scheduler
export are documented below; neither changes the main application's rolling horizon
or runs production migrations. SQLite remains the default preview storage.
PostgreSQL may share the existing database only through an explicitly selected
`flow_test` or `flow_test_preview` schema. Per-engine schema translation redirects
the shared ORM models without modifying their definitions or the main sessions.
The PostgreSQL test connection has no public search-path fallback. A provisioned
test login has schema-scoped access, with no access to existing scheduler tables.
The initializer creates only the 19 tables needed by the test workflow and
rejects unmarked populated namespaces or unrelated tables. Unqualified shared
database connections remain prohibited. Legacy dedicated `_flow_test` databases
remain supported. Main database initialization stays unchanged. The approved
bounded-search correction below preserves all clinical constraints.

The existing server also mounts the test at `/test`, serving the same HTML,
JavaScript, styles and UI handlers as the main application. Calendar views,
filters, appointment controls, preferences, directories, operations and the
generator screens run against the isolated test session. The shared test
generator uses the same one-day input builder as the standalone daily flow;
it does not call the production rolling planner. New slots are restricted to
one explicit Monday–Saturday date. Completed and saved past/future visits in
that week still count toward targets and stay fixed. Generating is read-only;
only selected-day appointments enter the preview/save payload. Saving is bounded
to that one date, so other-day pinned rows cannot be duplicated or rewritten.
Repeating generation/save does not duplicate existing visits. Weekly demand
remains distinct from the number of sessions on the selected day in the UI.
The normal app retains its rolling planner; the shared UI reads a test-only
planning-mode flag for daily labels. Slot and clinical constraint definitions stay unchanged.
The test default date and its "today" counts use Asia/Kolkata. Selecting a
different planning date clears the old UI preview; saving opens the date saved.
These boundaries are covered by `tests/test_flow_test_mount.py`. A test banner and
authentication dialog identify the workspace. WhatsApp actions extend the same
appointment drawer and Operations page: publish a continuous visit, view sending
and delivery separately from the validated parent reply, resolve Help requests,
and record per-therapy completed/no-show attendance after confirmation. Operations
supports a selected date, batch publication, replacement offers, queue history
and weekly progress. Open status panels refresh without sending. Preview-only
reply controls use the existing processor and drain the preview queue; live mode
hides these controls and rejects simulation server-side. Recipient/window checks
remain enforced by the backend. NO, HELP and RESCHEDULE are audited only after
their recipient, snapshot and event-time checks pass. Delivery never confirms a
visit. No new tables are required. Session selection uses a request-scoped
context, with a separate test preview store; neither is swapped globally.
Zoho/source import routes are blocked and the external assistant is disabled.
Unknown test routes never fall through to production. The standalone daily
flow and its APIs remain available. Default mode remains preview.
Test-only queue metadata is separate from production metadata. With explicitly
enabled mounted live mode, the signature-verified existing WhatsApp callback
routes registered parent/offer-recipient events for the configured business phone to test
storage; other contacts keep their existing flow. This is covered by
`tests/test_flow_test_mount.py`. The daily solver and scheduling rules are unchanged.

The admin selects an initial roster and one Monday–Saturday date. New proposals
are restricted to that date while saved bookings and completed sessions across
the same week count toward demand. Saved unrelated visits remain fixed. The
fictional roster is A–H, all eligible for the initial plan. Anu handles Speech
and Ravi handles OT across the existing configured working day (09:00–16:50,
with the existing breaks). No artificial child-time restrictions or fixed
appointment times are supplied. Missing child availability means all working
slots are permitted; OR assigns actual times. Explicit future family restrictions
remain supported. Centre capacity remains two simultaneous sessions.
A and E each require Speech and OT; the others require one therapy. A–D's
existing weekly frequencies are preserved. E needs Speech twice and
OT twice weekly; F/H need Speech twice and G needs OT twice. These are fictional
test inputs, not clinical policy. The v1/v2-to-v3 data upgrade adds missing E–H
once and removes the old fixture's availability restrictions. It retains existing appointments,
offers, replies, absences, attendance, patient requirements and audit history;
it neither plans appointments nor sends messages. Existing populated test stores
upgrade only from the recognized v1 marker, with the upgrade audited. Conflicting
new child codes require review rather than replacement. This is covered by
`tests/test_flow_test_expansion.py`; older fixed-time regression fixtures remain separate.

The user explicitly authorized a fresh start in the live isolated test schema.
An authenticated test reset with the exact confirmation `RESET flow_test` clears
old appointments, offers, queued jobs, parent responses, Help requests, attendance,
absences, contact windows, manual reservations and old audit rows. It reseeds only
the eight fictional inputs and two therapists, with a new reset audit and cutoff.
It never accesses the main/public schema or sends a message. It is serialized
with the test worker; cached previews are cleared. Old inbound callbacks at or
before the reset cutoff cannot restore erased replies or open contact windows;
receipts for deleted messages are ignored. Fresh START is required before sending
new session offers. Resetting database history does not delete messages already
delivered to WhatsApp phones. This is covered by `tests/test_flow_test_reset.py`.
Confirmed save policy for the shared `/test` calendar: saving the selected day
automatically queues offers for all still-proposed visits on that date **only
when it is tomorrow in Asia/Kolkata**. Saving today or a later date queues no
offers; there is no clock-triggered publication when that date becomes tomorrow.
Previewing still sends nothing. Existing pending/confirmed/completed visits are
not republished. One continuous visit receives one offer covering its therapies.
Saving appointments, creating offers and queuing jobs commit together; missing
parent phone bindings reject the save without partial bookings or messages.
Closed contact windows no longer block the save: those offers are committed as
WAITING_FOR_CONTACT while other parents' offers proceed. A new verified parent
message resumes the waiting offer. The save response includes the waiting count.
Before saving, a fingerprint of current weekly bookings, requirements, absences,
availability, preferences and branch capacity must still match the reviewed
preview. Changed inputs require regeneration; no stale appointments/offers are saved.
Retrying a successful preview returns its saved result without another message;
regeneration/save also skips previously published visits. If the date crosses
midnight and changes whether the preview would send, staff must refresh it.
The save control states whether it queues real or preview offers. This behavior
is covered by `tests/test_flow_test_mount.py`; live deployment is a separate step.
The standalone daily API and replacement drawer retain explicit publication.
Replacement proposals generated by a decline are not automatically messaged.
Each offer retains its appointment snapshot and recipient binding. Preview mode
uses the same queue but sends nothing externally.

Preview mode sends nothing externally and permits simulated replies. Live mode
requires sender credentials and a reachable authenticated webhook. Zoho-imported
patients use their validated Parent No. directly, without a separate number list
or per-patient local file entry. Optional local recipient bindings remain for
the standalone seeded fixture. These modes use different
databases or test schemas. Live storage settings never redirect the default
preview into live records. Only current parent contacts or recipients already bound
to saved test offers on the configured business number can supply
accepted incoming events, and the HTTP webhook verifies Meta's signature first.
The webhook commits incoming jobs before acknowledgment. A single-process durable
worker applies them, deduplicating callback IDs across restarts.

Live coworker offers default to `FLOW_TEST_MESSAGE_MODE=session`: each coworker
sends START first, opening a 24-hour service window. Ordinary interactive
Yes/Cannot attend/Help buttons require no template within that window. The window
is bound to the coworker and receiving business number, using verified inbound
event time rather than callback receipt time. Old callbacks never advance it;
delivery receipts and outbound sends never open it. Admin publication validates
all recipients' phone bindings before saving offers. Closed windows create waiting
jobs; the worker rechecks the window before sending.
If a queued offer's window closes, the job waits as WAITING_FOR_CONTACT without
attempting a send. A fresh incoming coworker message resumes these unsent jobs.
The dashboard shows per-recipient readiness. START itself changes no appointment.
Session offers greet the explicitly configured coworker name, falling back to
the fictional patient's name. They state the published therapy, weekday/date,
centre and each session's Indian local time. A continuous visit lists all its
therapies in one message; names are never inferred from a WhatsApp profile.
The test label and visit reference remain present. Copy changes do not alter
appointment generation or reply-button payloads.
Optional `FLOW_TEST_MESSAGE_MODE=template` retains proactive offers through a
matching approved template; its configured three buttons are required only in
that mode. Session mode does not bypass template requirements outside 24 hours.

YES confirms the whole current visit. **Confirmed decision: Cannot attend / NO
means unavailable for the entire appointment date.** After offer/recipient/time
validation, it immediately stages explicit `day` responses for every unfinished
visit belonging to that child on that date, releases those bookings as
`NEEDS_RESCHEDULING`, and marks affected open offers `DECLINED`. There is no
visit/day follow-up question. Other children and dates remain unchanged; a shared
coworker phone does not broaden the absence to other fictional patients.
A repeated webhook cannot apply the same decline twice. Existing completed therapy
is never undone; a conflicting day declaration goes to staff review under the
unchanged response-service validation. Explicit legacy VISIT commands still retain
their supplied visit scope; DAY is equivalent to the new NO default. The decline uses
the shared response repair to propose eligible replacements only in released
openings, retaining pending/no-response visits and unrelated bookings. A proposed
replacement is reserved demand, not family confirmation. HELP/RESCHEDULE creates
an admin task without moving or releasing appointments. Unmatched/stale replies
are sent to the admin inbox. Delivered/read message receipts are not acceptance.
Offer snapshots and recipient binding reject replies to changed/closed visits.
The verified reply's event timestamp determines whether it met the deadline,
not the later processing time. A timely delayed reply may resolve an EXPIRED
offer if its booking is still retained; an explicitly CLOSED/released offer
cannot be restored. The corresponding expiry inbox item is resolved after a
timely confirmation/decline. Late events still go to staff review.
Response changes and follow-up jobs commit together or roll back together.

The configurable test offer deadline defaults to 600 minutes (10 hours) after sending.
Expiry flags staff follow-up and rejects late acceptance; it does not
automatically cancel the booking. An explicit admin action can release it.
The user-approved manual final-schedule export described below can instead assume
acceptance for a sent unanswered offer at the manual cutoff, with a distinct audit.
This test deadline does not answer any pending production response-policy
question. Ambiguous outgoing sends are retained as UNKNOWN without automatic
resending and create a deduplicated staff inbox item; unfinished incoming jobs resume on restart. Obsolete offers are not
sent. The test requires one running process per database.
The mounted and standalone workers send batches of up to five different parent
numbers concurrently (`FLOW_TEST_SEND_BATCH_SIZE`, 1–20). A batch contains only
one message per number; repeated sends to a shared phone are ordered and paced
six seconds apart in live mode. Every offer keeps its own durable send result
and reply deadline. The workflow lock still serializes import, reset and reply
mutations while a batch is in flight; HTTP threads never share ORM sessions.
An ambiguous failure is UNKNOWN and is not automatically resent. Preview draining
can still run one job at a time for deterministic checks. Batch sending does not
remove the provider contact-window requirement before sending. Inbound callback
persistence has a separate lock, so a slow outbound request cannot hold up the
webhook acknowledgment. Imports and resets coordinate with both locks.
Long session offers switch to compact wording without omitting any therapy or
time, and are checked against the existing 1,024-character body limit before queuing.

User-approved search correction: a one-day or released-opening run with an explicit
`new_booking_slots` boundary considers every valid start in that boundary. The
two-start-per-day thinning shortcut is retained for unbounded weekly searches.
A 100-child/10-consultant/two-branch regression fits all 100 daily sessions in
both quick and CP-SAT paths; blocking one doctor-slot reduces that to 99. All
existing clash, lunch, availability, assigned-doctor and weekly-target rules remain.

Staff records COMPLETED or NO_SHOW per confirmed therapy, allowing partial visit
attendance. Completed sessions count as delivered; proposed/pending/confirmed
sessions count as reserved; released/no-show sessions count as neither. The next
daily run uses this updated same-week record. Automatic cross-week carry-forward
and production fairness/priority changes are outside this isolated test.

## Confirmed operating rules

| ID | Rule | Status | Current behavior or work needed |
|---|---|---|---|
| R-01 | A child must not attend two different locations on the same day. If the treatment plan uses Location A and Location B, those therapies must be placed on different days. | **Implemented** | New therapy groups are split by location and placed on different days. The final safety check also rejects saved or manually moved schedules that put one child at multiple locations on the same day. |
| R-02 | Therapies during one visit should be continuous so the child does not have to wait between therapies. | **Implemented** | Therapies in a group are placed back-to-back and in treatment-plan order. |
| R-03 | A continuous therapy group must not cross the lunch break, 12:35–13:15. Sessions may start at 13:15. | **Implemented** | Morning and afternoon are treated as separate blocks. The day/week calendar displays the actual lunch gap from the shared slot configuration, rather than shading the first afternoon slot. |
| R-04 | Every treatment requirement must be delivered only by its assigned therapist. | **Implemented** | Therapist continuity is a hard constraint. If the assigned therapist is absent, unavailable, inactive, or fully booked, the scheduler records the session as unmet and carries it forward where applicable; it never assigns another therapist. |
| R-05 | Generate a rolling plan from the run date through the current week's Saturday and the next two full Monday–Saturday weeks. | **Implemented** | A daily run never creates appointments before its run date. It plans the remaining working days of the current week plus two future full working weeks. Each Monday–Saturday period has its own weekly target count. |
| R-06 | Running the scheduler again with new data must not rearrange the existing weekly schedule unnecessarily. | **Implemented for saved appointments** | Proposed, pending, confirmed, completed, and manually changed appointments in the selected Monday–Saturday week are kept in place. Only unmet needs are placed into remaining openings. Unsaved previews are not permanent and may be regenerated. |
| R-07 | Manual changes made by management or an administrator take priority. The scheduler must preserve them and work around them. | **Implemented for saved manual moves** | Staff can move an appointment or its complete continuous group. The change is checked for clashes, recorded as manual, and protected during later scheduling runs. An explicit lock/unlock policy remains in `questions.md`. |
| R-08 | When a child or therapist absence releases an appointment, the affected requirement must receive priority without rearranging unrelated saved appointments. | **Implemented** | Staff can record an absence. A rolling run keeps unrelated saved appointments fixed, ranks released requirements ahead of new demand, and attempts the earliest safe replacement. |
| R-09 | When a future appointment is cancelled, try to move the best suitable child from a later nearby day into the newly empty time. Start with the nearest following day, then look farther ahead. | **Partly implemented** | The application ranks suitable later visits by nearest date. An administrator can approve moving the selected visit, including its continuous group, into the cancelled opening. Automatically continuing the replacement chain remains an open decision. |
| R-10 | Schedule as many required therapies as safely possible and allow a longer search when it can produce a better result. | **Implemented with four search choices** | The browser scheduler always runs in Best Effort mode. For large workloads, the 20-second quick preview uses a validated greedy pass that returns promptly; it applies the same therapist, branch, availability, continuity, capacity, one-visit-per-day, and plan-date rules. The 1-minute thorough, 2-minute extended, and 5-minute deep choices use the CP-SAT optimizer. Every choice reports unmet therapy rather than failing the whole schedule. |
| R-11 | A child added after the schedule was created may use a matching therapist’s free future slot without disturbing existing appointments. | **Implemented** | On the next run, the new child’s active treatment requirements are included, all saved active appointments remain fixed, and only suitable remaining openings are considered. |
| R-12 | Every unmet therapy should show the main reason it was not placed and any other constraints that contributed. | **Implemented** | The unmet-demand report now distinguishes therapist contention, location saturation, the one-visit-per-day limit, fixed availability or absence restrictions, continuous-block restrictions, and a search ending before an otherwise valid opening was used. This diagnostic does not change the generated timetable. |
| R-13 | Active therapists work every configured slot from Monday through Saturday unless they are marked absent; children may attend any configured slot. | **Implemented** | This is a manager-confirmed operating policy. It creates availability records for all active therapists and patients during import, without assigning a time preference. |
| R-14 | `Plan_Detail` is the source of demand for the active scheduler. | **Implemented** | The approved source sheets are persisted as `plan_detail`, `therapist_master`, and `specialty_master` tables. The scheduler projection is built from those tables: each valid row in a child's latest plan version creates a treatment requirement with its registration ID, therapy type, weekly frequency, assigned therapist, branch, duration, and plan start date. Exact duplicate rows and non-positive frequencies are excluded with import warnings. |
| R-16 | Therapy and specialty names must use `Specialty_Master` canonical values. | **Implemented** | The importer validates therapy types against the master mapping and stores its canonical specialty. A differing supplied specialty is normalized and logged for review. |
| R-17 | A patient absence or cancellation should show children who can fill the released appointment without breaking therapist continuity. | **Implemented** | Selecting a released appointment in Operations opens a dedicated **Possible replacements** drawer. Candidates need the same therapy, branch, and assigned therapist; must be available and not absent at the exact opening; and must still have an unmet weekly target. Suggestions never reassign the therapist or book anyone automatically. |
| R-22 | A patient's recurring preferred day and morning/afternoon time band must influence scheduling without preventing therapy when another available time is the only safe option. | **Implemented** | The patient preference table stores Patient ID, preferred day, and preferred morning or afternoon band. The scheduler retains a preferred candidate during its compact candidate search and ranks that day/time combination ahead of other available slots. |
| R-23 | If a weekly target cannot be safely met by Saturday, the unmet sessions must carry into the following week's target with priority. | **Implemented** | The rolling planner solves the current remaining week first, adds any unmet sessions to the next week's demand, and continues the carry-over through the two-week planning window. |
| R-20 | Today's-session views must allow an operator to narrow the displayed appointments by branch, therapist, and patient. | **Implemented** | The dashboard filters the current day's loaded appointments in the browser and updates the displayed count. Filtering does not alter appointments or scheduling rules. |
| R-15 | Repeated visits should have a gap wherever capacity permits. | **Implemented as a preference** | The scheduler prefers even spacing—such as Monday, Wednesday, Friday for 3× weekly—but will use consecutive or wider-gap days when that is the only way to meet weekly demand. Therapies belonging to the same visit remain continuous. |
| R-18 | A child’s therapy must be delivered at the branch recorded on the treatment plan. | **Implemented** | The scheduler uses the `Plan_Detail` branch as the appointment location and only considers therapist capability at that branch. |
| R-19 | A therapy plan is schedulable only from its plan start date until its stated duration ends. | **Implemented** | The scheduler rejects slots before the plan starts or after its calendar-month duration ends. Plans without a valid duration retain no expiry date and are logged during import. |
| R-20 | A cancellation or absence must not rearrange unrelated existing appointments. | **Implemented** | Only the affected appointment is cancelled or released for rescheduling. Replacement-child and pull-forward lists are advisory only; no other appointment is moved, cancelled, or rebooked unless an administrator explicitly makes and validates that change. |
| R-21 | Zoho Creator appointment sync uses the `therapy-scheduler` application's single flat schedule form. | **Implemented** | Each appointment is posted only to `All_Therapy_Schedules`, including its appointment date. Patient, therapist, therapy type, branch, and therapy plan are plain-text fields, so the sync never creates or depends on separate Creator lookup records. A one-time Zoho consent stores a local refresh token so restart reconnection is automatic. Bulk exports select unsynced active appointments only, send at most 200 in one Creator request, and store a local sync record only for Creator-confirmed rows. |
| R-22 | Creator Plan Detail imports must not overwrite the scheduler's live source data without explicit approval. | **Implemented** | A manual pull reads `Plan_Detail_Report` into PostgreSQL `zoho_plan_detail_records`, keyed by the immutable Creator record ID. Repeated pulls update only those staging rows. The live `plan_detail` source used by scheduling remains unchanged until a separately approved promotion workflow is added. |
| R-24 | A conversational scheduling assistant may interpret an operator request, but it must never make scheduling decisions or access scheduling data on its own. | **Implemented** | Azure OpenAI receives only the operator's current message and returns a constrained JSON action. The local scheduler calculates read-only daily, centre, therapist, therapy, patient, absence, rescheduling, available-slot answers, and a maximum of ten names from the last summary. If the model response is unavailable, the local application still handles a named patient summary, a named therapist availability request, and a greeting without sending or exposing scheduling data. A named available-slot request is scoped to the resolved therapist and lists only that therapist's unoccupied, available times; a general request returns the first openings across therapists. A named patient summary is scoped to that patient’s appointments and accepts the patient’s name, code, or displayed `Name · Code` label; it takes precedence over the prior-name-list follow-up shortcut. Therapy labels may omit a trailing “Session”; therapist lookups may omit a trailing source-system code only when that base name resolves uniquely, otherwise the assistant presents temporary-session choices and uses the operator’s number, full display name, or therapist code. A chat absence uses the existing full-day absence workflow: a patient’s released sessions include advisory replacements, while a therapist’s released sessions are listed for rescheduling. An ambiguous patient or therapist name requires an explicit temporary-session choice before any change. Browser-tab memory retains only the last summary scope and a pending choice for 30 minutes, and Clear memory removes that context and its visible conversation without changing the schedule. A cancellation still requires one uniquely matched active appointment; ambiguous or missing matches change nothing, and replacement suggestions use existing scheduling rules. |
| R-25 | WhatsApp webhook verification and test messages must not change appointments or expose a public send endpoint. | **Implemented for webhook receipt, backend test sender, Help acknowledgment, and button-click records** | Meta webhook verification returns its challenge only when the configured verify token matches. Incoming events require a valid `X-Hub-Signature-256` calculated with the Meta App Secret and are acknowledged without scheduling changes or logging message content. Authenticated template quick replies and interactive reply-button taps for the configured sender are retained in `whatsapp_button_clicks` with the WhatsApp profile name (nullable, not a verified patient identity), phone number, button label/payload, inbound message ID, receiving phone ID, and timezone-aware click/recording times (see R-26 for stored Indian-time columns). Repeated callbacks produce one record per receiving phone ID and message ID, including old taps that are no longer eligible for an auto-reply. Names and phone numbers are not written to application logs or exposed through a new public API. A fresh template Help button tap queues the exact reply "Do you want to reschedule or contact staff?" to the tapping user. A durable hashed event key prevents duplicate replies to repeated callbacks; only pending sends resume after restart, and expired events or ambiguous prior send attempts are not resent. Replying to this question does not automatically reschedule or notify staff. A trusted operator can run the backend-only test sender with a configured approved template and configured test recipient; it is not exposed through the browser API. Patient/guardian identity linking, consent, retention/reporting policy, appointment messages, and further action-button handling require a separate approved workflow. |
| R-26 | WhatsApp button-click records must retain Indian local time using Asia/Kolkata. | **Implemented** | The backend writes aware Asia/Kolkata timestamps. PostgreSQL keeps the original instants in timestamptz columns and physically stores database-generated `clicked_at_ist` and `recorded_at_ist` local timestamps, with `time_zone` set to Asia/Kolkata. Direct reports use the `_ist` columns without a second conversion. Startup migrates legacy UTC columns transactionally and idempotently, preserving each instant. The Help queue's UTC expiry calculations and all other scheduling timestamps remain unchanged. |

## Assumed parent-response simulation (R-27)

**Implemented for the isolated offline demo, not connected to WhatsApp or Zoho.**
`parent_availability_responses` holds assumed YES/NO replies linked to an appointment's
date and slot. A YES confirms the entire continuous visit without changing its time,
therapist, or branch. An appointment without a response is unchanged. A NO releases
the entire affected visit as `NEEDS_RESCHEDULING`. Each NO input must explicitly state
`visit` (the original visit's time range) or `day` (the whole appointment date);
the demo API retains explicit scope inputs; the connected test adapter now supplies
`day` for Cannot attend according to the confirmed rule above.

Only openings released on the selected Monday–Saturday date may receive new
replacement proposals. They must match the released therapist, therapy, branch,
and slot and pass the existing availability, absence, plan-date, capacity, and
continuous-group constraints. Inactive patients cannot receive new proposals.
Every unrelated saved appointment remains fixed, including no-response visits and
existing appointments later in the week. Replacement appointments are saved as
`PROPOSED`, never assumed to have family acceptance. Separate alternative suggestions
for declined families may use the remaining current week; they are not saved or booked.
This entry point does not generate appointments two weeks ahead or change the existing
browser generator's rolling planning behavior.

Weekly progress is per treatment requirement: target minus completed sessions minus
existing proposed, pending, or confirmed sessions. A cancellation, no-show, or released
appointment does not count as delivered/booked. Unsaved alternatives do not reduce the
remaining count. New solver visits use distinct bundle identifiers so they cannot be
merged with a pinned visit from an earlier run. Replaying a processed reply makes no
additional changes; stale or conflicting replies reject the batch, and a solver failure
rolls back its changes. Covered by `tests/test_parent_responses.py`.

The isolated browser mode (`python app.py --response-demo`, loopback port 8766)
exposes this workflow with a deterministic 80-patient dataset, 12 therapists, two
branches, and three therapies. A button invokes the real scheduling service in a
background worker, with state/result views for original appointments, released slots,
tentative replacements, weekly counts, and unsaved alternatives. Repeated clicks
cannot start another run against consumed replies. Reset creates a new synthetic
database and retains earlier files; it never resets the configured application
database. The demo's 30-second deterministic CP-SAT settings live in `config.py`.
The dataset includes an opening with no eligible replacement, which stays empty.
Covered by `tests/test_parent_response_demo_workspace.py` and the existing
response-service tests.

## Local admin reservations (R-28)

**Implemented in the main replacement drawer and local demo; covered by
`tests/test_admin_reservations.py`, `tests/test_main_ui_reservations.py`, and
`tests/test_parent_response_demo_workspace.py`.**
In Operations → Needs rescheduling → Possible replacements, and for released
calendar appointments, opening the slot shows an admin
patient picker by default. Administrators may use the suggested patient or search
the full patient list. Confirm without messaging reserves the selected slot/visit
immediately as `CONFIRMED`; no WhatsApp or Zoho request is made. Confirm with
messaging remains disabled in the production drawer; the isolated `/test`
released-slot drawer supports **Send offer & wait for confirmation** as described below.

Therapy/assigned-therapist/branch mismatches, patient or therapist availability,
absence, inactive or missing plans, weekly frequency, capacity, conflicting bookings,
and splitting a continuous visit produce warnings rather than a scheduling-policy
block. An explicit Reserve anyway action authorizes saving despite the displayed
warnings. The reservation records the local admin identity and the warnings accepted.
Automatic solver suggestions retain their existing constraints; this override is an
explicit administrator action, not permission for the solver to bypass rules.

A continuous released visit is selected in full by default; an admin may choose one
slot after reviewing grouping warnings. The selected tentative suggestion is confirmed
in place or superseded by the chosen patient's reservation, without moving unrelated
appointments. A slot already reserved by another request must be refreshed; duplicate
submissions of the same request must not create another booking. Invalid references
and stale previews are input/concurrency errors, not clinical-policy restrictions.
An off-plan reservation does not satisfy an unrelated therapy's weekly target.
For a patient without any requirement row, an inactive zero-frequency technical row
marked `ADMIN_RESERVATION_ONLY` maintains the appointment relationship; it is not an
active clinical plan and creates no solver demand. Local reservations use the
additive `admin_booking_decisions` and `admin_slot_reservations` tables. Alternatives
from the original automatic run are hidden after a manual reservation because their
availability assumptions may be stale. The main scheduler uses its existing configured
database and the same reservation service through reservation-context, preview, and
reserve endpoints. Reserved source rows are hidden from the open-slot list and the
calendar; superseded tentative rows remain in history. Patient cancellations also
offer this workflow. Production admin booking does not send external updates.

### Test replacement offers

**Implemented; covered by `tests/test_flow_test_mount.py`.** In `/test`, choose a
patient for a released slot, review warnings, then use **Send offer & wait for
confirmation**. Any displayed warnings are explicitly overridden by that action.
The replacement, reservation tracking, offer and outgoing queue job commit together;
the visit becomes `PENDING_CONFIRMATION`, not confirmed. Reused solver suggestions
and newly created sessions share one complete-visit offer. Live mode requires a
registered parent recipient; a closed session window queues a waiting offer.
Failed validation leaves the original draft, opening and queue unchanged.
Retries return the original booking and offer, including after a reply, without
resending or resetting the status. Only a valid Yes reply confirms the visit.
Cannot attend immediately records whole-day unavailability, releases the affected
bookings and runs repair; Help enters the existing admin inbox. Delivery receipts never confirm.
Preview mode uses the same queue/reply handling without contacting WhatsApp.
This uses existing test tables and the shared OR constraints. Empty calendar
cells retain the existing without-messaging flow.

### Empty calendar cells

**Implemented; covered by `tests/test_empty_slot_reservations.py`.** Double-clicking
an empty Day or Mon–Sat calendar cell (or focusing it and pressing Enter) opens the
same admin patient picker to create a new appointment. The date/time and, in a doctor
column, the doctor are prefilled. The admin selects or changes the doctor, therapy,
and branch, searches the original patient list, reviews warnings, then confirms.
This creates a `CONFIRMED` appointment without messaging or a fabricated cancelled
appointment. Opening the drawer and previewing write nothing. The tracking row points
to the new appointment itself; no additional schema change is required.
If that booking is later cancelled, it returns to the replacement workflow normally.
The 19-table test schema supports the doctor's primary department/branch without
needing extra mapping tables. Missing optional mapping tables grant no additional
capabilities or branch assignments; mismatch warnings still apply. This path is
also covered by `tests/test_flow_test_readiness.py`.

The same weekly-target, continuity, availability, and conflict warnings apply. Doctor
capability/branch mismatches and a date outside the Monday–Saturday week also warn.
Existing appointments hidden by filters still participate in conflict checks; this
flow never cancels or replaces them, including proposed appointments. Concurrent
changes require a fresh preview, and repeating the same confirmation is idempotent.
The admin may explicitly override the displayed scheduling warnings. A new single
session does not automatically create or move the patient's other therapies.

## Existing safety and capacity rules

These rules are already enforced by the scheduling engine, but the manager should confirm that they match real operations.

| ID | Rule | Status |
|---|---|---|
| S-01 | A therapist cannot treat two children at the same time. | **Implemented** |
| S-02 | A child cannot attend two therapies at the same time. | **Implemented** |
| S-03 | There is no separate room constraint. A therapist's one-child-per-slot limit is the practical capacity at each location. | **Implemented**; each location's stored ceiling equals its active therapist count, so it cannot limit bookings before the therapist rule does. |
| S-04 | A therapist who is marked absent cannot be booked during the absence. | **Implemented**; staff can record a full-day therapist absence in the manager interface and affected appointments are released for rescheduling. |
| S-05 | A therapist must be active and recorded as available for the time. | **Implemented** |
| S-06 | A child must be recorded as available for the time. | **Implemented**; the confirmed baseline records every child as available for all configured slots. |
| S-07 | Saved active appointments are kept in place during a new scheduling run. | **Implemented**; this includes proposed, pending, confirmed, completed, and manually changed appointments. |
| S-08 | Appointments use fixed 40-minute periods. | **Implemented** |
| S-09 | The working days are Monday through Saturday; Sunday is excluded. | **Implemented**; confirmed by management. |
| S-10 | If all requests cannot fit, the safe schedule is kept and missed therapies are reported rather than creating clashes. | **Implemented** in Best Effort mode. |

## Manager decisions still needed

### D-01: More than one visit in a day

Example: a child receives Speech at 9:00 and OT at 9:40 as one continuous visit. A 3:00 opening is also available that day.

**Decision needed:** May the child return for a second visit at 3:00, or must every child have no more than one visit per day? The current system allows only one therapy group per child per day.

### D-05: Which later appointments may be pulled forward after a cancellation

Proposed order for choosing a pull-forward replacement in the remaining days of the current Monday–Saturday week:

1. Look at suitable appointments on the next calendar day, then each later day in the current week, continuing only as far as management permits.
2. The child must be available at the empty time and must not have another appointment then.
3. The therapy and qualified therapist must match the empty slot.
4. Keep the child’s continuous therapy group together; do not move just one part and create a waiting gap.
5. Never move an administrator-locked appointment automatically.
6. Decide whether a confirmed family appointment may be moved automatically or only after family approval.
7. Prefer the move that fills the cancellation with the fewest changes to everyone else.

**Decision needed:** Confirm how many future days may be searched, whether confirmed appointments require approval, and whether the system should continue pulling later appointments forward to fill each newly created opening.

## Change-protection order

Unless management decides otherwise, the intended protection order is:

1. Administrator-locked manual appointments — never change automatically.
2. Confirmed appointments — keep unless an authorised person approves a move.
3. Previously saved proposed appointments — keep when unaffected; change only to respond to new data or fill a cancellation.
4. New, unsaved suggestions — may be freely improved.

This protection order is the agreed direction but is **not fully implemented**.

## Location-compatible splitting

When one treatment plan contains therapies from different locations, the scheduler now creates a separate continuous group for each location and places those groups on different days. It no longer rejects all therapies merely because the original treatment plan spans multiple locations.

## Cancellation example

A child scheduled on Thursday cancels. The intended workflow is:

1. Keep every unrelated appointment unchanged.
2. Show children who need the same therapy at the same branch with the same therapist and can attend the newly empty time.
3. Optionally check later appointments in the remaining week for a suitable pull-forward move.
4. Move the child’s complete continuous therapy group forward, subject to the protection rules above.
5. Recheck the schedule for clashes, availability, location capacity, and therapist absence.
6. Present the change for approval and record who approved it.
7. Optionally repeat the process for the new opening created on the later day, if management approves this cascading behavior.

## Zoho test input and daily draft — 22 October 2026

- The authorized read source is only `nidhin.kumar_turnb/test-gokul`, reports
  `Plan_Report` and `All_Consultants`. Import APIs are authenticated and mounted
  only under `/test`; the source job has no direct database connection and no Zoho write path.
- Confirmed test Schedule format: `therapy, sessions/week, duration in months,
  consultant`; `;` or a newline separates requirements. Entered order is retained.
  Consultants are matched by branch, name and consultation type. New branches
  are imported as separate test locations; identical consultant names at different
  branches do not collide. Text preprocessing normalizes Unicode width, spaces,
  tabs, newlines and invisible paste markers. Branch, consultant and therapy
  matching ignores case; Registration ID case is retained. Missing fields,
  ambiguous matches, invalid source IDs and malformed frequencies fail
  validation before any projection changes.
  Delimiter-only empty schedules and durations outside valid calendar dates are rejected.
- Confirmed revision (23 October 2026): Added Time and Modified Time are both IST.
  Each latest plan's Added Time supplies its treatment
  start date directly from its Asia/Kolkata date, without adding another offset.
  Modified Time still selects updates; it does not reset treatment start. The old
  shared `--plan-start`/`planStart` input is accepted only for compatibility and no
  longer overrides source dates. Calendar-month duration and bounds use the shared OR loader.
- The first run fetches all relevant rows before its cutoff. Later runs query
  Added Time OR Modified Time in `[last successful checkpoint, new cutoff)`.
  Criteria use Asia/Kolkata boundaries for both fields. Both are compared as UTC
  instants on import; the saved checkpoint remains UTC. For a 03:00 IST cutoff,
  both Zoho fields use 03:00 IST and the stored checkpoint uses 21:30 UTC on the previous date.
  All report pages must finish before import. Source deltas merge by Creator ID;
  missing rows mean no change, never deletion. Valid empty windows preserve data
  and advance the checkpoint. Data, source cache and checkpoint commit atomically;
  validation failures and stale concurrent runs cannot advance it.
- A checkpoint/source cache using an older time-zone contract requires one explicit
  `--apply --reconcile` pass. It fetches all rows before a frozen cutoff, merges
  by stable IDs without clearing data or deleting absent rows, and commits the
  field-specific time-zone marker/checkpoint with the import. Retained cached
  records are translated from the previous contract; source rows read during
  reconciliation take precedence because the earlier time-zone interpretation
  may have made their cached modification times appear too recent.
  Existing booking/history guards still apply. Incremental API requests must
  declare Added_Time=Asia/Kolkata and Modified_Time=Asia/Kolkata; old clients cannot silently
  advance the checkpoint. Invalid imports preserve the old cache and checkpoint.
- The user removed the A–H/count/name-prefix fixture limits. Any valid Registration
  ID and any number of consultants may be imported from the authorized test app
  across the branches in that test app. Initial input needs at least one plan and consultant. A
  new or edited plan row for a known Registration ID uses the complete latest
  Modified Time plan, with Added Time breaking ties. Distinct latest rows with
  both timestamps identical require review. The latest Schedule replaces the
  old plan snapshot; old schedules are not unioned. Unchanged therapy requirements
  retain their IDs and progress when a new therapy is added. An existing source
  ID cannot change its Registration ID.
  Following original rule S-03, each imported branch's capacity equals its active
  consultant count (minimum stored ceiling one when no active staff, with no new
  appointments possible for inactive staff). There is no separate two-room limit.
  The seed-only legacy fixture still starts with two active therapists/capacity two.
- Imported source, scenario and sync metadata use test-only `flow_test_settings`
  records; no production schema changes or additional tables are introduced.
  Repeated imports retain patient/requirement identities. Historical fixture
  appointments block switching sources until a deliberate fresh start. Frequency,
  order and date/duration changes update the current requirement in place; saved
  appointments and completed progress keep their IDs and values. Thus 2/week →
  3/week with two existing bookings leaves one outstanding session. History-related
  changes produce deduplicated warnings in the import response and staff inbox.
  Reduced targets do not cancel excess bookings automatically. Removing a therapy
  with history sets future demand to zero and retains its historical assignment
  for fixed-booking validation. New doctor/branch assignments on requirements with
  history remain pending staff review, retaining the existing requirement while
  other valid records import; a warning recurs until that conflict is resolved.
  Consultant capability/status changes with history and Registration ID mutations
  still require deliberate review. Sync never resets or rewrites appointment history.
  Per the user's instruction, valid Parent No.
  values from the authorized Zoho test records supply the recipient bindings
  directly; no additional manually configured number pool is required. Imported
  contacts update the transport and webhook routing and survive restart without
  a local recipient file. When a source phone changes, pending offers that have
  not been sent follow the new number and wait for its contact window if needed.
  Sent or uncertain offers retain their original phone binding and continue
  receiving their own replies/receipts; they are not resent automatically.
  Failed imports do not activate proposed contacts. Unregistered arbitrary
  numbers cannot be sent to, and a different family cannot confirm an offer.
- The daily job imports through the running app, so its existing lock also
  serializes imports, saves, replies and worker activity. It prepares tomorrow's
  unsaved draft in India time and skips a Sunday target. The generator opens that
  draft with the existing review/save controls, refreshing against current bookings.
  Only an explicit staff save publishes offers. No sync or timer sends WhatsApp.
- The Azure job launcher defaults to a manual job and requires a successful live
  test import first. `-EnableDailySchedule` configures 03:00 IST (21:30 UTC).
  Deployment and a live test import are still required after these local changes.

Covered by `tests/test_flow_test_zoho.py`, `tests/test_flow_test_zoho_incremental.py`,
`tests/test_flow_test_zoho_contacts.py`,
`tests/test_flow_test_source_rules.py`, `tests/test_flow_test_send_batches.py`,
the shared API/read/reset tests, and a
local Postman HTTP simulation based on masked real test report responses.
