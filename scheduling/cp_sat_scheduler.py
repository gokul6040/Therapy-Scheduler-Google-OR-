from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import replace
from datetime import timedelta
from math import ceil
from time import monotonic

from ortools.sat.python import cp_model

from config import SOLVER_SETTINGS, SolverSettings
from scheduling.bundle_builder import build_bundles
from scheduling.rules import placement_rejection_reason
from scheduling.schedule_validator import validate_schedule
from scheduling.slot_config import WORKING_DAYS, SLOTS
from scheduling.types import (
    ScheduleResult,
    ScheduledAppointment,
    SchedulingProblem,
    SolverMode,
    TherapyBundle,
    UnmetRequirement,
)


REASON_PRIORITY = (
    "PAST_PLANNING_DATE",
    "NO_VALID_ASSIGNED_THERAPIST",
    "PLAN_NOT_STARTED",
    "PLAN_ENDED",
    "THERAPIST_ABSENCE",
    "PATIENT_ABSENCE",
    "PATIENT_AVAILABILITY",
    "LOCATION_TRANSITION_IMPOSSIBLE",
    "CONSECUTIVE_BLOCK_UNAVAILABLE",
    "LOCATION_CAPACITY",
    "THERAPIST_CAPACITY",
    "PATIENT_DAILY_VISIT_LIMIT",
    "SOLVER_SEARCH_LIMIT",
)

# A full week has 60 possible single-session starts.  Keeping every one for
# every visit creates an impractically large model at production scale.  Two
# rotated starts per valid day retain weekday choice and spread patients across
# all daily slots, while keeping the CP-SAT model tractable.
MAX_CANDIDATE_STARTS_PER_DAY = 2


def _diverse_daily_candidates(
    problem: SchedulingProblem,
    bundle: TherapyBundle,
    candidates: list[tuple[int, int, tuple[int, ...]]],
) -> list[tuple[int, int, tuple[int, ...]]]:
    """Retain a small candidate set while preserving preferred time bands."""
    by_day: dict[int, dict[int, list[tuple[int, int, tuple[int, ...]]]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for day, start_slot, therapist_ids in candidates:
        by_day[day][start_slot].append((day, start_slot, therapist_ids))

    selected = []
    seed = bundle.patient_id + bundle.occurrence_index * 7 + bundle.items[0].requirement_id
    for day, by_start_slot in sorted(by_day.items()):
        starts = sorted(by_start_slot)
        rotation = (seed + day * 3) % len(starts)
        ordered_starts = starts[rotation:] + starts[:rotation]
        weekday = (problem.week_start + timedelta(days=day)).weekday()
        ordered_starts.sort(
            key=lambda start_slot: -sum(
                problem.patient_preferences.get((bundle.patient_id, weekday, start_slot + offset), 0)
                for offset in range(len(bundle.items))
            )
        )
        for start_slot in ordered_starts[:MAX_CANDIDATE_STARTS_PER_DAY]:
            selected.extend(by_start_slot[start_slot])
    return selected


def _candidate_placements(problem, bundles):
    candidates = defaultdict(list)
    rejected = defaultdict(Counter)
    for bundle in bundles:
        period_start = bundle.period_index * 7
        period_end = min(problem.horizon_days, period_start + 7)
        for day in range(period_start, period_end):
            for start_slot in range(len(SLOTS)):
                reason = placement_rejection_reason(
                    problem, bundle, day, start_slot, check_therapists=False,
                )
                if reason:
                    rejected[bundle.id][reason] += 1
                    continue

                target_date = problem.week_start + timedelta(days=day)
                weekday = target_date.weekday()
                therapist_options = []
                option_failure = None
                for offset, item in enumerate(bundle.items):
                    slot = start_slot + offset
                    therapist_id = item.therapist_id
                    if therapist_id not in problem.active_therapists:
                        option_failure = "NO_VALID_ASSIGNED_THERAPIST"
                        break
                    if (therapist_id, target_date, slot) in problem.absences:
                        option_failure = "THERAPIST_ABSENCE"
                        break
                    if (therapist_id, weekday, slot) not in problem.therapist_availability:
                        option_failure = "THERAPIST_CAPACITY"
                        break
                    therapist_options.append((therapist_id,))

                if option_failure:
                    rejected[bundle.id][option_failure] += 1
                    continue
                preferred = tuple(options[0] for options in therapist_options)
                candidates[bundle.id].append((day, start_slot, preferred))
        # A bounded daily/replacement run already limits the search space.
        # Retain all valid starts so ID rotation cannot hide available slots.
        if problem.new_booking_slots is None:
            candidates[bundle.id] = _diverse_daily_candidates(problem, bundle, candidates[bundle.id])
    return candidates, rejected


def _ranked_reasons(counter: Counter) -> tuple[str, ...]:
    """Return the main unmet reason followed by useful contributing reasons."""
    ranked = [reason for reason in REASON_PRIORITY if counter.get(reason)]
    ranked.extend(
        sorted(
            reason
            for reason, count in counter.items()
            if count and reason not in REASON_PRIORITY and reason != "NON_WORKING_DAY"
        )
    )
    return tuple(ranked) or ("THERAPIST_CAPACITY",)


def _selected_bundle_blockers(
    problem: SchedulingProblem,
    bundle: TherapyBundle,
    candidates: list[tuple[int, int, tuple[int, ...]]],
    therapist_usage: Counter,
    location_usage: Counter,
    patient_days: set[tuple[int, int]],
) -> Counter:
    """Explain which final-schedule constraints block a bundle's valid options."""
    blockers = Counter()
    for day, start, therapist_ids in candidates:
        candidate_blockers = set()
        if (bundle.patient_id, day) in patient_days:
            candidate_blockers.add("PATIENT_DAILY_VISIT_LIMIT")
        for offset, item in enumerate(bundle.items):
            slot = start + offset
            if therapist_usage[(therapist_ids[offset], day, slot)]:
                candidate_blockers.add("THERAPIST_CAPACITY")
            if location_usage[(item.location_id, day, slot)] >= problem.location_capacities.get(
                item.location_id, 0
            ):
                candidate_blockers.add("LOCATION_CAPACITY")
        if not candidate_blockers:
            candidate_blockers.add("SOLVER_SEARCH_LIMIT")
        blockers.update(candidate_blockers)
    return blockers


def _add_cadence_preferences(
    problem: SchedulingProblem,
    bundles: list[TherapyBundle],
    candidates: dict[str, list[tuple[int, int, tuple[int, ...]]]],
    variables: dict[tuple[str, int, int, tuple[int, ...]], cp_model.IntVar],
    model: cp_model.CpModel,
    objective: list,
    settings: SolverSettings,
) -> None:
    """Prefer evenly paced visits without sacrificing otherwise valid therapy."""
    frequencies = defaultdict(int)
    for requirement in problem.requirements:
        key = (requirement.patient_id, requirement.location_id)
        frequencies[key] = max(frequencies[key], requirement.sessions_per_week)

    grouped_bundles = defaultdict(list)
    selected = {}
    scheduled_day = {}
    for bundle in bundles:
        location_id = bundle.items[0].location_id
        grouped_bundles[(bundle.patient_id, location_id)].append(bundle)

        placement_vars = [
            variables[(bundle.id, day, start, therapist_ids)]
            for day, start, therapist_ids in candidates[bundle.id]
        ]
        is_selected = model.NewBoolVar(f"selected_{bundle.id}")
        model.Add(is_selected == sum(placement_vars))
        selected[bundle.id] = is_selected

        period_start = bundle.period_index * 7
        period_end = min(problem.horizon_days, period_start + 7)
        day_var = model.NewIntVar(
            period_start,
            max(period_start, period_end - 1),
            f"day_{bundle.id}",
        )
        # An unscheduled bundle uses the start of its period as a harmless sentinel.
        model.Add(
            day_var
            == period_start
            + sum(
                (day - period_start)
                * variables[(bundle.id, day, start, therapist_ids)]
                for day, start, therapist_ids in candidates[bundle.id]
            )
        )
        scheduled_day[bundle.id] = day_var

    for key, group in grouped_bundles.items():
        frequency = frequencies[key]
        if frequency <= 1:
            continue
        preferred_gap = max(1, 7 // frequency)
        ordered = sorted(
            group,
            key=lambda bundle: (bundle.period_index, bundle.occurrence_index),
        )
        for first, second in zip(ordered, ordered[1:]):
            # Do not connect non-adjacent seven-day periods when the horizon ends
            # with a partial period.
            if second.period_index - first.period_index > 1:
                continue
            target_gap = (
                (second.period_index - first.period_index) * 7
                + (second.occurrence_index - first.occurrence_index) * preferred_gap
            )
            if target_gap <= 0:
                continue

            both_selected = model.NewBoolVar(
                f"cadence_pair_{first.id}_{second.id}"
            )
            model.Add(both_selected <= selected[first.id])
            model.Add(both_selected <= selected[second.id])
            model.Add(
                both_selected >= selected[first.id] + selected[second.id] - 1
            )

            deviation = model.NewIntVar(
                0,
                problem.horizon_days + 7,
                f"cadence_deviation_{first.id}_{second.id}",
            )
            actual_gap = scheduled_day[second.id] - scheduled_day[first.id]
            model.Add(deviation >= actual_gap - target_gap).OnlyEnforceIf(
                both_selected
            )
            model.Add(deviation >= target_gap - actual_gap).OnlyEnforceIf(
                both_selected
            )
            model.Add(deviation == 0).OnlyEnforceIf(both_selected.Not())
            objective.append(-settings.cadence_deviation_penalty * deviation)


def _location_partitions(problem: SchedulingProblem) -> list[SchedulingProblem] | None:
    """Split a problem only when every child is assigned to one branch.

    Branches have independent therapist and room capacity in the imported
    master data.  Solving them separately avoids building one enormous model,
    while the patient daily-visit rule remains valid because a child never
    appears in more than one partition.  If that input assumption changes,
    return ``None`` and retain the global model for correctness.
    """
    patient_locations = defaultdict(set)
    requirements_by_location = defaultdict(list)
    for requirement in problem.requirements:
        patient_locations[requirement.patient_id].add(requirement.location_id)
        requirements_by_location[requirement.location_id].append(requirement)
    if len(requirements_by_location) < 2 or any(
        len(locations) > 1 for locations in patient_locations.values()
    ):
        return None

    partitions = []
    for location_id, requirements in sorted(requirements_by_location.items()):
        requirement_ids = {item.id for item in requirements}
        partitions.append(replace(
            problem,
            requirements=requirements,
            location_capacities={location_id: problem.location_capacities.get(location_id, 0)},
            eligible_therapists={
                requirement_id: problem.eligible_therapists.get(requirement_id, ())
                for requirement_id in requirement_ids
            },
            pinned=[item for item in problem.pinned if item.location_id == location_id],
        ))
    return partitions


def _combined_partition_result(
    problem: SchedulingProblem,
    results: list[ScheduleResult],
    started: float,
) -> ScheduleResult:
    """Merge independent branch results and validate them against the full week."""
    appointments = [item for result in results for item in result.appointments]
    unmet = [item for result in results for item in result.unmet]
    issues, validation = validate_schedule(problem, appointments)
    statuses = {result.status for result in results}
    if issues:
        status = "INVALID_SOLUTION"
    elif statuses == {"OPTIMAL"}:
        status = "OPTIMAL"
    elif statuses.issubset({"OPTIMAL", "FEASIBLE"}):
        status = "FEASIBLE"
    else:
        status = next(result.status for result in results if result.status not in {"OPTIMAL", "FEASIBLE"})
    return ScheduleResult(
        status=status,
        appointments=appointments,
        unmet=unmet,
        requested_sessions=sum(result.requested_sessions for result in results),
        scheduled_sessions=len(appointments),
        validation=validation,
        statistics={
            "variables": sum(int(result.statistics.get("variables", 0)) for result in results),
            "branches": sum(int(result.statistics.get("branches", 0)) for result in results),
            "conflicts": sum(int(result.statistics.get("conflicts", 0)) for result in results),
            "partitions": len(results),
            "wall_time_seconds": monotonic() - started,
        },
    )


def _week_bundles(problem: SchedulingProblem) -> tuple[list[TherapyBundle], int, int]:
    """Return outstanding weekly bundles, requested sessions, and period count."""
    period_count = max(1, ceil(problem.horizon_days / 7))
    pinned_counts = Counter()
    for item in problem.pinned:
        day_offset = (item.date - problem.week_start).days
        if 0 <= day_offset < problem.horizon_days:
            pinned_counts[(item.requirement_id, day_offset // 7)] += 1
    bundles = []
    used_bundle_ids = {item.bundle_id for item in problem.pinned}
    for period_index in range(period_count):
        remaining_requirements = [
            replace(
                item,
                sessions_per_week=max(
                    0,
                    item.sessions_per_week - pinned_counts[(item.id, period_index)],
                ),
            )
            for item in problem.requirements
        ]
        for bundle in build_bundles(remaining_requirements, period_index):
            identifier = bundle.id
            suffix = 1
            while identifier in used_bundle_ids:
                identifier = f"{bundle.id}-N{suffix}"
                suffix += 1
            used_bundle_ids.add(identifier)
            bundles.append(replace(bundle, id=identifier))
    requested = sum(item.sessions_per_week for item in problem.requirements) * period_count
    return bundles, requested, period_count


def _greedy_appointments(
    problem: SchedulingProblem,
    bundles: list[TherapyBundle],
    candidates: dict[str, list[tuple[int, int, tuple[int, ...]]]],
) -> tuple[list[ScheduledAppointment], set[str]]:
    """Build a fast, rule-safe best-effort schedule for the quick preview."""
    appointments = list(problem.pinned)
    selected_bundles: set[str] = set()
    therapist_usage = Counter()
    location_usage = Counter()
    patient_days = set()
    for item in appointments:
        day = (item.date - problem.week_start).days
        if 0 <= day < problem.horizon_days:
            therapist_usage[(item.therapist_id, day, item.slot_index)] += 1
            location_usage[(item.location_id, day, item.slot_index)] += 1
            patient_days.add((item.patient_id, day))

    frequency_by_patient_location = defaultdict(int)
    for bundle in bundles:
        frequency_by_patient_location[(bundle.patient_id, bundle.items[0].location_id)] = max(
            frequency_by_patient_location[(bundle.patient_id, bundle.items[0].location_id)],
            bundle.occurrence_index + 1,
        )
    for bundle in sorted(
        bundles,
        key=lambda item: (
            not any(part.requirement_id in problem.priority_requirement_ids for part in bundle.items),
            len(candidates[item.id]),
            item.occurrence_index,
            item.patient_id,
        ),
    ):
        frequency = frequency_by_patient_location[(bundle.patient_id, bundle.items[0].location_id)]
        target_day = min(problem.horizon_days - 1, bundle.occurrence_index * max(1, 6 // frequency))
        ordered_candidates = sorted(
            candidates[bundle.id],
            key=lambda item: (abs(item[0] - target_day), item[0], item[1], item[2]),
        )
        for day, start, therapist_ids in ordered_candidates:
            if (bundle.patient_id, day) in patient_days:
                continue
            if any(
                therapist_usage[(therapist_ids[offset], day, start + offset)]
                or location_usage[(item.location_id, day, start + offset)] >= problem.location_capacities.get(item.location_id, 0)
                for offset, item in enumerate(bundle.items)
            ):
                continue
            target_date = problem.week_start + timedelta(days=day)
            for offset, item in enumerate(bundle.items):
                slot = start + offset
                appointments.append(ScheduledAppointment(
                    patient_id=bundle.patient_id,
                    requirement_id=item.requirement_id,
                    therapist_id=therapist_ids[offset],
                    department_id=item.department_id,
                    location_id=item.location_id,
                    date=target_date,
                    slot_index=slot,
                    bundle_id=bundle.id,
                ))
                therapist_usage[(therapist_ids[offset], day, slot)] += 1
                location_usage[(item.location_id, day, slot)] += 1
            patient_days.add((bundle.patient_id, day))
            selected_bundles.add(bundle.id)
            break
    return appointments, selected_bundles


def _greedy_result(problem: SchedulingProblem, started: float) -> ScheduleResult:
    """Return a saveable, validated quick-preview schedule without CP-SAT."""
    bundles, requested, period_count = _week_bundles(problem)
    candidates, rejected = _candidate_placements(problem, bundles)
    appointments, selected_bundles = _greedy_appointments(problem, bundles, candidates)
    scheduled_by_requirement = Counter(item.requirement_id for item in appointments)
    therapist_usage = Counter()
    location_usage = Counter()
    patient_days = set()
    for appointment in appointments:
        day = (appointment.date - problem.week_start).days
        if 0 <= day < problem.horizon_days:
            therapist_usage[(appointment.therapist_id, day, appointment.slot_index)] += 1
            location_usage[(appointment.location_id, day, appointment.slot_index)] += 1
            patient_days.add((appointment.patient_id, day))
    missing_reasons = defaultdict(Counter)
    for bundle in bundles:
        if bundle.id in selected_bundles:
            continue
        blockers = _selected_bundle_blockers(
            problem, bundle, candidates[bundle.id], therapist_usage, location_usage, patient_days,
        ) if candidates[bundle.id] else rejected[bundle.id]
        for item in bundle.items:
            missing_reasons[(bundle.patient_id, item.requirement_id)].update(blockers)
    unmet = []
    for requirement in problem.requirements:
        missing = max(0, requirement.sessions_per_week * period_count - scheduled_by_requirement[requirement.id])
        if missing:
            ranked = _ranked_reasons(missing_reasons[(requirement.patient_id, requirement.id)])
            unmet.append(UnmetRequirement(
                requirement.patient_id, requirement.id, requirement.department_id,
                missing, ranked[0], ranked[1:],
            ))
    issues, validation = validate_schedule(problem, appointments)
    return ScheduleResult(
        status="INVALID_SOLUTION" if issues else "FEASIBLE",
        appointments=appointments,
        unmet=unmet,
        requested_sessions=requested,
        scheduled_sessions=len(appointments),
        validation=validation,
        statistics={
            "variables": sum(len(values) for values in candidates.values()),
            "strategy": "quick_greedy",
            "wall_time_seconds": monotonic() - started,
        },
    )


def generate_weekly_schedule(
    problem: SchedulingProblem,
    mode: SolverMode = SolverMode.BEST_EFFORT,
    settings: SolverSettings = SOLVER_SETTINGS,
    _allow_partitioning: bool = True,
) -> ScheduleResult:
    started = monotonic()
    if (
        mode == SolverMode.BEST_EFFORT
        and settings.max_time_seconds <= 20
        and len(problem.requirements) >= 100
    ):
        return _greedy_result(problem, started)
    partitions = _location_partitions(problem) if _allow_partitioning else None
    if partitions:
        total_requested = sum(
            sum(item.sessions_per_week for item in partition.requirements)
            for partition in partitions
        )
        results = []
        for partition in partitions:
            partition_requested = sum(
                item.sessions_per_week for item in partition.requirements
            )
            partition_seconds = settings.max_time_seconds * partition_requested / max(1, total_requested)
            partition_settings = replace(
                settings,
                # Very small branches still need enough time to produce a
                # first valid incumbent; below this threshold CP-SAT can
                # return UNKNOWN despite readily available capacity.
                max_time_seconds=max(0.5, partition_seconds),
            )
            results.append(generate_weekly_schedule(
                partition,
                mode=mode,
                settings=partition_settings,
                _allow_partitioning=False,
            ))
        return _combined_partition_result(problem, results, started)

    bundles, requested, period_count = _week_bundles(problem)
    candidates, rejected = _candidate_placements(problem, bundles)
    model = cp_model.CpModel()
    variables = {}

    for bundle in bundles:
        for day, start, therapist_ids in candidates[bundle.id]:
            key = (bundle.id, day, start, therapist_ids)
            therapist_label = "_".join(str(item_id) for item_id in therapist_ids)
            variables[key] = model.NewBoolVar(
                f"x_{bundle.id}_{day}_{start}_{therapist_label}"
            )

    for bundle in bundles:
        bundle_vars = [
            variables[(bundle.id, day, start, therapist_ids)]
            for day, start, therapist_ids in candidates[bundle.id]
        ]
        if mode == SolverMode.STRICT:
            model.Add(sum(bundle_vars) == 1)
        else:
            model.Add(sum(bundle_vars) <= 1)

    # Frequency occurrences are daily bundles: a patient can receive at most one bundle per day.
    patient_day_bundles = defaultdict(list)
    for bundle in bundles:
        for day, start, therapist_ids in candidates[bundle.id]:
            patient_day_bundles[(bundle.patient_id, day)].append(
                variables[(bundle.id, day, start, therapist_ids)]
            )
    pinned_patient_days = {
        (item.patient_id, (item.date - problem.week_start).days)
        for item in problem.pinned
        if 0 <= (item.date - problem.week_start).days < problem.horizon_days
    }
    for key, usage in patient_day_bundles.items():
        model.Add(sum(usage) <= (0 if key in pinned_patient_days else 1))

    therapist_usage = defaultdict(list)
    patient_usage = defaultdict(list)
    location_usage = defaultdict(list)
    for bundle in bundles:
        for day, start, therapist_ids in candidates[bundle.id]:
            variable = variables[(bundle.id, day, start, therapist_ids)]
            for offset, item in enumerate(bundle.items):
                slot = start + offset
                therapist_usage[(therapist_ids[offset], day, slot)].append(variable)
                patient_usage[(bundle.patient_id, day, slot)].append(variable)
                location_usage[(item.location_id, day, slot)].append(variable)

    pinned_therapist = Counter()
    pinned_patient = Counter()
    pinned_location = Counter()
    for item in problem.pinned:
        day = (item.date - problem.week_start).days
        if 0 <= day < problem.horizon_days and item.date.weekday() in WORKING_DAYS:
            pinned_therapist[(item.therapist_id, day, item.slot_index)] += 1
            pinned_patient[(item.patient_id, day, item.slot_index)] += 1
            pinned_location[(item.location_id, day, item.slot_index)] += 1

    for key, usage in therapist_usage.items():
        model.Add(sum(usage) + pinned_therapist[key] <= 1)
    for key, usage in patient_usage.items():
        model.Add(sum(usage) + pinned_patient[key] <= 1)
    for key, usage in location_usage.items():
        location_id = key[0]
        model.Add(sum(usage) + pinned_location[key] <= problem.location_capacities.get(location_id, 0))

    objective = []
    for bundle in bundles:
        for day, start, therapist_ids in candidates[bundle.id]:
            variable = variables[(bundle.id, day, start, therapist_ids)]
            weekday = (problem.week_start + timedelta(days=day)).weekday()
            preference = sum(
                problem.patient_preferences.get((bundle.patient_id, weekday, start + offset), 0)
                for offset in range(len(bundle.items))
            )
            # Avoiding unmet clinical sessions dominates every lower-priority preference.
            reward = (settings.unmet_bundle_penalty + settings.scheduled_session_reward) * len(bundle.items)
            reward += settings.preference_reward * preference
            if any(item.requirement_id in problem.priority_requirement_ids for item in bundle.items):
                reward += settings.absence_priority_reward * len(bundle.items)
            objective.append(reward * variable)
    _add_cadence_preferences(
        problem,
        bundles,
        candidates,
        variables,
        model,
        objective,
        settings,
    )
    model.Maximize(sum(objective))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = settings.max_time_seconds
    solver.parameters.random_seed = settings.random_seed
    solver.parameters.num_search_workers = settings.num_search_workers
    # The demo model is a large weighted set-packing problem. Disabling expensive
    # presolve/linearization produces a valid incumbent quickly and lets the time
    # limit improve it, instead of timing out before the first feasible solution.
    solver.parameters.cp_model_presolve = False
    solver.parameters.linearization_level = 0
    status_code = solver.Solve(model)
    status = solver.StatusName(status_code)

    if status_code not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        unmet = [
            UnmetRequirement(
                r.patient_id,
                r.id,
                r.department_id,
                r.sessions_per_week * period_count,
                "STRICT_INFEASIBLE",
            )
            for r in problem.requirements
        ]
        return ScheduleResult(
            status=status,
            appointments=list(problem.pinned),
            unmet=unmet,
            requested_sessions=requested,
            scheduled_sessions=len(problem.pinned),
            validation={key: 0 for key in (
                "wrong_therapist", "therapist_overlaps", "patient_overlaps",
                "absence_conflicts", "patient_absence_conflicts",
                "location_capacity_violations", "patient_location_transitions",
                "invalid_slot_durations", "broken_consecutive_bundles",
                "therapist_availability_conflicts", "patient_availability_conflicts",
            )},
            statistics={"variables": len(variables), "wall_time_seconds": monotonic() - started},
        )

    appointments = list(problem.pinned)
    selected_bundles = set()
    for bundle in bundles:
        for day, start, therapist_ids in candidates[bundle.id]:
            if solver.BooleanValue(variables[(bundle.id, day, start, therapist_ids)]):
                selected_bundles.add(bundle.id)
                target_date = problem.week_start + timedelta(days=day)
                for offset, item in enumerate(bundle.items):
                    appointments.append(
                        ScheduledAppointment(
                            patient_id=bundle.patient_id,
                            requirement_id=item.requirement_id,
                            therapist_id=therapist_ids[offset],
                            department_id=item.department_id,
                            location_id=item.location_id,
                            date=target_date,
                            slot_index=start + offset,
                            bundle_id=bundle.id,
                        )
                    )

    generated = [item for item in appointments if item not in problem.pinned]
    scheduled_by_requirement = Counter(item.requirement_id for item in appointments)
    selected_therapist_usage = Counter()
    selected_location_usage = Counter()
    selected_patient_days = set()
    for appointment in appointments:
        day = (appointment.date - problem.week_start).days
        if not 0 <= day < problem.horizon_days:
            continue
        selected_therapist_usage[(appointment.therapist_id, day, appointment.slot_index)] += 1
        selected_location_usage[(appointment.location_id, day, appointment.slot_index)] += 1
        selected_patient_days.add((appointment.patient_id, day))

    missing_bundle_reasons = defaultdict(Counter)
    for bundle in bundles:
        if bundle.id not in selected_bundles:
            if candidates[bundle.id]:
                bundle_reasons = _selected_bundle_blockers(
                    problem,
                    bundle,
                    candidates[bundle.id],
                    selected_therapist_usage,
                    selected_location_usage,
                    selected_patient_days,
                )
            else:
                bundle_reasons = rejected[bundle.id]
            for item in bundle.items:
                key = (bundle.patient_id, item.requirement_id)
                missing_bundle_reasons[key].update(bundle_reasons)
    unmet = []
    for requirement in problem.requirements:
        required = requirement.sessions_per_week * period_count
        missing = max(0, required - scheduled_by_requirement[requirement.id])
        if missing:
            ranked_reasons = _ranked_reasons(
                missing_bundle_reasons[(requirement.patient_id, requirement.id)]
            )
            unmet.append(
                UnmetRequirement(
                    requirement.patient_id,
                    requirement.id,
                    requirement.department_id,
                    missing,
                    ranked_reasons[0],
                    ranked_reasons[1:],
                )
            )

    issues, report = validate_schedule(problem, appointments)
    if issues:
        status = "INVALID_SOLUTION"
    return ScheduleResult(
        status=status,
        appointments=appointments,
        unmet=unmet,
        requested_sessions=requested,
        scheduled_sessions=len(appointments),
        validation=report,
        statistics={
            "variables": len(variables),
            "branches": solver.NumBranches(),
            "conflicts": solver.NumConflicts(),
            "objective": solver.ObjectiveValue(),
            "wall_time_seconds": monotonic() - started,
        },
    )
