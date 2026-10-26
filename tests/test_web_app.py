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
from services.whatsapp_service import WhatsAppWebhookSettings
from scheduling.types import ScheduleResult, SolverMode, UnmetRequirement


def test_monday_returns_start_of_week():
    assert web_app.monday(date(2026, 9, 10)) == date(2026, 9, 7)


def test_parse_date_rejects_non_iso_values():
    with pytest.raises(web_app.ApiError, match="YYYY-MM-DD"):
        web_app.parse_date("10/09/2026")


def test_whatsapp_webhook_challenge_requires_matching_verify_token(monkeypatch):
    monkeypatch.setattr(
        web_app,
        "webhook_settings_from_environment",
        lambda: WhatsAppWebhookSettings("verify-token", "app-secret"),
    )

    assert web_app.whatsapp_webhook_challenge({
        "hub.mode": ["subscribe"],
        "hub.verify_token": ["verify-token"],
        "hub.challenge": ["challenge-value"],
    }) == "challenge-value"

    with pytest.raises(web_app.ApiError, match="verification failed"):
        web_app.whatsapp_webhook_challenge({
            "hub.mode": ["subscribe"],
            "hub.verify_token": ["wrong"],
            "hub.challenge": ["challenge-value"],
        })


def test_sanitized_request_line_removes_query_parameters():
    assert (
        web_app.sanitized_request_line(
            "GET /webhooks/whatsapp?hub.verify_token=secret HTTP/1.1"
        )
        == "GET /webhooks/whatsapp HTTP/1.1"
    )


@pytest.mark.parametrize(("phrase", "expected"), [
    ("today", date(2026, 9, 28)),
    ("tomorrow", date(2026, 9, 29)),
    ("tmrw", date(2026, 9, 29)),
    ("day after tomorrow", date(2026, 9, 30)),
    ("day after tmrw", date(2026, 9, 30)),
    ("yesterday", date(2026, 9, 27)),
])
def test_relative_date_from_message_uses_scheduler_date(phrase, expected):
    assert web_app.relative_date_from_message(
        f"Give me a summary for {phrase}", date(2026, 9, 28)
    ) == expected


def test_local_assistant_fallback_handles_patient_summary_availability_and_greeting():
    reference_date = date(2026, 9, 28)

    assert web_app.local_assistant_action(
        "Give a summary of the patient Stella White P-6F4EC6", reference_date
    )["action"] == "patient_summary"
    assert web_app.local_assistant_action(
        "Give a summary of the patient Stella White P-6F4EC6", reference_date
    )["entity_query"] == "Stella White P-6F4EC6"
    assert web_app.local_assistant_action(
        "give the available slots of Abigail Foster T-EA5459 · THER_0168?", reference_date
    )["entity_query"] == "Abigail Foster T-EA5459 · THER_0168"
    assert web_app.local_assistant_action("hi", reference_date)["reply"].startswith("Hello.")


def test_assistant_uses_local_fallback_when_model_response_fails(monkeypatch):
    def unavailable(*args, **kwargs):
        raise web_app.URLError("unavailable")

    monkeypatch.setattr(web_app, "azure_openai_settings", lambda: {
        "endpoint": "https://example.test", "api_key": "test", "deployment": "test",
        "api_version": "2024-10-21", "responses_url": "",
    })
    monkeypatch.setattr(web_app, "urlopen", unavailable)

    action = web_app.interpret_chat_action(
        "Give a summary of the patient Stella White P-6F4EC6", date(2026, 9, 28)
    )

    assert action["action"] == "patient_summary"
    assert action["entity_query"] == "Stella White P-6F4EC6"


def test_professional_assistant_text_removes_emoji_symbols():
    assert web_app.professional_assistant_text("Done. ✅ Please review. 😊") == "Done. Please review."


def test_summary_entity_matching_accepts_optional_session_suffix_without_partial_matching():
    assert web_app.summary_entity_matches("Speech Therapy Session", "Speech Therapy")
    assert web_app.summary_entity_matches("Hydro Therapy", "hydro therapy")
    assert not web_app.summary_entity_matches("Speech Therapy Session", "Speech")
    assert web_app.is_name_list_request("Name that person who has hydro therapy")


def test_therapist_lookup_accepts_display_name_without_trailing_code(db_session):
    department = Department(name="Speech")
    location = Location(name="Clinic A", capacity=1)
    db_session.add_all([department, location])
    db_session.flush()
    therapist = Therapist(
        therapist_code="T-GABRIEL", name="Gabriel Wood T-D6D761",
        department_id=department.id, location_id=location.id,
    )
    db_session.add(therapist)
    db_session.commit()

    assert [item.id for item in web_app.matching_therapists(db_session, "Gabriel Wood")] == [therapist.id]
    assert [item.id for item in web_app.matching_therapists(db_session, "T-GABRIEL")] == [therapist.id]


def test_patient_lookup_accepts_code_and_combined_display_label(db_session):
    patient = Patient(patient_code="P-6F4EC6", name="Stella White")
    db_session.add(patient)
    db_session.commit()

    assert [item.id for item in web_app.matching_patients(db_session, "P-6F4EC6")] == [patient.id]
    assert [item.id for item in web_app.matching_patients(
        db_session, "Stella White P-6F4EC6"
    )] == [patient.id]
    assert [item.id for item in web_app.matching_patients(
        db_session, "Stella White · P-6F4EC6"
    )] == [patient.id]


def test_explicit_patient_summary_does_not_use_prior_name_list_shortcut():
    request = "Give a summary of the patient Stella White P-6F4EC6 and her therapy"

    assert web_app.is_memory_patient_question(request)
    assert web_app.is_explicit_patient_summary_request(request)


def test_ambiguous_therapist_summary_choice_resolves_number_name_or_code():
    context = {
        "pending_therapist_summary": {
            "date": "2026-09-29",
            "choices": [
                {"name": "Alex Kim T-001", "code": "T-001"},
                {"name": "Alex Kim T-002", "code": "T-002"},
            ],
        }
    }

    assert web_app.assistant_therapist_summary_choice(context, "2") == {
        "name": "Alex Kim T-002", "code": "T-002",
    }
    assert web_app.assistant_therapist_summary_choice(context, "first") == {
        "name": "Alex Kim T-001", "code": "T-001",
    }
    assert web_app.assistant_therapist_summary_choice(context, "T-002") == {
        "name": "Alex Kim T-002", "code": "T-002",
    }


def test_ambiguous_available_slots_choice_resolves_therapist_code():
    context = {
        "pending_available_slots": {
            "date": "2026-09-29",
            "choices": [
                {"name": "Alex Kim T-001", "code": "T-001"},
                {"name": "Alex Kim T-002", "code": "T-002"},
            ],
        }
    }

    assert web_app.assistant_available_slots_choice(context, "2") == {
        "name": "Alex Kim T-002", "code": "T-002",
    }
    assert web_app.assistant_available_slots_choice(context, "T-001") == {
        "name": "Alex Kim T-001", "code": "T-001",
    }


def test_read_only_browser_summary_memory_restores_after_server_context_loss():
    context = {}
    web_app.restore_assistant_summary_memory(context, {
        "summary": {
            "action": "therapy_summary", "date": "2026-09-29", "entity_query": "Speech Therapy",
        },
        "lastPatientIds": [4, 8, "invalid", -1],
    })

    assert web_app.assistant_memory_snapshot(context) == {
        "summary": {
            "action": "therapy_summary", "date": "2026-09-29", "entity_query": "Speech Therapy",
        },
        "lastPatientIds": [4, 8],
    }


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

    class FixedDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 9, 30)

    # Keep the selected October date in the future regardless of the test day.
    monkeypatch.setattr(web_app, "date", FixedDate)

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
    assert payload["calendarBreaks"] == [{
        "afterSlot": 4, "start": "12:35", "end": "13:15", "label": "Lunch break",
    }]
    assert payload["slotLabels"][5] == "13:15–13:55"
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


def test_scheduling_assistant_cancels_only_a_unique_local_match(db_session, monkeypatch):
    department = Department(name="Speech")
    location = Location(name="Clinic A", capacity=1)
    patient = Patient(patient_code="P-CHAT", name="Maya Kumar")
    db_session.add_all([department, location, patient])
    db_session.flush()
    therapist = Therapist(
        therapist_code="T-CHAT", name="Therapist", department_id=department.id,
        location_id=location.id,
    )
    db_session.add(therapist)
    db_session.flush()
    requirement = TreatmentRequirement(
        patient_id=patient.id, department_id=department.id,
        assigned_therapist_id=therapist.id, location_id=location.id,
        sessions_per_week=1, sequence_order=0,
    )
    db_session.add(requirement)
    db_session.flush()
    appointment = Appointment(
        patient_id=patient.id, treatment_requirement_id=requirement.id,
        therapist_id=therapist.id, department_id=department.id, location_id=location.id,
        date=date(2026, 9, 29), slot_index=1, status=AppointmentStatus.CONFIRMED,
        source="test",
    )
    db_session.add(appointment)
    db_session.commit()
    captured = {}

    class FakeHandler:
        def send_json(self, payload, status=None):
            captured["payload"] = payload
            captured["status"] = status

    monkeypatch.setattr(web_app, "SessionLocal", lambda: nullcontext(db_session))
    monkeypatch.setattr(web_app, "interpret_chat_action", lambda _message: {
        "action": "cancel_appointment", "patient_query": "Maya Kumar",
        "date": "2026-09-29", "reply": "",
    })

    web_app.TherapySchedulerHandler.handle_assistant_message(
        FakeHandler(), {"message": "Cancel Maya's therapy on 29 September"}
    )

    assert captured["payload"]["outcome"] == "cancelled"
    assert "has been cancelled" in captured["payload"]["message"]
    assert db_session.get(Appointment, appointment.id).status == AppointmentStatus.CANCELLED_PATIENT


def test_scheduling_assistant_does_not_cancel_an_ambiguous_match(db_session, monkeypatch):
    department = Department(name="Speech")
    location = Location(name="Clinic A", capacity=1)
    patient = Patient(patient_code="P-AMBIGUOUS", name="Maya Kumar")
    db_session.add_all([department, location, patient])
    db_session.flush()
    therapist = Therapist(
        therapist_code="T-AMBIGUOUS", name="Therapist", department_id=department.id,
        location_id=location.id,
    )
    db_session.add(therapist)
    db_session.flush()
    requirement = TreatmentRequirement(
        patient_id=patient.id, department_id=department.id,
        assigned_therapist_id=therapist.id, location_id=location.id,
        sessions_per_week=2, sequence_order=0,
    )
    db_session.add(requirement)
    db_session.flush()
    appointments = [
        Appointment(
            patient_id=patient.id, treatment_requirement_id=requirement.id,
            therapist_id=therapist.id, department_id=department.id, location_id=location.id,
            date=date(2026, 9, 29), slot_index=slot, status=AppointmentStatus.CONFIRMED,
            source="test",
        ) for slot in (1, 2)
    ]
    db_session.add_all(appointments)
    db_session.commit()
    captured = {}

    class FakeHandler:
        def send_json(self, payload, status=None):
            captured["payload"] = payload

    monkeypatch.setattr(web_app, "SessionLocal", lambda: nullcontext(db_session))
    monkeypatch.setattr(web_app, "interpret_chat_action", lambda _message: {
        "action": "cancel_appointment", "patient_query": "P-AMBIGUOUS",
        "date": "2026-09-29", "reply": "",
    })

    web_app.TherapySchedulerHandler.handle_assistant_message(FakeHandler(), {"message": "Cancel Maya"})

    assert captured["payload"]["outcome"] == "needs_clarification"
    assert len(captured["payload"]["appointments"]) == 2
    assert all(db_session.get(Appointment, item.id).status == AppointmentStatus.CONFIRMED for item in appointments)


def test_scheduling_assistant_builds_a_local_daily_and_centre_summary(db_session):
    speech = Department(name="Speech")
    ot = Department(name="Occupational Therapy")
    clinic_a = Location(name="Clinic A", capacity=2)
    clinic_b = Location(name="Clinic B", capacity=2)
    db_session.add_all([speech, ot, clinic_a, clinic_b])
    db_session.flush()
    first_patient = Patient(patient_code="P-SUMMARY-1", name="First child")
    second_patient = Patient(patient_code="P-SUMMARY-2", name="Second child")
    first_therapist = Therapist(
        therapist_code="T-SUMMARY-1", name="First therapist", department_id=speech.id,
        location_id=clinic_a.id,
    )
    second_therapist = Therapist(
        therapist_code="T-SUMMARY-2", name="Second therapist", department_id=ot.id,
        location_id=clinic_b.id,
    )
    db_session.add_all([first_patient, second_patient, first_therapist, second_therapist])
    db_session.flush()
    first_requirement = TreatmentRequirement(
        patient_id=first_patient.id, department_id=speech.id,
        assigned_therapist_id=first_therapist.id, location_id=clinic_a.id,
        sessions_per_week=1, sequence_order=0,
    )
    second_requirement = TreatmentRequirement(
        patient_id=second_patient.id, department_id=ot.id,
        assigned_therapist_id=second_therapist.id, location_id=clinic_b.id,
        sessions_per_week=1, sequence_order=0,
    )
    db_session.add_all([first_requirement, second_requirement])
    db_session.flush()
    db_session.add_all([
        Appointment(
            patient_id=first_patient.id, treatment_requirement_id=first_requirement.id,
            therapist_id=first_therapist.id, department_id=speech.id, location_id=clinic_a.id,
            date=date(2026, 9, 29), slot_index=0, status=AppointmentStatus.CONFIRMED, source="test",
        ),
        Appointment(
            patient_id=second_patient.id, treatment_requirement_id=second_requirement.id,
            therapist_id=second_therapist.id, department_id=ot.id, location_id=clinic_b.id,
            date=date(2026, 9, 29), slot_index=1, status=AppointmentStatus.NEEDS_RESCHEDULING, source="test",
        ),
    ])
    db_session.commit()

    overall = web_app.assistant_summary_payload(db_session, "day_summary", date(2026, 9, 29))
    clinic = web_app.assistant_summary_payload(
        db_session, "centre_summary", date(2026, 9, 29), "Clinic A"
    )

    assert overall["summary"]["scheduled"] == 1
    assert overall["summary"]["needsRescheduling"] == 1
    assert overall["summary"]["byTherapy"] == [{"name": "Speech", "count": 1}]
    assert clinic["summary"]["scope"] == "Centre: Clinic A"
    assert clinic["summary"]["patients"] == 1


def test_scheduling_assistant_builds_a_patient_summary(db_session, monkeypatch):
    department = Department(name="Speech")
    location = Location(name="Clinic A", capacity=2)
    stella = Patient(patient_code="P-6F4EC6", name="Stella White")
    another_patient = Patient(patient_code="P-OTHER", name="Another child")
    db_session.add_all([department, location, stella, another_patient])
    db_session.flush()
    therapist = Therapist(
        therapist_code="T-STELLA", name="Stella's therapist",
        department_id=department.id, location_id=location.id,
    )
    db_session.add(therapist)
    db_session.flush()
    stella_requirement = TreatmentRequirement(
        patient_id=stella.id, department_id=department.id,
        assigned_therapist_id=therapist.id, location_id=location.id,
        sessions_per_week=1, sequence_order=0,
    )
    other_requirement = TreatmentRequirement(
        patient_id=another_patient.id, department_id=department.id,
        assigned_therapist_id=therapist.id, location_id=location.id,
        sessions_per_week=1, sequence_order=0,
    )
    db_session.add_all([stella_requirement, other_requirement])
    db_session.flush()
    db_session.add_all([
        Appointment(
            patient_id=stella.id, treatment_requirement_id=stella_requirement.id,
            therapist_id=therapist.id, department_id=department.id, location_id=location.id,
            date=date(2026, 9, 28), slot_index=0, status=AppointmentStatus.CONFIRMED,
            source="test",
        ),
        Appointment(
            patient_id=another_patient.id, treatment_requirement_id=other_requirement.id,
            therapist_id=therapist.id, department_id=department.id, location_id=location.id,
            date=date(2026, 9, 28), slot_index=1, status=AppointmentStatus.CONFIRMED,
            source="test",
        ),
    ])
    db_session.commit()

    payload = web_app.assistant_summary_payload(
        db_session, "patient_summary", date(2026, 9, 28), "Stella White P-6F4EC6"
    )

    assert payload["outcome"] == "summary"
    assert payload["summary"] == {
        "date": "2026-09-28",
        "scope": "Patient: Stella White P-6F4EC6",
        "scheduled": 1,
        "completed": 0,
        "cancelled": 0,
        "needsRescheduling": 0,
        "therapists": 1,
        "patients": 1,
        "byTherapy": [{"name": "Speech", "count": 1}],
    }

    captured = {}

    class FakeHandler:
        def send_json(self, result, status=None):
            captured["payload"] = result

    monkeypatch.setattr(web_app, "SessionLocal", lambda: nullcontext(db_session))
    monkeypatch.setattr(web_app, "interpret_chat_action", lambda _message: {
        "action": "patient_summary", "entity_query": "P-6F4EC6",
        "date": "2026-09-28", "reply": "",
    })

    web_app.TherapySchedulerHandler.handle_assistant_message(
        FakeHandler(), {
            "message": "Give a summary of the patient Stella White P-6F4EC6 and her therapy"
        }
    )

    assert captured["payload"]["summary"]["scope"] == "Patient: P-6F4EC6"
    assert captured["payload"]["summary"]["scheduled"] == 1


def test_scheduling_assistant_available_slots_respect_therapist_availability(db_session):
    department = Department(name="Speech")
    location = Location(name="Clinic A", capacity=1)
    db_session.add_all([department, location])
    db_session.flush()
    therapist = Therapist(
        therapist_code="T-AVAILABLE", name="Available therapist",
        department_id=department.id, location_id=location.id,
    )
    db_session.add(therapist)
    db_session.flush()
    db_session.add(web_app.TherapistAvailability(
        therapist_id=therapist.id, day_of_week=1, slot_index=3, available=True,
    ))
    db_session.commit()

    payload = web_app.assistant_attention_payload(
        db_session, date(2026, 9, 29), "empty_slots"
    )

    assert payload["items"] == [{
        "name": "Available therapist",
        "detail": "Clinic A · 11:15–11:55",
    }]


def test_scheduling_assistant_scopes_available_slots_to_named_therapist(db_session, monkeypatch):
    department = Department(name="Speech")
    location = Location(name="Clinic A", capacity=2)
    db_session.add_all([department, location])
    db_session.flush()
    abigail = Therapist(
        therapist_code="THER_0168", name="Abigail Foster T-EA5459",
        department_id=department.id, location_id=location.id,
    )
    alex = Therapist(
        therapist_code="THER_0169", name="Alex Lee T-B08583",
        department_id=department.id, location_id=location.id,
    )
    db_session.add_all([abigail, alex])
    db_session.flush()
    db_session.add_all([
        web_app.TherapistAvailability(
            therapist_id=abigail.id, day_of_week=0, slot_index=0, available=True,
        ),
        web_app.TherapistAvailability(
            therapist_id=abigail.id, day_of_week=0, slot_index=3, available=True,
        ),
        web_app.TherapistAvailability(
            therapist_id=alex.id, day_of_week=0, slot_index=1, available=True,
        ),
    ])
    db_session.commit()

    payload = web_app.assistant_attention_payload(
        db_session, date(2026, 9, 28), "empty_slots",
        "Abigail Foster T-EA5459 · THER_0168",
    )

    assert payload["message"] == "Available slots for Abigail Foster T-EA5459 on 2026-09-28: 2."
    assert payload["itemsLabel"] == "Available slots"
    assert payload["items"] == [
        {"name": "Abigail Foster T-EA5459", "detail": "Clinic A · 09:00–09:40"},
        {"name": "Abigail Foster T-EA5459", "detail": "Clinic A · 11:15–11:55"},
    ]

    captured = {}

    class FakeHandler:
        def send_json(self, result, status=None):
            captured["payload"] = result

    monkeypatch.setattr(web_app, "SessionLocal", lambda: nullcontext(db_session))
    monkeypatch.setattr(web_app, "interpret_chat_action", lambda _message: {
        "action": "empty_slots", "entity_query": "THER_0168",
        "date": "2026-09-28", "reply": "",
    })

    web_app.TherapySchedulerHandler.handle_assistant_message(
        FakeHandler(), {"message": "Give the available slots of Abigail Foster"}
    )

    assert captured["payload"]["items"] == payload["items"]


def test_assistant_summary_names_respects_scope_and_ten_person_limit(db_session):
    speech = Department(name="Speech")
    clinic = Location(name="Clinic A", capacity=20)
    db_session.add_all([speech, clinic])
    db_session.flush()
    therapist = Therapist(
        therapist_code="T-NAMES", name="Speech therapist", department_id=speech.id,
        location_id=clinic.id,
    )
    db_session.add(therapist)
    db_session.flush()
    for index in range(11):
        patient = Patient(patient_code=f"P-NAMES-{index:02}", name=f"Child {index:02}")
        db_session.add(patient)
        db_session.flush()
        requirement = TreatmentRequirement(
            patient_id=patient.id, department_id=speech.id,
            assigned_therapist_id=therapist.id, location_id=clinic.id,
            sessions_per_week=1, sequence_order=0,
        )
        db_session.add(requirement)
        db_session.flush()
        db_session.add(Appointment(
            patient_id=patient.id, treatment_requirement_id=requirement.id,
            therapist_id=therapist.id, department_id=speech.id, location_id=clinic.id,
            date=date(2026, 9, 29), slot_index=index % 10,
            status=AppointmentStatus.CONFIRMED, source="test",
        ))
    db_session.commit()

    payload = web_app.assistant_summary_names_payload(db_session, {
        "summary": {
            "action": "therapy_summary", "date": "2026-09-29", "entity_query": "Speech",
        }
    })

    assert payload["outcome"] == "list"
    assert len(payload["items"]) == 10
    assert "10 of 11" in payload["message"]
    assert payload["items"][0]["name"] == "Child 00"


def test_scheduling_assistant_requires_choice_for_duplicate_absence_name(db_session, monkeypatch):
    first = Patient(patient_code="P-DUP-1", name="Alex Rao")
    second = Patient(patient_code="P-DUP-2", name="Alex Rao")
    db_session.add_all([first, second])
    db_session.commit()
    captured = {}

    class FakeHandler:
        def send_json(self, payload, status=None):
            captured["payload"] = payload

    session_id = "assistant-test-duplicate"
    web_app.clear_assistant_memory(session_id)
    monkeypatch.setattr(web_app, "SessionLocal", lambda: nullcontext(db_session))
    monkeypatch.setattr(web_app, "interpret_chat_action", lambda _message: {
        "action": "mark_absent", "entity_query": "Alex Rao", "absence_kind": "patient",
        "date": "2026-09-29", "reply": "",
    })

    web_app.TherapySchedulerHandler.handle_assistant_message(
        FakeHandler(), {"message": "Mark Alex Rao absent", "sessionId": session_id}
    )

    assert captured["payload"]["outcome"] == "needs_clarification"
    assert [item["label"] for item in captured["payload"]["choices"]] == [
        "Alex Rao · P-DUP-1 · Patient", "Alex Rao · P-DUP-2 · Patient",
    ]

    web_app.TherapySchedulerHandler.handle_assistant_message(
        FakeHandler(), {"message": "2", "sessionId": session_id}
    )

    assert captured["payload"]["outcome"] == "absence_recorded"
    assert db_session.scalar(select(web_app.PatientAbsence).where(
        web_app.PatientAbsence.patient_id == second.id
    )) is not None
    assert db_session.scalar(select(web_app.PatientAbsence).where(
        web_app.PatientAbsence.patient_id == first.id
    )) is None


def test_assistant_therapist_absence_lists_every_released_appointment(db_session):
    speech = Department(name="Speech")
    clinic = Location(name="Clinic A", capacity=2)
    therapist = Therapist(
        therapist_code="T-ABSENCE", name="Absent therapist", department=speech,
        location=clinic,
    )
    first = Patient(patient_code="P-RELEASE-1", name="First child")
    second = Patient(patient_code="P-RELEASE-2", name="Second child")
    db_session.add_all([speech, clinic, therapist, first, second])
    db_session.flush()
    appointments = []
    for index, patient in enumerate((first, second)):
        requirement = TreatmentRequirement(
            patient_id=patient.id, department_id=speech.id,
            assigned_therapist_id=therapist.id, location_id=clinic.id,
            sessions_per_week=1, sequence_order=0,
        )
        db_session.add(requirement)
        db_session.flush()
        appointments.append(Appointment(
            patient_id=patient.id, treatment_requirement_id=requirement.id,
            therapist_id=therapist.id, department_id=speech.id, location_id=clinic.id,
            date=date(2026, 9, 29), slot_index=index,
            status=AppointmentStatus.CONFIRMED, source="test",
        ))
    db_session.add_all(appointments)
    db_session.commit()

    payload = web_app.record_assistant_absence(db_session, {
        "kind": "therapist", "id": therapist.id, "name": therapist.name, "code": therapist.therapist_code,
    }, date(2026, 9, 29))

    assert payload["affected"] == 2
    assert [item["name"] for item in payload["items"]] == ["First child", "Second child"]
    assert all(item.status == AppointmentStatus.NEEDS_RESCHEDULING for item in appointments)


def test_assistant_memory_answers_pronoun_follow_up_for_single_listed_patient(db_session):
    speech = Department(name="Speech Therapy Session")
    clinic = Location(name="Clinic A", capacity=1)
    patient = Patient(patient_code="P-MEMORY", name="Memory patient")
    db_session.add_all([speech, clinic, patient])
    db_session.flush()
    therapist = Therapist(
        therapist_code="T-MEMORY", name="Memory therapist", department_id=speech.id,
        location_id=clinic.id,
    )
    db_session.add(therapist)
    db_session.flush()
    requirement = TreatmentRequirement(
        patient_id=patient.id, department_id=speech.id, assigned_therapist_id=therapist.id,
        location_id=clinic.id, sessions_per_week=1, sequence_order=0,
    )
    db_session.add(requirement)
    db_session.flush()
    db_session.add(Appointment(
        patient_id=patient.id, treatment_requirement_id=requirement.id,
        therapist_id=therapist.id, department_id=speech.id, location_id=clinic.id,
        date=date(2026, 9, 29), slot_index=1,
        status=AppointmentStatus.CONFIRMED, source="test",
    ))
    db_session.commit()

    payload = web_app.assistant_memory_patient_payload(db_session, {
        "last_patient_ids": [patient.id],
        "summary": {"date": "2026-09-29"},
    }, "Who is his therapist?")

    assert payload == {
        "outcome": "patient_detail",
        "message": "Memory patient's therapist for 2026-09-29 is Memory therapist.",
    }
