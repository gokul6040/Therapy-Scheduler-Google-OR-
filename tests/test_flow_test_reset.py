"""Explicit fresh-start reset must stay isolated and never send messages."""

from datetime import date, datetime
from io import BytesIO
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from database.models import AuditLog, Patient, PatientAvailability, Therapist, TreatmentRequirement
from flow_test import mount
from flow_test.models import FlowContactWindow, FlowJob, FlowSetting
from flow_test.service import FlowTest
from flow_test.storage import TEST_TABLES
from web_app import TherapySchedulerHandler


DAY = date(2026, 10, 22)


def request(path, body, token="test-key"):
    class Connection:
        def __init__(self, raw):
            self.input, self.output = BytesIO(raw), BytesIO()

        def makefile(self, *_args, **_kwargs):
            return self.input

        def sendall(self, data):
            self.output.write(data)

    raw = json.dumps(body).encode()
    headers = (f"POST {path} HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n"
               f"Authorization: Bearer {token}\r\nContent-Length: {len(raw)}\r\n\r\n")
    connection = Connection(headers.encode() + raw)
    TherapySchedulerHandler(connection, ("127.0.0.1", 1234), object())
    header, content = connection.output.getvalue().split(b"\r\n\r\n", 1)
    return int(header.split()[1]), content


def all_rows(flow):
    with flow.engine.connect() as connection:
        return {t.name: [dict(row) for row in connection.execute(select(t)).mappings()]
                for t in TEST_TABLES}


@pytest.fixture
def flow(tmp_path):
    app = FlowTest(f"sqlite:///{(tmp_path / 'reset.sqlite').as_posix()}")
    app.generate(DAY)
    app.publish(DAY)
    app.drain()
    tokens = {o["patient"]: o["token"] for o in app.state(DAY)["offers"]}
    app.simulate(tokens["TEST-A"], "YES")
    app.simulate(tokens["TEST-B"], "HELP")
    app.drain()
    a = next(a for a in app.state(DAY)["appointments"] if a["patient"] == "TEST-A")
    app.attendance(a["id"], "COMPLETED")
    with app.sessions.begin() as session:
        session.add(FlowContactWindow(recipient="919999999991", business_id="business-1",
                                      last_inbound=datetime.utcnow()))
    yield app
    app.engine.dispose()


def test_reset_clears_history_and_reseeds_only_unrestricted_inputs(flow, monkeypatch):
    assert flow.state(DAY)["offers"] and flow.state(DAY)["inbox"]
    monkeypatch.setattr(flow, "send", lambda *_: pytest.fail("Reset sent a message"))
    result = flow.reset_test_data("RESET preview")
    assert result["patients"] == 8 and result["therapists"] == 2
    assert result["messages_sent"] == 0
    state = flow.state(DAY)
    assert all(not state[key] for key in ("appointments", "offers", "jobs", "inbox"))
    assert all(p["completed"] == p["booked"] == 0 for p in state["progress"])
    assert sum(p["remaining"] for p in state["progress"]) == 21
    with flow.sessions() as session:
        assert not list(session.scalars(select(PatientAvailability)))
        assert not list(session.scalars(select(FlowContactWindow)))
        audits = list(session.scalars(select(AuditLog)))
        assert len(audits) == 1 and audits[0].action == "flow_test_reset"
        assert len(list(session.scalars(select(TreatmentRequirement)))) == 10


def test_reset_rejects_wrong_confirmation_and_unexpected_records(flow):
    before = all_rows(flow)
    with pytest.raises(ValueError, match="exact confirmation"):
        flow.reset_test_data("RESET flow_test")
    assert all_rows(flow) == before
    with flow.sessions.begin() as session:
        session.add(Patient(patient_code="UNEXPECTED", name="Fictional control"))
    before = all_rows(flow)
    with pytest.raises(ValueError, match="Unexpected child"):
        flow.reset_test_data("RESET preview")
    assert all_rows(flow) == before


def test_live_reset_cannot_use_a_preview_or_unqualified_database(flow):
    flow.live = True
    before = all_rows(flow)
    with pytest.raises(ValueError, match="isolated PostgreSQL"):
        flow.reset_test_data("RESET flow_test")
    flow.database_schema = "flow_test"
    with pytest.raises(ValueError, match="isolated PostgreSQL"):
        flow.reset_test_data("RESET flow_test")
    assert all_rows(flow) == before


def test_failed_seed_rolls_back_deletions_and_existing_history(flow, monkeypatch):
    before = all_rows(flow)

    def fail(session):
        session.add(Patient(patient_code="TEST-A"))
        session.flush()
        raise ValueError("Test failed seed")

    monkeypatch.setattr(flow, "_seed", fail)
    with pytest.raises(ValueError, match="failed seed"):
        flow.reset_test_data("RESET preview")
    assert all_rows(flow) == before


def test_reset_api_is_authenticated_and_invalidates_old_previews(flow, monkeypatch):
    instance = SimpleNamespace(flow=flow, token="test-key", previews={"old": object()})
    monkeypatch.setattr(mount, "runtime", lambda: instance)
    body = {"confirmation": "RESET preview"}
    assert request("/test/api/reset", body, token="wrong")[0] == 401
    assert request("/test/api/reset", {"confirmation": "wrong"})[0] == 409
    assert instance.previews
    status, raw = request("/test/api/reset", body)
    assert status == 200 and json.loads(raw)["reset"]
    assert instance.previews == {}
    assert request("/test/api/schedules/00000000-0000-0000-0000-000000000001/save", {})[0] == 404
    # This route cannot exist in the ordinary production namespace.
    assert request("/api/reset", body)[0] == 404


def test_old_callbacks_cannot_repopulate_the_fresh_workspace(flow, monkeypatch):
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "business-1")
    flow.recipients = {"TEST-A": "919999999991"}
    flow.reset_test_data("RESET preview")
    with flow.sessions() as session:
        cutoff = int(session.get(FlowSetting, "reset_cutoff").value)
    message = {"id": "old-start", "from": "919999999991", "timestamp": str(cutoff),
               "text": {"body": "START"}}
    value = {"metadata": {"phone_number_id": "business-1"}, "messages": [message],
             "statuses": [{"id": "deleted-offer-id", "recipient_id": "919999999991",
                           "status": "read", "timestamp": str(cutoff + 1)}]}
    payload = {"entry": [{"changes": [{"value": value}]}]}
    assert flow.receive(payload) == 0
    assert flow.state(DAY)["jobs"] == []
    assert not flow.state(DAY)["windows"]["TEST-A"]["open"]
    value["messages"] = [{**message, "id": "fresh-start", "timestamp": str(cutoff + 1)}]
    assert flow.receive(payload) == 1
    assert flow.state(DAY)["windows"]["TEST-A"]["open"]
