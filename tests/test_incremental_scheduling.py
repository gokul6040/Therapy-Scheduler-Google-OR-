from datetime import date

from sqlalchemy import select

from database.models import (
    Appointment,
    AppointmentStatus,
    Department,
    Location,
    Patient,
    PatientAvailability,
    Therapist,
    TherapistAvailability,
    TreatmentRequirement,
)
from database.repository import load_scheduling_problem, save_proposed_schedule
from scheduling.cp_sat_scheduler import generate_weekly_schedule
from services.appointment_service import move_appointment


def test_new_patient_fills_capacity_without_moving_existing_appointment(db_session):
    department = Department(name="Speech")
    location = Location(name="A", capacity=2)
    therapist = Therapist(
        therapist_code="T1", name="T1", department=department, location=location,
    )
    existing_patient = Patient(patient_code="EXISTING")
    new_patient = Patient(patient_code="NEW")
    db_session.add_all([department, location, therapist, existing_patient, new_patient])
    db_session.flush()
    requirements = [
        TreatmentRequirement(
            patient_id=patient.id,
            department_id=department.id,
            assigned_therapist_id=therapist.id,
            sessions_per_week=1,
            sequence_order=0,
        )
        for patient in (existing_patient, new_patient)
    ]
    db_session.add_all(requirements)
    db_session.flush()
    target = date(2026, 9, 7)
    existing = Appointment(
        patient_id=existing_patient.id,
        treatment_requirement_id=requirements[0].id,
        therapist_id=therapist.id,
        department_id=department.id,
        location_id=location.id,
        date=target,
        slot_index=0,
        status=AppointmentStatus.PROPOSED,
        bundle_id="EXISTING-BUNDLE",
        source="solver",
    )
    db_session.add(existing)
    for day in range(6):
        for slot in range(2):
            db_session.add(TherapistAvailability(
                therapist_id=therapist.id, day_of_week=day, slot_index=slot, available=True,
            ))
            for patient in (existing_patient, new_patient):
                db_session.add(PatientAvailability(
                    patient_id=patient.id, day_of_week=day, slot_index=slot, available=True,
                ))
    db_session.commit()

    problem = load_scheduling_problem(db_session, target, True, 7)
    result = generate_weekly_schedule(problem)

    existing_result = next(item for item in result.appointments if item.patient_id == existing_patient.id)
    new_result = next(item for item in result.appointments if item.patient_id == new_patient.id)
    assert (existing_result.date, existing_result.slot_index) == (target, 0)
    assert (new_result.date, new_result.slot_index) != (target, 0)

    added = save_proposed_schedule(db_session, result, target, 7)
    saved = list(db_session.scalars(select(Appointment).order_by(Appointment.id)))
    assert added == 1
    assert len(saved) == 2
    assert (saved[0].date, saved[0].slot_index) == (target, 0)

    refreshed_problem = load_scheduling_problem(db_session, target, True, 7)
    moved, messages = move_appointment(
        db_session,
        refreshed_problem,
        refreshed_problem.pinned,
        saved[0],
        date(2026, 9, 8),
        0,
    )
    assert moved, messages
    assert saved[0].date == date(2026, 9, 8)
    assert saved[0].source == "manual"
