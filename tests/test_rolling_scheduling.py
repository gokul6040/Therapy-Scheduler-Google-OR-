from datetime import date

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
from database.repository import save_proposed_schedule
from scheduling.types import ScheduleResult, ScheduledAppointment
from services.rolling_schedule_service import generate_rolling_schedule


def test_rolling_scheduler_carries_unmet_friday_demand_into_next_week(db_session):
    department = Department(name="Speech")
    location = Location(name="Clinic", capacity=1)
    patient = Patient(patient_code="P-ROLLING", name="Rolling Child", active=True)
    therapist = Therapist(
        therapist_code="T-ROLLING",
        name="Rolling Therapist",
        department=department,
        location=location,
        active=True,
    )
    db_session.add_all([department, location, patient, therapist])
    db_session.flush()
    requirement = TreatmentRequirement(
        patient_id=patient.id,
        department_id=department.id,
        assigned_therapist_id=therapist.id,
        location_id=location.id,
        sessions_per_week=1,
        sequence_order=0,
        active=True,
    )
    db_session.add(requirement)
    for day in range(6):
        for slot in range(10):
            available = day in (0, 1) and slot == 0
            db_session.add_all([
                PatientAvailability(
                    patient_id=patient.id,
                    day_of_week=day,
                    slot_index=slot,
                    available=available,
                ),
                TherapistAvailability(
                    therapist_id=therapist.id,
                    day_of_week=day,
                    slot_index=slot,
                    available=available,
                ),
            ])
    db_session.commit()

    result, week_start, planning_end = generate_rolling_schedule(
        db_session,
        date(2026, 9, 11),  # Friday: this week has no remaining available slot.
    )

    next_week = [item for item in result.appointments if date(2026, 9, 14) <= item.date <= date(2026, 9, 19)]
    assert week_start == date(2026, 9, 7)
    assert planning_end == date(2026, 9, 26)
    assert all(item.date >= date(2026, 9, 11) for item in result.appointments)
    assert len(next_week) == 2  # regular weekly need plus Friday's carried session.


def test_saving_a_replacement_closes_the_released_appointment(db_session):
    department = Department(name="Physio")
    location = Location(name="Clinic", capacity=1)
    patient = Patient(patient_code="P-RELEASED", name="Released Child", active=True)
    therapist = Therapist(
        therapist_code="T-RELEASED",
        name="Released Therapist",
        department=department,
        location=location,
        active=True,
    )
    db_session.add_all([department, location, patient, therapist])
    db_session.flush()
    requirement = TreatmentRequirement(
        patient_id=patient.id,
        department_id=department.id,
        assigned_therapist_id=therapist.id,
        location_id=location.id,
        sessions_per_week=1,
        sequence_order=0,
        active=True,
    )
    db_session.add(requirement)
    db_session.flush()
    released = Appointment(
        patient_id=patient.id,
        treatment_requirement_id=requirement.id,
        therapist_id=therapist.id,
        department_id=department.id,
        location_id=location.id,
        date=date(2026, 9, 11),
        slot_index=0,
        status=AppointmentStatus.NEEDS_RESCHEDULING,
        bundle_id="RELEASED",
        source="absence",
    )
    db_session.add(released)
    db_session.commit()
    result = ScheduleResult(
        status="FEASIBLE",
        appointments=[ScheduledAppointment(
            patient_id=patient.id,
            requirement_id=requirement.id,
            therapist_id=therapist.id,
            department_id=department.id,
            location_id=location.id,
            date=date(2026, 9, 14),
            slot_index=0,
            bundle_id="REPLACEMENT",
        )],
        unmet=[],
        requested_sessions=1,
        scheduled_sessions=1,
        validation={},
        statistics={},
    )

    assert save_proposed_schedule(db_session, result, date(2026, 9, 7), 14) == 1
    assert released.status == AppointmentStatus.CANCELLED_OTHER
