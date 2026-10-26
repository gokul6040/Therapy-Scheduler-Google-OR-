"""Audit regressions: stale saves, independent parents and delayed callbacks."""

from dataclasses import replace
from datetime import date, datetime, timedelta
import json
from threading import Event, Thread
import time

import pytest
from sqlalchemy import select

from database.models import Appointment, Location, Patient, PatientAbsence, Therapist, TherapistAbsence, TreatmentRequirement
from flow_test.models import FlowInbox, FlowOffer
from flow_test.service import FlowTest
from services.admin_reservation_service import EmptySlot, preview_empty_slot, reserve_admin_slot


DAY = date(2026, 10, 22)


@pytest.fixture
def flow(tmp_path, legacy_flow_scenario):
    item = FlowTest(f"sqlite:///{(tmp_path / 'audit.sqlite').as_posix()}", scenario=legacy_flow_scenario)
    item.next_offer_date = lambda: DAY
    yield item
    item.engine.dispose()


@pytest.mark.parametrize("change", ["therapist_absence", "patient_absence", "target", "booking"])
def test_save_rejects_changed_inputs_without_partial_offers(flow, change):
    preview, _ = flow.preview_daily(DAY)
    preview = replace(preview, appointments=[a for a in preview.appointments if a.date == DAY])
    if change == "booking":
        flow.generate(DAY)
    else:
        with flow.sessions.begin() as session:
            first = preview.appointments[0]
            if change == "therapist_absence":
                session.add(TherapistAbsence(therapist_id=first.therapist_id, date=DAY, full_day=True))
            elif change == "patient_absence":
                session.add(PatientAbsence(patient_id=first.patient_id, date=DAY, full_day=True))
            else:
                session.get(TreatmentRequirement, first.requirement_id).sessions_per_week += 1
    before = flow.state(DAY)["appointments"]
    with pytest.raises(ValueError, match="changed after this preview"):
        flow.save_calendar(preview, DAY, expected_offer_on_save=True)
    state = flow.state(DAY)
    assert state["appointments"] == before
    assert not state["offers"] and not state["jobs"]


def test_one_closed_window_does_not_block_other_parents(flow, monkeypatch):
    class AppointmentDayClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 10, 22, 12, tzinfo=tz)

    # Keep the live past-date guard meaningful after the fixture date passes.
    monkeypatch.setattr("flow_test.service.datetime", AppointmentDayClock)
    calls = []
    flow.live = True
    flow.recipients = {code: f"91999999999{i}" for i, code in enumerate(("TEST-A", "TEST-B", "TEST-C"))}
    ready = {flow.recipients["TEST-A"], flow.recipients["TEST-B"]}
    monkeypatch.setattr(flow, "_session_deadline", lambda _, phone: time.time() + 86400 if phone in ready else 0)
    monkeypatch.setattr(flow, "send", lambda _, payload: calls.append(payload["recipient"]) or "dummy")
    preview, _ = flow.preview_daily(DAY)
    response = flow.save_calendar(preview, DAY, expected_offer_on_save=True)
    assert (response["offers"], response["waitingForContact"]) == (3, 1)
    assert flow.process_batch() == 2
    assert set(calls) == ready
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123456789")
    ready.add(flow.recipients["TEST-C"])
    flow.receive({"entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": "123456789"},
        "messages": [{"id": "new-contact", "from": flow.recipients["TEST-C"],
                      "timestamp": str(int(time.time())), "text": {"body": "START"}}],
    }}]}]})
    assert flow.process_batch() == 1  # Incoming START.
    assert flow.process_batch() == 1  # Its previously waiting offer.
    assert set(calls) == ready and len(calls) == 3


@pytest.mark.parametrize("action,expected", [("YES", "CONFIRMED"), ("NO", "DECLINED")])
def test_timely_reply_survives_delayed_processing_after_expiry(flow, action, expected):
    flow.generate(DAY)
    flow.publish(DAY)
    flow.drain()
    token = next(o["token"] for o in flow.state(DAY)["offers"] if o["patient"] == "TEST-B")
    with flow.sessions.begin() as session:
        offer = session.get(FlowOffer, token)
        offer.created_at = datetime.utcnow() - timedelta(hours=11)
        offer.expires_at = datetime.utcnow() - timedelta(minutes=1)
        recipient = offer.recipient
        # Stored UTC values are naive; convert explicitly, independently of host timezone.
        stamp = str(int((offer.expires_at - timedelta(seconds=5) - datetime(1970, 1, 1)).total_seconds()))
        flow._job(session, f"delayed-{action}", "INBOUND", {
            "from": recipient, "timestamp": stamp, "button": {"payload": f"ft:{token}:{action}"},
        })
    assert flow.expire_offers() == 1
    flow.drain()
    with flow.sessions() as session:
        assert session.get(FlowOffer, token).state == expected
        assert session.scalar(select(FlowInbox).where(FlowInbox.event_key == f"expiry:{token}")).resolved


def test_released_offer_cannot_be_restored_by_delayed_reply(flow):
    flow.generate(DAY)
    flow.publish(DAY)
    flow.drain()
    token = next(o["token"] for o in flow.state(DAY)["offers"] if o["patient"] == "TEST-B")
    flow.release_offer(token)
    flow.simulate(token, "YES")
    flow.drain()
    assert next(o["state"] for o in flow.state(DAY)["offers"] if o["token"] == token) == "CLOSED"


def test_incoming_event_can_be_persisted_during_a_slow_outbound_send(flow, monkeypatch):
    entered, release, received = Event(), Event(), Event()
    phone = "919999999991"
    flow.recipients = {code: phone for code in ("TEST-A", "TEST-B", "TEST-C")}
    def send(*_):
        entered.set()
        assert release.wait(timeout=5)
        return "dummy"
    monkeypatch.setattr(flow, "send", send)
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "123456789")
    flow.generate(DAY)
    flow.publish(DAY)
    worker = Thread(target=flow.process_batch)
    payload = {"entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id": "123456789"},
        "messages": [{"id": "during-send", "from": phone, "timestamp": str(int(time.time())),
                      "text": {"body": "START"}}],
    }}]}]}
    callback = Thread(target=lambda: (flow.contact_numbers(), flow.receive(payload), received.set()))
    try:
        worker.start()
        assert entered.wait(timeout=5)
        callback.start()
        assert received.wait(timeout=2), "Callback was blocked behind the outbound request"
        assert not release.is_set()
    finally:
        release.set()
        worker.join(timeout=5)
        if callback.ident is not None:
            callback.join(timeout=5)


def test_unknown_send_has_an_actionable_deduplicated_staff_warning(flow):
    def failing_send(*_):
        raise TimeoutError("Sensitive provider details must not be copied into inbox text")
    flow.send = failing_send
    flow.generate(DAY)
    flow.publish(DAY)
    flow.process_batch()
    state = flow.state(DAY)
    assert len(state["inbox"]) == 3
    assert {i["reason"] for i in state["inbox"]} == {"WhatsApp send needs staff review"}
    assert all("Sensitive" not in i["text"] for i in state["inbox"])
    assert flow.process_batch() == 0
    assert len(flow.state(DAY)["inbox"]) == 3


def test_empty_cell_booking_uses_primary_assignment_without_extra_mapping_tables(flow):
    from sqlalchemy import inspect
    assert len(inspect(flow.engine).get_table_names()) == 19
    with flow.sessions.begin() as session:
        patient = session.scalar(select(Patient).where(Patient.patient_code == "TEST-A"))
        plan = session.scalar(select(TreatmentRequirement).where(TreatmentRequirement.patient_id == patient.id))
        opening = EmptySlot(DAY, 0, plan.assigned_therapist_id, plan.department_id, plan.location_id)
        preview = preview_empty_slot(session, opening, patient.id)
        result = reserve_admin_slot(session, source_id=None, opening=opening, patient_id=patient.id,
                                    whole_visit=False, request_id="audit-empty-cell", preview_token=preview["preview_token"],
                                    override_warnings=bool(preview["warnings"]), mode="without_messaging")
        assert session.get(Appointment, result["appointment_ids"][0]).status.value == "CONFIRMED"
    assert not flow.state(DAY)["offers"]
    assert len(inspect(flow.engine).get_table_names()) == 19


def test_missing_optional_mappings_do_not_grant_other_therapy_or_branch(flow):
    with flow.sessions.begin() as session:
        patient = session.scalar(select(Patient).where(Patient.patient_code == "TEST-A"))
        doctors = list(session.scalars(select(Therapist).order_by(Therapist.id)))
        branch = Location(name="Other fictional branch", capacity=1)
        session.add(branch)
        session.flush()
        opening = EmptySlot(DAY, 0, doctors[0].id, doctors[1].department_id, branch.id)
        preview = preview_empty_slot(session, opening, patient.id)
        assert {"THERAPIST_CAPABILITY", "THERAPIST_BRANCH"} <= {w["code"] for w in preview["warnings"]}
