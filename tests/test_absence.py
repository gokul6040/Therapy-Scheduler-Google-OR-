from datetime import date

from database.models import (
    Appointment, AppointmentStatus, Department, Location, Patient, Therapist, TreatmentRequirement,
)
from services.absence_service import mark_patient_absent, mark_therapist_absent


def test_absence_marks_only_affected_therapist_appointments(db_session):
    department = Department(name="OT")
    location = Location(name="A", capacity=2)
    patient = Patient(patient_code="P1")
    db_session.add_all([department, location, patient])
    db_session.flush()
    therapists = [
        Therapist(therapist_code=code, name=code, department_id=department.id, location_id=location.id)
        for code in ("T1", "T2")
    ]
    db_session.add_all(therapists)
    db_session.flush()
    requirements = [
        TreatmentRequirement(patient_id=patient.id, department_id=department.id, assigned_therapist_id=t.id, sessions_per_week=1, sequence_order=i)
        for i, t in enumerate(therapists)
    ]
    db_session.add_all(requirements)
    db_session.flush()
    target = date(2026, 9, 7)
    appointments = [
        Appointment(
            patient_id=patient.id, treatment_requirement_id=requirements[i].id,
            therapist_id=therapists[i].id, department_id=department.id, location_id=location.id,
            date=target, slot_index=i, status=AppointmentStatus.CONFIRMED,
            bundle_id=f"B{i}", source="test",
        ) for i in range(2)
    ]
    db_session.add_all(appointments)
    db_session.commit()

    affected = mark_therapist_absent(db_session, therapists[0].id, target, "Leave")
    assert [item.id for item in affected] == [appointments[0].id]
    assert appointments[0].status == AppointmentStatus.NEEDS_RESCHEDULING
    assert appointments[1].status == AppointmentStatus.CONFIRMED


def test_patient_absence_releases_only_that_patients_appointments(db_session):
    department = Department(name="Speech")
    location = Location(name="A", capacity=2)
    therapist = Therapist(
        therapist_code="T1", name="T1", department=department, location=location,
    )
    patients = [Patient(patient_code=code) for code in ("P1", "P2")]
    db_session.add_all([department, location, therapist, *patients])
    db_session.flush()
    requirements = [
        TreatmentRequirement(
            patient_id=patient.id,
            department_id=department.id,
            assigned_therapist_id=therapist.id,
            sessions_per_week=1,
            sequence_order=0,
        )
        for patient in patients
    ]
    db_session.add_all(requirements)
    db_session.flush()
    target = date(2026, 9, 7)
    appointments = [
        Appointment(
            patient_id=patient.id,
            treatment_requirement_id=requirement.id,
            therapist_id=therapist.id,
            department_id=department.id,
            location_id=location.id,
            date=target,
            slot_index=index,
            status=AppointmentStatus.CONFIRMED,
            bundle_id=f"B{index}",
            source="test",
        )
        for index, (patient, requirement) in enumerate(zip(patients, requirements))
    ]
    db_session.add_all(appointments)
    db_session.commit()

    affected = mark_patient_absent(db_session, patients[0].id, target, "Sick")

    assert [item.id for item in affected] == [appointments[0].id]
    assert appointments[0].status == AppointmentStatus.NEEDS_RESCHEDULING
    assert appointments[1].status == AppointmentStatus.CONFIRMED
