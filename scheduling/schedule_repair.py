from dataclasses import replace

from scheduling.cp_sat_scheduler import generate_weekly_schedule
from scheduling.types import SchedulingProblem, SolverMode


def repair_schedule(problem: SchedulingProblem, unaffected_appointments):
    """Pin unaffected appointments and solve only remaining requirements."""
    return generate_weekly_schedule(
        replace(problem, pinned=list(unaffected_appointments)),
        mode=SolverMode.BEST_EFFORT,
    )
