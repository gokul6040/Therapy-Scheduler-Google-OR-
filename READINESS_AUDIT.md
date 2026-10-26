# Scheduling flow readiness audit — 23 October 2026

Subsequent authorized addition: manual final-schedule export to test-gokul's
Scheduler form / Scheduler_Report is implemented locally. Manual cutoff assumes
sent unanswered offers accepted with a distinct audit/status, preserving explicit
replies and staff-review cases. Unique-key create/update, resumable batches and
IST completion/cutoff timestamps are tested over local HTTP. See `flow_test/EXPORT.md`
and the newest HANDOVER section. The user still needs two form fields, one-time
OAuth update/form-definition permission consent, deployment and a real push test.
No actual Scheduler write or live finalisation was performed by the agent.

Scope: the isolated Zoho → import → one-day OR → review/save → WhatsApp offer →
reply → replacement → attendance → weekly-progress flow. No live database reset,
import, deployment, real message, Zoho write, or timer activation was performed.
Changes below are local until the user deploys them. This is a functional audit,
not a production sign-off or a guarantee of zero defects.

Subsequent source-contract update on 23 October: the user confirmed **Added Time
is now IST, while Modified Time remains UTC**. Per-field criteria, comparison and
client/deployment checks were updated. Existing UTC caches require explicit
reconciliation, preserving bookings and translating retained source rows. The real
CLI/local HTTP migration, empty-delta and full workflow/outage checks passed; a
read-only real Zoho fetch validated 13 patients/two consultants. These newer
changes also await deployment; see the latest HANDOVER.md section for commands
and final regression count. No live import or reset was performed.

## Verified problems and fixes

| Problem | Fix | Evidence |
|---|---|---|
| A preview could be saved after bookings, requirements, absences or availability changed | Compare current weekly scheduling inputs with the reviewed fingerprint before saving; reject stale previews atomically | New tests for absence, target and competing-booking changes |
| One closed parent contact window blocked the entire save | Save all valid offers together; closed-window offers wait visibly while other parents proceed; a verified parent message resumes them | Independent-parent send/resume regression and shared UI inspection |
| An on-time reply processed after expiry was rejected | Check the parent's verified event timestamp against the deadline; a timely reply can resolve a retained expired offer, but never an explicitly released/closed one | Delayed Yes/No, late-event and released-offer tests |
| Slow outbound HTTP calls held up incoming callback persistence | Separate inbound persistence lock, coordinated with import/reset; sending threads still never share sessions | A callback commits while an outbound send remains blocked; reset waits for sends |
| Complete visits with long therapy labels could exceed the interactive message limit | Use compact wording without omitting therapies/times, then validate length before queuing | Five long therapy labels fit in one message |
| Delimiter-only schedules could remove demand; enormous durations could fail later during planning | Reject empty schedule entries and unrepresentable calendar dates before import commits | Invalid-source rollback regressions |
| Uncertain sends were visible only in queue history | Add deduplicated staff inbox warnings on failure and interrupted-send recovery; keep no automatic resend | Failure/no-resend, restart and warning tests |
| Redeployment forced session mode, replacing an existing template choice | Preserve the selected messaging mode, with an explicit `-MessageMode` override | Windows PowerShell tests |
| A healthy old revision could satisfy deployment checks | Require the requested image to be the latest ready revision as well as healthy test endpoints | Old-image rejection and new-image acceptance tests |
| Daily candidate thinning left usable slots empty | User-approved change retains all valid candidates for explicitly bounded daily/replacement runs; unbounded weekly search retains its shortcut | 100 fictional patients/10 consultants/two branches: 40 sessions before, 100 after; 99 when one doctor-slot is blocked; quick and CP-SAT paths validated |
| Manual empty-cell booking queried optional mapping tables absent from the 19-table test schema | Primary department/branch works directly; absent extra mappings grant no additional capabilities and still produce mismatch warnings | Empty-cell save and mismatch tests; table count remains 19 |

The OR search correction changes candidate enumeration, not clinical rules.
Assigned doctors, patient/doctor clashes, operating hours, breaks/lunch,
availability, absences, branch capacity, weekly demand and existing bookings
continue to be validated. No fixed patient/doctor count or two-room limit is added.

## Checks run

- Full pytest suite, including shared OR and workflow regressions:
  **397 passed in 217.09 seconds**, including the per-field time-zone migration
  and manual Scheduler export regressions. The contact-window regression fixes its local
  clock so a fixture appointment does not become a past live-send date overnight;
  the application guard remains enabled.
- Real sync CLI and real shared HTTP handlers against local Postman simulations,
  dummy OAuth and temporary SQLite. The reusable check is
  `postman/checks/verify_daily_flow.py`.
- HTTP scenario: eight source patients → seven initial offers with H held as
  standby → repeated save without duplication → Help and staff resolution → Yes
  → completed attendance → another child's No → replacement drawer offer for H
  → H's Yes → no-show → correct remaining weekly demand.
- Simulated Zoho HTTP 503: no import/checkpoint advance and saved appointments
  remain unchanged. Simulation request logs confirm OAuth/report calls and paging.
- Local UI fixture: publish controls remain usable when contact windows are closed;
  waiting offers explain that no message has been sent and the reply window starts
  after sending. Screenshot: `outputs/audit_waiting_offer.png`. It uses a dummy,
  network-free sender and fictional contacts, regardless of the displayed live-mode label.
- JavaScript syntax and the scoped UI detector pass. No design-rule suppressions
  were added; existing visual styling was retained.

The Postman mock is based on the existing masked test-report contract, not an
owner-published provider mock. Local passes do not verify Meta delivery, templates,
provider quotas, Azure PostgreSQL concurrency or production-scale performance.

## Live state observed, read-only

- Azure app: `orscheduler`, ready revision `orscheduler--0000020`, image
  `therapy-scheduler:test-flow-20261022-153956`; it predates these local changes.
- Current observed messaging mode is `session`.
- Azure job `orscheduler-dailyjob` remains `Manual`.
- A current source fetch converted successfully under the updated input rules.
  A fresh direct PostgreSQL source-cache identity comparison timed out, so the
  earlier TEST-K/L Registration ID corrections were not reverified. Do not assume
  they are resolved or change the firewall/reset data to work around this check.

## Required before production release

| Remaining work | Why it remains |
|---|---|
| Deploy to the existing test app/job and repeat the real WhatsApp scenario | All current fixes are local. PostgreSQL and provider behavior must be checked in the deployed environment. |
| Configure and verify an approved proactive WhatsApp template | Current session mode waits for parents to message first. Template name, language, variables and button payloads require a real provider test. |
| Individual staff authentication, permissions and attributed audits | The test uses one admin key and fixed test-admin audit identity. Production staff accountability has not been implemented here. |
| Failure alerts, sync-freshness checks, backup/restore and recovery drill | These operational outcomes have not been verified by this local audit. An ambiguous send must be reconciled before any resend. |
| Staff resolution of plan/doctor/branch/identity conflicts and source deactivation | Warnings retain affected bookings. Resolving an inbox item is not an approved plan-transfer action; absent delta rows are intentionally not deletions. Source stop/archive behavior needs an agreed rule. |
| Scale and deployment ownership plan | Current worker assumes one process and one replica. Unbounded weekly searches still thin candidates; this audit verified the bounded one-day path. Do not increase replicas without durable cross-process coordination. |

The daily trigger remains manual at the user's request. Production Zoho write-back,
scheduled reminders and the test Scheduling assistant remain intentionally deferred.
The newer test Scheduler export is manual and awaits its fields/consent/live check.
Test-only storage/source boundaries and clear fictional labels remain in place.

## User-run deployment

No reset is required:

```powershell
& .\flow_test\deploy_azure.ps1
& .\flow_test\deploy_zoho_job.ps1
az containerapp job start --name orscheduler-dailyjob --resource-group TherapyScheduler
az containerapp job execution list --name orscheduler-dailyjob --resource-group TherapyScheduler --output table
```

The job stays manual. Imports draft tomorrow's calendar; staff still reviews and
saves it. Any source identity error must be corrected deliberately rather than
silently renaming existing imported patients.
