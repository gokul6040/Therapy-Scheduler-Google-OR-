"""Incremental checkpoints, new source identities and transaction boundaries."""

from copy import deepcopy
from datetime import date, datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import func, select

from database.models import Appointment, Patient, Therapist, TreatmentRequirement
from flow_test import mount
from flow_test.models import FlowSetting
from flow_test.service import FlowTest
from flow_test.zoho_daily import window_criteria
from flow_test.zoho_import import CHECKPOINT_KEY, SOURCE_TIMEZONES, TIMEZONE_KEY, apply_reports, convert_reports
from tests.test_flow_test_mount import request

START = date(2026, 10, 22)
FIRST = "2026-10-22T05:30:00Z"
SECOND = "2026-10-22T06:30:00Z"
THIRD = "2026-10-22T07:30:00Z"


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr("flow_test.zoho_import.sync_now", lambda: datetime(2026, 10, 22, 12, tzinfo=timezone.utc))


@pytest.fixture
def reports():
    return json.loads((Path(__file__).parent / "fixtures/zoho_test_reports.json").read_text(encoding="utf-8"))


@pytest.fixture
def flow(tmp_path):
    item = FlowTest(f"sqlite:///{(tmp_path / 'incremental.sqlite').as_posix()}")
    item.next_offer_date = lambda: date(2026, 10, 23)
    yield item
    item.engine.dispose()


def checkpoint(flow):
    with flow.sessions() as session:
        item = session.get(FlowSetting, CHECKPOINT_KEY)
        return json.loads(item.value) if item else None


def initial(flow, reports):
    return apply_reports(flow, reports, START, window={"from": None, "to": FIRST})


def delta():
    return {"Plan_Report": [], "All_Consultants": []}


def new_records(reports):
    patient = deepcopy(reports["Plan_Report"][0])
    patient.update(ID="900001", RegistrationID="REG-NEW-009", PatientName="New Fictional Child",
                   Schedule="Psychology Therapy Session, 1, 3, New Clinician",
                   Added_Time="22-Oct-2026 11:30:00", Modified_Time="22-Oct-2026 11:30:00")
    doctor = deepcopy(reports["All_Consultants"][1])
    doctor.update(ID="900002", Name="New Clinician", Added_Time="22-Oct-2026 11:30:00",
                  Modified_Time="22-Oct-2026 11:30:00")
    return {"Plan_Report": [patient], "All_Consultants": [doctor]}


def test_initial_then_new_patient_and_doctor_then_empty_delta(flow, reports):
    assert initial(flow, reports)["checkpoint"] == FIRST
    with flow.sessions() as session:
        old_ids = set(session.scalars(select(TreatmentRequirement.id).where(TreatmentRequirement.active.is_(True))))
    result = apply_reports(flow, new_records(reports), START, window={"from": FIRST, "to": SECOND})
    assert (result["patients"], result["consultants"], result["requirements"], result["weekly_sessions"]) == (9, 3, 11, 22)
    with flow.sessions() as session:
        ids = set(session.scalars(select(TreatmentRequirement.id).where(TreatmentRequirement.active.is_(True))))
        assert old_ids < ids
    unchanged = apply_reports(flow, delta(), START, window={"from": SECOND, "to": THIRD})
    assert not unchanged["changed"] and unchanged["patients"] == 9
    assert checkpoint(flow) == THIRD
    with flow.sessions() as session:
        assert set(session.scalars(select(TreatmentRequirement.id).where(TreatmentRequirement.active.is_(True)))) == ids


def test_empty_first_snapshot_does_not_advance_checkpoint(flow):
    with pytest.raises(ValueError, match="initial source"):
        initial(flow, delta())
    assert checkpoint(flow) is None


def test_invalid_delta_and_stale_execution_never_advance_checkpoint(flow, reports):
    initial(flow, reports)
    incoming = new_records(reports)
    incoming["Plan_Report"][0]["Schedule"] = "Psychology Therapy Session, 1, 3, Missing Doctor"
    with pytest.raises(ValueError, match="consultant match"):
        apply_reports(flow, incoming, START, window={"from": FIRST, "to": SECOND})
    assert checkpoint(flow) == FIRST
    with flow.sessions() as session:
        assert session.scalar(select(func.count(Patient.id))) == 8
        assert session.scalar(select(func.count(Therapist.id)).where(Therapist.therapist_code == "ZC-900002")) == 0
    with pytest.raises(ValueError, match="checkpoint changed"):
        apply_reports(flow, delta(), START, window={"from": None, "to": SECOND})
    assert checkpoint(flow) == FIRST


def test_old_record_edit_is_included_and_lower_boundary_is_inclusive(flow, reports):
    initial(flow, reports)
    incoming = delta()
    edited = deepcopy(reports["Plan_Report"][0])
    edited.update(PatientName="Edited Fictional Name", Modified_Time="22-Oct-2026 11:00:00")
    incoming["Plan_Report"] = [edited]  # Exactly FIRST in source UTC; Added Time is older.
    result = apply_reports(flow, incoming, START, window={"from": FIRST, "to": SECOND})
    assert result["changed"] and checkpoint(flow) == SECOND
    with flow.sessions() as session:
        assert session.scalar(select(Patient.name).where(Patient.patient_code == edited["RegistrationID"])) == "Edited Fictional Name"


@pytest.mark.parametrize("modified", ["22-Oct-2026 10:59:59", "22-Oct-2026 12:00:00"])
def test_out_of_window_rows_rejected_without_advancement(flow, reports, modified):
    initial(flow, reports)
    incoming = delta()
    edited = deepcopy(reports["Plan_Report"][0])
    edited["Modified_Time"] = modified
    incoming["Plan_Report"] = [edited]
    with pytest.raises(ValueError, match="outside"):
        apply_reports(flow, incoming, START, window={"from": FIRST, "to": SECOND})
    assert checkpoint(flow) == FIRST


def test_booked_plan_change_updates_source_and_checkpoint_with_warning(flow, reports):
    initial(flow, reports)
    flow.generate(date(2026, 10, 23))
    changed = deepcopy(next(row for row in reports["Plan_Report"] if row["RegistrationID"] == "TEST-A"))
    changed["Schedule"] = changed["Schedule"].replace(", 3, 3,", ", 4, 3,")
    changed["Modified_Time"] = "22-Oct-2026 11:30:00"
    incoming = delta()
    incoming["Plan_Report"] = [changed]
    result = apply_reports(flow, incoming, START, window={"from": FIRST, "to": SECOND})
    assert result["warnings"] and checkpoint(flow) == SECOND
    with flow.sessions() as session:
        cached = json.loads(session.get(FlowSetting, "zoho_test_source").value)
        original = next(row for row in cached["Plan_Report"] if row["ID"] == changed["ID"])
        assert original["Schedule"] == changed["Schedule"]


def test_new_plan_row_uses_latest_update_without_creating_another_patient(flow, reports):
    initial(flow, reports)
    newer = deepcopy(reports["Plan_Report"][0])
    newer.update(ID="900003", Schedule="Psychology Therapy Session, 1, 3, TEST CONSULTANT P",
                 Added_Time="22-Oct-2026 11:30:00", Modified_Time="22-Oct-2026 11:30:00")
    incoming = delta()
    incoming["Plan_Report"] = [newer]
    result = apply_reports(flow, incoming, START, window={"from": FIRST, "to": SECOND})
    assert result["patients"] == 8
    patient = next(p for p in flow.scenario["patients"] if p["code"] == newer["RegistrationID"])
    assert patient["requirements"][0]["weekly"] == 1


@pytest.mark.parametrize("reverse", [False, True])
def test_edit_to_older_row_wins_over_newer_created_plan(reports, reverse):
    edited = reports["Plan_Report"][0]
    edited.update(Modified_Time="22-Oct-2026 11:30:00", PatientName="Latest Name",
                  Parent_No="918888888888")
    newer_created = deepcopy(edited)
    newer_created.update(ID="900003", Added_Time="22-Oct-2026 10:30:00",
                         Modified_Time="22-Oct-2026 10:30:00", PatientName="Older Name",
                         Parent_No="919999999999", Schedule="IEP Session, 1, 3, TEST CONSULTANT I")
    reports["Plan_Report"] = [edited, newer_created][::(-1 if reverse else 1)]
    patient = convert_reports(reports, START)["patients"][0]
    assert patient["name"] == "Latest Name" and patient["phone"] == "918888888888"
    assert [r["therapy"] for r in patient["requirements"]] == ["Psychology Therapy Session"]


def test_modified_time_tie_uses_added_time_and_latest_schedule_is_complete(reports):
    old = reports["Plan_Report"][0]
    old["Modified_Time"] = "22-Oct-2026 11:30:00"
    newer = deepcopy(old)
    newer.update(ID="900003", Added_Time="22-Oct-2026 10:30:00",
                 Schedule="IEP Session, 1, 3, TEST CONSULTANT I")
    reports["Plan_Report"] = [old, newer]
    requirements = convert_reports(reports, START)["patients"][0]["requirements"]
    assert [r["therapy"] for r in requirements] == ["IEP Session"]


def test_ambiguous_latest_plan_rolls_back_checkpoint(flow, reports):
    initial(flow, reports)
    incoming = delta()
    for source_id in ("900003", "900004"):
        row = deepcopy(reports["Plan_Report"][0])
        row.update(ID=source_id, Added_Time="22-Oct-2026 11:30:00",
                   Modified_Time="22-Oct-2026 11:30:00")
        incoming["Plan_Report"].append(row)
    with pytest.raises(ValueError, match="Ambiguous patient plan"):
        apply_reports(flow, incoming, START, window={"from": FIRST, "to": SECOND})
    assert checkpoint(flow) == FIRST
    with flow.sessions() as session:
        cached = json.loads(session.get(FlowSetting, "zoho_test_source").value)
        assert len(cached["Plan_Report"]) == 8


def test_tied_obsolete_plans_do_not_block_distinct_latest_plan(reports):
    old = reports["Plan_Report"][0]
    tied = dict(old, ID="900003")
    newest = dict(old, ID="900004", Modified_Time="22-Oct-2026 11:30:00")
    reports["Plan_Report"] = [old, tied, newest]
    assert len(convert_reports(reports, START)["patients"]) == 1


@pytest.mark.parametrize("new_source_row", [False, True])
def test_latest_plan_adds_y_and_preserves_completed_x(flow, reports, new_source_row):
    initial(flow, reports)
    target = date(2026, 10, 23)
    flow.generate(target)
    flow.publish(target)
    flow.drain()
    offer = next(item for item in flow.state(target)["offers"] if item["patient"] == "TEST-B")
    flow.simulate(offer["token"], "YES")
    flow.drain()
    appointment_id, = offer["appointment_ids"]
    flow.attendance(appointment_id, "COMPLETED")
    with flow.sessions() as session:
        appointment = session.get(Appointment, appointment_id)
        patient_id, requirement_id = appointment.patient_id, appointment.treatment_requirement_id
    row = deepcopy(next(row for row in reports["Plan_Report"] if row["RegistrationID"] == "TEST-B"))
    row["Modified_Time"] = "22-Oct-2026 11:30:00"
    row["Schedule"] += "; IEP Session, 2, 3, TEST CONSULTANT I"
    if new_source_row:
        row.update(ID="900003", Added_Time="22-Oct-2026 11:30:00")
    incoming = delta()
    incoming["Plan_Report"] = [row]
    result = apply_reports(flow, incoming, START, window={"from": FIRST, "to": SECOND})
    assert (result["patients"], result["requirements"], result["weekly_sessions"]) == (8, 11, 23)
    with flow.sessions() as session:
        appointment = session.get(Appointment, appointment_id)
        assert appointment.patient_id == patient_id
        assert appointment.treatment_requirement_id == requirement_id
        assert appointment.status.value == "COMPLETED"
        assert session.get(TreatmentRequirement, requirement_id).active
    progress = {item["therapy"]: item for item in flow.state(target)["progress"] if item["patient"] == "TEST-B"}
    assert progress["Psychology Therapy Session"]["completed"] == 1
    assert progress["Psychology Therapy Session"]["remaining"] == 1
    assert progress["IEP Session"]["completed"] == 0
    assert progress["IEP Session"]["remaining"] == 2


def test_first_source_accepts_one_patient_arbitrary_id_and_consultant_name(reports):
    only = new_records(reports)
    scenario = convert_reports(only, START)
    assert len(scenario["patients"]) == len(scenario["therapists"]) == 1
    assert scenario["patients"][0]["code"] == "REG-NEW-009"


def test_new_source_number_and_phone_edit_survive_restart_without_local_entries(tmp_path, reports):
    url = f"sqlite:///{(tmp_path / 'live.sqlite').as_posix()}"
    flow = FlowTest(url, live=True, recipients={}, send=lambda *_: "fake")
    try:
        initial(flow, reports)
        incoming = new_records(reports)
        incoming["Plan_Report"][0]["Parent_No"] = "918888888888"
        apply_reports(flow, incoming, START, window={"from": FIRST, "to": SECOND})
        assert flow.recipients["REG-NEW-009"] == "918888888888"
    finally:
        flow.engine.dispose()
    restarted = FlowTest(url, live=True, recipients={}, send=lambda *_: "fake")
    try:
        assert restarted.recipients["REG-NEW-009"] == "918888888888"
        changed = new_records(reports)
        changed["Plan_Report"][0].update(Parent_No="917777777777", Modified_Time="22-Oct-2026 12:30:00")
        changed["All_Consultants"] = []
        apply_reports(restarted, changed, START, window={"from": SECOND, "to": THIRD})
        assert checkpoint(restarted) == THIRD
        assert restarted.recipients["REG-NEW-009"] == "917777777777"
    finally:
        restarted.engine.dispose()


def test_api_exposes_and_commits_checkpoint(flow, reports, monkeypatch):
    instance = SimpleNamespace(flow=flow, token="test-key", previews={})
    monkeypatch.setattr(mount, "runtime", lambda: instance)
    assert json.loads(request("/test/api/zoho/status")[1])["checkpoint"] is None
    status, raw = request("/test/api/zoho/import", {"reports": reports, "planStart": START.isoformat(),
                                                 "sourceTimezones": SOURCE_TIMEZONES,
                                                 "syncWindow": {"from": None, "to": FIRST}})
    assert status == 200 and json.loads(raw)["checkpoint"] == FIRST
    assert json.loads(request("/test/api/zoho/status")[1])["checkpoint"] == FIRST


def test_criteria_uses_ist_for_both_timestamps_across_midnight():
    criteria = window_criteria("2026-10-21T21:30:00Z", "2026-10-22T21:30:00Z")
    assert "Added_Time >= '22-Oct-2026 03:00:00'" in criteria
    assert "Modified_Time >= '22-Oct-2026 03:00:00'" in criteria
    assert "Modified_Time < '23-Oct-2026 03:00:00'" in criteria
    assert "Added_Time < '23-Oct-2026 03:00:00'" in criteria


def test_live_missed_record_timestamp_is_inside_utc_window():
    criteria = window_criteria("2026-10-22T06:50:36Z", "2026-10-22T07:01:19Z")
    assert "Modified_Time >= '22-Oct-2026 12:20:36'" in criteria
    assert "Modified_Time < '22-Oct-2026 12:31:19'" in criteria
    assert "Added_Time >= '22-Oct-2026 12:20:36'" in criteria
    assert "Added_Time < '22-Oct-2026 12:31:19'" in criteria


def test_old_timezone_checkpoint_requires_reconciliation_and_preserves_data(flow, reports):
    initial(flow, reports)
    with flow.sessions.begin() as session:
        session.delete(session.get(FlowSetting, TIMEZONE_KEY))
    with pytest.raises(ValueError, match="--reconcile"):
        apply_reports(flow, delta(), START, window={"from": FIRST, "to": SECOND})
    assert checkpoint(flow) == FIRST


def test_reconcile_after_modified_time_zone_change_uses_latest_source_phone(flow, reports):
    initial(flow, reports)
    flow.generate(date(2026, 10, 23))
    current = deepcopy(reports)
    current["Plan_Report"][0].update(Modified_Time="22-Oct-2026 11:30:00", Parent_No="917777777777")
    with flow.sessions.begin() as session:
        original_ids = set(session.scalars(select(Appointment.id)))
        cache = session.get(FlowSetting, "zoho_test_source")
        old = json.loads(cache.value)
        old["Plan_Report"][0]["Modified_Time"] = "22-Oct-2026 11:30:00"
        cache.value = json.dumps(old)
        session.get(FlowSetting, TIMEZONE_KEY).value = json.dumps(
            {"Added_Time": "Asia/Kolkata", "Modified_Time": "UTC"})
    with pytest.raises(ValueError, match="--reconcile"):
        apply_reports(flow, delta(), window={"from": FIRST, "to": SECOND})
    apply_reports(flow, current, window={"from": FIRST, "to": SECOND}, full_snapshot=True)
    assert flow.recipients[current["Plan_Report"][0]["RegistrationID"]] == "917777777777"
    with flow.sessions() as session:
        assert set(session.scalars(select(Appointment.id))) == original_ids


def test_full_reconciliation_recovers_missed_record_without_resetting_bookings(flow, reports):
    initial(flow, reports)
    flow.generate(date(2026, 10, 23))
    with flow.sessions() as session:
        appointment_ids = set(session.scalars(select(Appointment.id)))
        requirement_ids = set(session.scalars(select(TreatmentRequirement.id)))
    apply_reports(flow, delta(), START, window={"from": FIRST, "to": SECOND})
    # Old timezone code advanced past the new record's source timestamp.
    with flow.sessions.begin() as session:
        session.delete(session.get(FlowSetting, TIMEZONE_KEY))
    full = deepcopy(reports)
    missed = dict(full["Plan_Report"][0], ID="900001", RegistrationID="TEST-I",
                  PatientName="Fictional Child I", Added_Time="22-Oct-2026 11:30:00",
                  Modified_Time="22-Oct-2026 11:30:00")
    full["Plan_Report"].append(missed)
    result = apply_reports(flow, full, START, window={"from": SECOND, "to": THIRD}, full_snapshot=True)
    assert (result["patients"], result["consultants"], result["requirements"], result["weekly_sessions"]) == (9, 2, 11, 23)
    assert checkpoint(flow) == THIRD
    with flow.sessions() as session:
        assert set(session.scalars(select(Appointment.id))) == appointment_ids
        assert requirement_ids < set(session.scalars(select(TreatmentRequirement.id)))
        assert json.loads(session.get(FlowSetting, TIMEZONE_KEY).value) == SOURCE_TIMEZONES


@pytest.mark.parametrize("source_zone", [None, "UTC", "Asia/Kolkata",
                                         {"Added_Time": "UTC", "Modified_Time": "UTC"},
                                         {"Added_Time": "Asia/Kolkata", "Modified_Time": "UTC"}])
def test_api_rejects_old_or_mismatched_timezone_client(flow, reports, monkeypatch, source_zone):
    initial(flow, reports)
    instance = SimpleNamespace(flow=flow, token="test-key", previews={})
    monkeypatch.setattr(mount, "runtime", lambda: instance)
    body = {"reports": delta(), "planStart": START.isoformat(),
            "syncWindow": {"from": FIRST, "to": SECOND}}
    if source_zone:
        body["sourceTimezones" if isinstance(source_zone, dict) else "sourceTimezone"] = source_zone
    status, raw = request("/test/api/zoho/import", body)
    assert status == 409 and "timezone mismatch" in json.loads(raw)["error"]
    assert checkpoint(flow) == FIRST


@pytest.mark.parametrize("added,modified,accepted", [
    ("22-Oct-2026 11:00:00", "22-Oct-2026 11:00:00", True),
    ("22-Oct-2026 11:59:59", "22-Oct-2026 11:59:59", True),
    ("22-Oct-2026 12:00:00", "22-Oct-2026 12:00:00", False),
    ("22-Oct-2026 10:59:59", "22-Oct-2026 10:59:59", False),
])
def test_ist_source_zones_have_inclusive_lower_exclusive_upper_boundary(flow, reports, added, modified, accepted):
    initial(flow, reports)
    incoming = new_records(reports)
    for rows in incoming.values():
        for row in rows:
            row.update(Added_Time=added, Modified_Time=modified)
    if accepted:
        assert apply_reports(flow, incoming, window={"from": FIRST, "to": SECOND})["patients"] == 9
        assert checkpoint(flow) == SECOND
    else:
        with pytest.raises(ValueError, match="outside"):
            apply_reports(flow, incoming, window={"from": FIRST, "to": SECOND})
        assert checkpoint(flow) == FIRST


def test_modified_before_added_compares_instants_and_rolls_back(flow, reports):
    initial(flow, reports)
    incoming = new_records(reports)
    incoming["Plan_Report"][0].update(Added_Time="22-Oct-2026 11:30:00", Modified_Time="22-Oct-2026 11:29:59")
    with pytest.raises(ValueError, match="precede"):
        apply_reports(flow, incoming, window={"from": FIRST, "to": SECOND})
    assert checkpoint(flow) == FIRST


def test_utc_cache_migration_retains_missing_rows_dates_and_bookings(flow, reports, monkeypatch):
    # A legacy UTC Added Time at 18:40 is 00:10 IST on the following day.
    retained = reports["Plan_Report"][0]
    retained.update(Added_Time="22-Oct-2026 00:10:00", Modified_Time="22-Oct-2026 00:10:00")
    initial(flow, reports)
    flow.generate(date(2026, 10, 23))
    with flow.sessions.begin() as session:
        bookings = [(a.id, a.patient_id, a.treatment_requirement_id, a.status)
                    for a in session.scalars(select(Appointment))]
        cache = session.get(FlowSetting, "zoho_test_source")
        legacy = json.loads(cache.value)
        for rows in legacy.values():
            for row in rows:
                for field in ("Added_Time", "Modified_Time"):
                    row[field] = datetime.strptime(row[field], "%d-%b-%Y %H:%M:%S").replace(
                        tzinfo=ZoneInfo("Asia/Kolkata")).astimezone(timezone.utc).strftime("%d-%b-%Y %H:%M:%S")
        cache.value = json.dumps(legacy)
        session.get(FlowSetting, TIMEZONE_KEY).value = json.dumps("UTC")
    instance = SimpleNamespace(flow=flow, token="test-key", previews={})
    monkeypatch.setattr(mount, "runtime", lambda: instance)
    assert json.loads(request("/test/api/zoho/status")[1])["reconciliationRequired"]
    with pytest.raises(ValueError, match="--reconcile"):
        apply_reports(flow, delta(), window={"from": FIRST, "to": SECOND})
    assert checkpoint(flow) == FIRST
    full = deepcopy(reports)
    full["Plan_Report"] = [row for row in full["Plan_Report"] if row["ID"] != retained["ID"]]
    apply_reports(flow, full, window={"from": FIRST, "to": SECOND}, full_snapshot=True)
    with flow.sessions() as session:
        assert [(a.id, a.patient_id, a.treatment_requirement_id, a.status)
                for a in session.scalars(select(Appointment))] == bookings
        saved = json.loads(session.get(FlowSetting, "zoho_test_source").value)
        assert next(r for r in saved["Plan_Report"] if r["ID"] == retained["ID"])["Added_Time"] == "22-Oct-2026 00:10:00"
        patient = session.scalar(select(Patient).where(Patient.patient_code == retained["RegistrationID"]))
        requirement = session.scalar(select(TreatmentRequirement).where(
            TreatmentRequirement.patient_id == patient.id, TreatmentRequirement.active.is_(True)))
        assert requirement.plan_start_date == date(2026, 10, 22)
        assert json.loads(session.get(FlowSetting, TIMEZONE_KEY).value) == SOURCE_TIMEZONES
    assert not json.loads(request("/test/api/zoho/status")[1])["reconciliationRequired"]
    assert not apply_reports(flow, delta(), window={"from": SECOND, "to": THIRD})["changed"]
