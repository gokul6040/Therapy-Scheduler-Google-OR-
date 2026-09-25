# Therapy Scheduler Rules

This is the plain-language source of truth for how the scheduler should behave.
Update a rule here before changing the scheduler.

## Status key

- **Implemented:** The current application enforces the rule.
- **Partly implemented:** Some supporting behavior exists, but the full workflow is not ready.
- **Not implemented:** This is agreed behavior that still needs to be built.
- **Manager decision needed:** The application should not assume an answer yet.

## Confirmed operating rules

| ID | Rule | Status | Current behavior or work needed |
|---|---|---|---|
| R-01 | A child must not attend two different locations on the same day. If the treatment plan uses Location A and Location B, those therapies must be placed on different days. | **Implemented** | New therapy groups are split by location and placed on different days. The final safety check also rejects saved or manually moved schedules that put one child at multiple locations on the same day. |
| R-02 | Therapies during one visit should be continuous so the child does not have to wait between therapies. | **Implemented** | Therapies in a group are placed back-to-back and in treatment-plan order. |
| R-03 | A continuous therapy group must not cross the lunch break. | **Implemented** | Morning and afternoon are treated as separate blocks. |
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
