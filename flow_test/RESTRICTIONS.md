# Current test limits and assumptions — 22 October 2026

This describes the implementation, including temporary test assumptions. Changes
to this file do not deploy code or enable a timer. The latest branch/start-date,
warning and batch-send changes are local until the updated app is deployed.

## Contact rule changed at the user's request

The extra manually approved phone-number pool has been removed for Zoho imports.
Each patient's valid Parent No. becomes their contact automatically after a
successful import, including new numbers and phone edits. No second local entry
is required. Contacts survive restart without a local recipients file.

Sending still uses a registered patient's number and an appointment offer.
The app has no endpoint for sending arbitrary messages to unrelated numbers.
Unsent queued or waiting offers follow a changed phone number. Sent or uncertain
offers keep their original recipient and are not automatically resent; their
replies and receipts continue to route to the test flow. Sender/offer matching remains
mandatory, so one family cannot answer another family's offer.

Optional `recipients.local.json` with explicit consent remains a way to configure
the original seeded fixture or greeting names. It is not an import allowlist.

## Test boundaries and configured defaults

| Area | Current implementation | Reason/status |
|---|---|---|
| Zoho source | Only `nidhin.kumar_turnb/test-gokul`, `Plan_Report` and `All_Consultants` | Authorized test boundary, not a general Zoho importer |
| Branch | Any branch in the two authorized test reports; patients match consultants within their branch | Locations and capacities remain separate |
| Storage | PostgreSQL `flow_test`, separate preview storage; 19 test tables | Isolation from main/public data |
| Patient/doctor count | No A–H, exact-count or TEST-name-prefix restriction | Any number of valid source patients and consultants; bootstrap needs at least one of each |
| Start date | Each latest plan's Added Time is already IST; use its date directly | Modified Time is IST and does not change the start date; old shared start argument no longer overrides it |
| Capacity | Active consultant count at each branch | Matches original rule S-03: three active consultants allow three parallel sessions, subject to other OR rules |
| Availability | Source rows seed all Mon–Sat working slots | Zoho does not supply detailed doctor/patient availability; explicit saved restrictions/absences are still respected |
| Job trigger | Manual, job `orscheduler-dailyjob` | User explicitly deferred daily automation |
| Daily planning | Drafts tomorrow in India time; Sunday target skipped | One-day adapter using full-week demand and existing bookings |
| Solver time | Automatic draft uses 5 seconds; manual generator exposes longer options | Finite search time; a feasible result need not be optimal |

## Input validation and sync behavior

- Schedule entries have exactly `therapy, sessions/week, duration months, consultant`.
  Use semicolon/newline between entries. Frequency and duration must be positive
  integers. A therapy may appear only once within the selected plan snapshot.
- Spaces, tabs, repeated whitespace, Unicode width and common invisible paste
  characters are normalized. Branch, consultant and therapy matching ignores
  case; Registration ID case stays meaningful. Missing values and misspellings
  are not guessed. A different branch is supported when matching consultants exist.
- The consultant must match by branch, name and consultation type. Ambiguous matches or
  invalid/inactive capability combinations cannot create valid appointments.
  A consultant row currently describes one consultation type, not a skill list.
- Creator record IDs must be numeric and unique. Registration ID must be nonempty
  and at most 40 characters; consultant name at most 120 characters. Parent phone
  must normalize to international digits (8–15 digits, no leading zero).
- Added Time and Modified Time are both Asia/Kolkata, as confirmed by the user.
  The checkpoint stays UTC and each field's query boundaries use its own zone. Incremental runs
  use a frozen half-open window and an atomic checkpoint. Old timezone checkpoints
  require one explicit full reconciliation, preserving data and history.
- Repeated Registration IDs select the complete latest Modified Time record,
  with Added Time breaking ties. A tie on both times across distinct latest rows
  requires review. Older plan text is not unioned into the current plan.
- A source row cannot silently change its Registration ID. Missing rows in a
  delta/reconciliation are not treated as deletions.
- Frequency/date/duration/order changes apply with a warning when history exists.
  Existing bookings and progress remain; a 2→3 target with two bookings adds one
  outstanding session. Removing a therapy with history sets its demand to zero;
  staff review existing bookings. Doctor/branch changes on booked requirements
  retain the affected requirement with a warning; other patients still import.
  Consultant capability/status changes with history still stop for review.
- Validation is atomic: one invalid/conflicting row fails the import and retains
  the previous database/cache/checkpoint. Other valid rows are not partially imported.
- Switching from the old seeded fixture to Zoho with saved fixture appointments
  is blocked until a deliberate fresh start. This is not needed for normal syncs.

## Messaging and operational behavior

| Area | Current behavior |
|---|---|
| Review/save | Import/job creates an unsaved draft and sends no messages. Staff reviews and saves. |
| Automatic offers | Shared calendar save queues offers only for tomorrow in India time. Saving today or a later date sends none automatically; there is no later clock-triggered publication. |
| Session messaging | Requires an open contact window before sending. A closed window creates a visible waiting offer; a new verified parent message resumes it. |
| Calendar save | Appointment/offer/job writes remain atomic. Missing phone bindings reject the save. A closed window for one parent does not block other parents. Changed scheduling inputs invalidate an older preview. |
| Template messaging | Supported with the configured approved template, language, variables and Yes / Cannot attend / Help buttons. Deployment preserves the current mode; `-MessageMode` is an explicit override. |
| Offer grouping | One offer per continuous visit, listing its therapies. Pending/confirmed/completed visits are not repeatedly published. |
| Reply expiry | Default 600 minutes / 10 hours after sending, configurable from 1–1,440 minutes. Existing sent offers retain their recorded deadline. |
| No response | Expiry alone retains the booking for follow-up. Explicit manual final-schedule export assumes sent unanswered offers accepted, with an audit and ASSUMED_CONFIRMED output status. Help/unsent/uncertain offers remain for staff. |
| Cannot attend | Means unavailable for the whole appointment date; releases unfinished bookings. Completed-session conflicts require review. |
| Replacement | Staff selects a replacement and chooses immediate confirmation or an offer-and-wait. Decline repair does not automatically message every proposed replacement. |
| Attendance | Per-therapy COMPLETED/NO_SHOW is accepted only for confirmed appointments. |
| Delivery | Delivery/read receipts do not confirm attendance or appointments. |
| Queue | Batches of up to five different numbers send concurrently; one message per number per batch. Repeated sends to one live number are ordered and paced six seconds apart. Durable results and UNKNOWN/no-blind-retry behavior remain. |
| Authentication | Test admin authentication, Meta webhook signature checks, receiving business ID, offer token, sender and event-time validation remain. Old reset-era callbacks cannot restore erased state. |

Timely replies are judged by their verified event time, even if processing is
delayed. A released/closed offer stays closed. Failed or uncertain outbound sends
produce staff inbox items and are not blindly retried. Callback persistence is
independent of slow outbound HTTP requests. Complete long visits use compact
message wording to remain within the existing interactive-body limit.

## Established OR rules, unchanged

The OR chooses actual times. Sessions are 40 minutes within the configured
09:00–16:50 Mon–Sat day, with breaks 10:20–10:35, lunch 12:35–13:15 and
15:15–15:30. Therapy groups cannot cross lunch. Capacity, doctor/patient clashes,
weekly requirements, absences, preferences and fixed existing appointments are
checked. Currently one generated visit per child per day is supported. Other
clinical/operational decisions remain in `../questions.md`; complete shared
scheduling rules are in `../rules.md`. The user approved retaining every valid
candidate time for bounded daily/replacement runs after the audit reproduced
40/100 sessions with thinning versus 100/100 without it. Clinical constraints
are unchanged. Unbounded weekly search still uses its existing candidate shortcut.

## Features still outside this implementation

- Production rollout. Test-only Scheduler write-back is implemented as a separate
  manual command; see `EXPORT.md`. Its fields, OAuth consent and live verification remain prerequisites.
- Scheduled reminders and automatic schedule approval/publication.
- The Scheduling assistant for test data (disabled).
- Consultant-phone notifications; current messages are appointment offers to parents.

The daily trigger remains manual until the user explicitly requests otherwise.
