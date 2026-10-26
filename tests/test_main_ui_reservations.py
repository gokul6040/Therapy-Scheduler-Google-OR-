from contextlib import nullcontext

import pytest
from sqlalchemy import select

import web_app
from database.models import AdminSlotReservation, Appointment, AppointmentStatus, Patient
from importers.parent_response_demo_importer import load_parent_response_demo
from services.parent_response_service import process_daily_responses


@pytest.fixture
def main_booking(db_session, monkeypatch):
    target = load_parent_response_demo(db_session)
    result = process_daily_responses(db_session, target)
    monkeypatch.setattr(web_app, "SessionLocal", lambda: nullcontext(db_session))
    patients = {item.patient_code: item for item in db_session.scalars(select(Patient))}

    class Handler:
        def send_json(self, payload, status=None):
            self.payload = payload

    return target, result.released_appointment_ids[0], patients, Handler()


def test_main_ui_can_preview_and_confirm_suggestion(main_booking, db_session):
    target, source_id, patients, handler = main_booking
    web_app.TherapySchedulerHandler.reservation_context(handler, source_id)
    assert handler.payload["appointmentId"] == source_id
    assert handler.payload["visitSessions"] == 1
    web_app.TherapySchedulerHandler.admin_reservation(handler, source_id, {
        "patientId": patients["DEMO-D"].id,
    }, False)
    preview = handler.payload
    assert not preview["warnings"]
    web_app.TherapySchedulerHandler.admin_reservation(handler, source_id, {
        "patientId": patients["DEMO-D"].id,
        "previewToken": preview["preview_token"], "requestId": "main-ui-suggestion",
    }, True)
    appointment = db_session.get(Appointment, handler.payload["appointment_ids"][0])
    assert appointment.status == AppointmentStatus.CONFIRMED
    assert appointment.slot_index == 1
    assert source_id not in {row["id"] for row in web_app.operations_payload(target)["needsRescheduling"]}
    assert source_id not in {row["id"] for row in web_app.schedule_payload(target, 1)["appointments"]}


def test_main_ui_allows_search_choice_with_explicit_warning_override(main_booking, db_session):
    _, source_id, patients, handler = main_booking
    web_app.TherapySchedulerHandler.admin_reservation(handler, source_id, {
        "patientId": patients["DEMO-E"].id,
    }, False)
    preview = handler.payload
    assert "WEEKLY_TARGET" in {row["code"] for row in preview["warnings"]}
    body = {
        "patientId": patients["DEMO-E"].id, "requestId": "main-ui-override",
        "previewToken": preview["preview_token"],
    }
    with pytest.raises(web_app.ApiError, match="Reserve anyway"):
        web_app.TherapySchedulerHandler.admin_reservation(handler, source_id, body, True)
    web_app.TherapySchedulerHandler.admin_reservation(handler, source_id, {**body, "overrideWarnings": True}, True)
    reservation = db_session.get(AdminSlotReservation, source_id)
    assert db_session.get(Appointment, reservation.appointment_id).patient_id == patients["DEMO-E"].id


def test_main_ui_supports_patient_cancellations_and_keeps_messaging_inactive(main_booking, db_session):
    _, source_id, patients, handler = main_booking
    db_session.get(Appointment, source_id).status = AppointmentStatus.CANCELLED_PATIENT
    db_session.commit()
    web_app.TherapySchedulerHandler.reservation_context(handler, source_id)
    web_app.TherapySchedulerHandler.admin_reservation(handler, source_id, {
        "patientId": patients["DEMO-D"].id,
    }, False)
    preview = handler.payload
    with pytest.raises(web_app.ApiError, match="display-only"):
        web_app.TherapySchedulerHandler.admin_reservation(handler, source_id, {
            "patientId": patients["DEMO-D"].id, "requestId": "no-message",
            "previewToken": preview["preview_token"], "mode": "with_messaging",
        }, True)
    assert db_session.get(AdminSlotReservation, source_id) is None


def test_local_ui_startup_does_not_import_or_start_background_messaging(monkeypatch):
    called = []

    class Server:
        def __init__(self, *args):
            pass

        def serve_forever(self):
            called.append("served")

        def server_close(self):
            called.append("closed")

    def forbidden(*args, **kwargs):
        raise AssertionError("Local UI must not import records or start messaging")

    monkeypatch.setattr(web_app, "init_admin_booking_tables", lambda: called.append("tracking_tables"))
    monkeypatch.setattr(web_app, "init_db", forbidden)
    monkeypatch.setattr(web_app, "initialize_source_database", forbidden)
    monkeypatch.setattr(web_app, "start_help_reply_worker", forbidden)
    monkeypatch.setattr(web_app, "ThreadingHTTPServer", Server)
    web_app.run(local_ui_only=True)
    assert called == ["tracking_tables", "served", "closed"]
