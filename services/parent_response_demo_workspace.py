"""Isolated, repeatable database runs for the local response-demo screen."""

from __future__ import annotations

from dataclasses import asdict
from copy import deepcopy
from datetime import date
import json
from pathlib import Path
from threading import Lock, Thread
from time import monotonic
from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from config import RESPONSE_DEMO_SOLVER_SETTINGS
from database.db import Base
from database.models import (
    AdminBookingDecision, AdminSlotReservation, Appointment, AppointmentStatus,
    Department, Location, ParentAvailabilityResponse,
    Patient, Therapist, TreatmentRequirement,
)
from database.repository import load_scheduling_problem
from importers.parent_response_demo_importer import LARGE_FIXTURE, load_parent_response_demo
from scheduling.schedule_validator import validate_schedule
from scheduling.slot_config import SLOTS
from services.parent_response_service import process_daily_responses, weekly_progress
from services.admin_reservation_service import preview_admin_reservation, reserve_admin_slot


def demo_snapshot(session: Session, target_date: date) -> dict:
    """Serialize fictional inputs, appointments, and weekly counts for display."""
    patients = {item.id: item for item in session.scalars(select(Patient))}
    therapists = {item.id: item for item in session.scalars(select(Therapist))}
    departments = {item.id: item for item in session.scalars(select(Department))}
    branches = {item.id: item for item in session.scalars(select(Location))}
    requirements = {item.id: item for item in session.scalars(select(TreatmentRequirement))}
    appointments = list(session.scalars(select(Appointment).order_by(
        Appointment.date, Appointment.slot_index, Appointment.therapist_id,
    )))
    appointment_map = {item.id: item for item in appointments}
    replies = []
    group_replies = {}
    for item in session.scalars(select(ParentAvailabilityResponse).order_by(ParentAvailabilityResponse.event_id)):
        appointment = appointment_map[item.appointment_id]
        group_replies[(appointment.patient_id, appointment.bundle_id, appointment.date)] = item.reply
        replies.append({
            "event_id": item.event_id, "patient": patients[appointment.patient_id].patient_code,
            "name": patients[appointment.patient_id].name, "appointment_id": item.appointment_id,
            "reply": item.reply, "scope": item.unavailable_scope,
            "time": SLOTS[item.appointment_slot].start_time.strftime("%H:%M"),
            "processed": item.processed_at is not None,
        })
    rows = [{
        "id": item.id, "patient_id": item.patient_id,
        "patient": patients[item.patient_id].patient_code,
        "name": patients[item.patient_id].name,
        "requirement_id": item.treatment_requirement_id,
        "therapist_id": item.therapist_id, "therapist": therapists[item.therapist_id].name,
        "branch": branches[item.location_id].name,
        "therapy": departments[item.department_id].name,
        "date": item.date.isoformat(), "slot": item.slot_index,
        "time": SLOTS[item.slot_index].start_time.strftime("%H:%M"),
        "end_time": SLOTS[item.slot_index].end_time.strftime("%H:%M"),
        "status": item.status.value, "bundle": item.bundle_id,
        "reply": group_replies.get((item.patient_id, item.bundle_id, item.date), "NO_REPLY"),
    } for item in appointments]
    progress = [{
        **asdict(item), "patient": patients[item.patient_id].patient_code,
        "name": patients[item.patient_id].name,
        "therapy": departments[requirements[item.requirement_id].department_id].name,
    } for item in weekly_progress(session, target_date)]
    target_rows = [item for item in rows if item["date"] == target_date.isoformat()]
    visit_count = len({(item["patient"], item["bundle"]) for item in target_rows})
    return {
        "appointments": rows, "responses": replies, "weekly": progress,
        "patients": [{
            "id": item.id, "code": item.patient_code, "name": item.name,
            "active": item.active, "scenario": item.notes,
        } for item in patients.values()],
        "summary": {
            "patients": len(patients), "therapists": len(therapists),
            "branches": len(branches), "therapy_requirements": len(requirements),
            "appointments": len(rows), "day_sessions": len(target_rows),
            "day_visits": visit_count, "replies": len(replies),
            "yes": sum(item["reply"] == "YES" for item in replies),
            "no": sum(item["reply"] == "NO" for item in replies),
            "no_reply": visit_count - len(replies),
        },
    }


class ResponseDemoWorkspace:
    """Own a synthetic SQLite database and run the real repair service in a worker."""

    def __init__(self, output_dir: Path, fixture_path: Path = LARGE_FIXTURE):
        self.output_dir = output_dir.resolve()
        self.fixture_path = fixture_path
        self.lock = Lock()
        self.engine = None
        self.worker = None
        self.status = "ready"
        self.reset()

    def reset(self) -> None:
        """Create a new isolated DB for replay; preserve every previous run file."""
        with self.lock:
            if self.status == "running":
                raise ValueError("Wait for the current run to finish before resetting")
            self.output_dir.mkdir(parents=True, exist_ok=True)
            run_id = uuid4().hex[:12]
            database_path = self.output_dir / f"response_demo_{run_id}.sqlite"
            engine = create_engine(f"sqlite:///{database_path.as_posix()}")
            try:
                Base.metadata.create_all(engine)
                with Session(engine, expire_on_commit=False) as session:
                    target = load_parent_response_demo(session, self.fixture_path)
                    problem = load_scheduling_problem(session, target)
                    issues, _ = validate_schedule(problem, problem.pinned)
                    if issues:
                        raise ValueError("The synthetic baseline has scheduling conflicts")
                    before = demo_snapshot(session, target)
            except Exception:
                engine.dispose()
                raise
            if self.engine is not None:
                self.engine.dispose()
            self.engine, self.target_date = engine, target
            self.run_id, self.database_path = run_id, database_path
            self.before, self.after, self.outcome = before, None, None
            self.status, self.error = "ready", None
            self.started_at, self.elapsed = None, 0.0

    def state(self) -> dict:
        with self.lock:
            elapsed = monotonic() - self.started_at if self.status == "running" else self.elapsed
            return deepcopy({
                "run_id": self.run_id, "status": self.status, "error": self.error,
                "target_date": self.target_date.isoformat(), "elapsed_seconds": round(elapsed, 2),
                "database_path": str(self.database_path), "before": self.before,
                "after": self.after, "outcome": self.outcome,
            })

    def start(self) -> bool:
        """Start at most one run; completed runs must be reset before replay."""
        with self.lock:
            if self.status != "ready":
                return False
            self.status, self.started_at = "running", monotonic()
            self.worker = Thread(target=self._execute, daemon=True)
            self.worker.start()
            return True

    def _check_booking_run(self, run_id: str, source_id: int | None = None) -> None:
        if self.status != "completed" or self.run_id != run_id:
            raise ValueError("Run the scheduler first, or refresh this reset demo")
        if source_id is not None and source_id not in {
            item["original"]["id"] for item in self.outcome["changes"]
        }:
            raise ValueError("Select a released opening from this demo")

    def slot_details(self, run_id: str, source_id: int) -> dict:
        """List every patient, plus strict suggestions for this released opening."""
        with self.lock:
            self._check_booking_run(run_id)
            source = next((item["original"] for item in self.outcome["changes"]
                           if item["original"]["id"] == source_id), None)
            if source is None:
                raise ValueError("Select a released opening from this demo")
            current = self.after
            slots = [item["original"] for item in self.outcome["changes"] if (
                item["original"]["bundle"] == source["bundle"]
                and not item.get("admin_reserved")
            )]
            patients = [{
                **item, "therapies": sorted({row["therapy"] for row in current["weekly"]
                                            if row["patient"] == item["code"]}),
            } for item in current["patients"]]
            proposed_patient = next((item["replacement"]["patient_id"]
                                     for item in self.outcome["changes"]
                                     if item["original"]["id"] == source_id
                                     and item["replacement"]), None)
            suggestions = []
            with Session(self.engine) as session:
                if session.get(AdminSlotReservation, source_id):
                    raise ValueError("This opening is already reserved. Refresh the schedule.")
                candidates = set(session.scalars(select(TreatmentRequirement.patient_id).where(
                    TreatmentRequirement.department_id == session.get(Appointment, source_id).department_id,
                    TreatmentRequirement.assigned_therapist_id == source["therapist_id"],
                    TreatmentRequirement.active.is_(True),
                )))
                for item in patients:
                    if item["id"] not in candidates or not item["active"]:
                        continue
                    preview = preview_admin_reservation(session, source_id, item["id"])
                    if not preview["warnings"]:
                        suggestions.append({**item, "current_proposal": item["id"] == proposed_patient})
            suggestions.sort(key=lambda item: (not item["current_proposal"], item["code"]))
            return {"run_id": run_id, "source": source, "slots": slots,
                    "patients": patients, "suggestions": suggestions}

    def preview_booking(self, payload: dict) -> dict:
        with self.lock:
            self._check_booking_run(payload["run_id"], payload["source_id"])
            with Session(self.engine) as session:
                preview = preview_admin_reservation(
                    session, payload["source_id"], payload["patient_id"],
                    payload.get("whole_visit", True),
                )
                return {**preview, "run_id": self.run_id}

    def reserve_booking(self, payload: dict) -> dict:
        """Persist an explicit admin choice, then refresh displayed slot ownership."""
        with self.lock:
            self._check_booking_run(payload["run_id"], payload["source_id"])
            with Session(self.engine, expire_on_commit=False) as session:
                result = reserve_admin_slot(
                    session, source_id=payload["source_id"], patient_id=payload["patient_id"],
                    request_id=payload["request_id"], preview_token=payload["preview_token"],
                    whole_visit=payload.get("whole_visit", True),
                    override_warnings=payload.get("override_warnings", False),
                    mode=payload.get("mode", "without_messaging"),
                )
                session.commit()
                self.after = demo_snapshot(session, self.target_date)
                by_id = {row["id"]: row for row in self.after["appointments"]}
                reserved = {row.source_appointment_id: row for row in session.scalars(select(AdminSlotReservation))}
                for change in self.outcome["changes"]:
                    reservation = reserved.get(change["original"]["id"])
                    change["admin_reserved"] = reservation is not None
                    if reservation:
                        change["replacement"] = by_id[reservation.appointment_id]
                    elif change["replacement"]:
                        current = by_id[change["replacement"]["id"]]
                        change["replacement"] = current if current["status"] == "PROPOSED" else None
                decisions = list(session.scalars(select(AdminBookingDecision)))
                self.outcome["reserved_sessions"] = len(reserved)
                self.outcome["override_warning_count"] = sum(len(json.loads(item.warnings)) for item in decisions)
                self.outcome["filled_sessions"] = sum(item["replacement"] is not None for item in self.outcome["changes"])
                self.outcome["unfilled_slots"] = sum(item["replacement"] is None for item in self.outcome["changes"])
                self.outcome["alternatives"] = []
                self.outcome["alternatives_stale"] = True
                self.outcome["unmet"] = []
                return result

    def _execute(self) -> None:
        try:
            with Session(self.engine, expire_on_commit=False) as session:
                result = process_daily_responses(
                    session, self.target_date, settings=RESPONSE_DEMO_SOLVER_SETTINGS,
                )
                after = demo_snapshot(session, self.target_date)
                requirements = {item.id: item for item in session.scalars(select(TreatmentRequirement))}
                patients = {item.id: item for item in session.scalars(select(Patient))}
                departments = {item.id: item for item in session.scalars(select(Department))}
                therapists = {item.id: item for item in session.scalars(select(Therapist))}
                proposed_ids = set(result.proposed_appointment_ids)
                proposed = [item for item in after["appointments"] if item["id"] in proposed_ids]
                released_ids = set(result.released_appointment_ids)
                released = [item for item in self.before["appointments"] if item["id"] in released_ids]
                changes = []
                for original in released:
                    replacement = next((item for item in proposed if all(
                        item[key] == original[key]
                        for key in ("therapist_id", "therapy", "branch", "date", "slot")
                    )), None)
                    changes.append({"original": original, "replacement": replacement})
                problem = load_scheduling_problem(session, self.target_date)
                issues, validation = validate_schedule(problem, problem.pinned)
                if issues:
                    raise ValueError("The resulting demo schedule failed validation")
                outcome = {
                    "processed_replies": result.processed,
                    "confirmed_sessions": len(result.kept_appointment_ids),
                    "released_sessions": len(released),
                    "proposed_sessions": len(proposed),
                    "proposed_visits": len({item["bundle"] for item in proposed}),
                    "unfilled_slots": sum(item["replacement"] is None for item in changes),
                    "validation": validation, "changes": changes,
                    "alternatives": [{
                        "patient": patients[item.patient_id].patient_code,
                        "name": patients[item.patient_id].name,
                        "therapy": departments[item.department_id].name,
                        "therapist": therapists[item.therapist_id].name,
                        "date": item.date.isoformat(),
                        "time": SLOTS[item.slot_index].start_time.strftime("%H:%M"),
                        "bundle": item.bundle_id,
                    } for item in result.alternatives],
                    "unmet": [{
                        "patient": patients[item.patient_id].patient_code,
                        "therapy": departments[requirements[item.requirement_id].department_id].name,
                        "missing_sessions": item.missing_sessions, "reason": item.reason,
                    } for item in result.alternative_unmet],
                }
            with self.lock:
                self.after, self.outcome = after, outcome
                self.status = "completed"
                self.elapsed = monotonic() - self.started_at
        except Exception as exc:
            with self.lock:
                self.status, self.error = "failed", str(exc)
                self.elapsed = monotonic() - self.started_at
