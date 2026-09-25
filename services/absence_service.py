from sqlalchemy import select

from database.models import (
    Appointment,
    AppointmentStatus,
    AuditLog,
    PatientAbsence,
    TherapistAbsence,
)


AFFECTABLE_STATUSES = (
    AppointmentStatus.PROPOSED,
    AppointmentStatus.PENDING_CONFIRMATION,
    AppointmentStatus.CONFIRMED,
)


def mark_therapist_absent(session, therapist_id, absence_date, reason, start_slot=None, end_slot=None):
    full_day = start_slot is None
    absence = TherapistAbsence(
        therapist_id=therapist_id, date=absence_date, start_slot=start_slot,
        end_slot=end_slot, full_day=full_day, reason=reason,
    )
    session.add(absence)
    affected = list(session.scalars(select(Appointment).where(
        Appointment.therapist_id == therapist_id,
        Appointment.date == absence_date,
        Appointment.status.in_(AFFECTABLE_STATUSES),
    )))
    if not full_day:
        affected = [item for item in affected if start_slot <= item.slot_index <= end_slot]
    for appointment in affected:
        appointment.status = AppointmentStatus.NEEDS_RESCHEDULING
    session.add(AuditLog(
        action="therapist_marked_absent", entity=f"therapist:{therapist_id}",
        new_value=f"date={absence_date}; affected={len(affected)}",
    ))
    session.commit()
    return affected


def mark_patient_absent(
    session, patient_id, absence_date, reason, start_slot=None, end_slot=None,
):
    """Record a patient absence and release only the affected appointments."""
    full_day = start_slot is None
    absence = PatientAbsence(
        patient_id=patient_id,
        date=absence_date,
        start_slot=start_slot,
        end_slot=end_slot,
        full_day=full_day,
        reason=reason,
    )
    session.add(absence)
    affected = list(session.scalars(select(Appointment).where(
        Appointment.patient_id == patient_id,
        Appointment.date == absence_date,
        Appointment.status.in_(AFFECTABLE_STATUSES),
    )))
    if not full_day:
        affected = [item for item in affected if start_slot <= item.slot_index <= end_slot]
    for appointment in affected:
        appointment.status = AppointmentStatus.NEEDS_RESCHEDULING
    session.add(AuditLog(
        action="patient_marked_absent",
        entity=f"patient:{patient_id}",
        new_value=f"date={absence_date}; affected={len(affected)}",
    ))
    session.commit()
    return affected
