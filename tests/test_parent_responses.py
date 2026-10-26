from datetime import timedelta

import pytest
from sqlalchemy import func, select

from database.models import (
    Appointment, AppointmentStatus, AuditLog, Department, ParentAvailabilityResponse,
    Patient, PatientAbsence, PatientAvailability, TreatmentRequirement,
)
from database.repository import load_scheduling_problem
from importers.parent_response_demo_importer import load_parent_response_demo
from scheduling.response_repair import fill_released_openings
from scheduling.schedule_validator import validate_schedule
from scheduling.types import RequirementInput, ScheduledAppointment
from services.parent_response_demo import run_demo
from services.parent_response_service import process_daily_responses, record_parent_response


@pytest.fixture
def response_demo(db_session):
    target = load_parent_response_demo(db_session)
    patients = {item.patient_code: item for item in db_session.scalars(select(Patient))}
    appointments = {item.bundle_id: item for item in db_session.scalars(select(Appointment))}
    return target, patients, appointments


def test_assumed_replies_repair_only_declined_opening_and_track_week(response_demo, db_session):
    target, patients, appointments = response_demo
    before = {item.id: (item.date, item.slot_index) for item in appointments.values()}

    result = process_daily_responses(db_session, target)

    assert result.processed == 2
    assert result.kept_appointment_ids == [appointments["DEMO-A-MON"].id]
    assert appointments["DEMO-A-MON"].status == AppointmentStatus.CONFIRMED
    assert appointments["DEMO-C-MON"].status == AppointmentStatus.PENDING_CONFIRMATION
    assert appointments["DEMO-B-MON"].status == AppointmentStatus.NEEDS_RESCHEDULING
    for item in appointments.values():
        assert (item.date, item.slot_index) == before[item.id]
    proposed = [db_session.get(Appointment, identifier) for identifier in result.proposed_appointment_ids]
    assert len(proposed) == 1
    assert proposed[0].patient_id == patients["DEMO-D"].id
    assert (proposed[0].date, proposed[0].slot_index) == (target, 1)
    assert proposed[0].status == AppointmentStatus.PROPOSED
    assert all(item.patient_id == patients["DEMO-B"].id for item in result.alternatives)
    assert len(result.alternatives) == 1
    assert target < result.alternatives[0].date <= target + timedelta(days=5)
    assert db_session.scalar(select(func.count()).select_from(Appointment)) == 6
    progress = {item.patient_id: item for item in result.weekly_progress}
    assert progress[patients["DEMO-A"].id].remaining == 2
    assert progress[patients["DEMO-B"].id].remaining == 2  # unsaved alternative does not count
    assert progress[patients["DEMO-D"].id].remaining == 0
    problem = load_scheduling_problem(db_session, target)
    issues, report = validate_schedule(problem, problem.pinned)
    assert not issues
    assert not any(report.values())


def test_replaying_response_batch_does_not_duplicate_changes(response_demo, db_session):
    target, _, appointments = response_demo
    process_daily_responses(db_session, target)
    counts = {
        model: db_session.scalar(select(func.count()).select_from(model))
        for model in (Appointment, PatientAbsence, AuditLog, ParentAvailabilityResponse)
    }
    record_parent_response(
        db_session, event_id="demo-no-b", appointment_id=appointments["DEMO-B-MON"].id,
        reply="NO", unavailable_scope="day",
    )
    replay = process_daily_responses(db_session, target)
    assert replay.processed == 0
    assert not replay.proposed_appointment_ids
    assert counts == {
        model: db_session.scalar(select(func.count()).select_from(model)) for model in counts
    }


def test_yes_and_no_reply_preserve_every_booking_without_generating_demand(response_demo, db_session):
    target, _, _ = response_demo
    response = db_session.get(ParentAvailabilityResponse, "demo-no-b")
    response.reply, response.unavailable_scope = "YES", None
    db_session.commit()
    result = process_daily_responses(db_session, target)
    assert not result.released_appointment_ids
    assert not result.proposed_appointment_ids
    assert not result.alternatives
    assert db_session.scalar(select(func.count()).select_from(Appointment)) == 5
    assert db_session.scalar(select(func.count()).select_from(PatientAbsence)) == 0


def test_unavailable_replacement_leaves_opening_empty_and_reports_unmet(response_demo, db_session):
    target, patients, _ = response_demo
    db_session.add_all([
        PatientAvailability(
            patient_id=patients["DEMO-D"].id, day_of_week=target.weekday(),
            slot_index=slot, available=False,
        ) for slot in range(10)
    ])
    db_session.add_all([
        PatientAbsence(patient_id=patients["DEMO-B"].id, date=target + timedelta(days=day), full_day=True)
        for day in range(1, 6)
    ])
    db_session.commit()
    result = process_daily_responses(db_session, target)
    assert not result.proposed_appointment_ids
    assert not result.alternatives
    assert len(result.alternative_unmet) == 1
    assert result.alternative_unmet[0].reason == "PATIENT_ABSENCE"


def test_inactive_patient_cannot_fill_opening(response_demo, db_session):
    target, patients, _ = response_demo
    patients["DEMO-D"].active = False
    db_session.commit()
    result = process_daily_responses(db_session, target)
    assert not result.proposed_appointment_ids


def test_completed_and_pending_sessions_both_reserve_weekly_demand(response_demo, db_session):
    target, patients, appointments = response_demo
    appointments["DEMO-D-TUE"].status = AppointmentStatus.PENDING_CONFIRMATION
    appointments["DEMO-E-TUE"].status = AppointmentStatus.COMPLETED
    db_session.commit()
    result = process_daily_responses(db_session, target)
    progress = {item.patient_id: item for item in result.weekly_progress}
    assert progress[patients["DEMO-D"].id].booked == 2
    assert progress[patients["DEMO-E"].id].completed == 1
    assert progress[patients["DEMO-E"].id].remaining == 0
    assert db_session.get(Appointment, result.proposed_appointment_ids[0]).patient_id == patients["DEMO-D"].id


def test_new_bundle_ids_do_not_merge_with_existing_generated_visits(response_demo, db_session):
    target, patients, appointments = response_demo
    existing = appointments["DEMO-D-TUE"]
    existing.bundle_id = f"P{patients['DEMO-D'].id}-W1-B1-L{existing.location_id}"
    db_session.commit()
    result = process_daily_responses(db_session, target)
    proposed = db_session.get(Appointment, result.proposed_appointment_ids[0])
    assert proposed.bundle_id != existing.bundle_id
    problem = load_scheduling_problem(db_session, target)
    assert not validate_schedule(problem, problem.pinned)[0]


def test_continuous_group_is_released_and_replaced_together(response_demo, db_session):
    target, patients, appointments = response_demo
    original = appointments["DEMO-B-MON"]
    other_therapist = db_session.scalar(select(TreatmentRequirement.assigned_therapist_id).where(
        TreatmentRequirement.patient_id == patients["DEMO-F"].id,
    ))
    department = Department(name="Demo OT")
    db_session.add(department)
    db_session.flush()
    requirements = []
    for code in ("DEMO-B", "DEMO-D"):
        requirement = TreatmentRequirement(
            patient_id=patients[code].id, department_id=department.id,
            assigned_therapist_id=other_therapist, location_id=original.location_id,
            sessions_per_week=1, sequence_order=1, active=True,
        )
        db_session.add(requirement)
        requirements.append(requirement)
    db_session.flush()
    second = Appointment(
        patient_id=original.patient_id, treatment_requirement_id=requirements[0].id,
        therapist_id=other_therapist, department_id=department.id,
        location_id=original.location_id, date=target, slot_index=2,
        bundle_id=original.bundle_id, status=AppointmentStatus.PENDING_CONFIRMATION,
    )
    db_session.add(second)
    db_session.commit()
    result = process_daily_responses(db_session, target)
    assert set(result.released_appointment_ids) == {original.id, second.id}
    proposed = [db_session.get(Appointment, identifier) for identifier in result.proposed_appointment_ids]
    assert len(proposed) == 2
    assert {item.patient_id for item in proposed} == {patients["DEMO-D"].id}
    assert sorted(item.slot_index for item in proposed) == [1, 2]
    assert len({item.bundle_id for item in proposed}) == 1
    assert len(result.alternatives) == 2
    assert len({item.bundle_id for item in result.alternatives}) == 1


def test_visit_scope_does_not_assume_whole_day_absence(response_demo, db_session):
    target, _, _ = response_demo
    db_session.get(ParentAvailabilityResponse, "demo-no-b").unavailable_scope = "visit"
    db_session.commit()
    result = process_daily_responses(db_session, target)
    absence = db_session.scalar(select(PatientAbsence))
    assert not absence.full_day
    assert (absence.start_slot, absence.end_slot) == (1, 1)
    assert all((item.date, item.slot_index) != (target, 1) for item in result.alternatives)


def test_stale_reply_rejects_entire_batch_without_changing_yes_visit(response_demo, db_session):
    target, _, appointments = response_demo
    appointments["DEMO-B-MON"].slot_index = 3
    db_session.commit()
    with pytest.raises(ValueError, match="Stale response"):
        process_daily_responses(db_session, target)
    assert appointments["DEMO-A-MON"].status == AppointmentStatus.PENDING_CONFIRMATION
    assert db_session.get(ParentAvailabilityResponse, "demo-yes-a").processed_at is None
    assert db_session.scalar(select(func.count()).select_from(PatientAbsence)) == 0


def test_conflicting_replies_for_one_visit_require_resolution(response_demo, db_session):
    target, _, appointments = response_demo
    record_parent_response(
        db_session, event_id="conflict", appointment_id=appointments["DEMO-A-MON"].id,
        reply="NO", unavailable_scope="day",
    )
    db_session.commit()
    with pytest.raises(ValueError, match="Conflicting responses"):
        process_daily_responses(db_session, target)
    assert appointments["DEMO-A-MON"].status == AppointmentStatus.PENDING_CONFIRMATION


def test_no_requires_explicit_scope_and_event_ids_cannot_be_reassigned(response_demo, db_session):
    _, _, appointments = response_demo
    with pytest.raises(ValueError, match="explicit unavailable_scope"):
        record_parent_response(
            db_session, event_id="missing-scope", appointment_id=appointments["DEMO-B-MON"].id,
            reply="NO",
        )
    with pytest.raises(ValueError, match="different response"):
        record_parent_response(
            db_session, event_id="demo-yes-a", appointment_id=appointments["DEMO-B-MON"].id,
            reply="YES",
        )


def test_demo_import_refuses_existing_data(response_demo, db_session):
    with pytest.raises(ValueError, match="empty database"):
        load_parent_response_demo(db_session)


def test_demo_creates_isolated_database_and_reviewable_report(tmp_path):
    report = run_demo(tmp_path)
    assert (tmp_path / "parent_response_demo.sqlite").exists()
    text = report.read_text(encoding="utf-8")
    assert "## Before" in text and "## After" in text
    assert "| DEMO-A | Demo Speech Therapy | 3 | 0 | 1 | 2 |" in text
    with pytest.raises(ValueError, match="already exists"):
        run_demo(tmp_path)


def test_solver_failure_rolls_back_replies_absences_and_appointment_changes(
    response_demo, db_session, monkeypatch,
):
    target, _, appointments = response_demo

    def fail(*args):
        raise RuntimeError("simulated solver failure")

    monkeypatch.setattr("services.parent_response_service.fill_released_openings", fail)
    with pytest.raises(RuntimeError, match="simulated solver failure"):
        process_daily_responses(db_session, target)
    assert appointments["DEMO-A-MON"].status == AppointmentStatus.PENDING_CONFIRMATION
    assert appointments["DEMO-B-MON"].status == AppointmentStatus.PENDING_CONFIRMATION
    assert db_session.scalar(select(func.count()).select_from(PatientAbsence)) == 0
    assert db_session.get(ParentAvailabilityResponse, "demo-no-b").processed_at is None


def test_released_opening_cannot_be_filled_by_another_therapy(base_problem):
    requirement = RequirementInput(1, 1, 99, 20, 1, 1, 0)
    problem = base_problem([requirement])
    released = ScheduledAppointment(2, 2, 20, 10, 1, problem.week_start, 0, "RELEASED")
    result = fill_released_openings(problem, [released])
    assert result.appointments == []
    assert result.unmet[0].missing_sessions == 1
    assert result.unmet[0].reason == "OUTSIDE_REPAIR_OPENING"


def test_expired_plan_cannot_receive_new_replacement(response_demo, db_session):
    target, patients, _ = response_demo
    requirement = db_session.scalar(select(TreatmentRequirement).where(
        TreatmentRequirement.patient_id == patients["DEMO-D"].id,
    ))
    requirement.plan_start_date = target - timedelta(days=100)
    requirement.duration_months = 1
    db_session.commit()
    result = process_daily_responses(db_session, target)
    assert not result.proposed_appointment_ids
