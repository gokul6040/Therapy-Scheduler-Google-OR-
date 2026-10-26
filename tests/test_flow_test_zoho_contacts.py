"""Source parent phones work through import, transport, callbacks and restart."""

from copy import deepcopy
from datetime import date
import json
from pathlib import Path
from types import SimpleNamespace
import time

import pytest
from flow_test import mount
from flow_test.models import FlowJob, FlowOffer
from flow_test.service import FlowTest
from flow_test.zoho_import import apply_reports

TARGET = date(2026, 10, 23)
START = date(2026, 10, 22)
BUSINESS = "dummy-business"
PARENT = "918888888888"


@pytest.fixture
def source_contacts(tmp_path, monkeypatch):
    reports = json.loads((Path(__file__).parent / "fixtures/zoho_test_reports.json").read_text(encoding="utf-8"))
    reports["Plan_Report"][0]["Parent_No"] = PARENT
    calls = []

    def capture(_sender, recipient, *_args):
        calls.append(recipient)
        return {"messages": [{"id": f"fake-message-{len(calls)}"}]}

    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", BUSINESS)
    monkeypatch.setenv("FLOW_TEST_LIVE", "true")
    monkeypatch.setenv("FLOW_TEST_MESSAGE_MODE", "session")
    monkeypatch.setattr("flow_test.service.sender_settings_from_environment", lambda: object())
    monkeypatch.setattr("flow_test.service.send_reply_buttons", capture)
    monkeypatch.setattr("flow_test.service.send_text_message", capture)
    flow = FlowTest(f"sqlite:///{(tmp_path / 'contacts.sqlite').as_posix()}", live=True, recipients={})
    flow.next_offer_date = lambda: TARGET
    apply_reports(flow, reports, START)
    yield flow, reports, calls
    flow.engine.dispose()


def event(sender, event_id, **message):
    return {"entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": BUSINESS},
        "messages": [{"from": sender, "id": event_id, "timestamp": str(int(time.time())), **message}],
    }}]}]}


def offered(flow):
    flow.receive(event(PARENT, "start", text={"body": "START"}))
    flow.drain()
    flow.generate(TARGET, ["TEST-H"])
    assert flow.publish(TARGET) == 1
    flow.drain()
    return flow.state(TARGET)["offers"][0]


def test_source_number_sends_and_confirms_without_manual_number_configuration(source_contacts):
    flow, _reports, calls = source_contacts
    offer = offered(flow)
    assert calls == [PARENT]
    # Another registered family cannot answer this patient's offer.
    button = {"payload": f"ft:{offer['token']}:YES"}
    flow.receive(event("919999999999", "wrong-family", button=button))
    flow.drain()
    assert flow.state(TARGET)["appointments"][0]["status"] == "PENDING_CONFIRMATION"
    flow.receive(event(PARENT, "yes", button=button))
    flow.drain()
    assert flow.state(TARGET)["appointments"][0]["status"] == "CONFIRMED"
    assert calls[-1] == PARENT
    assert flow.receive(event("917777777777", "unknown", text={"body": "START"})) == 0
    with pytest.raises(ValueError, match="registered test contact"):
        flow.transport.send("OFFER", {"recipient": "917777777777"})


def test_phone_edit_keeps_existing_offer_recipient_and_callback_routing(source_contacts, monkeypatch):
    flow, reports, calls = source_contacts
    offer = offered(flow)
    changed = deepcopy(reports)
    changed["Plan_Report"][0].update(Parent_No="917777777777", Modified_Time="22-Oct-2026 08:00:00")
    apply_reports(flow, changed, START)
    assert flow.recipients["TEST-H"] == "917777777777"
    assert {PARENT, "917777777777"} <= flow.transport.allowed
    with flow.sessions() as session:
        assert session.get(FlowOffer, offer["token"]).recipient == PARENT
    monkeypatch.setattr(mount, "_runtime", SimpleNamespace(flow=flow))
    rest = mount.route_webhook(event(PARENT, "old-offer-yes", button={"payload": f"ft:{offer['token']}:YES"}))
    assert rest["entry"][0]["changes"][0]["value"]["messages"] == []
    flow.drain()
    assert flow.state(TARGET)["appointments"][0]["status"] == "CONFIRMED"
    assert calls[-1] == PARENT
    restarted = FlowTest(str(flow.engine.url), live=True, recipients={})
    try:
        assert restarted.recipients["TEST-H"] == "917777777777"
        assert {PARENT, "917777777777"} <= restarted.transport.allowed
    finally:
        restarted.engine.dispose()


def test_invalid_import_does_not_activate_a_new_contact(source_contacts):
    flow, reports, _calls = source_contacts
    broken = deepcopy(reports)
    broken["Plan_Report"][0].update(Parent_No="917777777777", Schedule="bad schedule")
    with pytest.raises(ValueError, match="needs therapy"):
        apply_reports(flow, broken, START)
    assert flow.recipients["TEST-H"] == PARENT
    assert "917777777777" not in flow.transport.allowed


@pytest.mark.parametrize("old_window_open", [False, True])
def test_phone_edit_redirects_unsent_offer_and_requires_new_contact(source_contacts, old_window_open):
    flow, reports, calls = source_contacts
    if old_window_open:
        flow.receive(event(PARENT, "old-start", text={"body": "START"}))
        flow.drain()
    flow.generate(TARGET, ["TEST-H"])
    assert flow.publish(TARGET) == 1
    offer = flow.state(TARGET)["offers"][0]
    changed = deepcopy(reports)
    new_number = "917777777777"
    changed["Plan_Report"][0].update(Parent_No=new_number, Modified_Time="22-Oct-2026 08:00:00")
    apply_reports(flow, changed, START)
    with flow.sessions() as session:
        assert session.get(FlowOffer, offer["token"]).recipient == new_number
        jobs = session.query(FlowJob).filter_by(kind="OFFER").all()
        assert len(jobs) == 1
        assert json.loads(jobs[0].payload)["recipient"] == new_number
        assert jobs[0].status == "WAITING_FOR_CONTACT"
    flow.drain()
    assert calls == []
    flow.receive(event(new_number, "new-start", text={"body": "START"}))
    flow.drain()
    assert calls == [new_number]


def test_phone_edit_does_not_resend_uncertain_offer(source_contacts):
    flow, reports, calls = source_contacts
    flow.generate(TARGET, ["TEST-H"])
    flow.publish(TARGET)
    offer = flow.state(TARGET)["offers"][0]
    with flow.sessions.begin() as session:
        job = session.query(FlowJob).filter_by(kind="OFFER").one()
        job.status = "UNKNOWN"
    changed = deepcopy(reports)
    changed["Plan_Report"][0].update(Parent_No="917777777777", Modified_Time="22-Oct-2026 08:00:00")
    apply_reports(flow, changed, START)
    with flow.sessions() as session:
        assert session.get(FlowOffer, offer["token"]).recipient == PARENT
        assert session.query(FlowJob).filter_by(kind="OFFER").one().status == "UNKNOWN"
    assert calls == []
