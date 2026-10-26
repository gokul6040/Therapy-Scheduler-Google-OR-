"""Apply assumed Yes/No replies without calling WhatsApp, Zoho, or a live API."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from config import SOLVER_SETTINGS, SolverSettings
from database.models import (
    AdminSlotReservation, Appointment, AppointmentStatus, AuditLog, ParentAvailabilityResponse,
    Patient, PatientAbsence, TreatmentRequirement,
)
from database.repository import load_scheduling_problem
from scheduling.response_repair import fill_released_openings, suggest_declined_visits
from scheduling.types import ScheduledAppointment, UnmetRequirement


REPLYABLE_STATUSES = (
    AppointmentStatus.PROPOSED,
    AppointmentStatus.PENDING_CONFIRMATION,
    AppointmentStatus.CONFIRMED,
)


@dataclass(frozen=True)
class WeeklyProgress:
    requirement_id: int
    patient_id: int
    required: int
    completed: int
    booked: int
    remaining: int


@dataclass
class DailyResponseResult:
    target_date: date
    processed: int = 0
    kept_appointment_ids: list[int] = field(default_factory=list)
    released_appointment_ids: list[int] = field(default_factory=list)
    proposed_appointment_ids: list[int] = field(default_factory=list)
    alternatives: list[ScheduledAppointment] = field(default_factory=list)
    alternative_unmet: list[UnmetRequirement] = field(default_factory=list)
    weekly_progress: list[WeeklyProgress] = field(default_factory=list)


def record_parent_response(
    session: Session,
    *,
    event_id: str,
    appointment_id: int,
    reply: str,
    unavailable_scope: str | None = None,
) -> ParentAvailabilityResponse:
    """Stage an idempotent fake reply; the caller commits its input batch.

    No requires an explicit scope: ``visit`` blocks the entire original visit's
    time range, while ``day`` blocks every slot on that date.
    """
    if not isinstance(event_id, str) or not event_id.strip() or len(event_id) > 120:
        raise ValueError("A non-empty event ID of at most 120 characters is required")
    reply = reply.strip().upper()
    if reply not in {"YES", "NO"}:
        raise ValueError("Reply must be YES or NO")
    if reply == "NO" and unavailable_scope not in {"visit", "day"}:
        raise ValueError("NO requires explicit unavailable_scope: visit or day")
    if reply == "YES" and unavailable_scope is not None:
        raise ValueError("YES must not include an unavailability scope")
    session.flush()
    previous = session.get(ParentAvailabilityResponse, event_id)
    if previous is not None:
        if (previous.appointment_id, previous.reply, previous.unavailable_scope) != (
            appointment_id, reply, unavailable_scope,
        ):
            raise ValueError("Event ID already belongs to a different response")
        return previous
    appointment = session.get(Appointment, appointment_id)
    if appointment is None or appointment.status not in REPLYABLE_STATUSES:
        raise ValueError("Response must reference an active, unfinished appointment")
    response = ParentAvailabilityResponse(
        event_id=event_id, appointment_id=appointment_id,
        appointment_date=appointment.date, appointment_slot=appointment.slot_index,
        reply=reply, unavailable_scope=unavailable_scope, source="simulation",
    )
    session.add(response)
    session.flush()
    return response


def weekly_progress(session: Session, target_date: date) -> list[WeeklyProgress]:
    """Count delivered sessions separately from reserved sessions for this week."""
    start = target_date - timedelta(days=target_date.weekday())
    end = start + timedelta(days=5)
    appointments = list(session.scalars(select(Appointment).where(
        Appointment.date >= start, Appointment.date <= end,
        Appointment.id.notin_(select(AdminSlotReservation.appointment_id).where(
            AdminSlotReservation.counts_toward_requirement.is_(False),
        )),
    )))
    completed = Counter(
        item.treatment_requirement_id for item in appointments
        if item.status == AppointmentStatus.COMPLETED
    )
    booked = Counter(
        item.treatment_requirement_id for item in appointments
        if item.status in REPLYABLE_STATUSES
    )
    return [
        WeeklyProgress(
            requirement_id=item.id, patient_id=item.patient_id,
            required=item.sessions_per_week, completed=completed[item.id],
            booked=booked[item.id], remaining=max(
                0, item.sessions_per_week - completed[item.id] - booked[item.id],
            ),
        )
        for item in session.scalars(select(TreatmentRequirement).where(
            TreatmentRequirement.active.is_(True),
        ).order_by(TreatmentRequirement.id))
    ]


def _scheduled(item: Appointment) -> ScheduledAppointment:
    return ScheduledAppointment(
        patient_id=item.patient_id, requirement_id=item.treatment_requirement_id,
        therapist_id=item.therapist_id, department_id=item.department_id,
        location_id=item.location_id, date=item.date, slot_index=item.slot_index,
        bundle_id=item.bundle_id or f"PIN-{item.id}", status=item.status.value,
    )


def _response_groups(session: Session, responses: list[ParentAvailabilityResponse]):
    """Validate the entire batch before changing any visit."""
    groups = {}
    for response in responses:
        appointment = session.get(Appointment, response.appointment_id)
        if appointment is None or (
            appointment.date != response.appointment_date
            or appointment.slot_index != response.appointment_slot
            or appointment.status not in REPLYABLE_STATUSES
        ):
            raise ValueError(f"Stale response: {response.event_id}")
        if response.reply not in {"YES", "NO"} or (
            response.reply == "NO" and response.unavailable_scope not in {"visit", "day"}
        ) or (response.reply == "YES" and response.unavailable_scope is not None):
            raise ValueError(f"Invalid response: {response.event_id}")
        key = (appointment.patient_id, appointment.date, appointment.bundle_id or appointment.id)
        group = [appointment]
        if appointment.bundle_id:
            group = list(session.scalars(select(Appointment).where(
                Appointment.patient_id == appointment.patient_id,
                Appointment.date == appointment.date,
                Appointment.bundle_id == appointment.bundle_id,
                Appointment.status.in_(REPLYABLE_STATUSES),
            ).order_by(Appointment.slot_index)))
        decision = (response.reply, response.unavailable_scope)
        if key in groups and groups[key][0] != decision:
            raise ValueError("Conflicting responses for the same continuous visit")
        groups[key] = (decision, group)
    for (patient_id, target, _), (decision, group) in groups.items():
        if decision != ("NO", "day"):
            continue
        other_visits = list(session.scalars(select(Appointment).where(
            Appointment.patient_id == patient_id, Appointment.date == target,
            Appointment.status.in_((*REPLYABLE_STATUSES, AppointmentStatus.COMPLETED)),
        )))
        declined_ids = {
            item.id for (other_patient, other_date, _), (other_decision, items) in groups.items()
            if other_patient == patient_id and other_date == target and other_decision[0] == "NO"
            for item in items
        }
        if any(item.id not in declined_ids for item in other_visits):
            raise ValueError("Whole-day NO conflicts with another retained visit for this patient")
    return groups


def process_daily_responses(
    session: Session,
    target_date: date,
    *,
    settings: SolverSettings = SOLVER_SETTINGS,
) -> DailyResponseResult:
    """Apply queued replies and persist tentative replacements on this date only.

    Yes/no-response visits stay in place. Alternatives for declined families are
    returned as unsaved suggestions within the current week. This simulation
    entry point never invokes the two-week planner or any external integration.
    """
    if target_date.weekday() >= 6:
        raise ValueError("Daily response scheduling supports Monday through Saturday")
    session.flush()
    result = DailyResponseResult(target_date)
    with session.begin_nested():
        responses = list(session.scalars(select(ParentAvailabilityResponse).where(
            ParentAvailabilityResponse.appointment_date == target_date,
            ParentAvailabilityResponse.processed_at.is_(None),
        ).order_by(ParentAvailabilityResponse.event_id).with_for_update()))
        groups = _response_groups(session, responses)
        released = []
        for (reply, scope), group in groups.values():
            if reply == "YES":
                for item in group:
                    item.status = AppointmentStatus.CONFIRMED
                    result.kept_appointment_ids.append(item.id)
            else:
                released.extend(_scheduled(item) for item in group)
                slots = [item.slot_index for item in group]
                session.add(PatientAbsence(
                    patient_id=group[0].patient_id, date=target_date,
                    full_day=scope == "day",
                    start_slot=None if scope == "day" else min(slots),
                    end_slot=None if scope == "day" else max(slots),
                    reason="Assumed parent response: NO",
                ))
                for item in group:
                    item.status = AppointmentStatus.NEEDS_RESCHEDULING
                    result.released_appointment_ids.append(item.id)
        session.flush()
        if released:
            start = target_date - timedelta(days=target_date.weekday())
            problem = load_scheduling_problem(
                session, start, preserve_confirmed=True, horizon_days=6,
                planning_start_date=target_date,
            )
            # Keep existing bookings for inactive patients pinned, but never
            # select them as new replacement candidates.
            active_patients = set(session.scalars(select(Patient.id).where(Patient.active.is_(True))))
            pinned_counts = Counter(item.requirement_id for item in problem.pinned)
            problem = replace(problem, requirements=[
                item if item.patient_id in active_patients else replace(
                    item, sessions_per_week=pinned_counts[item.id],
                )
                for item in problem.requirements
            ])
            repair = fill_released_openings(problem, released, settings)
            if any(repair.validation.values()) or repair.status not in {"OPTIMAL", "FEASIBLE"}:
                raise ValueError("No validated daily repair could be produced")
            proposed = [item for item in repair.appointments if item not in problem.pinned]
            for item in proposed:
                appointment = Appointment(
                    patient_id=item.patient_id, treatment_requirement_id=item.requirement_id,
                    therapist_id=item.therapist_id, department_id=item.department_id,
                    location_id=item.location_id, date=item.date, slot_index=item.slot_index,
                    status=AppointmentStatus.PROPOSED, bundle_id=item.bundle_id,
                    source="parent_response_simulation",
                )
                session.add(appointment)
                session.flush()
                result.proposed_appointment_ids.append(appointment.id)
            # A declined visit already reproposed in another released opening
            # must not receive a second alternative for the same lost session.
            replaced_counts = Counter(item.requirement_id for item in proposed)
            remaining_released = []
            for item in released:
                if replaced_counts[item.requirement_id]:
                    replaced_counts[item.requirement_id] -= 1
                else:
                    remaining_released.append(item)
            alternatives_problem = replace(problem, pinned=repair.appointments)
            alternatives = suggest_declined_visits(alternatives_problem, remaining_released, settings)
            if any(alternatives.validation.values()) or alternatives.status not in {"OPTIMAL", "FEASIBLE"}:
                raise ValueError("No validated alternative search could be produced")
            result.alternatives = [
                item for item in alternatives.appointments if item not in alternatives_problem.pinned
            ]
            result.alternative_unmet = alternatives.unmet
        now = datetime.utcnow()
        for response in responses:
            response.processed_at = now
            session.add(AuditLog(
                action="parent_response_applied", entity=f"appointment:{response.appointment_id}",
                new_value=f"reply={response.reply}; scope={response.unavailable_scope or 'none'}",
                user="parent_response_simulation",
            ))
        result.processed = len(responses)
        session.flush()
        result.weekly_progress = weekly_progress(session, target_date)
    session.commit()
    return result
