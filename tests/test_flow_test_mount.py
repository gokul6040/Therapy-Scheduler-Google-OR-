"""Check HTTP isolation in the shared app without external connections."""

from datetime import date, datetime, timedelta
from contextlib import nullcontext
from io import BytesIO
import json
from types import SimpleNamespace

import pytest

from database.db import Base
from database.models import Appointment, AppointmentStatus, Patient, PatientAvailability, TreatmentRequirement
from sqlalchemy import select
from flow_test import mount
from flow_test.service import FlowTest
from web_app import TherapySchedulerHandler


@pytest.fixture
def mounted(tmp_path, monkeypatch, legacy_flow_scenario):
    flow = FlowTest(f"sqlite:///{(tmp_path / 'mounted.sqlite').as_posix()}", scenario=legacy_flow_scenario)
    # Historical scenario dates must not change meaning with the machine's date.
    monkeypatch.setattr(flow, "next_offer_date", lambda: date(2026, 10, 22))
    instance = SimpleNamespace(flow=flow, token="test-key", previews={})
    monkeypatch.setattr(mount, "runtime", lambda: instance)
    yield flow
    flow.engine.dispose()


def request(path, body=None, token="test-key"):
    class Connection:
        def __init__(self, raw):
            self.input, self.output = BytesIO(raw), BytesIO()

        def makefile(self, *_args, **_kwargs):
            return self.input

        def sendall(self, data):
            self.output.write(data)

    raw = json.dumps(body).encode() if body is not None else b""
    method = "POST" if body is not None else "GET"
    head = (f"{method} {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n"
            f"Authorization: Bearer {token}\r\nContent-Length: {len(raw)}\r\n\r\n")
    connection = Connection(head.encode() + raw)
    TherapySchedulerHandler(connection, ("127.0.0.1", 1234), object())
    headers, content = connection.output.getvalue().split(b"\r\n\r\n", 1)
    assert f"Content-Length: {len(content)}".encode() in headers
    return int(headers.split()[1]), content


def test_shared_shell_and_private_file_protection(mounted):
    import re
    import web_app

    status, html = request("/test")
    assert status == 200 and b'Calendar' in html and b'Clinical team' in html
    assert b'/test/workspace.js' in html and b'/test/shared.js' in html
    original = (web_app.WEB_DIR / "index.html").read_bytes()
    main = rb'<main id="mainContent".*?</main>'
    assert re.search(main, html, re.S)[0] == re.search(main, original, re.S)[0]
    assert request("/test/shared.js")[1] == (web_app.WEB_DIR / "app.js").read_bytes()
    assert request("/test/") == (status, html)
    for path in ("/test/.env", "/test/recipients.local.json", "/test/../.env"):
        assert request(path)[0] == 404
    assert request("/test/shared.css")[0] == 200


def test_multiple_requests_share_connection_without_repeating_response_headers(mounted):
    class Connection:
        def __init__(self):
            first = b"GET /test/api/state HTTP/1.1\r\nHost: local\r\nAuthorization: Bearer test-key\r\n\r\n"
            second = b"GET /test/api/state HTTP/1.1\r\nHost: local\r\nAuthorization: Bearer test-key\r\nConnection: close\r\n\r\n"
            self.input, self.output = BytesIO(first + second), BytesIO()

        def makefile(self, *_args, **_kwargs):
            return self.input

        def sendall(self, data):
            self.output.write(data)

    connection = Connection()
    TherapySchedulerHandler(connection, ("127.0.0.1", 1234), object())
    assert connection.output.getvalue().count(b"HTTP/1.1 200") == 2


def test_namespaced_daily_flow_never_opens_production_session(mounted, monkeypatch):
    import web_app

    def forbidden():
        pytest.fail("Mounted request attempted a production session")

    monkeypatch.setattr(web_app, "SessionLocal", forbidden)
    assert request("/test/api/state", token="wrong")[0] == 401
    assert request("/test/api/source/initialize", {})[0] == 404
    assert request("/test/api/zoho/appointments/push-batch", {})[0] == 404
    status, result = request("/test/api/generate", {"date": "2026-10-19"})
    assert status == 200 and json.loads(result)["added"] == 4
    assert request("/test/api/publish", {"date": "2026-10-19"})[0] == 200
    request("/test/api/process", {})
    data = json.loads(request("/test/api/state?date=2026-10-19")[1])
    token = next(o["token"] for o in data["offers"] if o["patient"] == "TEST-A")
    assert request("/test/api/simulate", {"token": token, "action": "YES"})[0] == 200
    request("/test/api/process", {})
    rows = mounted.state(date(2026, 10, 19))["appointments"]
    assert {a["status"] for a in rows if a["patient"] == "TEST-A"} == {"CONFIRMED"}
    assert not any(name.startswith("flow_test_") for name in Base.metadata.tables)


def test_shared_callback_routes_only_coworkers_on_test_business(mounted, monkeypatch):
    monkeypatch.setenv("FLOW_TEST_LIVE", "true")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "business-1")
    mounted.live, mounted.recipients = True, {"TEST-A": "919999999991"}
    captured = []
    monkeypatch.setattr(mounted, "receive", captured.append)
    value = {"metadata": {"phone_number_id": "business-1"}, "messages": [
        {"id": "test", "from": "919999999991"}, {"id": "normal", "from": "919999999992"},
    ], "statuses": [{"id": "test-status", "recipient_id": "919999999991"}]}
    original = {"entry": [{"changes": [{"value": value}]}]}
    rest = mount.route_webhook(original)["entry"][0]["changes"][0]["value"]
    assert [m["id"] for m in rest["messages"]] == ["normal"]
    selected = captured[0]["entry"][0]["changes"][0]["value"]
    assert [m["id"] for m in selected["messages"]] == ["test"]
    assert len(value["messages"]) == 2
    value["metadata"]["phone_number_id"] = "another-business"
    assert mount.route_webhook(original) == original
    assert not captured[-1]["entry"][0]["changes"][0]["value"]["messages"]


def test_shared_screens_and_preferences_use_test_rows_only(mounted, db_session, monkeypatch):
    import web_app

    patient = Patient(patient_code="MAIN-CONTROL", name="Original record", active=True, school_going=False)
    db_session.add(patient)
    db_session.commit()
    monkeypatch.setattr(web_app, "ProductionSessionLocal", lambda: nullcontext(db_session))
    for path in ("overview", "schedule", "patients", "therapists", "operations", "assistant/status"):
        assert request(f"/test/api/{path}")[0] == 200
    assert json.loads(request("/test/api/overview")[1])["planningMode"] == "day"
    assert "planningMode" not in json.loads(request("/api/overview")[1])
    test_people = json.loads(request("/test/api/patients")[1])["patients"]
    assert {p["code"] for p in test_people} == {"TEST-A", "TEST-B", "TEST-C", "TEST-D"}
    assert {p["code"] for p in json.loads(request("/api/patients")[1])["patients"]} == {"MAIN-CONTROL"}
    test_id = test_people[0]["id"]
    assert request(f"/test/api/patients/{test_id}/preferences", {"day": 2, "period": "morning"})[0] == 200
    assert json.loads(request(f"/test/api/patients/{test_id}/preferences")[1])["preference"]["day"] == 2
    assert not list(db_session.scalars(select(PatientAvailability)))
    assert web_app.WORKSPACE_CONTEXT.get() is None


def test_shared_preview_save_is_isolated_from_main_preview_store(mounted, monkeypatch):
    import web_app

    def forbidden():
        pytest.fail("A test UI handler opened the production database")

    monkeypatch.setattr(web_app, "ProductionSessionLocal", forbidden)
    monkeypatch.setattr(web_app, "generate_rolling_schedule", forbidden)
    monkeypatch.setattr(web_app, "PREVIEWS", {})
    target = date.today()
    if target.weekday() == 6:
        target += timedelta(days=1)
    status, raw = request("/test/api/schedules/generate", {"start": target.isoformat(), "searchSeconds": 20})
    assert status == 201
    preview = json.loads(raw)
    assert preview["canSave"] and preview["appointments"]
    assert preview["planningDays"] == 1 and preview["start"] == preview["end"] == target.isoformat()
    assert web_app.PREVIEWS == {}
    assert request("/test/api/schedules/00000000-0000-0000-0000-000000000001/save", {})[0] == 404
    status, result = request(f"/test/api/schedules/{preview['previewId']}/save", {})
    assert status == 200 and json.loads(result)["added"] > 0
    assert request(f"/test/api/schedules/{preview['previewId']}/save", {}) == (status, result)
    assert web_app.WORKSPACE_CONTEXT.get() is None


def test_shared_daily_preview_counts_fixed_past_future_and_completed_sessions(mounted):
    monday, target, friday = date(2026, 10, 19), date(2026, 10, 21), date(2026, 10, 23)
    mounted.generate(monday)
    mounted.generate(friday)
    with mounted.sessions.begin() as session:
        appointment = session.scalar(select(Appointment).where(Appointment.date == monday))
        appointment.status = AppointmentStatus.COMPLETED
    def snapshot():
        with mounted.sessions() as session:
            return {a.id: (a.patient_id, a.treatment_requirement_id, a.therapist_id,
                           a.department_id, a.location_id, a.date, a.slot_index,
                           a.status, a.bundle_id) for a in session.scalars(select(Appointment))}
    before = snapshot()
    assert len(before) == 8
    status, raw = request("/test/api/schedules/generate", {
        "start": target.isoformat(), "searchSeconds": 20, "days": 14,
    })
    preview = json.loads(raw)
    assert status == 201 and preview["canSave"]
    assert preview["newSessions"] == 1 and preview["scheduledOnSelectedDay"] == 1
    assert {a["date"] for a in preview["appointments"]} == {target.isoformat()}
    assert preview["unscheduled"] == 0  # other saved days already cover the remaining targets
    assert snapshot() == before  # generation is read-only
    assert mounted.state(target)["jobs"] == []
    status, raw = request(f"/test/api/schedules/{preview['previewId']}/save", {})
    assert status == 200 and json.loads(raw)["added"] == 1
    after = snapshot()
    assert all(after[key] == value for key, value in before.items())
    assert {value[5] for key, value in after.items() if key not in before} == {target}
    assert len(after) == 9
    assert mounted.state(target)["jobs"] == []  # publication remains a separate admin action
    preview = json.loads(request("/test/api/schedules/generate", {"start": target.isoformat(), "searchSeconds": 20})[1])
    assert preview["newSessions"] == 0
    assert json.loads(request(f"/test/api/schedules/{preview['previewId']}/save", {})[1])["added"] == 0
    assert snapshot() == after


def test_shared_daily_preview_rejects_sunday_without_saving(mounted):
    status, raw = request("/test/api/schedules/generate", {"start": "2026-10-25", "searchSeconds": 20})
    assert status == 400 and "Monday through Saturday" in json.loads(raw)["error"]
    with mounted.sessions() as session:
        assert not list(session.scalars(select(Appointment)))
    assert mount.runtime().previews == {}


def calendar_preview(target="2026-10-22"):
    status, raw = request("/test/api/schedules/generate", {
        "start": target, "searchSeconds": 20,
    })
    assert status == 201, raw
    return json.loads(raw)


def test_saving_tomorrow_queues_complete_visits_once_and_never_sends_in_request(mounted, monkeypatch):
    tomorrow = date(2026, 10, 22)
    sender = mounted.send
    monkeypatch.setattr(mounted, "next_offer_date", lambda: tomorrow)
    monkeypatch.setattr(mounted, "send", lambda *_: pytest.fail("Save sent a message synchronously"))
    preview = calendar_preview()
    assert preview["autoOfferOnSave"] and not preview["liveMessaging"]
    assert not mounted.state(tomorrow)["appointments"]
    path = f"/test/api/schedules/{preview['previewId']}/save"
    status, raw = request(path, {})
    assert status == 200, raw
    saved = json.loads(raw)
    assert saved["added"] == 4 and saved["offers"] == 3
    assert saved["offerDate"] == tomorrow.isoformat()
    current = mounted.state(tomorrow)
    assert len(current["appointments"]) == 4
    assert {a["status"] for a in current["appointments"]} == {"PENDING_CONFIRMATION"}
    assert len(current["offers"]) == len(current["jobs"]) == 3
    assert all(o["send_status"] == "QUEUED" for o in current["offers"])
    assert len(next(o for o in current["offers"] if o["patient"] == "TEST-A")["appointment_ids"]) == 2
    assert request(path, {}) == (status, raw)
    assert mounted.state(tomorrow) == current
    # Another reviewed save must not resend existing pending or confirmed visits.
    monkeypatch.setattr(mounted, "send", sender)
    mounted.drain()
    a_token = next(o["token"] for o in current["offers"] if o["patient"] == "TEST-A")
    mounted.simulate(a_token, "YES")
    mounted.drain()
    before = mounted.state(tomorrow)
    second = calendar_preview()
    second_saved = json.loads(request(f"/test/api/schedules/{second['previewId']}/save", {})[1])
    assert second_saved["added"] == second_saved["offers"] == 0
    after = mounted.state(tomorrow)
    assert after["appointments"] == before["appointments"]
    assert after["offers"] == before["offers"]
    assert after["jobs"] == before["jobs"]


@pytest.mark.parametrize("target", ["2026-10-21", "2026-10-23"])
def test_saving_another_date_never_publishes_its_visits_or_tomorrows(mounted, monkeypatch, target):
    tomorrow = date(2026, 10, 22)
    monkeypatch.setattr(mounted, "next_offer_date", lambda: tomorrow)
    mounted.generate(tomorrow)
    tomorrow_before = mounted.state(tomorrow)["appointments"]
    preview = calendar_preview(target)
    assert not preview["autoOfferOnSave"]
    status, raw = request(f"/test/api/schedules/{preview['previewId']}/save", {})
    assert status == 200, raw
    assert json.loads(raw)["offers"] == 0
    assert mounted.state(tomorrow)["appointments"] == tomorrow_before
    assert not mounted.state(tomorrow)["offers"]
    current = mounted.state(date.fromisoformat(target))
    assert current["appointments"] and not current["offers"] and not current["jobs"]
    assert {a["status"] for a in current["appointments"]} == {"PROPOSED"}


@pytest.mark.parametrize("missing", ["recipient"])
def test_tomorrow_save_rolls_back_whole_batch_until_recipients_ready(mounted, monkeypatch, missing):
    from database.models import AuditLog
    from flow_test.models import FlowJob, FlowOffer

    tomorrow = date(2026, 10, 22)
    monkeypatch.setattr(mounted, "next_offer_date", lambda: tomorrow)
    mounted.live = True
    mounted.recipients = {code: f"91999999999{i}" for i, code in enumerate(("TEST-A", "TEST-B", "TEST-C"))}
    monkeypatch.setattr(mounted, "_session_deadline", lambda _session, phone: 0 if missing == "window" and phone.endswith("2") else float("inf"))
    if missing == "recipient":
        mounted.recipients.pop("TEST-C")
    monkeypatch.setattr(mounted, "send", lambda *_: pytest.fail("Save sent a real message"))
    preview = calendar_preview()
    assert preview["liveMessaging"]
    path = f"/test/api/schedules/{preview['previewId']}/save"
    status, raw = request(path, {})
    assert status == 409, raw
    assert (b"parent phone number" if missing == "recipient" else b"START") in raw
    with mounted.sessions() as session:
        assert all(not list(session.scalars(select(model))) for model in (Appointment, AuditLog, FlowOffer, FlowJob))
    # The same preview remains retryable after fixing contact readiness.
    mounted.recipients["TEST-C"] = "919999999992"
    monkeypatch.setattr(mounted, "_session_deadline", lambda *_: float("inf"))
    status, raw = request(path, {})
    assert status == 200, raw
    assert json.loads(raw)["offers"] == 3
    assert len(mounted.state(tomorrow)["appointments"]) == 4
    assert all(j["status"] == "QUEUED" for j in mounted.state(tomorrow)["jobs"])


def test_tomorrow_save_queues_existing_proposals_without_touching_other_dates(mounted, monkeypatch):
    tomorrow = date(2026, 10, 22)
    monkeypatch.setattr(mounted, "next_offer_date", lambda: tomorrow)
    mounted.generate(tomorrow)
    other = date(2026, 10, 23)
    mounted.generate(other)
    other_before = mounted.state(other)["appointments"]
    preview = calendar_preview()
    status, raw = request(f"/test/api/schedules/{preview['previewId']}/save", {})
    assert status == 200, raw
    assert json.loads(raw)["added"] == 0 and json.loads(raw)["offers"] == 3
    assert mounted.state(other)["appointments"] == other_before
    assert not mounted.state(other)["offers"]


def test_tomorrow_queue_failure_rolls_back_saved_rows_and_partial_offers(mounted, monkeypatch):
    from database.models import AuditLog
    from flow_test.models import FlowJob, FlowOffer

    monkeypatch.setattr(mounted, "next_offer_date", lambda: date(2026, 10, 22))
    preview = calendar_preview()
    job = mounted._job
    calls = []

    def failing_job(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise ValueError("Test queue write failure")
        return job(*args, **kwargs)

    monkeypatch.setattr(mounted, "_job", failing_job)
    status, raw = request(f"/test/api/schedules/{preview['previewId']}/save", {})
    assert status == 409 and b"queue write failure" in raw
    with mounted.sessions() as session:
        assert all(not list(session.scalars(select(model))) for model in (Appointment, AuditLog, FlowOffer, FlowJob))


@pytest.mark.parametrize("initial,next_date", [("2026-10-22", "2026-10-23"), ("2026-10-21", "2026-10-22")])
def test_save_requires_new_preview_if_midnight_changes_sending(initial, next_date, mounted, monkeypatch):
    monkeypatch.setattr(mounted, "next_offer_date", lambda: date.fromisoformat(initial))
    preview = calendar_preview()
    monkeypatch.setattr(mounted, "next_offer_date", lambda: date.fromisoformat(next_date))
    status, raw = request(f"/test/api/schedules/{preview['previewId']}/save", {})
    assert status == 409 and b"local date changed" in raw
    assert not mounted.state(date(2026, 10, 22))["appointments"]


def test_auto_offer_tomorrow_uses_indian_local_date(monkeypatch):
    from flow_test import service

    class LocalClock:
        @staticmethod
        def now(zone):
            assert str(zone) == "Asia/Kolkata"
            # India has crossed midnight while the UTC server date is still 20th.
            return datetime(2026, 10, 21, 0, 30)

    monkeypatch.setattr(service, "datetime", LocalClock)
    assert FlowTest.next_offer_date() == date(2026, 10, 22)


def test_eight_child_calendar_save_queues_eight_visits(tmp_path, monkeypatch):
    flow = FlowTest(f"sqlite:///{(tmp_path / 'eight-mounted.sqlite').as_posix()}")
    monkeypatch.setattr(flow, "next_offer_date", lambda: date(2026, 10, 22))
    instance = SimpleNamespace(flow=flow, token="test-key", previews={})
    monkeypatch.setattr(mount, "runtime", lambda: instance)
    try:
        assert len(json.loads(request("/test/api/patients")[1])["patients"]) == 8
        preview = calendar_preview()
        assert preview["newSessions"] == 10 and preview["autoOfferOnSave"]
        status, raw = request(f"/test/api/schedules/{preview['previewId']}/save", {})
        assert status == 200, raw
        assert json.loads(raw)["offers"] == 8
        state = flow.state(date(2026, 10, 22))
        assert len(state["offers"]) == 8
    finally:
        flow.engine.dispose()


def test_shared_daily_default_date_and_counts_use_indian_local_day(mounted, monkeypatch):
    import web_app
    from flow_test import shared_ui

    class ServerDate(date):
        @classmethod
        def today(cls):
            return cls(2026, 10, 20)

    class LocalClock:
        @staticmethod
        def now(zone):
            assert str(zone) == "Asia/Kolkata"
            return datetime(2026, 10, 21, 0, 30)

    mounted.generate(date(2026, 10, 21))
    monkeypatch.setattr(web_app, "date", ServerDate)
    monkeypatch.setattr(shared_ui, "datetime", LocalClock)
    status, raw = request("/test/api/overview")
    overview = json.loads(raw)
    assert status == 200 and overview["today"] == "2026-10-21"
    assert overview["appointmentsToday"] == 4


def test_shared_ui_whatsapp_publish_help_confirm_and_attendance(mounted, monkeypatch):
    import web_app

    monkeypatch.setattr(web_app, "ProductionSessionLocal", lambda: pytest.fail("Production DB used"))
    target = "2026-10-19"
    request("/test/api/generate", {"date": target})
    rows = json.loads(request(f"/test/api/schedule?start={target}&days=1")[1])["appointments"]
    speech = next(a for a in rows if a["patientCode"] == "TEST-A" and a["therapy"] == "Speech")
    status, result = request("/test/api/publish", {"date": target, "appointment_id": speech["id"]})
    assert status == 200 and json.loads(result)["offers"] == 1
    state = lambda: json.loads(request(f"/test/api/state?date={target}")[1])
    assert state()["offers"][0]["send_status"] == "QUEUED"
    request("/test/api/process", {})
    offer = state()["offers"][0]
    assert len(offer["appointment_ids"]) == 2
    assert offer["send_status"] == "SIMULATED" and offer["delivery"] is None
    assert "Hi Fictional Child A" in offer["message_preview"]
    request("/test/api/simulate", {"token": offer["token"], "action": "HELP"})
    request("/test/api/process", {})
    current = state()
    assert current["offers"][0]["last_reply"] == "HELP"
    assert current["offers"][0]["state"] == "PENDING"
    assert not current["inbox"][0]["resolved"]
    request("/test/api/resolve", {"id": current["inbox"][0]["id"]})
    assert state()["inbox"][0]["resolved"]
    request("/test/api/simulate", {"token": offer["token"], "action": "YES"})
    request("/test/api/process", {})
    assert state()["offers"][0]["last_reply"] == "YES"
    assert request("/test/api/attendance", {"id": speech["id"], "status": "COMPLETED"})[0] == 200
    progress = next(p for p in state()["progress"] if p["patient"] == "TEST-A" and p["therapy"] == "Speech")
    assert progress["completed"] == 1
    assert request("/test/api/attendance", {"id": speech["id"], "status": "COMPLETED"})[0] == 400


def test_shared_ui_no_declines_day_then_requires_replacement_publication(mounted):
    target = "2026-10-19"
    request("/test/api/generate", {"date": target})
    request("/test/api/publish", {"date": target})
    request("/test/api/process", {})
    state = lambda: json.loads(request(f"/test/api/state?date={target}")[1])
    token = next(o["token"] for o in state()["offers"] if o["patient"] == "TEST-B")
    request("/test/api/simulate", {"token": token, "action": "NO"})
    request("/test/api/process", {})
    offer = next(o for o in state()["offers"] if o["token"] == token)
    assert offer["last_reply"] == "NO" and offer["state"] == "DECLINED"
    current = state()
    assert next(o for o in current["offers"] if o["token"] == token)["last_reply"] == "NO"
    replacement = next(a for a in current["appointments"] if a["patient"] == "TEST-D")
    assert replacement["status"] == "PROPOSED"
    assert not any(o["patient"] == "TEST-D" for o in current["offers"])
    assert request("/test/api/publish", {"date": target, "appointment_id": replacement["id"]})[0] == 200
    request("/test/api/process", {})
    assert next(o for o in state()["offers"] if o["patient"] == "TEST-D")["send_status"] == "SIMULATED"


def test_shared_ui_live_mode_rejects_simulation_and_unconfigured_recipients(mounted):
    mounted.live = True
    target = date.today()
    if target.weekday() == 6:
        target += timedelta(days=1)
    mounted.generate(target)
    assert request("/test/api/publish", {"date": target.isoformat()})[0] == 400
    assert request("/test/api/simulate", {"token": "a" * 20, "action": "YES"})[0] == 400
    assert mounted.state(target)["offers"] == []


def replacement_offer_request(flow):
    """Prepare a released visit and the admin's reviewed replacement choice."""
    target = date(2026, 10, 19)
    flow.generate(target)
    flow.publish(target)
    flow.drain()
    token = next(o["token"] for o in flow.state(target)["offers"] if o["patient"] == "TEST-B")
    flow.simulate(token, "DAY")
    flow.drain()
    with flow.sessions() as session:
        source = session.scalar(select(Appointment).join(Patient).where(
            Patient.patient_code == "TEST-B"))
        patient = session.scalar(select(Patient).where(Patient.patient_code == "TEST-D"))
        source_id, patient_id = source.id, patient.id
    status, raw = request(f"/test/api/appointments/{source_id}/reservation-preview", {
        "patientId": patient_id, "wholeVisit": True,
    })
    assert status == 200
    preview = json.loads(raw)
    return target, f"/test/api/appointments/{source_id}/reserve", {
        "patientId": patient_id, "wholeVisit": True, "mode": "with_messaging",
        "previewToken": preview["preview_token"], "requestId": "replacement-message-1",
        "overrideWarnings": bool(preview["warnings"]),
    }


def test_drawer_message_offer_waits_for_yes_and_retries_do_not_resend(mounted):
    target, path, body = replacement_offer_request(mounted)
    before = mounted.state(target)
    status, raw = request(path, body)
    assert status == 200, raw
    result = json.loads(raw)
    assert result["status"] == "PENDING_CONFIRMATION"
    assert result["offer_state"] == "PENDING"
    current = mounted.state(target)
    assert len(current["offers"]) == len(before["offers"]) + 1
    assert len(current["jobs"]) == len(before["jobs"]) + 1
    assert request(path, body) == (status, raw)
    assert len(mounted.state(target)["jobs"]) == len(current["jobs"])
    # Sending/delivery do not confirm the appointment.
    mounted.drain()
    assert next(a for a in mounted.state(target)["appointments"]
                if a["id"] == result["appointment_ids"][0])["status"] == "PENDING_CONFIRMATION"
    mounted.simulate(result["offer_token"], "YES")
    mounted.drain()
    retried = json.loads(request(path, body)[1])
    assert retried["status"] == "CONFIRMED"
    assert retried["offer_token"] == result["offer_token"]
    assert len(mounted.state(target)["offers"]) == len(current["offers"])
    assert request(path, {**body, "patientId": body["patientId"] - 1})[0] == 409


@pytest.mark.parametrize("missing", ["recipient"])
def test_drawer_message_failure_rolls_back_all_reservation_changes(mounted, missing):
    from database.models import AdminBookingDecision, AdminSlotReservation, AuditLog
    from flow_test.models import FlowOffer, FlowJob

    target, path, body = replacement_offer_request(mounted)
    before = mounted.state(target)
    with mounted.sessions() as session:
        counts = [len(list(session.scalars(select(model)))) for model in
                  (AdminBookingDecision, AdminSlotReservation, AuditLog, FlowOffer, FlowJob)]
    mounted.live = True
    mounted.message_mode = "session"
    mounted.recipients = {} if missing == "recipient" else {"TEST-D": "919999999991"}
    status, raw = request(path, body)
    assert status == 409
    assert (b"parent phone number" if missing == "recipient" else b"START") in raw
    after = mounted.state(target)
    assert after["appointments"] == before["appointments"]
    with mounted.sessions() as session:
        assert counts == [len(list(session.scalars(select(model)))) for model in
                          (AdminBookingDecision, AdminSlotReservation, AuditLog, FlowOffer, FlowJob)]
    # The same action may be retried safely after configuration is corrected.
    mounted.live = False
    assert request(path, body)[0] == 200


def test_drawer_offer_decline_releases_pending_replacement(mounted):
    target, path, body = replacement_offer_request(mounted)
    result = json.loads(request(path, body)[1])
    mounted.drain()
    mounted.simulate(result["offer_token"], "NO")
    mounted.drain()
    assert next(a for a in mounted.state(target)["appointments"]
                if a["id"] == result["appointment_ids"][0])["status"] == "NEEDS_RESCHEDULING"
    assert json.loads(request(path, body)[1])["offer_state"] == "DECLINED"


def test_drawer_offer_rejects_stale_preview_and_invalid_input(mounted):
    from database.models import AdminBookingDecision

    target, path, body = replacement_offer_request(mounted)
    assert request(path, {**body, "wholeVisit": "false"})[0] == 400
    assert request(path, {**body, "patientId": True})[0] == 400
    assert request(path, {**body, "previewToken": "stale"})[0] == 409
    with mounted.sessions() as session:
        assert not list(session.scalars(select(AdminBookingDecision)))
    assert request(path, body, token="wrong")[0] == 401


def test_drawer_complete_visit_queues_one_offer_for_reused_and_new_rows(mounted):
    target = date(2026, 10, 19)
    mounted.generate(target)
    mounted.publish(target)
    mounted.drain()
    token = next(o["token"] for o in mounted.state(target)["offers"] if o["patient"] == "TEST-A")
    mounted.simulate(token, "DAY")
    mounted.drain()
    with mounted.sessions.begin() as session:
        source = session.scalar(select(Appointment).join(Patient).where(
            Patient.patient_code == "TEST-A", Appointment.department_id == 1))
        patient = session.scalar(select(Patient).where(Patient.patient_code == "TEST-D"))
        # Create a solver-style tentative row in one of the released sessions.
        requirement = session.scalar(select(TreatmentRequirement).where(
            TreatmentRequirement.patient_id == patient.id))
        row = Appointment(patient_id=patient.id, treatment_requirement_id=requirement.id,
                          therapist_id=source.therapist_id, department_id=source.department_id,
                          location_id=source.location_id, date=target, slot_index=source.slot_index,
                          status=AppointmentStatus.PROPOSED, bundle_id="solver-original", source="flow_test")
        session.add(row)
        session.flush()
        source_id, patient_id, reused_id = source.id, patient.id, row.id
    path = f"/test/api/appointments/{source_id}"
    status, raw = request(path + "/reservation-preview", {"patientId": patient_id, "wholeVisit": True})
    assert status == 200
    preview = json.loads(raw)
    assert any(s["reuse_id"] == reused_id for s in preview["slots"])
    assert preview["warnings"]  # The second therapy is deliberately off-plan.
    body = {"patientId": patient_id, "wholeVisit": True, "requestId": "whole-message",
            "mode": "with_messaging", "previewToken": preview["preview_token"]}
    assert request(path + "/reserve", body)[0] == 409
    status, raw = request(path + "/reserve", {**body, "overrideWarnings": True})
    assert status == 200, raw
    result = json.loads(raw)
    assert len(result["appointment_ids"]) == 2 and reused_id in result["appointment_ids"]
    current = mounted.state(target)
    offers = [o for o in current["offers"] if o["token"] == result["offer_token"]]
    assert len(offers) == 1
    assert all(a["status"] == "PENDING_CONFIRMATION" for a in current["appointments"]
               if a["id"] in result["appointment_ids"])
    mounted.drain()
    mounted.simulate(result["offer_token"], "YES")
    mounted.drain()
    assert all(a["status"] == "CONFIRMED" for a in mounted.state(target)["appointments"]
               if a["id"] in result["appointment_ids"])


def test_drawer_live_offer_only_queues_without_sending_during_request(mounted, monkeypatch):
    target, path, body = replacement_offer_request(mounted)
    mounted.live = True
    mounted.recipients = {"TEST-D": "919999999991"}
    monkeypatch.setattr(mounted, "_session_deadline", lambda *_args: float("inf"))
    monkeypatch.setattr(mounted, "send", lambda *_args: pytest.fail("HTTP request sent a message"))
    status, raw = request(path, body)
    assert status == 200, raw
    assert json.loads(raw)["mode"] == "live"
    offer = next(o for o in mounted.state(target)["offers"] if o["patient"] == "TEST-D")
    assert offer["send_status"] == "QUEUED" and offer["state"] == "PENDING"


def test_mounted_contacts_can_come_from_azure_secret_without_file(tmp_path, monkeypatch):
    import base64
    from flow_test import mount as module
    from flow_test.service import load_recipient_names, load_recipients

    monkeypatch.setattr(module, "ROOT", tmp_path)
    monkeypatch.setenv("FLOW_TEST_LIVE", "false")
    monkeypatch.setenv("FLOW_TEST_ADMIN_TOKEN", "test-key")
    contacts = {"TEST-A": {"phone": "919999999991", "name": "Coworker", "consent": True}}
    monkeypatch.setenv("FLOW_TEST_RECIPIENTS_B64", base64.b64encode(json.dumps(contacts).encode()).decode())
    def fake_flow(*_args, **kwargs):
        assert kwargs["recipients"] == {"TEST-A": "919999999991"}
        assert kwargs["recipient_names"] == {"TEST-A": "Coworker"}
        return SimpleNamespace()
    monkeypatch.setattr(module, "FlowTest", fake_flow)
    instance = module.MountedFlow()
    assert instance.token == "test-key" and instance.worker is None
