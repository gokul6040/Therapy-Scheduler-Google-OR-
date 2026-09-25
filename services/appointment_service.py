from __future__ import annotations

from dataclasses import replace
import json

from sqlalchemy import select

from database.models import Appointment, AppointmentStatus, AuditLog
from scheduling.schedule_validator import validate_schedule
from scheduling.types import ScheduledAppointment


def move_appointment(session, problem, existing, appointment: Appointment, new_date, new_slot):
    """Move one appointment or its entire consecutive bundle as a manual change."""
    group = [appointment]
    if appointment.bundle_id:
        group = list(session.scalars(select(Appointment).where(
            Appointment.bundle_id == appointment.bundle_id,
            Appointment.patient_id == appointment.patient_id,
            Appointment.date == appointment.date,
            Appointment.status.in_([
                AppointmentStatus.PROPOSED,
                AppointmentStatus.PENDING_CONFIRMATION,
                AppointmentStatus.CONFIRMED,
            ]),
        ).order_by(Appointment.slot_index)))
    current_items = [
        ScheduledAppointment(
            item.patient_id,
            item.treatment_requirement_id,
            item.therapist_id,
            item.department_id,
            item.location_id,
            item.date,
            item.slot_index,
            item.bundle_id or f"A-{item.id}",
            item.status.value,
        )
        for item in group
    ]
    original_start = min(item.slot_index for item in group)
    replacements = [
        replace(
            current,
            date=new_date,
            slot_index=new_slot + (current.slot_index - original_start),
        )
        for current in current_items
    ]
    candidate = [item for item in existing if item not in current_items] + replacements
    issues, _ = validate_schedule(problem, candidate)
    if issues:
        return False, sorted({issue.message for issue in issues})
    old = {
        "date": str(appointment.date),
        "start_slot": original_start,
        "appointments": len(group),
    }
    for item, replacement in zip(group, replacements):
        item.date = replacement.date
        item.slot_index = replacement.slot_index
        item.source = "manual"
    session.add(AuditLog(
        action="appointment_bundle_moved",
        entity=f"appointment:{appointment.id}",
        old_value=json.dumps(old),
        new_value=json.dumps({
            "date": str(new_date), "start_slot": new_slot, "appointments": len(group),
        }),
    ))
    session.commit()
    return True, []
