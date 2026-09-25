# Open Questions for Management

This file contains only decisions that still need management confirmation. When a decision is made, move it into `rules.md` and update the application and tests in the same change.

## Scheduling decisions

1. **More than one visit in one day:** After a child finishes one continuous visit, may the child return later that day for another visit at the same location? Current behavior allows only one generated visit per day.

2. **Confirmed appointment changes:** May the system move a confirmed appointment automatically when filling a cancellation, or must it always ask an administrator/family first? Recommended: always require approval.

3. **Cancellation chain:** After moving a proposed appointment from day 8 into an opening on day 7, should the system continue by moving another appointment into the new day-8 opening? Recommended: show the complete proposed chain and require one approval before saving it.

4. **Cancellation search distance:** Should the system search only the next three days, or every remaining day in the current Monday–Saturday schedule? Recommended: search the remaining week, nearest date first.

## Absence and manual-change decisions

13. **Manual lock duration:** Should an administrator's manual change remain locked until explicitly unlocked? Recommended: yes.

14. **Family approval:** Which proposed changes require family confirmation before saving: time changes, day changes, therapist substitutions, or all three?
