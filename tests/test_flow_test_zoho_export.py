"""Manual finalisation, incremental export state and failure/restart guarantees."""

from datetime import date, datetime, timedelta
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from database.models import Appointment, AppointmentStatus, AuditLog
from flow_test import mount
from flow_test.models import FlowJob, FlowOffer
from flow_test.service import FlowTest
from flow_test.zoho_export import (
    acknowledge_export, export_preview, finish_export, prepare_export,
)
from flow_test.zoho_authorize import save_test_refresh_token
from flow_test.zoho_push import export_settings, verify_target
from tests.test_flow_test_mount import request


DAY = date(2026, 10, 22)


def test_calendar_push_requires_auth_and_a_valid_date(flow, monkeypatch):
    monkeypatch.setattr(mount, "runtime", lambda: SimpleNamespace(flow=flow, token="test-key", previews={}))
    assert request("/test/api/zoho/export/push", {"date": DAY.isoformat()}, token="wrong")[0] == 401
    assert request("/test/api/zoho/export/push", {"date": "bad"})[0] == 400
    assert {o["state"] for o in flow.state(DAY)["offers"]} == {"PENDING"}


def test_calendar_missing_connection_preserves_schedule(flow, monkeypatch):
    from flow_test import zoho_calendar
    monkeypatch.setattr(mount, "runtime", lambda: SimpleNamespace(flow=flow, token="test-key", previews={}))
    monkeypatch.setattr(zoho_calendar, "export_settings", lambda: __import__("dataclasses").replace(export_settings(), refresh_token=""))
    before = flow.state(DAY)["appointments"]
    status, body = request("/test/api/zoho/export/push", {"date": DAY.isoformat()})
    assert status == 409 and b"not configured" in body
    assert flow.state(DAY)["appointments"] == before
    assert export_preview(flow, DAY)["openBatch"] is None


def test_calendar_network_does_not_hold_workflow_lock_and_provider_secrets_are_hidden(flow, monkeypatch):
    from dataclasses import replace
    from threading import Thread
    from flow_test import zoho_calendar
    monkeypatch.setattr(mount, "runtime", lambda: SimpleNamespace(flow=flow, token="test-key", previews={}))
    monkeypatch.setattr(zoho_calendar, "export_settings", lambda: replace(export_settings(), client_id="dummy", client_secret="dummy", refresh_token="dummy"))
    acquired = []

    def reconnect(_settings):
        def incoming():
            locked = flow.lock.acquire(timeout=1)
            acquired.append(locked)
            if locked:
                flow.lock.release()
        worker = Thread(target=incoming)
        worker.start()
        worker.join(timeout=2)
        raise ValueError("provider echoed secret-dummy-token")

    monkeypatch.setattr(zoho_calendar, "refresh_access_token", reconnect)
    status, body = request("/test/api/zoho/export/push", {"date": DAY.isoformat()})
    assert status == 409 and b"could not reconnect" in body and b"secret-dummy-token" not in body
    assert acquired == [True]
    # A failed connection must release the push lock for the next attempt.
    assert request("/test/api/zoho/export/push", {"date": DAY.isoformat()})[0] == 409
    assert acquired == [True, True]


def test_calendar_rejects_overlapping_pushes_without_finalising(flow, monkeypatch):
    from flow_test import zoho_calendar
    monkeypatch.setattr(mount, "runtime", lambda: SimpleNamespace(flow=flow, token="test-key", previews={}))
    with zoho_calendar._push_lock:
        status, body = request("/test/api/zoho/export/push", {"date": DAY.isoformat()})
    assert status == 409 and b"already running" in body
    assert export_preview(flow, DAY)["openBatch"] is None


@pytest.fixture
def flow(tmp_path, legacy_flow_scenario):
    item = FlowTest(f"sqlite:///{(tmp_path / 'export.sqlite').as_posix()}", scenario=legacy_flow_scenario)
    item.generate(DAY)
    item.publish(DAY)
    yield item
    item.engine.dispose()


def acknowledge_all(flow, batch):
    for row in batch["changes"]:
        acknowledge_export(flow, batch["id"], row["appointmentId"], row["fingerprint"], str(900000 + row["appointmentId"]))


def test_preview_does_not_assume_yes_and_unsent_offers_are_not_confirmed(flow):
    before = flow.state(DAY)
    assert export_preview(flow, DAY)["wouldAssumeAcceptedVisits"] == 0
    batch = prepare_export(flow, DAY)
    assert not batch["changes"] and batch["assumedAcceptedVisits"] == 0
    assert len(batch["awaitingStaff"]) == 4
    assert flow.state(DAY)["appointments"] == before["appointments"]


def test_manual_cutoff_assumes_sent_unanswered_and_repeat_push_has_no_changes(flow):
    flow.drain()
    preview = export_preview(flow, DAY)
    assert preview["wouldAssumeAcceptedVisits"] == 3
    assert {o["state"] for o in flow.state(DAY)["offers"]} == {"PENDING"}
    batch = prepare_export(flow, DAY)
    assert batch["assumedAcceptedVisits"] == 3 and len(batch["changes"]) == 4
    assert {r["fields"]["Status"] for r in batch["changes"]} == {"ASSUMED_CONFIRMED"}
    assert all("Added_Time" not in r["fields"] for r in batch["changes"])
    assert batch["cutoffIST"].endswith("+05:30")
    acknowledge_all(flow, batch)
    result = finish_export(flow, batch["id"])
    assert result["completedAtIST"].endswith("+05:30")
    assert not prepare_export(flow, DAY)["changes"]
    with flow.sessions() as session:
        assert len(list(session.scalars(select(AuditLog).where(AuditLog.action == "flow_assumed_yes")))) == 3
        assert not list(session.scalars(select(FlowJob).where(FlowJob.kind == "TEXT")))


def test_already_received_yes_no_help_are_processed_before_assuming_acceptance(flow):
    flow.drain()
    tokens = {o["patient"]: o["token"] for o in flow.state(DAY)["offers"]}
    flow.simulate(tokens["TEST-A"], "YES")
    flow.simulate(tokens["TEST-B"], "NO")
    flow.simulate(tokens["TEST-C"], "HELP")
    batch = prepare_export(flow, DAY)
    assert batch["assumedAcceptedVisits"] == 0
    assert {r["fields"]["Reg_ID"] for r in batch["changes"]} == {"TEST-A"}
    assert {r["fields"]["Status"] for r in batch["changes"]} == {"CONFIRMED"}
    assert batch["awaitingStaff"]


def test_partial_export_keeps_checkpoint_and_resumes_after_restart(flow):
    flow.drain()
    batch = prepare_export(flow, DAY)
    first = batch["changes"][0]
    acknowledge_export(flow, batch["id"], first["appointmentId"], first["fingerprint"], "999001")
    with pytest.raises(ValueError, match="incomplete"):
        finish_export(flow, batch["id"])
    assert export_preview(flow, DAY)["lastPush"] is None
    restarted = FlowTest(str(flow.engine.url), scenario=flow.scenario)
    try:
        resumed = prepare_export(restarted, DAY)
        assert resumed["id"] == batch["id"] and resumed["resumed"]
        assert resumed["acknowledged"] == {str(first["appointmentId"]): "999001"}
        acknowledge_all(restarted, resumed)
        assert finish_export(restarted, batch["id"])["pushed"] == 4
    finally:
        restarted.engine.dispose()


def test_cancellation_and_move_update_exported_rows_without_new_keys(flow):
    flow.drain()
    batch = prepare_export(flow, DAY)
    acknowledge_all(flow, batch)
    finish_export(flow, batch["id"])
    first, second = batch["changes"][:2]
    with flow.sessions.begin() as session:
        session.get(Appointment, first["appointmentId"]).status = AppointmentStatus.CANCELLED_PATIENT
        session.get(Appointment, second["appointmentId"]).date = date(2026, 10, 23)
    changes = prepare_export(flow, DAY)["changes"]
    assert len(changes) == 2
    assert {r["fields"]["Scheduler_Key"] for r in changes} == {r["fields"]["Scheduler_Key"] for r in (first, second)}
    assert {r["fields"]["Status"] for r in changes} >= {"CANCELLED_PATIENT"}
    assert next(r for r in changes if r["appointmentId"] == second["appointmentId"])["fields"]["Therapy_Date"] == "23-10-2026"


def test_changed_fingerprint_during_push_is_picked_up_next_time(flow):
    flow.drain()
    batch = prepare_export(flow, DAY)
    first = batch["changes"][0]
    with flow.sessions.begin() as session:
        session.get(Appointment, first["appointmentId"]).status = AppointmentStatus.NO_SHOW
    acknowledge_all(flow, batch)
    finish_export(flow, batch["id"])
    next_batch = prepare_export(flow, DAY)
    assert len(next_batch["changes"]) == 1
    assert next_batch["changes"][0]["fields"]["Status"] == "NO_SHOW"


def test_export_api_is_authenticated_and_rejects_forged_ack(flow, monkeypatch):
    monkeypatch.setattr(mount, "runtime", lambda: SimpleNamespace(flow=flow, token="test-key", previews={}))
    assert request("/test/api/zoho/export/prepare", {"date": DAY.isoformat()}, token="wrong")[0] == 401
    assert request("/test/api/zoho/export/preview", {"date": DAY.isoformat()})[0] == 200
    status, body = request("/test/api/zoho/export/ack", {"batchId": "fake", "appointmentId": 1, "fingerprint": "x", "recordId": "1"})
    assert status == 409 and "match" in json.loads(body)["error"]


@pytest.mark.parametrize("send_status,delivery", [("UNKNOWN", None), ("WAITING_FOR_CONTACT", None), ("SIMULATED", "failed")])
def test_unsent_uncertain_or_failed_delivery_is_not_assumed_accepted(flow, send_status, delivery):
    flow.drain()
    with flow.sessions.begin() as session:
        for job in session.scalars(select(FlowJob)):
            job.status, job.delivery = send_status, delivery
    batch = prepare_export(flow, DAY)
    assert batch["assumedAcceptedVisits"] == 0 and not batch["changes"]


def test_test_oauth_token_save_preserves_other_settings(tmp_path, monkeypatch):
    path = tmp_path / ".env"
    path.write_text("EXISTING=value\nZOHO_CREATOR_REFRESH_TOKEN=old-main\nFLOW_TEST_ZOHO_REFRESH_TOKEN=old-test\n")
    monkeypatch.setenv("FLOW_TEST_ZOHO_REFRESH_TOKEN", "dummy-before-test")
    save_test_refresh_token(path, "dummy-new-test")
    assert path.read_text() == "EXISTING=value\nZOHO_CREATOR_REFRESH_TOKEN=old-main\nFLOW_TEST_ZOHO_REFRESH_TOKEN=dummy-new-test\n"
    with pytest.raises(ValueError):
        save_test_refresh_token(path, "invalid\nvalue")
    assert "dummy-new-test" in path.read_text()


def test_export_client_rejects_other_app_or_remote_api_host():
    from dataclasses import replace
    settings = export_settings()
    verify_target(settings)
    with pytest.raises(ValueError, match="restricted"):
        verify_target(replace(settings, app_link_name="production"))
    with pytest.raises(ValueError, match="restricted"):
        verify_target(replace(settings, api_url="https://example.invalid"))


@pytest.mark.parametrize("resolve", [False, True])
def test_late_no_is_not_mistaken_for_silence_even_after_inbox_resolution(flow, resolve):
    flow.drain()
    token = next(o["token"] for o in flow.state(DAY)["offers"] if o["patient"] == "TEST-B")
    with flow.sessions.begin() as session:
        session.get(FlowOffer, token).expires_at = datetime.utcnow() - timedelta(minutes=1)
    flow.simulate(token, "NO")
    flow.drain()
    if resolve:
        for item in flow.state(DAY)["inbox"]:
            flow.resolve(item["id"])
    batch = prepare_export(flow, DAY)
    assert batch["assumedAcceptedVisits"] == 2
    assert "TEST-B" not in {r["fields"]["Reg_ID"] for r in batch["changes"]}
    assert batch["awaitingStaff"]


def test_unmatched_reply_waiting_for_staff_is_not_silence(flow):
    flow.drain()
    with flow.sessions.begin() as session:
        offer = session.scalar(select(FlowOffer).where(FlowOffer.patient_code == "TEST-B"))
        flow._job(session, "ambiguous-export-reply", "INBOUND", {
            "from":offer.recipient,"timestamp":str(int(datetime.now().timestamp())),"text":{"body":"NO"}})
    batch = prepare_export(flow, DAY)
    assert batch["assumedAcceptedVisits"] == 2
    assert "TEST-B" not in {r["fields"]["Reg_ID"] for r in batch["changes"]}
