"""Exercise the eight-child fixture and its additive four-child upgrade."""

from datetime import date

import pytest
from sqlalchemy import select

from database.models import Patient, PatientAvailability, Therapist, TherapistAvailability, TreatmentRequirement
from flow_test.service import FlowTest
from flow_test.storage import TEST_TABLES


MONDAY = date(2026, 10, 19)


def snapshot(flow):
    """Capture fictional test rows, including all message and attendance history."""
    with flow.engine.connect() as connection:
        return {
            table.name: {
                tuple(row[column.name] for column in table.primary_key): dict(row)
                for row in connection.execute(select(table)).mappings()
            }
            for table in TEST_TABLES
        }


def test_eight_children_have_no_fixed_times_and_or_plans_all_eight(tmp_path):
    flow = FlowTest(f"sqlite:///{(tmp_path / 'eight.sqlite').as_posix()}")
    try:
        with flow.sessions() as session:
            assert set(session.scalars(select(Patient.patient_code))) == {
                f"TEST-{letter}" for letter in "ABCDEFGH"
            }
            assert len(list(session.scalars(select(Therapist)))) == 2
            requirements = list(session.scalars(select(TreatmentRequirement)))
            assert len(requirements) == 10
            assert sum(r.sessions_per_week for r in requirements) == 21
            assert not list(session.scalars(select(PatientAvailability)))
            assert not list(session.scalars(select(TherapistAvailability)))
        assert all("slots" not in p for p in flow.scenario["patients"])
        assert flow.generate(MONDAY)["added"] == 10
        current = flow.state(MONDAY)
        assert {a["patient"] for a in current["appointments"]} == {
            f"TEST-{letter}" for letter in "ABCDEFGH"
        }
        assert flow.publish(MONDAY) == 8
        flow.drain()
        state = flow.state(MONDAY)
        for code in ("TEST-A", "TEST-E"):
            offer = next(o for o in state["offers"] if o["patient"] == code)
            assert len(offer["appointment_ids"]) == 2
            assert "Speech:" in offer["message_preview"]
            assert "OT:" in offer["message_preview"]
        assert all(j["status"] == "SIMULATED" for j in state["jobs"])
        assert flow.generate(MONDAY)["added"] == 0
    finally:
        flow.engine.dispose()


@pytest.mark.parametrize("declines,replacement", [("TEST-B", "TEST-D"), ("TEST-F", "TEST-H")])
def test_each_morning_block_has_a_standby_replacement(tmp_path, declines, replacement, fixed_eight_flow_scenario):
    flow = FlowTest(f"sqlite:///{(tmp_path / 'replacement.sqlite').as_posix()}",
                    scenario=fixed_eight_flow_scenario)
    try:
        flow.generate(MONDAY)
        flow.publish(MONDAY)
        flow.drain()
        before = flow.state(MONDAY)
        offer = next(o for o in before["offers"] if o["patient"] == declines)
        flow.simulate(offer["token"], "NO")
        flow.drain()
        after = flow.state(MONDAY)
        released = next(a for a in after["appointments"] if a["patient"] == declines)
        candidate = next(a for a in after["appointments"] if a["patient"] == replacement)
        assert released["status"] == "NEEDS_RESCHEDULING"
        assert candidate["status"] == "PROPOSED"
        assert candidate["time"] == released["time"]
        assert not any(o["patient"] == replacement for o in after["offers"])
        for booking in before["appointments"]:
            if booking["patient"] != declines:
                assert booking in after["appointments"]
    finally:
        flow.engine.dispose()


def test_existing_four_child_history_survives_expansion_once(tmp_path, legacy_flow_scenario):
    url = f"sqlite:///{(tmp_path / 'upgrade.sqlite').as_posix()}"
    old = FlowTest(url, scenario=legacy_flow_scenario)
    old.generate(MONDAY)
    old.publish(MONDAY)
    old.drain()
    tokens = {o["patient"]: o["token"] for o in old.state(MONDAY)["offers"]}
    for code in ("TEST-A", "TEST-C"):
        old.simulate(tokens[code], "YES")
        old.drain()
    a_speech = next(a for a in old.state(MONDAY)["appointments"]
                    if a["patient"] == "TEST-A" and a["therapy"] == "Speech")
    c = next(a for a in old.state(MONDAY)["appointments"] if a["patient"] == "TEST-C")
    old.attendance(a_speech["id"], "COMPLETED")
    old.attendance(c["id"], "NO_SHOW")
    old.simulate(tokens["TEST-B"], "NO")
    old.drain()
    before = snapshot(old)
    old.engine.dispose()

    upgraded = FlowTest(url)
    try:
        after = snapshot(upgraded)
        for table, rows in before.items():
            for key, row in rows.items():
                if table == "flow_test_settings" and row["key"] == "scenario":
                    assert after[table][key]["value"] == "malaparamba-fictional-v3"
                elif table in {"therapist_availability", "patient_availability"}:
                    assert after[table][key] == {**row, "available": True}
                else:
                    assert after[table][key] == row
        assert len(after["patients"]) == 8
        assert len(after["treatment_requirements"]) == 10
        assert len(after["therapists"]) == 2
        assert sum(row["action"] == "flow_scenario_expanded"
                   for row in after["audit_logs"].values()) == 1
    finally:
        upgraded.engine.dispose()
    restarted = FlowTest(url)
    try:
        assert snapshot(restarted) == after
        # Existing appointments remain fixed when later children are planned.
        saved = restarted.state(MONDAY)["appointments"]
        assert restarted.generate(MONDAY)["added"] >= 5
        assert sum(a["patient"] in {"TEST-E", "TEST-F", "TEST-G", "TEST-H"}
                   for a in restarted.state(MONDAY)["appointments"]) == 5
        assert all(a in restarted.state(MONDAY)["appointments"] for a in saved)
    finally:
        restarted.engine.dispose()


def test_expansion_conflicting_child_code_requires_review(tmp_path, legacy_flow_scenario):
    url = f"sqlite:///{(tmp_path / 'collision.sqlite').as_posix()}"
    old = FlowTest(url, scenario=legacy_flow_scenario)
    with old.sessions.begin() as session:
        session.add(Patient(patient_code="TEST-E", name="Existing control"))
    before = snapshot(old)
    with pytest.raises(ValueError, match="conflicts with existing"):
        FlowTest(url)
    assert snapshot(old) == before
    old.engine.dispose()


def test_expansion_failure_rolls_back_partial_patient_additions(tmp_path, legacy_flow_scenario, monkeypatch):
    url = f"sqlite:///{(tmp_path / 'failed.sqlite').as_posix()}"
    old = FlowTest(url, scenario=legacy_flow_scenario)
    before = snapshot(old)
    add = FlowTest._add_patients

    def fail_after_partial_add(self, session, rows, *args):
        add(self, session, rows[:2], *args)
        raise ValueError("Test interrupted expansion")

    monkeypatch.setattr(FlowTest, "_add_patients", fail_after_partial_add)
    with pytest.raises(ValueError, match="interrupted expansion"):
        FlowTest(url)
    assert snapshot(old) == before
    old.engine.dispose()


def test_current_scenario_restart_preserves_staff_edits(tmp_path):
    url = f"sqlite:///{(tmp_path / 'edits.sqlite').as_posix()}"
    flow = FlowTest(url)
    with flow.sessions.begin() as session:
        session.scalar(select(Patient).where(Patient.patient_code == "TEST-E")).name = "Edited fictional E"
        session.add(TherapistAvailability(
            therapist_id=1, day_of_week=0, slot_index=2, available=False,
        ))
    before = snapshot(flow)
    flow.engine.dispose()
    reopened = FlowTest(url)
    try:
        assert snapshot(reopened) == before
    finally:
        reopened.engine.dispose()


def test_existing_fixed_eight_child_scenario_upgrades_without_duplicates(tmp_path, fixed_eight_flow_scenario):
    url = f"sqlite:///{(tmp_path / 'v2-upgrade.sqlite').as_posix()}"
    previous = FlowTest(url, scenario=fixed_eight_flow_scenario)
    assert previous.generate(MONDAY)["added"] == 8
    bookings = previous.state(MONDAY)["appointments"]
    previous.engine.dispose()
    upgraded = FlowTest(url)
    try:
        assert upgraded.state(MONDAY)["appointments"] == bookings
        with upgraded.sessions() as session:
            assert len(list(session.scalars(select(Patient)))) == 8
            assert len(list(session.scalars(select(TreatmentRequirement)))) == 10
            assert not list(session.scalars(select(PatientAvailability).where(
                PatientAvailability.available.is_(False))))
            assert not list(session.scalars(select(TherapistAvailability).where(
                TherapistAvailability.available.is_(False))))
        assert upgraded.generate(MONDAY)["added"] == 2
    finally:
        upgraded.engine.dispose()
