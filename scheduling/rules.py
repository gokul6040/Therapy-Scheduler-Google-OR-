from datetime import timedelta

from scheduling.slot_config import WORKING_DAYS, valid_consecutive_block
from scheduling.types import SchedulingProblem, TherapyBundle


def placement_rejection_reason(
    problem: SchedulingProblem,
    bundle: TherapyBundle,
    day: int,
    start_slot: int,
    therapist_ids: tuple[int, ...] | None = None,
    check_therapists: bool = True,
) -> str | None:
    target_date = problem.week_start + timedelta(days=day)
    weekday = target_date.weekday()
    if day < 0 or day >= problem.horizon_days or weekday not in WORKING_DAYS:
        return "NON_WORKING_DAY"
    if problem.planning_start_date and target_date < problem.planning_start_date:
        return "PAST_PLANNING_DATE"
    if not valid_consecutive_block(start_slot, len(bundle.items)):
        return "CONSECUTIVE_BLOCK_UNAVAILABLE"
    locations = {item.location_id for item in bundle.items}
    if len(locations) > 1:
        return "LOCATION_TRANSITION_IMPOSSIBLE"
    if therapist_ids is None:
        therapist_ids = tuple(item.therapist_id for item in bundle.items)
    for offset, item in enumerate(bundle.items):
        slot = start_slot + offset
        if item.plan_start_date and target_date < item.plan_start_date:
            return "PLAN_NOT_STARTED"
        if item.plan_end_date and target_date > item.plan_end_date:
            return "PLAN_ENDED"
        if (bundle.patient_id, weekday, slot) not in problem.patient_availability:
            return "PATIENT_AVAILABILITY"
        if (bundle.patient_id, target_date, slot) in problem.patient_absences:
            return "PATIENT_ABSENCE"
        if check_therapists:
            therapist_id = therapist_ids[offset]
            if therapist_id not in problem.active_therapists:
                return "NO_VALID_ASSIGNED_THERAPIST"
            if (therapist_id, weekday, slot) not in problem.therapist_availability:
                return "THERAPIST_CAPACITY"
            if (therapist_id, target_date, slot) in problem.absences:
                return "THERAPIST_ABSENCE"
        if problem.location_capacities.get(item.location_id, 0) < 1:
            return "LOCATION_CAPACITY"
    return None
