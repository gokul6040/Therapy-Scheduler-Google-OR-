from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum


class SolverMode(str, Enum):
    STRICT = "STRICT"
    BEST_EFFORT = "BEST_EFFORT"


@dataclass(frozen=True)
class RequirementInput:
    id: int
    patient_id: int
    department_id: int
    therapist_id: int
    location_id: int
    sessions_per_week: int
    sequence_order: int
    plan_start_date: date | None = None
    plan_end_date: date | None = None


@dataclass(frozen=True)
class BundleItem:
    requirement_id: int
    department_id: int
    therapist_id: int
    location_id: int
    plan_start_date: date | None = None
    plan_end_date: date | None = None


@dataclass(frozen=True)
class TherapyBundle:
    id: str
    patient_id: int
    occurrence_index: int
    items: tuple[BundleItem, ...]
    period_index: int = 0


@dataclass
class SchedulingProblem:
    week_start: date
    requirements: list[RequirementInput]
    therapist_availability: set[tuple[int, int, int]]
    patient_availability: set[tuple[int, int, int]]
    patient_preferences: dict[tuple[int, int, int], int] = field(default_factory=dict)
    absences: set[tuple[int, date, int]] = field(default_factory=set)
    location_capacities: dict[int, int] = field(default_factory=dict)
    active_therapists: set[int] = field(default_factory=set)
    pinned: list["ScheduledAppointment"] = field(default_factory=list)
    horizon_days: int = 6
    eligible_therapists: dict[int, tuple[int, ...]] = field(default_factory=dict)
    patient_absences: set[tuple[int, date, int]] = field(default_factory=set)
    planning_start_date: date | None = None
    priority_requirement_ids: set[int] = field(default_factory=set)


@dataclass(frozen=True)
class ScheduledAppointment:
    patient_id: int
    requirement_id: int
    therapist_id: int
    department_id: int
    location_id: int
    date: date
    slot_index: int
    bundle_id: str
    status: str = "PROPOSED"


@dataclass(frozen=True)
class UnmetRequirement:
    patient_id: int
    requirement_id: int
    department_id: int
    missing_sessions: int
    reason: str
    secondary_reasons: tuple[str, ...] = ()


@dataclass
class ScheduleResult:
    status: str
    appointments: list[ScheduledAppointment]
    unmet: list[UnmetRequirement]
    requested_sessions: int
    scheduled_sessions: int
    validation: dict[str, int]
    statistics: dict[str, float | int | str]


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str
