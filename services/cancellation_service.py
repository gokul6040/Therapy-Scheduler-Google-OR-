from database.models import AppointmentStatus, AuditLog


def cancel_patient_appointment(session, appointment):
    old = appointment.status.value
    appointment.status = AppointmentStatus.CANCELLED_PATIENT
    session.add(AuditLog(
        action="appointment_cancelled", entity=f"appointment:{appointment.id}",
        old_value=old, new_value=AppointmentStatus.CANCELLED_PATIENT.value,
    ))
    session.commit()
