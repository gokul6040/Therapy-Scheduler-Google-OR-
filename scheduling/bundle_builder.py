from collections import defaultdict

from scheduling.types import BundleItem, RequirementInput, TherapyBundle


def build_bundles(
    requirements: list[RequirementInput], period_index: int = 0,
) -> list[TherapyBundle]:
    """Build consecutive, same-location therapy visits for one seven-day period."""
    by_patient: dict[int, list[RequirementInput]] = defaultdict(list)
    for requirement in requirements:
        if requirement.sessions_per_week > 0:
            by_patient[requirement.patient_id].append(requirement)

    bundles: list[TherapyBundle] = []
    for patient_id in sorted(by_patient):
        patient_requirements = sorted(
            by_patient[patient_id], key=lambda item: (item.sequence_order, item.id)
        )
        max_frequency = max(item.sessions_per_week for item in patient_requirements)
        for occurrence in range(max_frequency):
            by_location: dict[int, list[BundleItem]] = defaultdict(list)
            for item in patient_requirements:
                if item.sessions_per_week <= occurrence:
                    continue
                by_location[item.location_id].append(BundleItem(
                    requirement_id=item.id,
                    department_id=item.department_id,
                    therapist_id=item.therapist_id,
                    location_id=item.location_id,
                    plan_start_date=item.plan_start_date,
                    plan_end_date=item.plan_end_date,
                ))
            for location_id, location_items in by_location.items():
                bundles.append(
                    TherapyBundle(
                        id=(
                            f"P{patient_id}-W{period_index + 1}-"
                            f"B{occurrence + 1}-L{location_id}"
                        ),
                        patient_id=patient_id,
                        occurrence_index=occurrence,
                        items=tuple(location_items),
                        period_index=period_index,
                    )
                )
    return bundles
