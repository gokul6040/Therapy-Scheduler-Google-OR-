"""Rolling, day-aware schedule generation for daily operations."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import replace
from datetime import date, timedelta

from sqlalchemy import select

from config import SOLVER_SETTINGS, SolverSettings
from database.models import Appointment, AppointmentStatus
from database.repository import load_scheduling_problem
from scheduling.cp_sat_scheduler import generate_weekly_schedule
from scheduling.types import ScheduleResult, SolverMode


WORKING_DAYS_PER_WEEK = 6
FUTURE_FULL_WEEKS = 2


def monday(value: date) -> date:
    """Return the Monday containing ``value``."""
    return value - timedelta(days=value.weekday())


def generate_rolling_schedule(
    session,
    run_date: date,
    *,
    mode: SolverMode = SolverMode.BEST_EFFORT,
    settings: SolverSettings = SOLVER_SETTINGS,
) -> tuple[ScheduleResult, date, date]:
    """Plan from ``run_date`` through two following full working weeks.

    The current week only receives appointments on or after ``run_date``. Any
    unsatisfied weekly demand is added to the next week's target, where it is
    treated as high priority. Saved active appointments remain pinned.
    """
    current_week = monday(run_date)
    week_starts = [
        current_week + timedelta(days=7 * index)
        for index in range(FUTURE_FULL_WEEKS + 1)
    ]
    planning_end = week_starts[-1] + timedelta(days=WORKING_DAYS_PER_WEEK - 1)
    needs_by_week: dict[int, set[int]] = defaultdict(set)
    for item in session.scalars(select(Appointment).where(
        Appointment.status == AppointmentStatus.NEEDS_RESCHEDULING,
        Appointment.date <= planning_end,
    )):
        week_index = max(0, min(
            FUTURE_FULL_WEEKS,
            (item.date - current_week).days // 7,
        ))
        needs_by_week[week_index].add(item.treatment_requirement_id)

    carryover: Counter[int] = Counter()
    results: list[ScheduleResult] = []
    appointments = []
    for week_index, week_start in enumerate(week_starts):
        planning_start = max(run_date, week_start)
        priority_requirements = needs_by_week[week_index] | set(carryover)
        problem = load_scheduling_problem(
            session,
            week_start,
            preserve_confirmed=True,
            horizon_days=WORKING_DAYS_PER_WEEK,
            planning_start_date=planning_start,
            carryover_by_requirement=dict(carryover),
            priority_requirement_ids=priority_requirements,
        )
        weekly_settings = replace(
            settings,
            max_time_seconds=max(0.5, settings.max_time_seconds / len(week_starts)),
        )
        result = generate_weekly_schedule(problem, mode=mode, settings=weekly_settings)
        results.append(result)
        appointments.extend(result.appointments)
        carryover = Counter({
            item.requirement_id: item.missing_sessions
            for item in result.unmet
            if item.missing_sessions > 0
        })

    validation: Counter[str] = Counter()
    for result in results:
        validation.update(result.validation)
    final_result = results[-1]
    status = "INVALID_SOLUTION" if any(validation.values()) else (
        "FEASIBLE" if all(result.status in ("OPTIMAL", "FEASIBLE") for result in results)
        else final_result.status
    )
    combined = ScheduleResult(
        status=status,
        appointments=appointments,
        unmet=final_result.unmet,
        requested_sessions=sum(result.requested_sessions for result in results),
        scheduled_sessions=len(appointments),
        validation=dict(validation),
        statistics={
            "weeks_planned": len(week_starts),
            "rolling_start": run_date.isoformat(),
            "rolling_end": planning_end.isoformat(),
            "carryover_sessions": sum(carryover.values()),
            "variables": sum(int(result.statistics.get("variables", 0)) for result in results),
        },
    )
    return combined, current_week, planning_end
