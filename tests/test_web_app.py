from contextlib import nullcontext
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import select

import web_app
from database.models import (
    Appointment,
    AppointmentStatus,
    Department,
    Location,
    Patient,
    PatientAvailability,
    Therapist,
    TreatmentRequirement,
)
from scheduling.types import ScheduleResult, SolverMode, UnmetRequirement


def test_monday_returns_start_of_week():
    assert web_app.monday(date(2026, 9, 10)) == date(2026, 9, 7)


def test_parse_date_rejects_non_iso_values():
    with pytest.raises(web_app.ApiError, match="YYYY-MM-DD"):
        web_app.parse_date("10/09/2026")


def test_parse_search_seconds_accepts_manager_facing_options():
    assert web_app.parse_search_seconds(None) == 20.0
    assert web_app.parse_search_seconds("120") == 120.0
    assert web_app.parse_search_seconds(300) == 300.0


def test_parse_search_seconds_rejects_unsupported_option():
    with pytest.raises(web_app.ApiError, match="20, 60, 120, 300"):
        web_app.parse_search_seconds(90)


def test_overview_sessions_offer_branch_therapist_and_patient_filters():
    app_script = Path("web/app.js").read_text(encoding="utf-8")

    assert 'id="overviewBranchFilter"' in app_script
    assert 'id="overviewTherapistFilter"' in app_script
    assert 'id="overviewPatientFilter"' in app_script
    assert "renderTodaySessions" in app_script


def test_patient_preference_table_saves_day_level_availability(db_session, monkeypatch):
    patient = Patient(patient_code="P-PREFERENCE", name="Preference Child", active=True)
    db_session.add(patient)
    db_session.commit()
    captured = {}

    class FakeHandler:
        def send_json(self, payload, status=None):
            captured["payload"] = payload
            captured["status"] = status

    monkeypatch.setattr(web_app, "SessionLocal", lambda: nullcontext(db_session))
    web_app.TherapySchedulerHandler.save_patient_preferences(
        FakeHandler(),
        patient.id,
        {"day": 0, "period": "morning"},
    )

    rows = list(db_session.scalars(select(PatientAvailability).where(
        PatientAvailability.patient_id == patient.id
    )))
    assert len(rows) == 60
    assert all(item.available and item.preference_score == 100 for item in rows if item.day_of_week == 0 and item.slot_index < 5)
    assert all(item.preference_score == 0 for item in rows if item.day_of_week != 0 or item.slot_index >= 5)
    assert captured["payload"]["preference"] == {"day": 0, "period": "morning"}


def test_generate_schedule_always_uses_best_effort_and_selected_search_time(monkeypatch):
    captured = {}

    class FakeHandler:
        def send_json(self, payload, status=None):
            captured["payload"] = payload
            captured["status"] = status

    result = ScheduleResult(
        status="FEASIBLE",
        appointments=[],
        unmet=[],
        requested_sessions=0,
        scheduled_sessions=0,
        validation={},
        statistics={},
    )
    monkeypatch.setattr(web_app, "SessionLocal", lambda: nullcontext(object()))
    monkeypatch.setattr(web_app, "serialize_preview", lambda *args: {})

    def fake_generate(_session, run_date, mode, settings):
        captured["run_date"] = run_date
        captured["mode"] = mode
        captured["seconds"] = settings.max_time_seconds
        return result, web_app.monday(run_date), run_date

    monkeypatch.setattr(web_app, "generate_rolling_schedule", fake_generate)

    web_app.TherapySchedulerHandler.generate_schedule(
        FakeHandler(), {"start": "2026-09-07", "searchSeconds": 120, "mode": "STRICT"}
    )

    assert captured["run_date"] == date.today()
    assert captured["mode"] == SolverMode.BEST_EFFORT
    assert captured["seconds"] == 120.0


def test_generate_schedule_uses_selected_future_day_and_monday_window(monkeypatch):
    captured = {}

    class FakeHandler:
        def send_json(self, payload, status):
            captured["status"] = status

    result = ScheduleResult(
        status="FEASIBLE", appointments=[], unmet=[], requested_sessions=0,
        scheduled_sessions=0, validation={}, statistics={},
    )
    monkeypatch.setattr(web_app, "SessionLocal", lambda: nullcontext(object()))
    monkeypatch.setattr(web_app, "serialize_preview", lambda *args: {})
    def fake_generate(_session, run_date, **_kwargs):
        captured["run_date"] = run_date
        return result, web_app.monday(run_date), date(2026, 10, 17)

    monkeypatch.setattr(web_app, "generate_rolling_schedule", fake_generate)

    web_app.TherapySchedulerHandler.generate_schedule(
        FakeHandler(), {"start": "2026-10-01", "searchSeconds": 20},
    )

    assert captured["run_date"] == date(2026, 10, 1)


def test_schedule_payload_contains_display_ready_appointment(db_session, monkeypatch):
    department = Department(name="Occupational Therapy", active=True)
    location = Location(name="Clinic A", capacity=4, active=True)
    db_session.add_all([department, location])
    db_session.flush()
    patient = Patient(patient_code="P-001", name="Demo Patient", active=True)
    therapist = Therapist(
        therapist_code="OT-01",
        name="Asha Menon",
        department_id=department.id,
        location_id=location.id,
        active=True,
    )
    db_session.add_all([patient, therapist])
    db_session.flush()
    requirement = TreatmentRequirement(
        patient_id=patient.id,
        department_id=department.id,
        assigned_therapist_id=therapist.id,
        sessions_per_week=1,
        sequence_order=0,
        active=True,
    )
    db_session.add(requirement)
    db_session.flush()
    appointment = Appointment(
        patient_id=patient.id,
        treatment_requirement_id=requirement.id,
        therapist_id=therapist.id,
        department_id=department.id,
        location_id=location.id,
        date=date(2026, 9, 10),
        slot_index=2,
        status=AppointmentStatus.CONFIRMED,
        source="test",
    )
    db_session.add(appointment)
    db_session.commit()

    monkeypatch.setattr(web_app, "SessionLocal", lambda: nullcontext(db_session))
    payload = web_app.schedule_payload(date(2026, 9, 7), 6)

    assert payload["patients"] == [{"id": patient.id, "code": "P-001", "name": "Demo Patient"}]
    assert payload["appointments"] == [{
        "id": appointment.id,
        "date": "2026-09-10",
        "slot": 2,
        "time": "10:35–11:15",
        "patientId": patient.id,
        "patientCode": "P-001",
        "patientName": "Demo Patient",
        "therapistId": therapist.id,
        "therapistCode": "OT-01",
        "therapistName": "Asha Menon",
        "departmentId": department.id,
        "therapy": "Occupational Therapy",
        "locationId": location.id,
        "location": "Clinic A",
        "status": "CONFIRMED",
        "bundle": None,
        "source": "test",
    }]


def test_preview_payload_labels_primary_and_secondary_unmet_reasons(db_session, monkeypatch):
    result = ScheduleResult(
        status="FEASIBLE",
        appointments=[],
        unmet=[
            UnmetRequirement(
                patient_id=1,
                requirement_id=2,
                department_id=3,
                missing_sessions=1,
                reason="THERAPIST_CAPACITY",
                secondary_reasons=("PATIENT_DAILY_VISIT_LIMIT", "LOCATION_CAPACITY"),
            )
        ],
        requested_sessions=1,
        scheduled_sessions=0,
        validation={},
        statistics={},
    )
    monkeypatch.setattr(web_app, "SessionLocal", lambda: nullcontext(db_session))

    payload = web_app.serialize_preview(result, date(2026, 9, 8))

    assert payload["unmet"][0]["reasonLabel"] == "Suitable therapist times are already occupied"
    assert payload["unmet"][0]["secondaryReasonLabels"] == [
        "Child already has another visit that day",
        "Location is full",
    ]


def test_patient_absence_returns_same_therapist_branch_candidates(db_session, monkeypatch):
    department = Department(name="Speech")
    location = Location(name="Clinic A", capacity=1)
    db_session.add_all([department, location])
    db_session.flush()
    therapist = Therapist(
        therapist_code="T-01", name="Therapist", department_id=department.id, location_id=location.id,
    )
    absent_patient = Patient(patient_code="P-ABSENT", name="Absent child")
    candidate_patient = Patient(patient_code="P-CANDIDATE", name="Candidate child")
    db_session.add_all([therapist, absent_patient, candidate_patient])
    db_session.flush()
    absent_requirement = TreatmentRequirement(
        patient_id=absent_patient.id, department_id=department.id,
        assigned_therapist_id=therapist.id, location_id=location.id,
        sessions_per_week=1, sequence_order=0,
    )
    candidate_requirement = TreatmentRequirement(
        patient_id=candidate_patient.id, department_id=department.id,
        assigned_therapist_id=therapist.id, location_id=location.id,
        sessions_per_week=2, sequence_order=0,
    )
    db_session.add_all([absent_requirement, candidate_requirement])
    db_session.flush()
    appointment = Appointment(
        patient_id=absent_patient.id, treatment_requirement_id=absent_requirement.id,
        therapist_id=therapist.id, department_id=department.id, location_id=location.id,
        date=date(2026, 9, 7), slot_index=2, status=AppointmentStatus.CONFIRMED,
        bundle_id="B-1", source="test",
    )
    db_session.add_all([
        appointment,
        PatientAvailability(
            patient_id=candidate_patient.id, day_of_week=0, slot_index=2, available=True,
        ),
    ])
    db_session.commit()

    captured = {}

    class FakeHandler:
        def send_json(self, payload, status):
            captured["payload"] = payload
            captured["status"] = status

    monkeypatch.setattr(web_app, "SessionLocal", lambda: nullcontext(db_session))
    web_app.TherapySchedulerHandler.record_absence(FakeHandler(), {
        "kind": "patient", "entityId": absent_patient.id, "date": "2026-09-07", "reason": "Sick",
    })

    assert captured["status"] == 201
    group = captured["payload"]["replacementGroups"][0]
    assert group["branch"] == "Clinic A"
    assert group["therapy"] == "Speech"
    assert [item["patient_code"] for item in group["candidates"]] == ["P-CANDIDATE"]

    operations = web_app.operations_payload(today=date(2026, 9, 7))

    assert len(operations["needsRescheduling"]) == 1
    assert "replacementCandidates" not in operations["needsRescheduling"][0]

    web_app.TherapySchedulerHandler.replacement_candidates(FakeHandler(), appointment.id)

    assert captured["status"] == 200
    assert [item["patient_code"] for item in captured["payload"]["candidates"]] == ["P-CANDIDATE"]
