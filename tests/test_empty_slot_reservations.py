from contextlib import nullcontext
from dataclasses import replace

import pytest
from sqlalchemy import func, select

import web_app
from database.models import (
    AdminSlotReservation, Appointment, AppointmentStatus, Department, Location,
    Patient, TreatmentRequirement,
)
from importers.parent_response_demo_importer import load_parent_response_demo
from services.admin_reservation_service import (
    EmptySlot, preview_admin_reservation, preview_empty_slot, reserve_admin_slot,
)
from services.parent_response_service import weekly_progress


@pytest.fixture
def empty_booking(db_session):
    target = load_parent_response_demo(db_session)
    patient = db_session.scalar(select(Patient).where(Patient.patient_code == "DEMO-D"))
    plan = db_session.scalar(select(TreatmentRequirement).where(
        TreatmentRequirement.patient_id == patient.id,
    ))
    return patient, EmptySlot(target, 4, plan.assigned_therapist_id, plan.department_id, plan.location_id)


def book(session, patient, opening, preview, request_id="empty-cell", override=False):
    result = reserve_admin_slot(
        session, source_id=None, opening=opening, patient_id=patient.id,
        whole_visit=False, request_id=request_id, preview_token=preview["preview_token"],
        override_warnings=override,
    )
    session.commit()
    return result


def test_empty_preview_writes_nothing_and_confirm_saves_exact_cell(db_session, empty_booking):
    patient, opening = empty_booking
    before = {row.id: (row.patient_id, row.status, row.slot_index) for row in db_session.scalars(select(Appointment))}
    preview = preview_empty_slot(db_session, opening, patient.id)
    assert not preview["warnings"]
    assert db_session.scalar(select(func.count(Appointment.id))) == len(before)
    result = book(db_session, patient, opening, preview)
    booking = db_session.get(Appointment, result["appointment_ids"][0])
    assert (booking.date, booking.slot_index, booking.therapist_id) == (
        opening.date, opening.slot_index, opening.therapist_id,
    )
    assert booking.status == AppointmentStatus.CONFIRMED
    assert booking.source == "admin_without_messaging"
    assert db_session.get(AdminSlotReservation, booking.id).appointment_id == booking.id
    assert book(db_session, patient, opening, preview) == result
    assert db_session.scalar(select(func.count(Appointment.id))) == len(before) + 1
    for identifier, snapshot in before.items():
        row = db_session.get(Appointment, identifier)
        assert (row.patient_id, row.status, row.slot_index) == snapshot


def test_empty_booking_requires_explicit_override_for_plan_mismatch(db_session, empty_booking):
    _, opening = empty_booking
    patient = db_session.scalar(select(Patient).where(Patient.patient_code == "DEMO-F"))
    preview = preview_empty_slot(db_session, opening, patient.id)
    assert "THERAPIST_MISMATCH" in {row["code"] for row in preview["warnings"]}
    with pytest.raises(ValueError, match="Reserve anyway"):
        book(db_session, patient, opening, preview)
    assert book(db_session, patient, opening, preview, override=True)["appointment_ids"]


def test_filtered_out_proposals_are_warned_about_and_never_cancelled(db_session, empty_booking):
    patient, opening = empty_booking
    opening = replace(opening, slot_index=0)
    existing = db_session.scalar(select(Appointment).where(
        Appointment.date == opening.date, Appointment.slot_index == 0,
    ))
    existing.status = AppointmentStatus.PROPOSED
    db_session.commit()
    preview = preview_empty_slot(db_session, opening, patient.id)
    assert "THERAPIST_CONFLICT" in {row["code"] for row in preview["warnings"]}
    assert preview["superseded_ids"] == []
    book(db_session, patient, opening, preview, override=True)
    assert existing.status == AppointmentStatus.PROPOSED


def test_booking_that_fills_cell_invalidates_an_older_preview(db_session, empty_booking):
    patient, opening = empty_booking
    preview = preview_empty_slot(db_session, opening, patient.id)
    book(db_session, patient, opening, preview)
    with pytest.raises(ValueError, match="details changed"):
        book(db_session, patient, opening, preview, request_id="second-admin", override=True)


def test_empty_booking_without_plan_does_not_create_weekly_demand(db_session, empty_booking):
    _, opening = empty_booking
    patient = Patient(patient_code="EMPTY-NO-PLAN", name="Test patient", active=True)
    db_session.add(patient)
    db_session.commit()
    preview = preview_empty_slot(db_session, opening, patient.id)
    assert "NO_ACTIVE_PLAN" in {row["code"] for row in preview["warnings"]}
    result = book(db_session, patient, opening, preview, override=True)
    assert not db_session.get(AdminSlotReservation, result["appointment_ids"][0]).counts_toward_requirement
    assert not any(row.patient_id == patient.id for row in weekly_progress(db_session, opening.date))


def test_invalid_slot_and_doctor_are_input_errors(db_session, empty_booking):
    patient, opening = empty_booking
    with pytest.raises(ValueError, match="valid session"):
        preview_empty_slot(db_session, replace(opening, slot_index=99), patient.id)
    with pytest.raises(ValueError, match="existing doctor"):
        preview_empty_slot(db_session, replace(opening, therapist_id=99999), patient.id)


def test_main_ui_empty_slot_api_previews_and_saves(db_session, empty_booking, monkeypatch):
    patient, opening = empty_booking
    monkeypatch.setattr(web_app, "SessionLocal", lambda: nullcontext(db_session))

    class Handler:
        def send_json(self, payload):
            self.payload = payload

    handler = Handler()
    body = {
        "patientId": patient.id, "therapistId": opening.therapist_id,
        "departmentId": opening.department_id, "locationId": opening.location_id,
        "date": opening.date.isoformat(), "slot": opening.slot_index,
    }
    web_app.TherapySchedulerHandler.empty_slot_reservation(handler, body, False)
    preview = handler.payload
    web_app.TherapySchedulerHandler.empty_slot_reservation(handler, {
        **body, "previewToken": preview["preview_token"], "requestId": "main-empty",
    }, True)
    booking_id = handler.payload["appointment_ids"][0]
    assert booking_id in {row["id"] for row in web_app.schedule_payload(opening.date, 1)["appointments"]}
    with pytest.raises(web_app.ApiError, match="valid session time"):
        web_app.TherapySchedulerHandler.empty_slot_reservation(handler, {**body, "slot": True}, False)
    with pytest.raises(web_app.ApiError, match="appointment date"):
        web_app.TherapySchedulerHandler.empty_slot_reservation(handler, {**body, "date": []}, False)


def test_empty_booking_messaging_option_never_saves(db_session, empty_booking):
    patient, opening = empty_booking
    preview = preview_empty_slot(db_session, opening, patient.id)
    with pytest.raises(ValueError, match="display-only"):
        reserve_admin_slot(
            db_session, source_id=None, opening=opening, patient_id=patient.id,
            request_id="no-send", preview_token=preview["preview_token"], mode="with_messaging",
        )
    assert db_session.scalar(select(func.count(AdminSlotReservation.appointment_id))) == 0


def test_new_calendar_booking_can_later_be_cancelled_and_replaced(db_session, empty_booking, monkeypatch):
    patient, opening = empty_booking
    first = book(db_session, patient, opening, preview_empty_slot(db_session, opening, patient.id))
    source_id = first["appointment_ids"][0]
    db_session.get(Appointment, source_id).status = AppointmentStatus.CANCELLED_PATIENT
    db_session.commit()
    monkeypatch.setattr(web_app, "SessionLocal", lambda: nullcontext(db_session))
    assert source_id in {row["id"] for row in web_app.operations_payload(opening.date)["needsRescheduling"]}
    preview = preview_admin_reservation(db_session, source_id, patient.id)
    result = reserve_admin_slot(
        db_session, source_id=source_id, patient_id=patient.id,
        request_id="replace-new-booking", preview_token=preview["preview_token"],
    )
    db_session.commit()
    assert db_session.get(AdminSlotReservation, source_id).appointment_id == result["appointment_ids"][0]
    assert source_id not in {row["id"] for row in web_app.operations_payload(opening.date)["needsRescheduling"]}


def test_empty_booking_warns_for_doctor_capability_and_branch_mismatches(db_session, empty_booking):
    patient, opening = empty_booking
    therapy = Department(name="Different therapy", active=True)
    branch = Location(name="Different branch", capacity=10, active=True)
    db_session.add_all([therapy, branch])
    db_session.commit()
    opening = replace(opening, department_id=therapy.id, location_id=branch.id)
    preview = preview_empty_slot(db_session, opening, patient.id)
    assert {"THERAPIST_CAPABILITY", "THERAPIST_BRANCH", "THERAPY_NOT_IN_PLAN"} <= {
        row["code"] for row in preview["warnings"]
    }
    booking_id = book(db_session, patient, opening, preview, override=True)["appointment_ids"][0]
    assert db_session.get(Appointment, booking_id).location_id == branch.id
