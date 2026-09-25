from collections import Counter
from dataclasses import replace
from datetime import date

from config import SolverSettings
from scheduling.cp_sat_scheduler import generate_weekly_schedule
from scheduling.schedule_validator import validate_schedule
from scheduling.types import ScheduledAppointment, SolverMode
from tests.conftest import req


def assert_valid(result):
    assert result.status in ("OPTIMAL", "FEASIBLE")
    assert not any(result.validation.values())


def test_one_patient_one_therapy_schedules(base_problem):
    result = generate_weekly_schedule(base_problem([req(1, 1, 1, 1)]))
    assert_valid(result)
    assert result.scheduled_sessions == 1
    assert result.appointments[0].therapist_id == 1


def test_patient_preference_prioritizes_a_preferred_monday_morning(base_problem):
    problem = base_problem([req(1, 1, 1, 1)])
    problem.patient_preferences[(1, 0, 0)] = 100

    result = generate_weekly_schedule(problem)

    assert_valid(result)
    assert result.appointments[0].date == problem.week_start
    assert result.appointments[0].slot_index < 5


def test_scheduler_never_places_new_visits_before_the_run_day(base_problem):
    problem = base_problem([req(1, 1, 1, 1)])
    problem.planning_start_date = date(2026, 9, 10)

    result = generate_weekly_schedule(problem)

    assert_valid(result)
    assert result.appointments[0].date >= date(2026, 9, 10)


def test_absence_priority_wins_when_only_one_safe_slot_remains(base_problem):
    problem = base_problem(
        [req(1, 1, 1, 1), req(2, 2, 1, 1)],
        therapist_availability={(1, 0, 0)},
        patient_availability={(1, 0, 0), (2, 0, 0)},
    )
    problem.priority_requirement_ids = {2}

    result = generate_weekly_schedule(problem)

    assert_valid(result)
    assert result.appointments[0].requirement_id == 2


def test_large_quick_preview_uses_a_validated_fast_schedule(base_problem):
    requirements = [req(index, index, 1, index) for index in range(1, 121)]

    result = generate_weekly_schedule(
        base_problem(requirements, capacities={1: 120}),
        settings=SolverSettings(max_time_seconds=20),
    )

    assert_valid(result)
    assert result.statistics["strategy"] == "quick_greedy"
    assert result.scheduled_sessions == 120


def test_therapist_is_not_double_booked(base_problem):
    availability = {(1, 0, 0)}
    patients = {(1, 0, 0), (2, 0, 0)}
    result = generate_weekly_schedule(base_problem(
        [req(1, 1, 1, 1), req(2, 2, 1, 1)], availability, patients
    ))
    assert_valid(result)
    assert result.scheduled_sessions == 1
    assert len(result.unmet) == 1
    assert result.unmet[0].reason == "THERAPIST_CAPACITY"
    assert result.unmet[0].secondary_reasons == ()


def test_patient_is_not_double_booked(base_problem):
    problem = base_problem([req(1, 1, 1, 1), req(2, 1, 2, 2, order=1)])
    result = generate_weekly_schedule(problem)
    assert_valid(result)
    keys = [(a.patient_id, a.date, a.slot_index) for a in result.appointments]
    assert len(keys) == len(set(keys))


def test_therapist_absence_blocks_appointment(base_problem):
    problem = base_problem(
        [req(1, 1, 1, 1)],
        therapist_availability={(1, 0, 0)},
        patient_availability={(1, 0, 0)},
    )
    problem.absences.add((1, date(2026, 9, 7), 0))
    result = generate_weekly_schedule(problem)
    assert result.scheduled_sessions == 0
    assert result.unmet[0].reason == "THERAPIST_ABSENCE"


def test_location_capacity_is_respected(base_problem):
    availability = {(1, 0, 0), (2, 0, 0)}
    patients = {(1, 0, 0), (2, 0, 0)}
    result = generate_weekly_schedule(base_problem(
        [req(1, 1, 1, 1), req(2, 2, 2, 2)], availability, patients, {1: 1}
    ))
    assert_valid(result)
    assert result.scheduled_sessions == 1
    assert result.unmet[0].reason == "LOCATION_CAPACITY"


def test_unmet_demand_reports_daily_visit_limit_as_secondary_reason(base_problem):
    requirements = [
        req(1, 1, 1, 1, location=1),
        req(2, 1, 2, 1, location=2),
    ]
    problem = base_problem(
        requirements,
        therapist_availability={(1, 0, 0)},
        patient_availability={(1, 0, 0)},
        capacities={1: 1, 2: 1},
    )

    result = generate_weekly_schedule(problem)

    assert_valid(result)
    assert result.scheduled_sessions == 1
    assert result.unmet[0].reason == "THERAPIST_CAPACITY"
    assert result.unmet[0].secondary_reasons == ("PATIENT_DAILY_VISIT_LIMIT",)


def test_insufficient_capacity_returns_unmet_not_invalid_booking(base_problem):
    requirements = [req(index, index, 1, 1) for index in range(1, 4)]
    availability = {(1, 0, 0)}
    patients = {(index, 0, 0) for index in range(1, 4)}
    result = generate_weekly_schedule(base_problem(requirements, availability, patients))
    assert_valid(result)
    assert result.scheduled_sessions == 1
    assert sum(item.missing_sessions for item in result.unmet) == 2


def test_strict_mode_reports_infeasible(base_problem):
    requirements = [req(1, 1, 1, 1), req(2, 2, 1, 1)]
    problem = base_problem(requirements, {(1, 0, 0)}, {(1, 0, 0), (2, 0, 0)})
    result = generate_weekly_schedule(problem, SolverMode.STRICT)
    assert result.status == "INFEASIBLE"


def test_bundle_is_consecutive_and_same_day(base_problem):
    requirements = [req(1, 1, 1, 1, 1, 0), req(2, 1, 2, 2, 1, 1)]
    result = generate_weekly_schedule(base_problem(requirements))
    assert_valid(result)
    assert len({a.date for a in result.appointments}) == 1
    assert sorted(a.slot_index for a in result.appointments) in [list(range(i, i + 2)) for i in range(9) if i != 4]
    assert Counter(a.patient_id for a in result.appointments)[1] == 2


def test_cross_location_therapies_are_split_across_days(base_problem):
    requirements = [req(1, 1, 1, 1, location=1), req(2, 1, 2, 2, order=1, location=2)]
    problem = base_problem(requirements, capacities={1: 2, 2: 2})
    result = generate_weekly_schedule(problem)
    assert_valid(result)
    assert result.scheduled_sessions == 2
    assert len({item.date for item in result.appointments}) == 2


def test_default_schedule_covers_one_monday_to_saturday_week(base_problem):
    problem = base_problem([req(1, 1, 1, 1, frequency=2)])
    result = generate_weekly_schedule(problem)
    assert_valid(result)
    assert result.requested_sessions == 2
    assert result.scheduled_sessions == 2
    assert max((item.date - problem.week_start).days for item in result.appointments) <= 5


def test_substitute_therapist_is_never_used_when_assigned_therapist_is_unavailable(base_problem):
    problem = base_problem(
        [req(1, 1, 1, 1)],
        therapist_availability={(2, 0, 0)},
        patient_availability={(1, 0, 0)},
    )
    problem.active_therapists.add(2)
    problem.eligible_therapists = {1: (1, 2)}
    result = generate_weekly_schedule(problem)
    assert_valid(result)
    assert result.scheduled_sessions == 0
    assert result.unmet


def test_validator_rejects_an_appointment_with_a_different_therapist(base_problem):
    problem = base_problem([req(1, 1, 1, 1)])
    problem.active_therapists.add(2)
    problem.eligible_therapists = {1: (1, 2)}
    appointment = ScheduledAppointment(
        patient_id=1,
        requirement_id=1,
        therapist_id=2,
        department_id=1,
        location_id=1,
        date=problem.week_start,
        slot_index=0,
        bundle_id="manual-substitute",
    )

    issues, _ = validate_schedule(problem, [appointment])

    assert any(issue.code == "wrong_therapist" for issue in issues)


def test_patient_absence_blocks_appointment(base_problem):
    problem = base_problem(
        [req(1, 1, 1, 1)],
        therapist_availability={(1, 0, 0)},
        patient_availability={(1, 0, 0)},
    )
    problem.patient_absences.add((1, date(2026, 9, 7), 0))
    result = generate_weekly_schedule(problem)
    assert result.scheduled_sessions == 0
    assert result.unmet[0].reason == "PATIENT_ABSENCE"


def test_plan_dates_block_slots_before_start_and_after_end(base_problem):
    requirement = req(1, 1, 1, 1)
    problem = base_problem(
        [replace(
            requirement,
            plan_start_date=date(2026, 9, 10),
            plan_end_date=date(2026, 9, 12),
        )],
        therapist_availability={(1, day, 0) for day in range(6)},
        patient_availability={(1, day, 0) for day in range(6)},
    )

    result = generate_weekly_schedule(problem)

    assert_valid(result)
    assert result.scheduled_sessions == 1
    assert date(2026, 9, 10) <= result.appointments[0].date <= date(2026, 9, 12)


def test_same_patient_frequency_occurs_on_distinct_days(base_problem):
    result = generate_weekly_schedule(base_problem([req(1, 1, 1, 1, frequency=3)]))
    assert_valid(result)
    assert len({a.date for a in result.appointments}) == 3


def test_weekly_frequencies_are_evenly_paced_when_capacity_allows(base_problem):
    problem = base_problem([
        req(1, 1, 1, 1, frequency=3),
        req(2, 2, 2, 2, frequency=2),
    ])

    result = generate_weekly_schedule(problem)

    assert_valid(result)
    days_by_patient = {
        patient_id: sorted({
            (item.date - problem.week_start).days
            for item in result.appointments
            if item.patient_id == patient_id
        })
        for patient_id in (1, 2)
    }
    assert [later - earlier for earlier, later in zip(
        days_by_patient[1], days_by_patient[1][1:]
    )] == [2, 2]
    assert [later - earlier for earlier, later in zip(
        days_by_patient[2], days_by_patient[2][1:]
    )] == [3]


def test_331_plan_keeps_therapies_together_and_repeats_the_visit_every_two_days(
    base_problem,
):
    problem = base_problem([
        req(1, 1, 1, 1, frequency=3, order=0),
        req(2, 1, 2, 2, frequency=3, order=1),
        req(3, 1, 3, 3, frequency=1, order=2),
    ])

    result = generate_weekly_schedule(problem)

    assert_valid(result)
    visit_dates = sorted({item.date for item in result.appointments})
    visits = [
        [
            item.requirement_id
            for item in sorted(
                (item for item in result.appointments if item.date == visit_date),
                key=lambda item: item.slot_index,
            )
        ]
        for visit_date in visit_dates
    ]
    assert visits == [[1, 2, 3], [1, 2], [1, 2]]
    assert [(later - earlier).days for earlier, later in zip(
        visit_dates, visit_dates[1:]
    )] == [2, 2]


def test_cadence_preference_does_not_drop_sessions_when_only_consecutive_days_work(
    base_problem,
):
    availability = {(1, day, 0) for day in range(3)}
    problem = base_problem(
        [req(1, 1, 1, 1, frequency=3)],
        therapist_availability=availability,
        patient_availability={(1, day, 0) for day in range(3)},
    )

    result = generate_weekly_schedule(problem)

    assert_valid(result)
    assert result.scheduled_sessions == 3
    assert sorted((item.date - problem.week_start).days for item in result.appointments) == [
        0, 1, 2,
    ]


def test_gap_preference_does_not_block_widely_spaced_available_sessions(
    base_problem,
):
    availability = {(1, day, 0) for day in (0, 4, 5)}
    problem = base_problem(
        [req(1, 1, 1, 1, frequency=3)],
        therapist_availability=availability,
        patient_availability={(1, day, 0) for day in (0, 4, 5)},
    )

    result = generate_weekly_schedule(problem)

    assert_valid(result)
    assert result.scheduled_sessions == 3
    assert not result.unmet


def test_two_weekly_sessions_may_use_the_required_four_day_gap(base_problem):
    availability = {(1, day, 0) for day in (0, 4)}
    problem = base_problem(
        [req(1, 1, 1, 1, frequency=2)],
        therapist_availability=availability,
        patient_availability={(1, day, 0) for day in (0, 4)},
    )

    result = generate_weekly_schedule(problem)

    assert_valid(result)
    assert result.scheduled_sessions == 2
