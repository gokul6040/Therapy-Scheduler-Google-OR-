"""Warning-based manual reservations for the scheduler's replacement workflow."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date, timedelta
import hashlib
import json
from uuid import uuid4

from sqlalchemy import Select, inspect, select
from sqlalchemy.orm import Session

from database.models import (
    AdminBookingDecision, AdminSlotReservation, Appointment, AppointmentStatus, Department,
    AuditLog, Location, Patient, PatientAbsence, PatientAvailability, Therapist,
    TherapistAbsence, TherapistAvailability, TherapistCapability,
    TherapistLocation, TreatmentRequirement,
)
from database.repository import ACTIVE_SCHEDULE_STATUSES, _plan_end_date
from scheduling.slot_config import SLOTS, valid_consecutive_block


def _optional_mapping_exists(session: Session, model, *conditions) -> bool:
    """Missing optional mappings grant no extra capability or branch access."""
    connection = session.connection()
    schema = connection.get_execution_options().get("schema_translate_map", {}).get(model.__table__.schema)
    if not inspect(connection).has_table(model.__tablename__, schema=schema):
        return False
    return session.scalar(select(model.id).where(*conditions)) is not None

RELEASED_STATUSES = (
    AppointmentStatus.NEEDS_RESCHEDULING,
    AppointmentStatus.CANCELLED_PATIENT,
    AppointmentStatus.CANCELLED_OTHER,
)


@dataclass(frozen=True)
class EmptySlot:
    """An explicitly chosen calendar position; never persisted as a vacancy."""

    date: date
    slot_index: int
    therapist_id: int
    department_id: int
    location_id: int

def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


def reserved_source_ids() -> Select[tuple[int]]:
    """Released sources filled by another booking, excluding new-booking markers."""
    return select(AdminSlotReservation.source_appointment_id).where(
        AdminSlotReservation.source_appointment_id != AdminSlotReservation.appointment_id,
    )


def is_reserved_source(session: Session, appointment_id: int) -> bool:
    reservation = session.get(AdminSlotReservation, appointment_id)
    return reservation is not None and reservation.appointment_id != appointment_id


def _sources(session: Session, source_id: int, whole_visit: bool) -> list[Appointment]:
    source = session.get(Appointment, source_id)
    if source is None or source.status not in RELEASED_STATUSES:
        raise ValueError("Select a released or cancelled appointment")
    if is_reserved_source(session, source_id):
        raise ValueError("This opening is already reserved. Refresh the schedule.")
    items = [source]
    if whole_visit and source.bundle_id:
        items = list(session.scalars(select(Appointment).where(
            Appointment.patient_id == source.patient_id,
            Appointment.date == source.date,
            Appointment.bundle_id == source.bundle_id,
            Appointment.status.in_(RELEASED_STATUSES),
            Appointment.id.notin_(reserved_source_ids()),
        ).order_by(Appointment.slot_index)))
    for item in items:
        if is_reserved_source(session, item.id):
            raise ValueError("This opening is already reserved. Refresh the schedule.")
    return items


def _covers(absence, target: date, slot: int) -> bool:
    return absence.date == target and (
        absence.full_day or (absence.start_slot or 0) <= slot <= (
            absence.end_slot if absence.end_slot is not None else absence.start_slot or 0
        )
    )


def preview_admin_reservation(
    session: Session, source_id: int, patient_id: int, whole_visit: bool = True,
) -> dict:
    """Return all warnings; scheduling-policy mismatches do not block selection."""
    sources = _sources(session, source_id, whole_visit)
    return _preview_reservation(session, sources, patient_id, source_id, whole_visit)


def preview_empty_slot(session: Session, opening: EmptySlot, patient_id: int) -> dict:
    """Preview a new appointment without writing a placeholder appointment."""
    if not 0 <= opening.slot_index < len(SLOTS):
        raise ValueError("Choose a valid session time")
    for model, identifier in (
        (Therapist, opening.therapist_id), (Department, opening.department_id),
        (Location, opening.location_id),
    ):
        if session.get(model, identifier) is None:
            raise ValueError("Choose an existing doctor, therapy, and branch")
    source = Appointment(
        patient_id=patient_id, therapist_id=opening.therapist_id,
        department_id=opening.department_id, location_id=opening.location_id,
        date=opening.date, slot_index=opening.slot_index,
    )
    return _preview_reservation(session, [source], patient_id, None, False, opening)


def _preview_reservation(
    session: Session, sources: list[Appointment], patient_id: int,
    source_id: int | None, whole_visit: bool, opening: EmptySlot | None = None,
) -> dict:
    patient = session.get(Patient, patient_id)
    if patient is None:
        raise ValueError("Select a patient from the patient list")
    requirements = list(session.scalars(select(TreatmentRequirement).where(
        TreatmentRequirement.patient_id == patient_id,
    ).order_by(TreatmentRequirement.id)))
    appointments = list(session.scalars(select(Appointment).where(
        Appointment.status.in_(ACTIVE_SCHEDULE_STATUSES),
    ).order_by(Appointment.id)))
    warnings = {}

    def warn(code: str, message: str) -> None:
        warnings.setdefault(code, {"code": code, "message": message})

    if not patient.active:
        warn("INACTIVE_PATIENT", "This patient is inactive.")
    if opening is not None:
        doctor = session.get(Therapist, opening.therapist_id)
        capability = doctor.department_id == opening.department_id or _optional_mapping_exists(session, TherapistCapability,
            TherapistCapability.therapist_id == doctor.id,
            TherapistCapability.department_id == opening.department_id,
        )
        if not capability:
            warn("THERAPIST_CAPABILITY", "This therapy is not listed among the doctor's capabilities.")
        branch = doctor.location_id == opening.location_id or _optional_mapping_exists(session, TherapistLocation,
            TherapistLocation.therapist_id == doctor.id,
            TherapistLocation.location_id == opening.location_id,
        )
        if not branch:
            warn("THERAPIST_BRANCH", "This branch is not listed for the doctor.")
        if not session.get(Department, opening.department_id).active:
            warn("INACTIVE_THERAPY", "The selected therapy is inactive.")
        if opening.date.weekday() == 6:
            warn("NON_OPERATIONAL_DAY", "This date is outside the Monday–Saturday scheduling week.")
    partial = sources[0].bundle_id and session.scalar(
        select(Appointment.id).where(
            Appointment.bundle_id == sources[0].bundle_id,
            Appointment.patient_id == sources[0].patient_id,
            Appointment.date == sources[0].date,
            Appointment.id.notin_([item.id for item in sources]),
        ).limit(1)
    )
    if partial:
        warn("PARTIAL_VISIT", "Only this slot will be reserved; it belongs to a continuous visit.")
    slots = []
    superseded = set()
    for source in sources:
        requirement = min(requirements, key=lambda item: (
            item.department_id != source.department_id, not item.active,
            item.assigned_therapist_id != source.therapist_id,
            item.location_id not in (None, source.location_id), item.id,
        )) if requirements else None
        matching_therapy = requirement is not None and requirement.department_id == source.department_id
        if not matching_therapy:
            warn("THERAPY_NOT_IN_PLAN", "This therapy is not in the patient's plan. This reservation will not satisfy a different therapy's weekly target.")
        if requirement is None or not requirement.active or requirement.source_status == "ADMIN_RESERVATION_ONLY":
            warn("NO_ACTIVE_PLAN", "There is no active therapy plan for this reservation.")
        if requirement is not None:
            if requirement.assigned_therapist_id != source.therapist_id:
                warn("THERAPIST_MISMATCH", "The slot's therapist differs from the patient's assigned therapist.")
            assigned = session.get(Therapist, requirement.assigned_therapist_id)
            branch = requirement.location_id or assigned.location_id
            if branch != source.location_id:
                warn("BRANCH_MISMATCH", "The slot is at a different branch from the treatment plan.")
            if requirement.plan_start_date and source.date < requirement.plan_start_date:
                warn("PLAN_NOT_STARTED", "The treatment plan has not started on this date.")
            end = _plan_end_date(requirement.plan_start_date, requirement.duration_months)
            if end and source.date > end:
                warn("PLAN_ENDED", "The treatment plan has ended before this date.")
        current = [item for item in appointments if (
            item.date, item.slot_index, item.therapist_id
        ) == (source.date, source.slot_index, source.therapist_id)
            and (opening is not None or item.location_id == source.location_id)]
        proposals = [item for item in current if item.status == AppointmentStatus.PROPOSED]
        if opening is None:
            superseded.update(item.id for item in proposals)
        reusable = next((item for item in proposals if (
            item.patient_id == patient_id and requirement is not None
            and item.treatment_requirement_id == requirement.id
            and item.department_id == source.department_id
        )), None) if opening is None else None
        if current and (opening is not None or any(item.status != AppointmentStatus.PROPOSED for item in current)):
            warn("THERAPIST_CONFLICT", "The therapist already has another saved booking at this time.")
        slots.append({
            "source_id": source.id, "date": source.date.isoformat(),
            "slot": source.slot_index, "time": SLOTS[source.slot_index].start_time.strftime("%H:%M"),
            "therapist_id": source.therapist_id, "department_id": source.department_id,
            "location_id": source.location_id,
            "requirement_id": requirement.id if requirement else None,
            "reuse_id": reusable.id if reusable else None,
            "counts_toward_requirement": bool(matching_therapy and requirement.active
                                              and requirement.source_status != "ADMIN_RESERVATION_ONLY"),
        })
    remaining = [item for item in appointments if item.id not in superseded]
    same_day = [item for item in remaining if item.patient_id == patient_id and item.date == sources[0].date]
    if same_day:
        warn("ANOTHER_VISIT", "The patient already has another visit on this date.")
    if any(item.location_id != sources[0].location_id for item in same_day):
        warn("MULTIPLE_BRANCHES", "The patient would attend more than one branch on the same day.")
    patient_absences = list(session.scalars(select(PatientAbsence).where(PatientAbsence.patient_id == patient_id)))
    therapist_absences = list(session.scalars(select(TherapistAbsence)))
    for slot in slots:
        target = date.fromisoformat(slot["date"])
        therapist = session.get(Therapist, slot["therapist_id"])
        if not therapist.active:
            warn("INACTIVE_THERAPIST", "The slot's therapist is inactive.")
        if any(item.slot_index == slot["slot"] for item in same_day):
            warn("PATIENT_CONFLICT", "The patient already has a booking at this time.")
        if any(_covers(item, target, slot["slot"]) for item in patient_absences):
            warn("PATIENT_ABSENCE", "The patient is recorded as absent or has declined this date/time.")
        if any(item.therapist_id == slot["therapist_id"] and _covers(item, target, slot["slot"]) for item in therapist_absences):
            warn("THERAPIST_ABSENCE", "The therapist is recorded as absent at this time.")
        for model, field, identifier, code, message in (
            (PatientAvailability, PatientAvailability.patient_id, patient_id,
             "PATIENT_UNAVAILABLE", "The patient is recorded as unavailable at this time."),
            (TherapistAvailability, TherapistAvailability.therapist_id, slot["therapist_id"],
             "THERAPIST_UNAVAILABLE", "The therapist is recorded as unavailable at this time."),
        ):
            if session.scalar(select(model.id).where(
                field == identifier, model.day_of_week == target.weekday(),
                model.slot_index == slot["slot"], model.available.is_(False),
            )) is not None:
                warn(code, message)
        location = session.get(Location, slot["location_id"])
        usage = sum(item.location_id == slot["location_id"] and item.date == target and item.slot_index == slot["slot"] for item in remaining)
        if not location.active or usage >= location.capacity:
            warn("BRANCH_CAPACITY", "The branch is inactive or its capacity would be exceeded.")
    selected_requirements = {item["requirement_id"] for item in slots}
    if any(item.active and item.location_id in (None, sources[0].location_id)
           and item.id not in selected_requirements for item in requirements):
        warn("CONTINUITY", "Other therapies in this patient's plan are not included in this visit.")
    selected_slots = sorted(item["slot"] for item in slots)
    if selected_slots != list(range(selected_slots[0], selected_slots[0] + len(slots))) or not valid_consecutive_block(selected_slots[0], len(slots)):
        warn("CONTINUOUS_BLOCK", "This visit crosses a break or is not a valid continuous block.")
    for item in appointments:
        if item.id in superseded and item.bundle_id:
            if any(other.bundle_id == item.bundle_id and other.id not in superseded for other in appointments):
                warn("SPLIT_SUGGESTION", "This replaces only part of another patient's proposed continuous visit.")
    start = sources[0].date - timedelta(days=sources[0].date.weekday())
    off_plan = set(session.scalars(select(AdminSlotReservation.appointment_id).where(
        AdminSlotReservation.counts_toward_requirement.is_(False),
    )))
    counts = Counter(item.treatment_requirement_id for item in remaining if (
        start <= item.date <= start + timedelta(days=5) and item.id not in off_plan
    ))
    additions = Counter(item["requirement_id"] for item in slots if item["counts_toward_requirement"])
    for requirement in requirements:
        if additions[requirement.id] and counts[requirement.id] + additions[requirement.id] > requirement.sessions_per_week:
            warn("WEEKLY_TARGET", "This reservation exceeds the patient's weekly target for a selected therapy.")
    token = _digest({
        "patient_id": patient_id, "slots": slots, "warnings": list(warnings.values()),
        "superseded": sorted(superseded),
        "bookings": [(item.id, item.status.value, item.patient_id, item.date, item.slot_index,
                      item.therapist_id, item.department_id, item.location_id) for item in appointments],
    })
    return {
        "patient_id": patient_id, "patient": patient.patient_code, "name": patient.name,
        "source_id": source_id, "whole_visit": whole_visit, "slots": slots,
        "superseded_ids": sorted(superseded), "warnings": list(warnings.values()),
        "preview_token": token,
    }


def reserve_admin_slot(
    session: Session, *, source_id: int | None, patient_id: int, request_id: str,
    preview_token: str, whole_visit: bool = True, override_warnings: bool = False,
    mode: str = "without_messaging", admin_user: str = "local_demo_admin",
    opening: EmptySlot | None = None,
    allow_pending_offer: bool = False,
) -> dict:
    """Reserve after preview; explicit admin overrides authorize policy warnings.

    The caller owns the transaction and commits the appointment, reservation link,
    and audit event together. The test flow may opt into a draft that it must
    publish within the same transaction; this service never sends messages.
    """
    pending_offer = mode == "with_messaging" and allow_pending_offer
    if mode != "without_messaging" and not pending_offer:
        raise ValueError("Messaging is a display-only option for admin reservations")
    if not isinstance(request_id, str) or not request_id or len(request_id) > 120:
        raise ValueError("A booking request ID is required")
    fingerprint = _digest((source_id, patient_id, whole_visit, mode, asdict(opening) if opening else None))
    previous = session.get(AdminBookingDecision, request_id)
    if previous:
        if previous.fingerprint != fingerprint:
            raise ValueError("Booking request ID belongs to a different reservation")
        return {"appointment_ids": json.loads(previous.appointment_ids), "warnings": json.loads(previous.warnings)}
    if opening is not None:
        # Serialize new bookings for this doctor. A second request must see the
        # first booking and obtain a new preview before overriding a conflict.
        session.execute(select(Therapist).where(
            Therapist.id == opening.therapist_id,
        ).with_for_update()).scalar_one_or_none()
        preview = preview_empty_slot(session, opening, patient_id)
    else:
        preview = preview_admin_reservation(session, source_id, patient_id, whole_visit)
    if preview_token != preview["preview_token"]:
        raise ValueError("The booking details changed. Review the refreshed warnings before reserving.")
    if preview["warnings"] and not override_warnings:
        raise ValueError("Review the warnings and use Reserve anyway to override them")
    decision = AdminBookingDecision(
        request_id=request_id, fingerprint=fingerprint, appointment_ids="[]",
        warnings=json.dumps(preview["warnings"]), admin_user=admin_user,
    )
    session.add(decision)
    session.flush()
    reused = {item["reuse_id"] for item in preview["slots"]}
    for identifier in preview["superseded_ids"]:
        if identifier not in reused:
            session.get(Appointment, identifier).status = AppointmentStatus.CANCELLED_OTHER
    bundle = f"ADMIN-{uuid4().hex}"
    appointments = []
    for slot in preview["slots"]:
        requirement_id = slot["requirement_id"]
        if requirement_id is None:
            # This inactive technical row keeps the required FK intact. It is
            # explicitly not a clinical plan and never creates therapy demand.
            requirement = TreatmentRequirement(
                patient_id=patient_id, department_id=slot["department_id"],
                assigned_therapist_id=slot["therapist_id"], location_id=slot["location_id"],
                sessions_per_week=0, sequence_order=0, active=False,
                source_status="ADMIN_RESERVATION_ONLY",
            )
            session.add(requirement)
            session.flush()
            requirement_id = requirement.id
        appointment = session.get(Appointment, slot["reuse_id"]) if slot["reuse_id"] else Appointment(
            patient_id=patient_id, treatment_requirement_id=requirement_id,
            therapist_id=slot["therapist_id"], department_id=slot["department_id"],
            location_id=slot["location_id"], date=date.fromisoformat(slot["date"]),
            slot_index=slot["slot"], bundle_id=bundle,
        )
        appointment.status = AppointmentStatus.PROPOSED if pending_offer else AppointmentStatus.CONFIRMED
        appointment.source = "admin_with_messaging" if pending_offer else "admin_without_messaging"
        if pending_offer:
            # Reused solver suggestions and new rows must share one offer.
            appointment.bundle_id = bundle
        session.add(appointment)
        session.flush()
        appointments.append(appointment.id)
        if slot["source_id"]:
            marker = session.get(AdminSlotReservation, slot["source_id"])
            if marker is not None and marker.appointment_id == marker.source_appointment_id:
                # The new booking was later cancelled. Replace its accounting
                # marker with the actual source-to-replacement relationship.
                session.delete(marker)
                session.flush()
        session.add(AdminSlotReservation(
            # A new calendar booking has no cancelled source; its tracking row
            # points to itself so off-plan weekly accounting remains identical.
            source_appointment_id=slot["source_id"] or appointment.id, appointment_id=appointment.id,
            decision_id=request_id,
            counts_toward_requirement=slot["counts_toward_requirement"],
        ))
    decision.appointment_ids = json.dumps(appointments)
    session.add(AuditLog(
        action="admin_slot_reserved", entity=f"admin_booking:{request_id}", user=admin_user,
        new_value=json.dumps({
            "patient_id": patient_id, "appointment_ids": appointments,
            "source_ids": [item["source_id"] for item in preview["slots"]],
            "warnings_overridden": preview["warnings"], "messaging": pending_offer,
        }),
    ))
    session.flush()
    return {"appointment_ids": appointments, "warnings": preview["warnings"]}
