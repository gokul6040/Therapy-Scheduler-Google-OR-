"""Test the isolated daily flow with a fake transport, never real recipients."""

from datetime import date, datetime, timedelta
import hashlib
import hmac
from io import BytesIO
import json
import time

import pytest
from sqlalchemy import create_engine, select

from config import DATABASE_URL
from database.models import Appointment, AppointmentStatus, Patient
from flow_test.models import FlowContactWindow, FlowInbox, FlowJob, FlowOffer
from flow_test.service import (
    FlowTest, TestTransport as Transport, format_offer_message,
    load_recipient_names, load_recipients,
)
from flow_test.web_app import WEBHOOK_PATH, handler_for


MONDAY = date(2026, 10, 19)


@pytest.fixture
def flow(tmp_path, legacy_flow_scenario):
    app = FlowTest(f"sqlite:///{(tmp_path / 'test.sqlite').as_posix()}", scenario=legacy_flow_scenario)
    yield app
    app.engine.dispose()


def publish(flow, target=MONDAY):
    flow.generate(target)
    flow.publish(target)
    flow.drain()
    return {o["patient"]: o["token"] for o in flow.state(target)["offers"]}


def reply(flow, token, action):
    flow.simulate(token, action)
    flow.drain()


def webhook(message, phone_id="123456789"):
    return {"entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": phone_id}, "messages": [message],
    }}]}]}


def incoming(token, action="YES", sender="919999999991", event="message-1"):
    return {"id": event, "from": sender, "timestamp": str(int(time.time())),
            "button": {"payload": f"ft:{token}:{action}"}}


def test_daily_draft_is_single_day_continuous_and_idempotent(flow):
    assert flow.generate(MONDAY)["added"] == 4
    state = flow.state(MONDAY)
    assert {a["date"] for a in state["appointments"]} == {MONDAY.isoformat()}
    a = [a for a in state["appointments"] if a["patient"] == "TEST-A"]
    assert [(x["therapy"], x["time"]) for x in a] == [("Speech", "09:00–09:40"), ("OT", "09:40–10:20")]
    assert flow.generate(MONDAY)["added"] == 0
    assert not flow.state(MONDAY)["jobs"]


def test_sunday_and_unknown_roster_rejected(flow):
    with pytest.raises(ValueError, match="Monday"):
        flow.generate(date(2026, 10, 25))
    with pytest.raises(ValueError, match="fictional"):
        flow.generate(MONDAY, ["REAL-PATIENT"])


def test_publish_once_per_visit_without_network(flow):
    flow.generate(MONDAY)
    assert flow.publish(MONDAY) == 3
    assert flow.publish(MONDAY) == 0
    flow.drain()
    state = flow.state(MONDAY)
    assert len(state["offers"]) == 3
    assert {j["status"] for j in state["jobs"]} == {"SIMULATED"}
    assert all(a["status"] == "PENDING_CONFIRMATION" for a in state["appointments"])


def test_yes_confirms_whole_visit_without_moving_others(flow):
    tokens = publish(flow)
    before = flow.state(MONDAY)["appointments"]
    reply(flow, tokens["TEST-A"], "YES")
    after = flow.state(MONDAY)["appointments"]
    assert all(a["status"] == "CONFIRMED" for a in after if a["patient"] == "TEST-A")
    assert [a for a in before if a["patient"] != "TEST-A"] == [a for a in after if a["patient"] != "TEST-A"]
    assert {j["status"] for j in flow.state(MONDAY)["jobs"]} <= {"DONE", "SIMULATED"}


def test_no_immediately_declines_whole_day_and_proposes_standby(flow):
    tokens = publish(flow)
    reply(flow, tokens["TEST-B"], "NO")
    state = flow.state(MONDAY)
    assert next(a for a in state["appointments"] if a["patient"] == "TEST-B")["status"] == "NEEDS_RESCHEDULING"
    standby = next(a for a in state["appointments"] if a["patient"] == "TEST-D")
    assert standby["status"] == "PROPOSED"
    assert len(state["offers"]) == 3  # replacement does not auto-message
    assert flow.publish(MONDAY, standby["id"]) == 1
    flow.drain()
    d_token = next(o["token"] for o in flow.state(MONDAY)["offers"] if o["patient"] == "TEST-D")
    reply(flow, d_token, "YES")
    flow.attendance(standby["id"], "COMPLETED")
    d_progress = next(p for p in flow.state(MONDAY)["progress"] if p["patient"] == "TEST-D")
    assert (d_progress["completed"], d_progress["booked"], d_progress["remaining"]) == (1, 0, 1)


def test_help_and_reschedule_create_inbox_without_changing_booking(flow):
    tokens = publish(flow)
    before = flow.state(MONDAY)["appointments"]
    reply(flow, tokens["TEST-A"], "HELP")
    reply(flow, tokens["TEST-A"], "RESCHEDULE")
    assert flow.state(MONDAY)["appointments"] == before
    assert len(flow.state(MONDAY)["inbox"]) == 2
    flow.resolve(flow.state(MONDAY)["inbox"][0]["id"])
    assert flow.state(MONDAY)["inbox"][0]["resolved"]


def test_duplicate_webhook_changes_once_and_wrong_sender_cannot_confirm(flow, monkeypatch):
    tokens = publish(flow)
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123456789")
    flow.recipients = {"TEST-A": "919999999991", "TEST-B": "919999999992"}
    with flow.sessions.begin() as session:
        session.get(FlowOffer, tokens["TEST-A"]).recipient = "919999999991"
    event = webhook(incoming(tokens["TEST-A"]))
    flow.receive(event)
    flow.receive(event)
    flow.drain()
    assert all(a["status"] == "CONFIRMED" for a in flow.state(MONDAY)["appointments"] if a["patient"] == "TEST-A")
    with flow.sessions() as session:
        assert len(list(session.scalars(select(FlowJob).where(FlowJob.kind == "INBOUND")))) == 1
    flow.receive(webhook(incoming(tokens["TEST-B"], event="wrong-person")))
    flow.drain()
    assert next(a for a in flow.state(MONDAY)["appointments"] if a["patient"] == "TEST-B")["status"] == "PENDING_CONFIRMATION"
    assert "Unmatched" in flow.state(MONDAY)["inbox"][0]["reason"]


def test_unknown_recipient_and_wrong_business_number_are_ignored(flow, monkeypatch):
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123456789")
    flow.recipients = {"TEST-A": "919999999991"}
    assert flow.receive(webhook(incoming("a" * 20, sender="918888888888"))) == 0
    assert flow.receive(webhook(incoming("a" * 20), phone_id="different")) == 0
    assert not flow.state(MONDAY)["jobs"]


def test_changed_offer_and_old_replies_cannot_modify_schedule(flow):
    tokens = publish(flow)
    with flow.sessions.begin() as session:
        offer = session.get(FlowOffer, tokens["TEST-A"])
        item = session.get(Appointment, json.loads(offer.appointment_ids)[0])
        item.slot_index = 2
    reply(flow, tokens["TEST-A"], "YES")
    assert "Stale" in flow.state(MONDAY)["inbox"][0]["reason"]
    assert all(a["status"] == "PENDING_CONFIRMATION" for a in flow.state(MONDAY)["appointments"])


def test_expiry_keeps_booking_and_rejects_late_acceptance(flow):
    tokens = publish(flow)
    with flow.sessions.begin() as session:
        session.get(FlowOffer, tokens["TEST-B"]).expires_at = datetime.utcnow() - timedelta(seconds=1)
    assert flow.expire_offers() == 1
    reply(flow, tokens["TEST-B"], "YES")
    assert next(a for a in flow.state(MONDAY)["appointments"] if a["patient"] == "TEST-B")["status"] == "PENDING_CONFIRMATION"
    flow.release_offer(tokens["TEST-B"])
    assert next(a for a in flow.state(MONDAY)["appointments"] if a["patient"] == "TEST-B")["status"] == "NEEDS_RESCHEDULING"


def test_attendance_requires_confirmation_and_no_show_restores_demand(flow):
    tokens = publish(flow)
    item = next(a for a in flow.state(MONDAY)["appointments"] if a["patient"] == "TEST-B")
    with pytest.raises(ValueError, match="confirmed"):
        flow.attendance(item["id"], "COMPLETED")
    reply(flow, tokens["TEST-B"], "YES")
    flow.attendance(item["id"], "NO_SHOW")
    progress = next(p for p in flow.state(MONDAY)["progress"] if p["patient"] == "TEST-B")
    assert (progress["completed"], progress["booked"], progress["remaining"]) == (0, 0, 2)


def test_tuesday_uses_weekly_progress_and_keeps_monday_fixed(flow):
    tokens = publish(flow)
    reply(flow, tokens["TEST-A"], "YES")
    for a in flow.state(MONDAY)["appointments"]:
        if a["patient"] == "TEST-A":
            flow.attendance(a["id"], "COMPLETED")
    before = flow.state(MONDAY)["appointments"]
    assert flow.generate(MONDAY + timedelta(days=1))["added"] > 0
    assert flow.state(MONDAY)["appointments"] == before
    a_ot = next(p for p in flow.state(MONDAY)["progress"] if p["patient"] == "TEST-A" and p["therapy"] == "OT")
    assert a_ot["completed"] == 1
    assert a_ot["booked"] == 1


def test_pending_jobs_survive_restart_and_uncertain_sends_are_not_retried(flow):
    flow.generate(MONDAY)
    flow.publish(MONDAY)
    with flow.sessions.begin() as session:
        first = session.scalar(select(FlowJob).order_by(FlowJob.id))
        first.status = "PROCESSING"
    restored = FlowTest(str(flow.engine.url), scenario=flow.scenario)
    try:
        assert restored.drain() == 2
        assert sum(j["status"] == "UNKNOWN" for j in restored.state(MONDAY)["jobs"]) == 1
    finally:
        restored.engine.dispose()


def test_transport_failure_has_no_automatic_resend(flow):
    calls = []

    def failure(kind, payload):
        calls.append(kind)
        raise TimeoutError("acceptance uncertain")

    flow.send = failure
    flow.generate(MONDAY)
    flow.publish(MONDAY)
    flow.drain()
    assert len(calls) == 3
    assert {j["status"] for j in flow.state(MONDAY)["jobs"]} == {"UNKNOWN"}
    assert flow.drain() == 0


def test_released_unsent_offer_is_suppressed(flow):
    flow.generate(MONDAY)
    flow.publish(MONDAY)
    token = flow.state(MONDAY)["offers"][0]["token"]
    flow.release_offer(token)
    flow.drain()
    assert sum(j["status"] == "OBSOLETE" for j in flow.state(MONDAY)["jobs"]) == 1


def test_response_failure_rolls_back_appointment_and_ack(flow, monkeypatch):
    tokens = publish(flow)
    before = flow.state(MONDAY)["appointments"]

    def failure(*args, **kwargs):
        raise RuntimeError("solver failed")

    monkeypatch.setattr("flow_test.service.process_daily_responses", failure)
    reply(flow, tokens["TEST-B"], "DAY")
    assert flow.state(MONDAY)["appointments"] == before
    assert flow.state(MONDAY)["jobs"][0]["status"] == "FAILED"


def test_recipient_file_requires_explicit_consent(tmp_path):
    path = tmp_path / "recipients.json"
    path.write_text(json.dumps({"TEST-A": {"phone": "+91 99999 99999", "consent": False}}))
    with pytest.raises(ValueError, match="consent"):
        load_recipients(path)
    path.write_text(json.dumps({"TEST-A": {"phone": "+91 99999 99999", "consent": True}}))
    assert load_recipients(path) == {"TEST-A": "919999999999"}


def test_offer_uses_bound_name_and_all_sessions_of_the_correct_visit(flow):
    flow.recipient_names = {"TEST-A": "Asha", "TEST-B": "Maya"}
    flow.generate(MONDAY)
    flow.publish(MONDAY)
    with flow.sessions() as session:
        offers = [json.loads(job.payload) for job in session.scalars(
            select(FlowJob).where(FlowJob.kind == "OFFER")
        )]
    a = next(o for o in offers if o["fields"]["greeting_name"] == "Asha")
    text = format_offer_message(a["fields"])
    assert "Hi Asha," in text
    assert "Speech and OT scheduled on Monday, 19 October 2026" in text
    assert "Speech: 9:00 AM–9:40 AM IST" in text
    assert "OT: 9:40 AM–10:20 AM IST" in text
    assert a["token"] in text
    assert len(offers) == 3
    b = next(o for o in offers if o["fields"]["greeting_name"] == "Maya")
    assert b["fields"]["therapy"] == "Speech"


def test_optional_greeting_name_is_not_inferred_from_phone(tmp_path):
    path = tmp_path / "recipients.json"
    path.write_text(json.dumps({"TEST-A": {"name": " Asha ", "phone": "", "consent": False},
                                "TEST-B": {"phone": "", "consent": False}}))
    assert load_recipient_names(path) == {"TEST-A": "Asha"}


def test_transport_registered_contact_check_and_preview_never_calls_meta(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Preview must not access Meta")

    monkeypatch.setattr("flow_test.service.send_template_message", forbidden)
    transport = Transport(False, {})
    assert transport.send("OFFER", {"recipient": "preview:TEST-A"}).startswith("preview-")
    transport.live = True
    with pytest.raises(ValueError, match="registered test contact"):
        transport.send("OFFER", {"recipient": "919999999999"})


def test_live_simulation_and_unmapped_publish_are_rejected(flow):
    flow.generate(MONDAY)
    flow.live = True
    with pytest.raises(ValueError, match="parent phone number"):
        flow.publish(MONDAY)
    assert not flow.state(MONDAY)["jobs"]
    with pytest.raises(ValueError, match="preview"):
        flow.simulate("a" * 20, "YES")


def test_main_and_unmarked_databases_are_rejected(tmp_path):
    with pytest.raises(ValueError, match="main application"):
        FlowTest(DATABASE_URL)
    with pytest.raises(ValueError, match="_flow_test"):
        FlowTest("postgresql+psycopg://unused:unused@localhost/main")
    url = f"sqlite:///{(tmp_path / 'existing.sqlite').as_posix()}"
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE existing_data (id INTEGER)")
    engine.dispose()
    with pytest.raises(ValueError, match="marker"):
        FlowTest(url)


def test_live_and_preview_cannot_share_database(flow):
    with pytest.raises(ValueError, match="separate"):
        FlowTest(str(flow.engine.url), live=True, send=lambda *a: "fake")


def http_call(flow, path, *, body=None, headers=None):
    """Exercise the real HTTP parser/handler without network permissions."""
    class Connection:
        def __init__(self, request):
            self.input = BytesIO(request)
            self.output = BytesIO()

        def makefile(self, *args, **kwargs):
            return self.input

        def sendall(self, value):
            self.output.write(value)

    method = "POST" if body is not None else "GET"
    headers = dict(headers or {})
    raw = body or b""
    if body is not None:
        headers["Content-Length"] = str(len(raw))
    head = f"{method} {path} HTTP/1.1\r\nHost: localhost\r\n" + "".join(f"{k}: {v}\r\n" for k, v in headers.items())
    connection = Connection(head.encode() + b"\r\n" + raw)
    handler_for(flow, "test-admin-token")(connection, ("127.0.0.1", 1234), object())
    head, content = connection.output.getvalue().split(b"\r\n\r\n", 1)
    return int(head.split(b" ")[1]), content


def test_admin_endpoints_require_token_and_static_files_do_not_expose_data(flow):
    assert http_call(flow, "/api/state?date=2026-10-19")[0] == 401
    assert b"Daily flow test" in http_call(flow, "/")[1]
    assert http_call(flow, "/recipients.local.json")[0] == 404
    status, content = http_call(flow, "/api/state?date=2026-10-19", headers={"Authorization": "Bearer test-admin-token"})
    assert status == 200
    assert json.loads(content)["appointments"] == []


def test_signed_webhook_verification_and_durable_receipt(flow, monkeypatch):
    monkeypatch.setenv("WHATSAPP_VERIFY_TOKEN", "verify-test")
    monkeypatch.delenv("FLOW_TEST_VERIFY_TOKEN", raising=False)
    monkeypatch.setenv("WHATSAPP_APP_SECRET", "test-secret")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123456789")
    flow.recipients = {"TEST-A": "919999999991"}
    flow.live = True
    assert http_call(flow, WEBHOOK_PATH + "?hub.mode=subscribe&hub.verify_token=verify-test&hub.challenge=challenge") == (200, b"challenge")
    raw = json.dumps(webhook(incoming("a" * 20))).encode()
    assert http_call(flow, WEBHOOK_PATH, body=raw)[0] == 401
    signature = hmac.new(b"test-secret", raw, hashlib.sha256).hexdigest()
    assert http_call(flow, WEBHOOK_PATH, body=raw, headers={"X-Hub-Signature-256": "sha256=" + signature})[0] == 200
    assert flow.state(MONDAY)["jobs"][0]["status"] == "QUEUED"


def test_delivery_status_does_not_confirm_appointments(flow, monkeypatch):
    tokens = publish(flow)
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123456789")
    flow.recipients = {"TEST-A": "919999999991"}
    with flow.sessions.begin() as session:
        original = session.scalar(select(FlowJob).where(FlowJob.kind == "OFFER"))
        body = json.loads(original.payload)
        body["recipient"] = "919999999991"
        original.payload = json.dumps(body)
        mid = original.message_id
    value = {"metadata": {"phone_number_id": "123456789"}, "statuses": [
        {"id": mid, "recipient_id": "919999999991", "status": "read", "timestamp": str(int(time.time()))},
    ]}
    flow.receive({"entry": [{"changes": [{"value": value}]}]})
    flow.drain()
    assert all(a["status"] == "PENDING_CONFIRMATION" for a in flow.state(MONDAY)["appointments"])
    assert any(j["delivery"] == "read" for j in flow.state(MONDAY)["jobs"])
    assert any(o.get("delivery") == "read" for o in flow.state(MONDAY)["offers"])
    assert all(o["state"] == "PENDING" for o in flow.state(MONDAY)["offers"])


@pytest.fixture
def live_flow(tmp_path, monkeypatch, legacy_flow_scenario):
    monkeypatch.setenv("FLOW_TEST_MESSAGE_MODE", "session")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123456789")
    calls = []

    def send(kind, payload):
        calls.append((kind, payload))
        return f"provider-{len(calls)}"

    app = FlowTest(
        f"sqlite:///{(tmp_path / 'live.sqlite').as_posix()}", live=True,
        recipients={"TEST-A": "919999999991", "TEST-B": "919999999992",
                    "TEST-C": "919999999993", "TEST-D": "919999999994"},
        send=send, scenario=legacy_flow_scenario,
    )
    app.test_calls = calls
    yield app
    app.engine.dispose()


def start_contact(flow, code, *, timestamp=None, event=None):
    phone = flow.recipients[code]
    flow.receive(webhook({
        "id": event or f"start-{code}-{time.time_ns()}", "from": phone,
        "timestamp": str(int(time.time()) if timestamp is None else timestamp),
        "type": "text", "text": {"body": "START"},
    }))


def future_monday():
    today = date.today()
    return today + timedelta(days=7 - today.weekday())


def test_session_mode_queues_waiting_offers_until_coworker_messages(live_flow):
    target = future_monday()
    live_flow.generate(target)
    assert live_flow.publish(target) == 3
    assert not live_flow.test_calls
    assert len(live_flow.state(target)["offers"]) == 3
    assert all(j["status"] == "WAITING_FOR_CONTACT" for j in live_flow.state(target)["jobs"])


def test_start_opens_window_then_session_offers_send_without_template(live_flow):
    target = future_monday()
    for code in ("TEST-A", "TEST-B", "TEST-C"):
        start_contact(live_flow, code)
    live_flow.drain()
    assert live_flow.state(target)["windows"]["TEST-A"]["open"]
    assert not live_flow.state(target)["inbox"]
    live_flow.generate(target)
    assert live_flow.publish(target) == 3
    live_flow.drain()
    assert len(live_flow.test_calls) == 3
    assert all(kind == "OFFER" and body["session_expires_at"] > time.time()
               for kind, body in live_flow.test_calls)


def test_old_callback_cannot_reopen_window_or_cross_business_numbers(live_flow, monkeypatch):
    target = future_monday()
    start_contact(live_flow, "TEST-A", timestamp=int(time.time()) - 90000)
    assert not live_flow.state(target)["windows"]["TEST-A"]["open"]
    start_contact(live_flow, "TEST-A", event="fresh-start")
    assert live_flow.state(target)["windows"]["TEST-A"]["open"]
    with live_flow.sessions.begin() as session:
        window = session.get(FlowContactWindow, (live_flow.recipients["TEST-A"], "123456789"))
        last_inbound = window.last_inbound
    start_contact(live_flow, "TEST-A", timestamp=int(time.time()) - 90000)
    with live_flow.sessions() as session:
        assert session.get(FlowContactWindow, (live_flow.recipients["TEST-A"], "123456789")).last_inbound == last_inbound
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "987654321")
    assert not live_flow.state(target)["windows"]["TEST-A"]["open"]


def test_optional_template_mode_keeps_existing_proactive_path(monkeypatch):
    monkeypatch.setenv("FLOW_TEST_MESSAGE_MODE", "template")
    monkeypatch.setenv("FLOW_TEST_TEMPLATE_NAME", "approved_test_offer")
    monkeypatch.setenv("FLOW_TEST_TEMPLATE_PARAMETERS", '["{patient}", "{visit_details}"]')
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "fake-access-token")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123456789")
    captured = {}

    def send(settings, recipient, template, language, values, buttons):
        captured.update(template=template, values=values, buttons=buttons)
        return {"messages": [{"id": "template-message"}]}

    monkeypatch.setattr("flow_test.service.send_template_message", send)
    transport = Transport(True, {"TEST-A": "919999999991"})
    assert transport.send("OFFER", {
        "recipient": "919999999991", "token": "a" * 20,
        "fields": {"patient": "TEST Child A", "visit_details": "Fictional visit"},
    }) == "template-message"
    assert captured["template"] == "approved_test_offer"
    assert len(captured["buttons"]) == 3


def test_window_is_rechecked_at_send_and_unsent_jobs_resume_on_start(live_flow):
    target = future_monday()
    start_contact(live_flow, "TEST-A")
    live_flow.drain()
    live_flow.generate(target, ["TEST-A"])
    live_flow.publish(target)
    with live_flow.sessions.begin() as session:
        session.get(FlowContactWindow, (live_flow.recipients["TEST-A"], "123456789")).last_inbound = datetime.utcnow() - timedelta(hours=25)
    live_flow.drain()
    assert not live_flow.test_calls
    assert any(j["status"] == "WAITING_FOR_CONTACT" for j in live_flow.state(target)["jobs"])
    start_contact(live_flow, "TEST-A")
    live_flow.drain()
    assert len(live_flow.test_calls) == 1


def test_delivery_receipt_cannot_open_a_session(live_flow):
    value = {"metadata": {"phone_number_id": "123456789"}, "statuses": [
        {"id": "some-message", "recipient_id": live_flow.recipients["TEST-A"],
         "status": "read", "timestamp": str(int(time.time()))},
    ]}
    live_flow.receive({"entry": [{"changes": [{"value": value}]}]})
    live_flow.drain()
    assert not live_flow.state(future_monday())["windows"]["TEST-A"]["open"]


def test_session_transport_sends_interactive_buttons_without_template(monkeypatch):
    monkeypatch.setenv("FLOW_TEST_MESSAGE_MODE", "session")
    monkeypatch.delenv("FLOW_TEST_TEMPLATE_NAME", raising=False)
    monkeypatch.setenv("WHATSAPP_ACCESS_TOKEN", "fake-access-token")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123456789")
    captured = {}

    def send(settings, recipient, text, buttons):
        captured.update(recipient=recipient, text=text, buttons=buttons)
        return {"messages": [{"id": "interactive-message"}]}

    monkeypatch.setattr("flow_test.service.send_reply_buttons", send)
    transport = Transport(True, {"TEST-A": "919999999991"})
    payload = {"recipient": "919999999991", "token": "a" * 20,
               "fields": {"patient": "TEST Child A", "centre": "TEST Centre", "visit_details": "Fictional visit"},
               "session_expires_at": time.time() + 3600}
    assert transport.send("OFFER", payload) == "interactive-message"
    assert [title for _, title in captured["buttons"]] == ["Yes", "Cannot attend", "Help"]
    payload["session_expires_at"] = time.time() - 1
    with pytest.raises(ValueError, match="24-hour"):
        transport.send("OFFER", payload)


def test_reply_button_transport_payload_is_validated(monkeypatch):
    from services.whatsapp_service import (
        WhatsAppConfigurationError, WhatsAppSenderSettings, send_reply_buttons,
    )

    settings = WhatsAppSenderSettings("fake-token", "123456789", "v26.0")
    captured = {}

    def send(_settings, payload):
        captured.update(payload)
        return {"messages": [{"id": "interactive-message"}]}

    monkeypatch.setattr("services.whatsapp_service._send_message", send)
    send_reply_buttons(settings, "919999999991", "TEST ONLY", (("yes-a", "Yes"), ("no-a", "Cannot attend")))
    assert captured["type"] == "interactive"
    assert captured["interactive"]["action"]["buttons"][0]["reply"] == {"id": "yes-a", "title": "Yes"}
    with pytest.raises(WhatsAppConfigurationError):
        send_reply_buttons(settings, "919999999991", "TEST ONLY", ())
    with pytest.raises(WhatsAppConfigurationError):
        send_reply_buttons(settings, "919999999991", "TEST ONLY", (("same", "Yes"), ("same", "No")))


def test_deployed_recipient_secret_works_without_local_file(tmp_path, monkeypatch):
    import base64

    path = tmp_path / "missing-recipients.json"
    contacts = {"TEST-A": {"phone": "+91 9999999991", "name": "Test coworker", "consent": True}}
    def configure(data):
        encoded = base64.b64encode(json.dumps(data).encode()).decode()
        monkeypatch.setenv("FLOW_TEST_RECIPIENTS_B64", encoded)
    configure(contacts)
    assert load_recipients(path) == {"TEST-A": "919999999991"}
    assert load_recipient_names(path) == {"TEST-A": "Test coworker"}
    path.write_text('{"TEST-B":{"phone":"919999999992","consent":true}}')
    assert set(load_recipients(path)) == {"TEST-A"}
    contacts["TEST-A"]["consent"] = False
    configure(contacts)
    with pytest.raises(ValueError, match="consent"):
        load_recipients(path)
    monkeypatch.setenv("FLOW_TEST_RECIPIENTS_B64", "not!base64")
    with pytest.raises(ValueError):
        load_recipients(path)
    monkeypatch.delenv("FLOW_TEST_RECIPIENTS_B64")
    assert set(load_recipients(path)) == {"TEST-B"}


def test_no_button_declines_all_same_day_bundles_once_without_scope_question(flow, monkeypatch):
    from database.models import ParentAvailabilityResponse, PatientAbsence

    flow.generate(MONDAY)
    with flow.sessions.begin() as session:
        appointments = list(session.scalars(select(Appointment).order_by(Appointment.id)))
        child_a = [a for a in appointments if session.get(Patient, a.patient_id).patient_code == "TEST-A"]
        # Manual work can create separate visits: a day decline covers both.
        child_a[1].bundle_id = "A-second-visit"
        child_a[1].status = AppointmentStatus.CONFIRMED
        extra = Appointment(patient_id=child_a[0].patient_id,
                            treatment_requirement_id=child_a[0].treatment_requirement_id,
                            therapist_id=child_a[0].therapist_id, department_id=child_a[0].department_id,
                            location_id=child_a[0].location_id, date=MONDAY+timedelta(days=1),
                            slot_index=0, status=AppointmentStatus.CONFIRMED, bundle_id="next-day")
        session.add(extra)
        session.flush()
        a_ids = {a.id for a in child_a}
        next_day_id = extra.id
    flow.publish(MONDAY)
    flow.drain()
    token = next(o["token"] for o in flow.state(MONDAY)["offers"] if o["patient"] == "TEST-A")
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123456789")
    flow.recipients = {"TEST-A": "919999999991"}
    with flow.sessions.begin() as session:
        session.get(FlowOffer, token).recipient = "919999999991"
    before = flow.state(MONDAY)["appointments"]
    event = webhook(incoming(token, action="NO", event="decline-day-event"))
    assert flow.receive(event) == 1
    flow.receive(event)
    flow.drain()
    current = flow.state(MONDAY)
    assert sum(j["kind"] == "INBOUND" for j in current["jobs"]) == 1
    assert {a["status"] for a in current["appointments"] if a["id"] in a_ids} == {"NEEDS_RESCHEDULING"}
    for old in before:
        if old["id"] not in a_ids:
            assert old in current["appointments"]
    with flow.sessions() as session:
        assert session.get(Appointment, next_day_id).status == AppointmentStatus.CONFIRMED
        responses = list(session.scalars(select(ParentAvailabilityResponse)))
        assert len(responses) == 2 and all(r.unavailable_scope == "day" and r.processed_at for r in responses)
        assert all(a.full_day for a in session.scalars(select(PatientAbsence)))
        texts = [json.loads(j.payload)["text"] for j in session.scalars(select(FlowJob).where(FlowJob.kind == "TEXT"))]
        assert len(texts) == 1 and "whole day" in texts[0]
        assert not any("Reply VISIT" in text or "until you choose" in text for text in texts)
    assert next(o for o in current["offers"] if o["token"] == token)["state"] == "DECLINED"
    assert not any(j["status"] == "FAILED" for j in current["jobs"])
    reply(flow, token, "YES")
    assert {a["status"] for a in flow.state(MONDAY)["appointments"] if a["id"] in a_ids} == {"NEEDS_RESCHEDULING"}


def test_whole_day_no_closes_other_open_offers_for_same_child_only(flow):
    flow.generate(MONDAY)
    with flow.sessions.begin() as session:
        a_rows = list(session.scalars(select(Appointment).where(Appointment.patient_id == 1)))
        a_rows[1].bundle_id = "A-separate-offer"
    flow.publish(MONDAY)
    flow.drain()
    offers = flow.state(MONDAY)["offers"]
    a_offers = [o for o in offers if o["patient"] == "TEST-A"]
    assert len(a_offers) == 2
    reply(flow, a_offers[0]["token"], "NO")
    after = flow.state(MONDAY)
    assert {o["state"] for o in after["offers"] if o["patient"] == "TEST-A"} == {"DECLINED"}
    assert all(o["state"] == "PENDING" for o in after["offers"] if o["patient"] != "TEST-A")


def test_no_preserves_completed_therapy_and_routes_conflict_to_staff(flow):
    flow.generate(MONDAY)
    with flow.sessions.begin() as session:
        a = list(session.scalars(select(Appointment).where(Appointment.patient_id == 1)))
        a[1].bundle_id = "delivered-earlier"
        a[1].status = AppointmentStatus.COMPLETED
    flow.publish(MONDAY)
    flow.drain()
    token = next(o["token"] for o in flow.state(MONDAY)["offers"] if o["patient"] == "TEST-A")
    reply(flow, token, "YES")
    before = flow.state(MONDAY)["appointments"]
    reply(flow, token, "NO")
    state = flow.state(MONDAY)
    assert state["appointments"] == before
    assert any("completed therapy" in i["reason"] for i in state["inbox"])
    assert not any(j["status"] == "FAILED" for j in state["jobs"])
