"""Verify the actual Zoho test report contract and isolated conversion workflow."""

from copy import deepcopy
from datetime import date
from pathlib import Path
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from database.models import Appointment, Patient, Therapist, TreatmentRequirement
from flow_test import mount
from flow_test.service import FlowTest
from flow_test.zoho_import import apply_reports, convert_reports
from tests.test_flow_test_mount import request


@pytest.fixture
def reports():
    return json.loads((Path(__file__).parent / "fixtures/zoho_test_reports.json").read_text(encoding="utf-8"))


@pytest.fixture
def source_flow(tmp_path):
    flow = FlowTest(f"sqlite:///{(tmp_path / 'source.sqlite').as_posix()}")
    flow.next_offer_date = lambda: date(2026, 10, 23)
    yield flow
    flow.engine.dispose()


def test_actual_report_shape_converts_two_therapy_lines_and_added_time_start(reports):
    scenario = convert_reports(reports, date(2026, 10, 22))
    assert len(scenario["patients"]) == 8
    assert len(scenario["therapists"]) == 2
    assert sum(len(p["requirements"]) for p in scenario["patients"]) == 10
    assert sum(r["weekly"] for p in scenario["patients"] for r in p["requirements"]) == 21
    child = scenario["patients"][0]
    assert child["code"] == "TEST-A"
    assert [r["therapy"] for r in child["requirements"]] == ["Psychology Therapy Session", "IEP Session"]
    assert child["requirements"][0]["plan_start_date"] == "2026-10-21"
    assert child["requirements"][0]["plan_added_at"].startswith("2026-10-21")
    assert child["requirements"][0]["duration_months"] == 3


@pytest.mark.parametrize("report", ["Plan_Report", "All_Consultants"])
def test_branch_surrounding_whitespace_is_ignored(reports, report, source_flow):
    reports[report][0]["Branch"] = "\t Malaparamba EIC \r\n"
    result = apply_reports(source_flow, reports, date(2026, 10, 22))
    assert (result["patients"], result["consultants"], result["requirements"]) == (8, 2, 10)


@pytest.mark.parametrize("report", ["Plan_Report", "All_Consultants"])
def test_missing_branch_fails_with_record_context(reports, report, source_flow):
    reports[report][0]["Branch"] = "\t "
    expected = "Patient 'TEST-H'.*Branch" if report == "Plan_Report" else "Consultant .*Branch"
    with pytest.raises(ValueError, match=expected):
        apply_reports(source_flow, reports, date(2026, 10, 22))
    assert source_flow.scenario.get("source") is None


@pytest.mark.parametrize("change", ["ambiguous", "malformed", "branch", "phone", "timestamp"])
def test_invalid_snapshot_rejected_before_any_changes(reports, source_flow, change):
    if change == "ambiguous":
        duplicate = deepcopy(reports["All_Consultants"][0])
        duplicate["ID"] = "999999"
        reports["All_Consultants"].append(duplicate)
    elif change == "malformed":
        reports["Plan_Report"][0]["Schedule"] = "Psychology Therapy Session, 2, 3, TEST CONSULTANT P IEP Session, 2, 3, TEST CONSULTANT I"
    elif change == "branch":
        reports["Plan_Report"][0]["Branch"] = "Another centre"
    elif change == "phone":
        reports["Plan_Report"][0]["Parent_No"] = "invalid"
    else:
        reports["Plan_Report"][0]["Modified_Time"] = "invalid"
    with pytest.raises(ValueError):
        apply_reports(source_flow, reports, date(2026, 10, 22))
    assert source_flow.scenario.get("source") is None
    with source_flow.sessions() as session:
        assert session.scalar(select(func.count(Patient.id))) == 8
        assert session.scalar(select(func.count(Therapist.id))) == 2


def test_import_repeat_restart_reset_and_schedule_preserve_source(reports, source_flow):
    first = apply_reports(source_flow, reports, date(2026, 10, 22))
    assert first["changed"] is True
    assert apply_reports(source_flow, reports, date(2026, 10, 22))["changed"] is False
    with source_flow.sessions() as session:
        ids = list(session.scalars(select(TreatmentRequirement.id).where(TreatmentRequirement.active.is_(True))))
        assert len(ids) == 10
    result, pinned = source_flow.preview_daily(date(2026, 10, 23))
    assert not pinned
    assert len(result.appointments) == 10
    assert not any(result.validation.values())
    restarted = FlowTest(str(source_flow.engine.url))
    try:
        assert restarted.scenario["source"].endswith("test-gokul")
        restarted.reset_test_data("RESET preview")
        with restarted.sessions() as session:
            requirements = list(session.scalars(select(TreatmentRequirement)))
            assert len(requirements) == 10
            assert all(item.plan_start_date == date(2026, 10, 21) for item in requirements)
        assert apply_reports(restarted, reports, date(2026, 10, 22))["changed"] is True
    finally:
        restarted.engine.dispose()


def test_plan_change_with_saved_history_updates_target_and_warns(reports, source_flow):
    apply_reports(source_flow, reports, date(2026, 10, 22))
    source_flow.generate(date(2026, 10, 23))
    changed = deepcopy(reports)
    row = next(item for item in changed["Plan_Report"] if item["RegistrationID"] == "TEST-A")
    row["Schedule"] = row["Schedule"].replace(", 3, 3,", ", 4, 3,")
    result = apply_reports(source_flow, changed)
    assert result["warnings"] and result["warnings"][0]["patient"] == "TEST-A"
    assert apply_reports(source_flow, changed)["changed"] is False
    with source_flow.sessions() as session:
        assert session.scalar(select(func.count(Appointment.id))) == 10


def test_live_import_uses_valid_source_phone_without_a_separate_file_entry(reports, source_flow):
    source_flow.live = True
    reports["Plan_Report"][0]["Parent_No"] = "918888888888"
    apply_reports(source_flow, reports, date(2026, 10, 22))
    assert source_flow.recipients["TEST-H"] == "918888888888"
    assert "918888888888" in source_flow.transport.allowed


def test_import_endpoint_drafts_without_saving_or_sending(reports, source_flow, monkeypatch):
    instance = SimpleNamespace(flow=source_flow, token="test-key", previews={})
    monkeypatch.setattr(mount, "runtime", lambda: instance)
    body = {"reports": reports, "planStart": "2026-10-22"}
    assert request("/test/api/zoho/import", body, token="wrong")[0] == 401
    status, raw = request("/test/api/zoho/import", body)
    assert status == 200
    result = json.loads(raw)
    assert result["messages_sent"] == 0
    assert result["draftSessions"] == 10
    assert result["draftDate"] == "2026-10-23"
    draft = json.loads(request("/test/api/zoho/draft")[1])["draft"]
    assert draft["canSave"] and len(draft["appointments"]) == 10
    assert not source_flow.state(date(2026, 10, 23))["appointments"]
    assert not source_flow.state(date(2026, 10, 23))["offers"]
    # Existing shared save and preview message/reply flow works with imported records.
    assert request(f"/test/api/schedules/{draft['previewId']}/save", {})[0] == 200
    state = source_flow.state(date(2026, 10, 23))
    assert len(state["offers"]) == 8
    offer = next(item for item in state["offers"] if item["patient"] == "TEST-A")
    source_flow.simulate(offer["token"], "YES")
    source_flow.drain()
    for appointment_id in offer["appointment_ids"]:
        source_flow.attendance(appointment_id, "COMPLETED")
    progress = [item for item in source_flow.state(date(2026, 10, 23))["progress"] if item["patient"] == "TEST-A"]
    assert sum(item["completed"] for item in progress) == 2
    assert sum(item["remaining"] for item in progress) == 3


def test_existing_fixture_history_is_not_discarded(reports, source_flow):
    source_flow.generate(date(2026, 10, 23))
    with pytest.raises(ValueError, match="Existing fixture appointments"):
        apply_reports(source_flow, reports, date(2026, 10, 22))
    assert source_flow.scenario.get("source") is None


def test_changed_names_and_modified_time_update_without_duplicates(reports, source_flow):
    apply_reports(source_flow, reports, date(2026, 10, 22))
    row = reports["Plan_Report"][0]
    row["PatientName"] = "Fictional Child H Updated"
    row["Modified_Time"] = "22-Oct-2026 08:00:00"
    assert apply_reports(source_flow, reports, date(2026, 10, 22))["changed"]
    with source_flow.sessions() as session:
        assert session.scalar(select(func.count(Patient.id))) == 8
        assert session.scalar(select(Patient.name).where(Patient.patient_code == "TEST-H")) == "Fictional Child H Updated"


def test_consultant_deactivation_with_bookings_needs_review(reports, source_flow):
    apply_reports(source_flow, reports, date(2026, 10, 22))
    source_flow.generate(date(2026, 10, 23))
    reports["All_Consultants"][0]["Status"] = "Inactive"
    with pytest.raises(ValueError, match="status change"):
        apply_reports(source_flow, reports, date(2026, 10, 22))
    with source_flow.sessions() as session:
        assert session.scalar(select(func.count(Therapist.id)).where(
            Therapist.therapist_code.like("ZC-%"), Therapist.active.is_(True))) == 2


def test_sunday_draft_target_is_skipped(reports, source_flow, monkeypatch):
    source_flow.next_offer_date = lambda: date(2026, 10, 25)
    instance = SimpleNamespace(flow=source_flow, token="test-key", previews={})
    monkeypatch.setattr(mount, "runtime", lambda: instance)
    status, raw = request("/test/api/zoho/import", {"reports": reports, "planStart": "2026-10-22"})
    assert status == 200 and json.loads(raw)["draftDate"] is None
    assert json.loads(request("/test/api/zoho/draft")[1]) == {"draft": None}
