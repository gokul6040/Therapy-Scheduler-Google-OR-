# Open Questions for Management

This file contains only decisions that still need management confirmation. When a decision is made, move it into `rules.md` and update the application and tests in the same change.

## Scheduling decisions

1. **More than one visit in one day:** After a child finishes one continuous visit, may the child return later that day for another visit at the same location? Current behavior allows only one generated visit per day.

2. **Confirmed appointment changes:** May the system move a confirmed appointment automatically when filling a cancellation, or must it always ask an administrator/family first? Recommended: always require approval.

3. **Cancellation chain:** After moving a proposed appointment from day 8 into an opening on day 7, should the system continue by moving another appointment into the new day-8 opening? Recommended: show the complete proposed chain and require one approval before saving it.

4. **Cancellation search distance:** Should the system search only the next three days, or every remaining day in the current Monday–Saturday schedule? Recommended: search the remaining week, nearest date first.

## Absence and manual-change decisions

13. **Manual lock duration:** Should an administrator's manual change remain locked until explicitly unlocked? Recommended: yes.

14. **Automatic changes and family approval:** Which changes initiated by the system
    require family confirmation before saving? Explicit local admin reservations
    without messaging are confirmed immediately, with warning overrides permitted
    by R-28; that decision does not authorize automatic changes by the solver.
    Separately, R-31 now records the user's approved manual-cutoff treatment of
    sent unanswered offers; that does not resolve the policy for other automatic changes.

15. **Source plan/patient deactivation:** Which Zoho field or explicit action means
    a patient or plan has stopped, and what should staff do with existing future
    bookings? Missing rows in an incremental pull are not deletions. Do not infer
    cancellation or erase completed progress from a missing source record.
