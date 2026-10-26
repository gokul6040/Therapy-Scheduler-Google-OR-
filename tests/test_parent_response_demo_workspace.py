from http.server import ThreadingHTTPServer
import json
from pathlib import Path
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from services.parent_response_demo_workspace import ResponseDemoWorkspace
from web_app import ParentResponseDemoHandler


@pytest.fixture
def workspace(tmp_path):
    demo = ResponseDemoWorkspace(tmp_path)
    yield demo
    if demo.worker:
        demo.worker.join(timeout=10)
    demo.engine.dispose()


def test_large_dataset_repairs_only_released_slots_and_keeps_other_bookings(workspace):
    before = workspace.state()
    assert before["status"] == "ready"
    summary = before["before"]["summary"]
    assert summary == {
        "patients": 80, "therapists": 12, "branches": 2,
        "therapy_requirements": 120, "appointments": 96,
        "day_sessions": 48, "day_visits": 32,
        "replies": 24, "yes": 12, "no": 12, "no_reply": 8,
    }
    assert workspace.start()
    workspace.worker.join(timeout=15)
    after = workspace.state()
    assert after["status"] == "completed", after["error"]
    outcome = after["outcome"]
    assert outcome["processed_replies"] == 24
    assert outcome["released_sessions"] == 16
    assert outcome["proposed_sessions"] == 15
    assert outcome["proposed_visits"] == 11
    assert outcome["unfilled_slots"] == 1
    assert not any(outcome["validation"].values())
    originals = {row["id"]: row for row in before["before"]["appointments"]}
    updated = {row["id"]: row for row in after["after"]["appointments"]}
    released = {change["original"]["id"] for change in outcome["changes"]}
    for identifier, original in originals.items():
        current = updated[identifier]
        for field in ("date", "slot", "therapist_id", "branch", "bundle"):
            assert current[field] == original[field]
        if identifier not in released:
            assert current["status"] == (
                "CONFIRMED" if original["reply"] == "YES" else original["status"]
            )
    for change in outcome["changes"]:
        if change["replacement"]:
            for field in ("date", "slot", "therapist_id", "therapy", "branch"):
                assert change["replacement"][field] == change["original"][field]
            assert change["replacement"]["status"] == "PROPOSED"
    assert all(row["date"] == before["target_date"] for identifier, row in updated.items() if identifier not in originals)
    # A repeated click does not process already consumed responses again.
    assert not workspace.start()
    assert workspace.state()["after"] == after["after"]


def test_reset_creates_new_fake_database_and_preserves_previous_run(workspace):
    previous = workspace.state()
    workspace.reset()
    current = workspace.state()
    assert current["status"] == "ready"
    assert current["run_id"] != previous["run_id"]
    assert current["after"] is None
    assert current["before"]["summary"] == previous["before"]["summary"]
    assert Path(previous["database_path"]).exists()
    assert Path(current["database_path"]).exists()


def test_reset_is_blocked_while_worker_is_running(workspace):
    workspace.status = "running"
    try:
        with pytest.raises(ValueError, match="Wait for the current run"):
            workspace.reset()
    finally:
        workspace.status = "ready"


def test_admin_can_confirm_entire_suggested_visit_and_refresh_the_screen(workspace):
    workspace.start()
    workspace.worker.join(timeout=15)
    state = workspace.state()
    change = next(item for item in state["outcome"]["changes"] if item["original"]["therapy"] == "Speech")
    details = workspace.slot_details(state["run_id"], change["original"]["id"])
    assert len(details["patients"]) == 80
    assert len(details["slots"]) == 2
    assert details["suggestions"][0]["current_proposal"]
    payload = {
        "run_id": state["run_id"], "source_id": change["original"]["id"],
        "patient_id": change["replacement"]["patient_id"], "whole_visit": True,
    }
    preview = workspace.preview_booking(payload)
    assert not preview["warnings"]
    reservation = workspace.reserve_booking({
        **payload, "request_id": "group-admin-booking", "preview_token": preview["preview_token"],
    })
    assert len(reservation["appointment_ids"]) == 2
    current = workspace.state()
    assert current["outcome"]["reserved_sessions"] == 2
    assert current["outcome"]["alternatives_stale"]
    assert current["outcome"]["unfilled_slots"] == 1
    assert all(row["status"] == "CONFIRMED" for row in current["after"]["appointments"]
               if row["id"] in reservation["appointment_ids"])


def test_off_plan_admin_reservation_does_not_fulfill_unrelated_therapy(workspace):
    workspace.start()
    workspace.worker.join(timeout=15)
    state = workspace.state()
    source = next(item["original"] for item in state["outcome"]["changes"] if item["original"]["therapy"] == "Speech")
    chosen = next(item for item in state["after"]["patients"] if item["code"] == "DEMO-080")
    payload = {"run_id": state["run_id"], "source_id": source["id"], "patient_id": chosen["id"]}
    preview = workspace.preview_booking(payload)
    assert {"THERAPY_NOT_IN_PLAN", "THERAPIST_MISMATCH", "BRANCH_MISMATCH"} <= {item["code"] for item in preview["warnings"]}
    original_progress = [row for row in state["after"]["weekly"] if row["patient"] == "DEMO-080"]
    workspace.reserve_booking({
        **payload, "request_id": "off-plan", "preview_token": preview["preview_token"],
        "override_warnings": True,
    })
    assert [row for row in workspace.state()["after"]["weekly"] if row["patient"] == "DEMO-080"] == original_progress


def test_partial_visit_warns_but_admin_can_reserve_selected_slot(workspace):
    workspace.start()
    workspace.worker.join(timeout=15)
    state = workspace.state()
    change = next(item for item in state["outcome"]["changes"] if item["original"]["therapy"] == "Speech")
    payload = {
        "run_id": state["run_id"], "source_id": change["original"]["id"],
        "patient_id": change["replacement"]["patient_id"], "whole_visit": False,
    }
    preview = workspace.preview_booking(payload)
    assert "PARTIAL_VISIT" in {item["code"] for item in preview["warnings"]}
    result = workspace.reserve_booking({
        **payload, "request_id": "partial", "preview_token": preview["preview_token"],
        "override_warnings": True,
    })
    assert len(result["appointment_ids"]) == 1
    assert workspace.state()["outcome"]["reserved_sessions"] == 1


def test_admin_booking_rejects_a_stale_run_after_reset(workspace):
    old_run = workspace.state()["run_id"]
    workspace.reset()
    with pytest.raises(ValueError, match="refresh"):
        workspace.preview_booking({"run_id": old_run, "source_id": 1, "patient_id": 1})


def test_demo_http_serves_only_demo_routes_and_rejects_foreign_origin(workspace):
    server = ThreadingHTTPServer(("127.0.0.1", 0), ParentResponseDemoHandler)
    server.demo_workspace = workspace
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_port}"
    try:
        with urlopen(base + "/api/demo/state", timeout=5) as response:
            assert json.load(response)["before"]["summary"]["patients"] == 80
        with urlopen(base + "/", timeout=5) as response:
            assert b"Run scheduler" in response.read()
        with pytest.raises(HTTPError) as error:
            urlopen(base + "/api/patients", timeout=5)
        assert error.value.code == 404
        request = Request(base + "/api/demo/run", method="POST", headers={"Origin": "https://example.com"})
        with pytest.raises(HTTPError) as error:
            urlopen(request, timeout=5)
        assert error.value.code == 403
        request = Request(base + "/api/demo/reset", method="POST", headers={"Origin": base})
        with urlopen(request, timeout=5) as response:
            assert json.load(response)["status"] == "ready"
    finally:
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()
