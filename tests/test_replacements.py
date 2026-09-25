from datetime import date

from database.models import (
    Appointment, AppointmentStatus, Department, Location, Patient, PatientAvailability,
    Therapist, TherapistAvailability, TreatmentRequirement,
)
from services.replacement_service import find_pull_forward_candidates, find_replacement_candidates


def test_replacement_candidates_preserve_therapist_and_branch_continuity(db_session):
    department = Department(name="Speech")
    location = Location(name="A", capacity=2)
    other_location = Location(name="B", capacity=1)
    db_session.add_all([department, location, other_location])
    db_session.flush()
    therapist = Therapist(therapist_code="T1", name="One", department_id=department.id, location_id=location.id)
    other = Therapist(therapist_code="T2", name="Two", department_id=department.id, location_id=location.id)
    patients = [
        Patient(patient_code=code)
        for code in ("CANCELLED", "ELIGIBLE", "WRONG-THERAPIST", "WRONG-BRANCH")
    ]
    db_session.add_all([therapist, other, *patients])
    db_session.flush()
    vacant_req = TreatmentRequirement(patient_id=patients[0].id, department_id=department.id, assigned_therapist_id=therapist.id, location_id=location.id, sessions_per_week=1, sequence_order=0)
    eligible_req = TreatmentRequirement(patient_id=patients[1].id, department_id=department.id, assigned_therapist_id=therapist.id, location_id=location.id, sessions_per_week=2, sequence_order=0)
    wrong_req = TreatmentRequirement(patient_id=patients[2].id, department_id=department.id, assigned_therapist_id=other.id, location_id=location.id, sessions_per_week=2, sequence_order=0)
    wrong_branch_req = TreatmentRequirement(patient_id=patients[3].id, department_id=department.id, assigned_therapist_id=therapist.id, location_id=other_location.id, sessions_per_week=2, sequence_order=0)
    db_session.add_all([vacant_req, eligible_req, wrong_req, wrong_branch_req])
    db_session.flush()
    target_date = date(2026, 9, 7)
    vacant = Appointment(
        patient_id=patients[0].id, treatment_requirement_id=vacant_req.id,
        therapist_id=therapist.id, department_id=department.id, location_id=location.id,
        date=target_date, slot_index=2, status=AppointmentStatus.CANCELLED_PATIENT,
        bundle_id="B", source="test",
    )
    db_session.add_all([
        vacant,
        PatientAvailability(patient_id=patients[1].id, day_of_week=0, slot_index=2, available=True),
        PatientAvailability(patient_id=patients[2].id, day_of_week=0, slot_index=2, available=True),
        PatientAvailability(patient_id=patients[3].id, day_of_week=0, slot_index=2, available=True),
        TherapistAvailability(therapist_id=therapist.id, day_of_week=0, slot_index=2, available=True),
    ])
    future = Appointment(
        patient_id=patients[1].id, treatment_requirement_id=eligible_req.id,
        therapist_id=therapist.id, department_id=department.id, location_id=location.id,
        date=date(2026, 9, 8), slot_index=2, status=AppointmentStatus.PROPOSED,
        bundle_id="FUTURE", source="test",
    )
    db_session.add(future)
    db_session.commit()

    candidates = find_replacement_candidates(db_session, vacant, target_date)
    assert [item["patient_code"] for item in candidates] == ["ELIGIBLE"]
    assert candidates[0]["weekly_progress"] == "1/2"
    assert "same therapist" in candidates[0]["reason"]

    pull_forward = find_pull_forward_candidates(
        db_session, vacant, date(2026, 9, 10),
    )
    assert [item["patient_code"] for item in pull_forward] == ["ELIGIBLE"]
    assert pull_forward[0]["days_later"] == 1
