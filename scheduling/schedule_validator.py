from collections import Counter, defaultdict

from scheduling.slot_config import SLOTS, valid_consecutive_block
from scheduling.types import ScheduledAppointment, SchedulingProblem, ValidationIssue


REPORT_KEYS = (
    "wrong_therapist",
    "therapist_overlaps",
    "patient_overlaps",
    "absence_conflicts",
    "patient_absence_conflicts",
    "location_capacity_violations",
    "patient_location_transitions",
    "invalid_slot_durations",
    "broken_consecutive_bundles",
    "therapist_availability_conflicts",
    "patient_availability_conflicts",
)


def validate_schedule(problem: SchedulingProblem, appointments: list[ScheduledAppointment]):
    issues: list[ValidationIssue] = []
    requirement_map = {item.id: item for item in problem.requirements}
    therapist_slots = Counter((a.therapist_id, a.date, a.slot_index) for a in appointments)
    patient_slots = Counter((a.patient_id, a.date, a.slot_index) for a in appointments)
    location_slots = Counter((a.location_id, a.date, a.slot_index) for a in appointments)
    patient_day_locations: dict[tuple[int, object], set[int]] = defaultdict(set)

    for appointment in appointments:
        requirement = requirement_map.get(appointment.requirement_id)
        if not requirement or (
            appointment.therapist_id != requirement.therapist_id
            or appointment.department_id != requirement.department_id
        ):
            issues.append(ValidationIssue("wrong_therapist", "Assigned therapist/department mismatch."))
        if not 0 <= appointment.slot_index < len(SLOTS):
            issues.append(ValidationIssue("invalid_slot_durations", "Unknown slot index."))
        if (appointment.therapist_id, appointment.date, appointment.slot_index) in problem.absences:
            issues.append(ValidationIssue("absence_conflicts", "Therapist is absent."))
        if (appointment.patient_id, appointment.date, appointment.slot_index) in problem.patient_absences:
            issues.append(ValidationIssue("patient_absence_conflicts", "Patient is absent."))
        weekday = appointment.date.weekday()
        if (appointment.therapist_id, weekday, appointment.slot_index) not in problem.therapist_availability:
            issues.append(ValidationIssue(
                "therapist_availability_conflicts", "Therapist is unavailable in the requested slot."
            ))
        if (appointment.patient_id, weekday, appointment.slot_index) not in problem.patient_availability:
            issues.append(ValidationIssue(
                "patient_availability_conflicts", "Patient is unavailable in the requested slot."
            ))
        patient_day_locations[(appointment.patient_id, appointment.date)].add(appointment.location_id)

    for count in therapist_slots.values():
        if count > 1:
            issues.append(ValidationIssue("therapist_overlaps", "Therapist is double-booked."))
    for count in patient_slots.values():
        if count > 1:
            issues.append(ValidationIssue("patient_overlaps", "Patient is double-booked."))
    for (location_id, _, _), count in location_slots.items():
        if count > problem.location_capacities.get(location_id, 0):
            issues.append(ValidationIssue("location_capacity_violations", "Location capacity exceeded."))
    for locations in patient_day_locations.values():
        if len(locations) > 1:
            issues.append(ValidationIssue(
                "patient_location_transitions", "Patient is scheduled at multiple locations in one day."
            ))

    by_bundle: dict[str, list[ScheduledAppointment]] = defaultdict(list)
    for appointment in appointments:
        if appointment.bundle_id:
            by_bundle[appointment.bundle_id].append(appointment)
    for bundle_appointments in by_bundle.values():
        dates = {item.date for item in bundle_appointments}
        slots = sorted(item.slot_index for item in bundle_appointments)
        locations = {item.location_id for item in bundle_appointments}
        if (
            len(dates) != 1
            or len(locations) != 1
            or slots != list(range(slots[0], slots[0] + len(slots)))
            or not valid_consecutive_block(slots[0], len(slots))
        ):
            issues.append(ValidationIssue("broken_consecutive_bundles", "Bundle is not consecutive."))

    report = {key: sum(issue.code == key for issue in issues) for key in REPORT_KEYS}
    return issues, report


def validate_move(
    problem: SchedulingProblem,
    existing: list[ScheduledAppointment],
    appointment: ScheduledAppointment,
    replacement: ScheduledAppointment,
) -> list[ValidationIssue]:
    if appointment.bundle_id and appointment.bundle_id != replacement.bundle_id:
        return [ValidationIssue("broken_consecutive_bundles", "Move the entire bundle together.")]
    candidate = [item for item in existing if item != appointment] + [replacement]
    issues, _ = validate_schedule(problem, candidate)
    return issues
