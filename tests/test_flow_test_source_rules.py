"""Source preprocessing, multiple branches, plan targets and retained history."""

from copy import deepcopy
from datetime import date
import json
from pathlib import Path

import pytest
from sqlalchemy import select

from database.models import Appointment, Location, Patient, Therapist, TherapistAvailability, TreatmentRequirement
from flow_test.models import FlowInbox
from flow_test.service import FlowTest
from flow_test.zoho_import import apply_reports, convert_reports
from scheduling.slot_config import SLOTS


@pytest.fixture
def reports():
    return json.loads((Path(__file__).parent / "fixtures/zoho_test_reports.json").read_text(encoding="utf-8"))


@pytest.fixture
def flow(tmp_path):
    item = FlowTest(f"sqlite:///{(tmp_path / 'source-rules.sqlite').as_posix()}")
    yield item
    item.engine.dispose()


def test_each_plan_starts_on_added_time_india_date_not_modified_or_cli_date(reports):
    rows = reports["Plan_Report"]
    rows[0].update(Added_Time="21-Oct-2026 20:00:00", Modified_Time="23-Oct-2026 11:00:00")
    rows[1].update(Added_Time="22-Oct-2026 01:00:00", Modified_Time="24-Oct-2026 11:00:00")
    people = {p["code"]: p for p in convert_reports(reports, date(2030, 1, 1))["patients"]}
    assert people["TEST-H"]["requirements"][0]["plan_start_date"] == "2026-10-21"
    assert people["TEST-G"]["requirements"][0]["plan_start_date"] == "2026-10-22"
    assert people["TEST-A"]["requirements"][0]["plan_start_date"] == "2026-10-21"


def test_preprocessing_handles_spaces_tabs_unicode_and_case_without_new_identities(flow, reports):
    apply_reports(flow, reports)
    edited = deepcopy(reports)
    for row in edited["Plan_Report"]:
        row["Branch"] = " \ufeffmalaparamba\u00a0  EIC\t"
        row["RegistrationID"] = "\u200b " + row["RegistrationID"] + " \n"
        row["Parent_No"] = " +91 (99999) 99999 "
        row["Schedule"] = row["Schedule"].replace("TEST CONSULTANT", "test\t consultant").replace(",", "，")
    for row in edited["All_Consultants"]:
        row["Branch"] = " Malaparamba\t EIC "
        row["ConsultationType"] = row["ConsultationType"].upper()
        row["Name"] = "\t " + row["Name"] + " \ufeff"
        row["Status"] = " ACTIVE \n"
    apply_reports(flow, edited)
    with flow.sessions() as session:
        assert len(list(session.scalars(select(Patient)))) == 8
        assert len(list(session.scalars(select(TreatmentRequirement).where(TreatmentRequirement.active.is_(True))))) == 10
        assert len(list(session.scalars(select(Location)))) == 1


def test_new_branch_with_same_consultant_name_is_separate_and_survives_reset(flow, reports):
    doctor = dict(reports["All_Consultants"][1], ID="900101", Branch="Another EIC",
                  ConsultationType="PSYCHOLOGY THERAPY SESSION")
    child = dict(reports["Plan_Report"][0], ID="900102", RegistrationID="OTHER-1", Branch=" Another EIC ")
    reports["All_Consultants"].append(doctor)
    reports["Plan_Report"].append(child)
    result = apply_reports(flow, reports)
    assert (result["patients"], result["consultants"], result["branches"]) == (9, 3, 2)
    with flow.sessions() as session:
        requirement = session.scalar(select(TreatmentRequirement).join(Patient).where(Patient.patient_code == "OTHER-1"))
        location = session.get(Location, requirement.location_id)
        assert location.name == "Another EIC — FICTIONAL TEST" and location.capacity == 1
        assert session.get(Therapist, requirement.assigned_therapist_id).location_id == location.id
        assert session.scalar(select(Location).where(Location.name.like("Malaparamba%"))).capacity == 2
    flow.reset_test_data("RESET preview")
    with flow.sessions() as session:
        assert len(list(session.scalars(select(Location)))) == 2
    apply_reports(flow, reports)
    flow.generate(date(2026, 10, 23))
    flow.publish(date(2026, 10, 23))
    flow.drain()
    offer = next(o for o in flow.state(date(2026, 10, 23))["offers"] if o["patient"] == "OTHER-1")
    assert "Another EIC" in offer["message_preview"]


def test_three_active_consultants_allow_three_simultaneous_sessions(flow, reports):
    base_doctor = reports["All_Consultants"][1]
    doctors = [dict(base_doctor, ID=str(910000 + i), Name=f"Consultant {i}") for i in range(3)]
    children = [dict(reports["Plan_Report"][0], ID=str(920000 + i), RegistrationID=f"PATIENT-{i}",
                     Schedule=f"Psychology Therapy Session, 1, 3, Consultant {i}") for i in range(3)]
    apply_reports(flow, {"Plan_Report": children, "All_Consultants": doctors})
    target = date(2026, 10, 23)
    with flow.sessions.begin() as session:
        for doctor in session.scalars(select(Therapist).where(Therapist.active.is_(True))):
            session.add_all(TherapistAvailability(therapist_id=doctor.id, day_of_week=target.weekday(),
                                                slot_index=slot.slot_index, available=False)
                            for slot in SLOTS if slot.slot_index != 0)
    flow.generate(target)
    with flow.sessions() as session:
        items = list(session.scalars(select(Appointment)))
        assert len(items) == 3 and {a.slot_index for a in items} == {0}
        assert len({a.therapist_id for a in items}) == 3
        assert session.get(Location, items[0].location_id).capacity == 3


def test_increased_target_preserves_two_bookings_and_adds_one_on_next_run(flow, reports):
    child = dict(reports["Plan_Report"][0], Schedule="Psychology Therapy Session, 2, 3, TEST CONSULTANT P")
    reports["Plan_Report"] = [child]
    apply_reports(flow, reports)
    for target in (date(2026, 10, 21), date(2026, 10, 22)):
        assert flow.generate(target)["added"] == 1
    with flow.sessions() as session:
        before = [(a.id, a.slot_index, a.status, a.treatment_requirement_id) for a in session.scalars(select(Appointment))]
    child["Schedule"] = child["Schedule"].replace(", 2,", ", 3,")
    result = apply_reports(flow, reports)
    assert len(result["warnings"]) == 1
    assert flow.generate(date(2026, 10, 23))["added"] == 1
    assert flow.generate(date(2026, 10, 24))["added"] == 0
    with flow.sessions() as session:
        for saved_id, slot, status, requirement_id in before:
            item = session.get(Appointment, saved_id)
            assert (item.slot_index, item.status, item.treatment_requirement_id) == (slot, status, requirement_id)
        assert session.get(TreatmentRequirement, before[0][3]).sessions_per_week == 3
        assert len(list(session.scalars(select(FlowInbox)))) == 1
    assert not apply_reports(flow, reports)["warnings"]


def test_reduced_target_keeps_bookings_but_generates_no_excess(flow, reports):
    child = dict(reports["Plan_Report"][0], Schedule="Psychology Therapy Session, 2, 3, TEST CONSULTANT P")
    reports["Plan_Report"] = [child]
    apply_reports(flow, reports)
    flow.generate(date(2026, 10, 21))
    flow.generate(date(2026, 10, 22))
    child["Schedule"] = child["Schedule"].replace(", 2,", ", 1,")
    assert apply_reports(flow, reports)["warnings"]
    assert flow.generate(date(2026, 10, 23))["added"] == 0
    progress = flow.state(date(2026, 10, 23))["progress"][0]
    assert (progress["target"], progress["booked"], progress["remaining"]) == (1, 2, 0)


def test_removed_therapy_stops_new_demand_without_changing_saved_visit(flow, reports):
    reports["Plan_Report"] = [next(r for r in reports["Plan_Report"] if r["RegistrationID"] == "TEST-A")]
    apply_reports(flow, reports)
    flow.generate(date(2026, 10, 23))
    with flow.sessions() as session:
        original = [(a.id, a.treatment_requirement_id, a.status) for a in session.scalars(select(Appointment))]
    reports["Plan_Report"][0]["Schedule"] = "Psychology Therapy Session, 3, 3, TEST CONSULTANT P"
    result = apply_reports(flow, reports)
    assert "removed" in result["warnings"][0]["message"]
    assert flow.generate(date(2026, 10, 24))["added"] == 1
    with flow.sessions() as session:
        for saved_id, requirement_id, status in original:
            assert session.get(Appointment, saved_id).status == status
            assert session.get(Appointment, saved_id).treatment_requirement_id == requirement_id
        assert sorted(session.scalars(select(TreatmentRequirement.sessions_per_week).where(
            TreatmentRequirement.patient_id == session.get(Appointment, original[0][0]).patient_id,
            TreatmentRequirement.active.is_(True)))) == [0, 3]


def test_doctor_change_with_bookings_warns_and_imports_other_records(flow, reports):
    apply_reports(flow, reports)
    flow.generate(date(2026, 10, 23))
    doctor = dict(reports["All_Consultants"][1], ID="930001", Name="New Doctor")
    reports["All_Consultants"].append(doctor)
    reports["Plan_Report"][0]["Schedule"] = "Psychology Therapy Session, 2, 3, New Doctor"
    reports["Plan_Report"][1]["PatientName"] = "Updated Name"
    result = apply_reports(flow, reports)
    assert "staff review" in result["warnings"][0]["message"]
    with flow.sessions() as session:
        assert session.scalar(select(Patient.name).where(Patient.patient_code == "TEST-G")) == "Updated Name"
    assert not any(flow.preview_daily(date(2026, 10, 24))[0].validation.values())


@pytest.mark.parametrize("schedule", [";\n;", "Psychology Therapy Session, 2, 999999, TEST CONSULTANT P"])
def test_empty_entries_and_unrepresentable_dates_fail_without_erasing_data(flow, reports, schedule):
    apply_reports(flow, reports)
    before = deepcopy(flow.scenario)
    reports["Plan_Report"][0]["Schedule"] = schedule
    with pytest.raises(ValueError, match="Schedule entries|calendar duration"):
        apply_reports(flow, reports)
    assert flow.scenario == before


def test_five_long_therapy_names_fit_one_message_without_omission(flow, reports):
    doctors, schedules = [], []
    labels = []
    for i in range(5):
        therapy = f"Therapy {i} " + "x" * 110
        labels.append(therapy)
        doctors.append(dict(reports["All_Consultants"][0], ID=str(980000+i),
                            Name=f"Consultant {i}", ConsultationType=therapy))
        schedules.append(f"{therapy}, 1, 3, Consultant {i}")
    child = dict(reports["Plan_Report"][0], Schedule="; ".join(schedules), PatientName="x" * 120)
    apply_reports(flow, {"Plan_Report": [child], "All_Consultants": doctors})
    flow.generate(date(2026, 10, 23))
    assert flow.publish(date(2026, 10, 23)) == 1
    message = flow.state(date(2026, 10, 23))["offers"][0]["message_preview"]
    assert len(message) <= 1024
    assert all(label in message for label in labels)
    assert child["RegistrationID"] in message and "whole day" in message
