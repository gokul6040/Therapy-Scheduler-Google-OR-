"""Local web application for the EIC therapy scheduler.

This module deliberately uses the Python standard library for HTTP serving so the
existing scheduling project can offer a modern browser UI without adding a second
application framework. Business rules remain in ``scheduling`` and ``services``.
"""

from __future__ import annotations

from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import re
from typing import Any
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from sqlalchemy import func, select

from config import SCHEDULING_WEEK_DAYS, SOLVER_SEARCH_TIME_OPTIONS, SOLVER_SETTINGS
from database.db import SessionLocal, init_db
from database.models import (
    Appointment,
    AppointmentStatus,
    Department,
    ImportWarning,
    Location,
    Patient,
    PatientAbsence,
    PatientAvailability,
    Therapist,
    TherapistAbsence,
    TreatmentRequirement,
    ZohoAppointmentSync,
    ZohoPlanDetailRecord,
)
from database.repository import load_scheduling_problem, save_proposed_schedule
from importers.initialize_source import initialize_source_database
from scheduling.cp_sat_scheduler import generate_weekly_schedule
from scheduling.slot_config import SLOTS, slot_label
from scheduling.types import ScheduleResult, SolverMode
from services.absence_service import mark_patient_absent, mark_therapist_absent
from services.appointment_service import move_appointment
from services.cancellation_service import cancel_patient_appointment
from services.replacement_service import find_pull_forward_candidates, find_replacement_candidates
from services.rolling_schedule_service import generate_rolling_schedule
from services.zoho_creator_service import (
    authorization_url,
    create_appointment_schedule,
    create_appointment_schedules,
    get_report_records,
    exchange_authorization_code,
    refresh_access_token,
    save_refresh_token,
    settings_from_environment,
)


BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"
ACTIVE_STATUSES = (
    AppointmentStatus.PROPOSED,
    AppointmentStatus.PENDING_CONFIRMATION,
    AppointmentStatus.CONFIRMED,
    AppointmentStatus.COMPLETED,
)
PREFERENCE_DAY_LABELS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
PREFERRED_AVAILABILITY_SCORE = 100
PREFERENCE_PERIODS = {
    "morning": range(0, 5),
    "afternoon": range(5, len(SLOTS)),
}
PREFERENCE_PERIOD_LABELS = {
    "morning": "Morning",
    "afternoon": "Afternoon",
}
PREVIEWS: dict[str, dict[str, Any]] = {}
ZOHO_OAUTH_STATES: dict[str, datetime] = {}
# OAuth responses are deliberately memory-only until deployment secrets are
# configured in Azure Key Vault. Never log or return these token values.
ZOHO_CREATOR_CONNECTION: dict[str, Any] = {}
UNMET_REASON_LABELS = {
    "NO_VALID_ASSIGNED_THERAPIST": "No active assigned or qualified substitute therapist",
    "PLAN_NOT_STARTED": "Therapy plan has not started",
    "PLAN_ENDED": "Therapy plan duration has ended",
    "THERAPIST_ABSENCE": "All suitable therapists are absent",
    "PATIENT_ABSENCE": "Child is marked absent",
    "PATIENT_AVAILABILITY": "Child is unavailable during all suitable openings",
    "LOCATION_TRANSITION_IMPOSSIBLE": "Therapies require incompatible locations",
    "CONSECUTIVE_BLOCK_UNAVAILABLE": "No continuous block is long enough",
    "LOCATION_CAPACITY": "Location is full",
    "THERAPIST_CAPACITY": "Suitable therapist times are already occupied",
    "PATIENT_DAILY_VISIT_LIMIT": "Child already has another visit that day",
    "PAST_PLANNING_DATE": "No remaining working day is available in this week",
    "SOLVER_SEARCH_LIMIT": "A valid opening remained when the search time ended",
    "STRICT_INFEASIBLE": "Not every request can fit without breaking a rule",
}


class ApiError(Exception):
    """A request error that should be returned as JSON."""

    def __init__(self, status: HTTPStatus, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def monday(value: date) -> date:
    """Return the Monday containing ``value``."""
    return value - timedelta(days=value.weekday())


def parse_date(value: str | None, field: str = "date") -> date:
    """Parse an ISO date for an API request."""
    if not value:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{field} is required")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, f"{field} must use YYYY-MM-DD") from exc


def parse_search_seconds(value: Any) -> float:
    """Accept only the manager-facing solver time limits."""
    if value is None:
        return SOLVER_SETTINGS.max_time_seconds
    try:
        seconds = float(value)
    except (TypeError, ValueError) as exc:
        raise ApiError(HTTPStatus.BAD_REQUEST, "Search time must be a supported number of seconds") from exc
    if seconds not in SOLVER_SEARCH_TIME_OPTIONS:
        choices = ", ".join(str(int(option)) for option in SOLVER_SEARCH_TIME_OPTIONS)
        raise ApiError(HTTPStatus.BAD_REQUEST, f"Search time must be one of: {choices} seconds")
    return seconds


def zoho_creator_status() -> dict[str, Any]:
    """Return non-secret status for the Creator appointment-sync connection."""
    settings = settings_from_environment()
    return {
        "configured": settings.configured,
        "connected": bool(ZOHO_CREATOR_CONNECTION.get("access_token")),
        "automaticReconnectConfigured": bool(settings.refresh_token),
        "target": {
            "accountOwner": settings.account_owner,
            "appLinkName": settings.app_link_name,
            "formLinkName": settings.form_link_name,
            "reportLinkName": settings.report_link_name or None,
            "planDetailReportLinkName": settings.plan_detail_report_link_name or None,
        },
        "requiredScopes": list(settings.scopes),
    }


def appointment_query(start: date, end: date):
    """Build the common joined appointment query."""
    return (
        select(
            Appointment,
            Patient.patient_code,
            Patient.name.label("patient_name"),
            Therapist.therapist_code,
            Therapist.name.label("therapist_name"),
            Department.name.label("department_name"),
            Location.name.label("location_name"),
        )
        .join(Patient, Patient.id == Appointment.patient_id)
        .join(Therapist, Therapist.id == Appointment.therapist_id)
        .join(Department, Department.id == Appointment.department_id)
        .join(Location, Location.id == Appointment.location_id)
        .where(Appointment.date >= start, Appointment.date <= end)
        .order_by(Appointment.date, Appointment.slot_index, Therapist.therapist_code)
    )


def serialize_appointment(row) -> dict[str, Any]:
    """Convert a joined appointment row into browser-ready data."""
    appointment = row[0]
    return {
        "id": appointment.id,
        "date": appointment.date.isoformat(),
        "slot": appointment.slot_index,
        "time": slot_label(appointment.slot_index),
        "patientId": appointment.patient_id,
        "patientCode": row.patient_code,
        "patientName": row.patient_name or row.patient_code,
        "therapistId": appointment.therapist_id,
        "therapistCode": row.therapist_code,
        "therapistName": row.therapist_name,
        "departmentId": appointment.department_id,
        "therapy": row.department_name,
        "locationId": appointment.location_id,
        "location": row.location_name,
        "status": appointment.status.value,
        "bundle": appointment.bundle_id,
        "source": appointment.source,
    }


def creator_status(appointment_status: AppointmentStatus) -> str:
    """Map internal states to the existing Creator pick-list values."""
    return {
        AppointmentStatus.PROPOSED: "Scheduled",
        AppointmentStatus.PENDING_CONFIRMATION: "Scheduled",
        AppointmentStatus.CONFIRMED: "Scheduled",
        AppointmentStatus.COMPLETED: "Completed",
        AppointmentStatus.CANCELLED_PATIENT: "Cancelled",
        AppointmentStatus.CANCELLED_OTHER: "Cancelled",
        AppointmentStatus.NEEDS_RESCHEDULING: "Rescheduled",
        AppointmentStatus.NO_SHOW: "Other",
    }[appointment_status]


def creator_appointment_fields(row) -> dict[str, Any]:
    """Build the flat fields required by Creator's therapy-scheduler form."""
    appointment = row[0]
    slot = SLOTS[appointment.slot_index]
    patient_code = row.patient_code
    therapy_plan = f"{patient_code} - {row.department_name} - Plan {appointment.treatment_requirement_id}"
    return {
        "Appointment_ID": appointment.id,
        "Patient": row.patient_name or patient_code,
        "Therapist": row.therapist_name,
        "Therapy_Type": row.department_name,
        "Branch": row.location_name,
        "Therapy_Plan": therapy_plan,
        "Appointment_Date": appointment.date.strftime("%d-%b-%Y"),
        "Start": slot.start_time.strftime("%H:%M:%S"),
        "End_Time": slot.end_time.strftime("%H:%M:%S"),
        "Duration_Minutes": 40,
        "Status": creator_status(appointment.status),
        "Notes": f"Generated by Therapy Scheduler. Local appointment #{appointment.id}.",
    }


def creator_plan_detail_fields(record: dict[str, Any]) -> dict[str, Any]:
    """Normalize the Plan_Detail report's field-link names into DB columns."""
    normalized = {
        re.sub(r"[^a-z0-9]", "", str(key).casefold()): value
        for key, value in record.items()
    }

    def value(name: str) -> str | None:
        item = normalized.get(re.sub(r"[^a-z0-9]", "", name.casefold()))
        if item is None:
            return None
        return str(item).strip() or None

    def parsed_date(item: str | None) -> date | None:
        if not item:
            return None
        for pattern in ("%d-%b-%Y", "%Y-%m-%d", "%m/%d/%Y"):
            try:
                return datetime.strptime(item, pattern).date()
            except ValueError:
                continue
        raise ValueError(f"Plan Detail Start Date has an invalid value: {item}")

    def parsed_datetime(item: str | None) -> datetime | None:
        if not item:
            return None
        for pattern in (
            "%d-%b-%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
        ):
            try:
                return datetime.strptime(item, pattern)
            except ValueError:
                continue
        raise ValueError(f"Plan Detail Plan Added Time has an invalid value: {item}")

    creator_record_id = value("ID")
    if not creator_record_id:
        raise ValueError("Zoho Plan Detail row is missing its Creator record ID")
    return {
        "creator_record_id": creator_record_id,
        "registration_id": value("Registration Id"),
        "name": value("Name"),
        "branch": value("Branch"),
        "therapy_type": value("Therapy Type"),
        "specialty": value("Specialty"),
        "weekly_frequency": value("Weekly Frequency"),
        "duration_months": value("Duration Months"),
        "therapist": value("Therapist"),
        "start_date": parsed_date(value("Start Date")),
        "plan_added_time": parsed_datetime(value("Plan Added Time")),
        "status": value("Status"),
    }


def replacement_group(session, appointment: Appointment, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Serialize a vacancy and its advisory same-branch candidate list."""
    department = session.get(Department, appointment.department_id)
    location = session.get(Location, appointment.location_id)
    return {
        "appointmentId": appointment.id,
        "date": appointment.date.isoformat(),
        "time": slot_label(appointment.slot_index),
        "therapy": department.name if department else str(appointment.department_id),
        "branch": location.name if location else str(appointment.location_id),
        "candidates": candidates,
    }


def serialize_preview(
    result: ScheduleResult,
    start: date,
    end: date | None = None,
) -> dict[str, Any]:
    """Serialize a solver result and enrich its references with display names."""
    with SessionLocal() as session:
        patients = {
            item.id: (item.patient_code, item.name or item.patient_code)
            for item in session.scalars(select(Patient))
        }
        therapists = {
            item.id: (item.therapist_code, item.name)
            for item in session.scalars(select(Therapist))
        }
        departments = {item.id: item.name for item in session.scalars(select(Department))}
        locations = {item.id: item.name for item in session.scalars(select(Location))}
    appointments = []
    for item in result.appointments:
        patient_code, patient_name = patients.get(item.patient_id, (str(item.patient_id), str(item.patient_id)))
        therapist_code, therapist_name = therapists.get(
            item.therapist_id, (str(item.therapist_id), str(item.therapist_id))
        )
        appointments.append({
            "id": None,
            "date": item.date.isoformat(),
            "slot": item.slot_index,
            "time": slot_label(item.slot_index),
            "patientId": item.patient_id,
            "patientCode": patient_code,
            "patientName": patient_name,
            "therapistId": item.therapist_id,
            "therapistCode": therapist_code,
            "therapistName": therapist_name,
            "departmentId": item.department_id,
            "therapy": departments.get(item.department_id, str(item.department_id)),
            "locationId": item.location_id,
            "location": locations.get(item.location_id, str(item.location_id)),
            "status": item.status,
            "bundle": item.bundle_id,
            "source": "solver-preview",
        })
    unmet = []
    for item in result.unmet:
        record = asdict(item)
        record["patientCode"] = patients.get(item.patient_id, (str(item.patient_id), ""))[0]
        record["therapy"] = departments.get(item.department_id, str(item.department_id))
        record["reasonLabel"] = UNMET_REASON_LABELS.get(
            item.reason, item.reason.replace("_", " ").title()
        )
        record["secondaryReasonLabels"] = [
            UNMET_REASON_LABELS.get(reason, reason.replace("_", " ").title())
            for reason in item.secondary_reasons
        ]
        unmet.append(record)
    return {
        "start": start.isoformat(),
        "end": (end or start + timedelta(days=SCHEDULING_WEEK_DAYS - 1)).isoformat(),
        "status": result.status,
        "requested": result.requested_sessions,
        "scheduled": result.scheduled_sessions,
        "unscheduled": sum(item.missing_sessions for item in result.unmet),
        "validation": result.validation,
        "statistics": result.statistics,
        "appointments": appointments,
        "unmet": unmet,
        "canSave": result.status in ("OPTIMAL", "FEASIBLE") and not any(result.validation.values()),
    }


def overview_payload() -> dict[str, Any]:
    """Return the counts and operational health shown in the shell."""
    today = date.today()
    week_start = monday(today)
    week_end = week_start + timedelta(days=5)
    with SessionLocal() as session:
        patient_count = session.scalar(select(func.count(Patient.id)).where(Patient.active.is_(True))) or 0
        therapist_count = session.scalar(
            select(func.count(Therapist.id)).where(Therapist.active.is_(True))
        ) or 0
        week_count = session.scalar(select(func.count(Appointment.id)).where(
            Appointment.date >= week_start,
            Appointment.date <= week_end,
            Appointment.status.in_(ACTIVE_STATUSES),
        )) or 0
        today_count = session.scalar(select(func.count(Appointment.id)).where(
            Appointment.date == today,
            Appointment.status.in_(ACTIVE_STATUSES),
        )) or 0
        attention_count = session.scalar(select(func.count(Appointment.id)).where(
            Appointment.status == AppointmentStatus.NEEDS_RESCHEDULING
        )) or 0
        warning_count = session.scalar(select(func.count(ImportWarning.id))) or 0
        active_requirements = session.scalar(select(func.count(TreatmentRequirement.id)).where(
            TreatmentRequirement.active.is_(True)
        )) or 0
    return {
        "today": today.isoformat(),
        "weekStart": week_start.isoformat(),
        "patients": patient_count,
        "therapists": therapist_count,
        "appointmentsThisWeek": week_count,
        "appointmentsToday": today_count,
        "needsAttention": attention_count,
        "activeRequirements": active_requirements,
        "importWarnings": warning_count,
        "slotLabels": [slot_label(index) for index in range(len(SLOTS))],
        "schedulingWeekDays": SCHEDULING_WEEK_DAYS,
    }


def schedule_payload(start: date, days: int) -> dict[str, Any]:
    """Return appointments and filter data for a date range."""
    end = start + timedelta(days=days - 1)
    with SessionLocal() as session:
        rows = list(session.execute(appointment_query(start, end).where(
            Appointment.status.in_(ACTIVE_STATUSES + (AppointmentStatus.NEEDS_RESCHEDULING,))
        )))
        therapists = [{
            "id": item.id,
            "code": item.therapist_code,
            "name": item.name,
            "department": item.department.name,
            "location": item.location.name,
            "active": item.active,
        } for item in session.scalars(select(Therapist).order_by(Therapist.name))]
        departments = [{"id": item.id, "name": item.name} for item in session.scalars(
            select(Department).where(Department.active.is_(True)).order_by(Department.name)
        )]
        locations = [{"id": item.id, "name": item.name, "capacity": item.capacity} for item in session.scalars(
            select(Location).where(Location.active.is_(True)).order_by(Location.name)
        )]
        patients = [{
            "id": item.id,
            "code": item.patient_code,
            "name": item.name or item.patient_code,
        } for item in session.scalars(
            select(Patient).where(Patient.active.is_(True)).order_by(Patient.patient_code)
        )]
    return {
        "start": start.isoformat(),
        "end": end.isoformat(),
        "days": days,
        "appointments": [serialize_appointment(row) for row in rows],
        "therapists": therapists,
        "patients": patients,
        "departments": departments,
        "locations": locations,
        "slotLabels": [slot_label(index) for index in range(len(SLOTS))],
    }


def patients_payload(query: str = "") -> dict[str, Any]:
    """Return patient directory rows with treatment-plan summaries."""
    query = query.strip().lower()
    with SessionLocal() as session:
        requirement_counts = dict(session.execute(
            select(TreatmentRequirement.patient_id, func.count(TreatmentRequirement.id))
            .where(TreatmentRequirement.active.is_(True))
            .group_by(TreatmentRequirement.patient_id)
        ).all())
        patients = list(session.scalars(select(Patient).order_by(Patient.patient_code)))
        preference_scores: dict[int, dict[str, dict[int | str, int]]] = {}
        for item in session.scalars(select(PatientAvailability).where(
            PatientAvailability.available.is_(True),
            PatientAvailability.preference_score > 0,
        )):
            scores = preference_scores.setdefault(item.patient_id, {"days": {}, "periods": {}})
            scores["days"][item.day_of_week] = scores["days"].get(item.day_of_week, 0) + item.preference_score
            period = "morning" if item.slot_index in PREFERENCE_PERIODS["morning"] else "afternoon"
            scores["periods"][period] = scores["periods"].get(period, 0) + item.preference_score
    rows = []
    for patient in patients:
        searchable = f"{patient.patient_code} {patient.name or ''}".lower()
        if query and query not in searchable:
            continue
        scores = preference_scores.get(patient.id, {"days": {}, "periods": {}})
        preferred_day = max(scores["days"], key=scores["days"].get) if scores["days"] else None
        preferred_period = max(scores["periods"], key=scores["periods"].get) if scores["periods"] else None
        rows.append({
            "id": patient.id,
            "code": patient.patient_code,
            "name": patient.name or "Name not provided",
            "schoolGoing": patient.school_going,
            "preferenceDay": PREFERENCE_DAY_LABELS[preferred_day] if preferred_day is not None else "Not set",
            "preferencePeriod": PREFERENCE_PERIOD_LABELS.get(preferred_period, patient.preferred_time or "Not set"),
            "active": patient.active,
            "therapies": requirement_counts.get(patient.id, 0),
        })
    return {"patients": rows, "total": len(rows)}


def serialize_patient_preferences(session, patient: Patient) -> dict[str, Any]:
    """Return one patient's preferred weekly day and time band."""
    availability = list(session.scalars(select(PatientAvailability).where(
        PatientAvailability.patient_id == patient.id,
        PatientAvailability.available.is_(True),
        PatientAvailability.preference_score > 0,
    )))
    day_scores: dict[int, int] = {}
    period_scores: dict[str, int] = {}
    for item in availability:
        day_scores[item.day_of_week] = day_scores.get(item.day_of_week, 0) + item.preference_score
        period = "morning" if item.slot_index in PREFERENCE_PERIODS["morning"] else "afternoon"
        period_scores[period] = period_scores.get(period, 0) + item.preference_score
    preferred_day = max(day_scores, key=day_scores.get) if day_scores else None
    preferred_period = max(period_scores, key=period_scores.get) if period_scores else None
    return {
        "patient": {
            "id": patient.id,
            "code": patient.patient_code,
            "name": patient.name or patient.patient_code,
        },
        "preference": {
            "day": preferred_day,
            "period": preferred_period,
        },
    }


def patient_preferences_payload(patient_id: int) -> dict[str, Any]:
    """Fetch the recurring availability and preference table for one patient."""
    with SessionLocal() as session:
        patient = session.get(Patient, patient_id)
        if patient is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "Patient not found")
        return serialize_patient_preferences(session, patient)


def therapists_payload(start: date) -> dict[str, Any]:
    """Return therapist directory rows and selected-week workload."""
    end = start + timedelta(days=5)
    with SessionLocal() as session:
        counts = dict(session.execute(
            select(Appointment.therapist_id, func.count(Appointment.id))
            .where(
                Appointment.date >= start,
                Appointment.date <= end,
                Appointment.status.in_(ACTIVE_STATUSES),
            )
            .group_by(Appointment.therapist_id)
        ).all())
        therapists = list(session.scalars(select(Therapist).order_by(Therapist.name)))
        rows = [{
            "id": item.id,
            "code": item.therapist_code,
            "name": item.name,
            "department": item.department.name,
            "location": item.location.name,
            "appointments": counts.get(item.id, 0),
            "capacity": None,
            "active": item.active,
        } for item in therapists]
    return {"therapists": rows, "weekStart": start.isoformat()}


def operations_payload(today: date | None = None) -> dict[str, Any]:
    """Return absence history and appointments awaiting rescheduling."""
    reference_date = today or date.today()
    with SessionLocal() as session:
        therapist_absences = list(session.execute(
            select(TherapistAbsence, Therapist.therapist_code, Therapist.name)
            .join(Therapist, Therapist.id == TherapistAbsence.therapist_id)
            .order_by(TherapistAbsence.date.desc(), TherapistAbsence.id.desc())
            .limit(20)
        ))
        patient_absences = list(session.execute(
            select(PatientAbsence, Patient.patient_code, Patient.name)
            .join(Patient, Patient.id == PatientAbsence.patient_id)
            .order_by(PatientAbsence.date.desc(), PatientAbsence.id.desc())
            .limit(20)
        ))
        attention_rows = list(session.execute(
            appointment_query(
                reference_date - timedelta(days=30),
                reference_date + timedelta(days=SCHEDULING_WEEK_DAYS),
            )
            .where(Appointment.status == AppointmentStatus.NEEDS_RESCHEDULING)
        ))
    absences = [{
        "kind": "Therapist",
        "code": row.therapist_code,
        "name": row.name,
        "date": row[0].date.isoformat(),
        "fullDay": row[0].full_day,
        "reason": row[0].reason or "No reason provided",
    } for row in therapist_absences] + [{
        "kind": "Patient",
        "code": row.patient_code,
        "name": row.name or row.patient_code,
        "date": row[0].date.isoformat(),
        "fullDay": row[0].full_day,
        "reason": row[0].reason or "No reason provided",
    } for row in patient_absences]
    absences.sort(key=lambda item: item["date"], reverse=True)
    return {
        "absences": absences[:20],
        "needsRescheduling": [serialize_appointment(row) for row in attention_rows],
    }


class TherapySchedulerHandler(SimpleHTTPRequestHandler):
    """Serve the SPA and a compact JSON API."""

    server_version = "EICTherapyScheduler/1.0"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def log_message(self, format_string: str, *args) -> None:
        print(f"[{self.log_date_time_string()}] {format_string % args}")

    def send_json(self, payload: Any, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload, default=self.json_default).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def send_html(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def redirect(self, location: str) -> None:
        self.send_response(HTTPStatus.SEE_OTHER)
        self.send_header("Location", location)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    @staticmethod
    def json_default(value: Any):
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        if hasattr(value, "value"):
            return value.value
        raise TypeError(f"Cannot serialize {type(value).__name__}")

    def read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Invalid content length") from exc
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Request body must be valid JSON") from exc

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/oauth/zoho/start":
                self.start_zoho_oauth()
                return
            if parsed.path == "/oauth/zoho/callback":
                self.complete_zoho_oauth(parse_qs(parsed.query))
                return
            if parsed.path == "/api/zoho/status":
                self.send_json(zoho_creator_status())
                return
            if parsed.path == "/api/overview":
                self.send_json(overview_payload())
                return
            if parsed.path == "/api/schedule":
                params = parse_qs(parsed.query)
                start = parse_date(params.get("start", [monday(date.today()).isoformat()])[0], "start")
                days = min(14, max(1, int(params.get("days", ["6"])[0])))
                self.send_json(schedule_payload(start, days))
                return
            if parsed.path == "/api/patients":
                params = parse_qs(parsed.query)
                self.send_json(patients_payload(params.get("q", [""])[0]))
                return
            match = re.fullmatch(r"/api/patients/(\d+)/preferences", parsed.path)
            if match:
                self.send_json(patient_preferences_payload(int(match.group(1))))
                return
            if parsed.path == "/api/therapists":
                params = parse_qs(parsed.query)
                start = monday(parse_date(
                    params.get("start", [date.today().isoformat()])[0], "start"
                ))
                self.send_json(therapists_payload(start))
                return
            if parsed.path == "/api/operations":
                self.send_json(operations_payload())
                return
            match = re.fullmatch(r"/api/appointments/(\d+)/replacement-candidates", parsed.path)
            if match:
                self.replacement_candidates(int(match.group(1)))
                return
            if parsed.path == "/api/health":
                self.send_json({"ok": True})
                return
            if parsed.path.startswith("/api/"):
                raise ApiError(HTTPStatus.NOT_FOUND, "API route not found")
            if parsed.path in ("/", "/index.html"):
                self.path = "/index.html"
            elif not (WEB_DIR / parsed.path.lstrip("/")).is_file():
                self.path = "/index.html"
            super().do_GET()
        except ApiError as exc:
            self.send_json({"error": exc.message}, exc.status)
        except (ValueError, TypeError) as exc:
            self.send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:  # pragma: no cover - final HTTP safety net
            print(f"Unhandled GET error: {exc!r}")
            self.send_json({"error": "The scheduler could not complete that request."}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        try:
            body = self.read_json()
            if parsed.path == "/api/source/initialize":
                with SessionLocal() as session:
                    report = initialize_source_database(session)
                self.send_json(report)
                return
            if parsed.path == "/api/absences":
                self.record_absence(body)
                return
            match = re.fullmatch(r"/api/patients/(\d+)/preferences", parsed.path)
            if match:
                self.save_patient_preferences(int(match.group(1)), body)
                return
            match = re.fullmatch(r"/api/appointments/(\d+)/cancel", parsed.path)
            if match:
                self.cancel_appointment(int(match.group(1)))
                return
            match = re.fullmatch(r"/api/appointments/(\d+)/move", parsed.path)
            if match:
                self.move_appointment(int(match.group(1)), body)
                return
            match = re.fullmatch(r"/api/zoho/appointments/(\d+)/push", parsed.path)
            if match:
                self.push_appointment_to_zoho(int(match.group(1)))
                return
            if parsed.path == "/api/zoho/appointments/push-batch":
                self.push_appointment_batch_to_zoho(body)
                return
            if parsed.path == "/api/zoho/plan-details/pull":
                self.pull_plan_details_from_zoho()
                return
            if parsed.path == "/api/schedules/generate":
                self.generate_schedule(body)
                return
            match = re.fullmatch(r"/api/schedules/([0-9a-f-]+)/save", parsed.path)
            if match:
                self.save_schedule(match.group(1))
                return
            raise ApiError(HTTPStatus.NOT_FOUND, "API route not found")
        except ApiError as exc:
            self.send_json({"error": exc.message}, exc.status)
        except Exception as exc:  # pragma: no cover - final HTTP safety net
            print(f"Unhandled POST error: {exc!r}")
            self.send_json({"error": "The scheduler could not complete that request."}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def record_absence(self, body: dict[str, Any]) -> None:
        kind = body.get("kind")
        entity_id = int(body.get("entityId", 0))
        absence_date = parse_date(body.get("date"))
        reason = str(body.get("reason", "")).strip()
        if kind not in ("therapist", "patient") or entity_id < 1:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Choose a therapist or patient")
        with SessionLocal() as session:
            if kind == "therapist":
                if session.get(Therapist, entity_id) is None:
                    raise ApiError(HTTPStatus.NOT_FOUND, "Therapist not found")
                affected = mark_therapist_absent(session, entity_id, absence_date, reason)
            else:
                if session.get(Patient, entity_id) is None:
                    raise ApiError(HTTPStatus.NOT_FOUND, "Patient not found")
                affected = mark_patient_absent(session, entity_id, absence_date, reason)
            replacement_groups = [
                replacement_group(session, appointment, find_replacement_candidates(
                    session, appointment, monday(absence_date)
                ))
                for appointment in affected
            ] if kind == "patient" else []
        self.send_json({
            "message": "Absence recorded",
            "affected": len(affected),
            "replacementGroups": replacement_groups,
        }, HTTPStatus.CREATED)

    def save_patient_preferences(self, patient_id: int, body: dict[str, Any]) -> None:
        """Store one recurring preferred day and morning/afternoon time band."""
        day = body.get("day")
        period = body.get("period")
        if day in (None, "") and period in (None, ""):
            preferred_day = None
            preferred_period = None
        else:
            try:
                preferred_day = int(day)
            except (TypeError, ValueError) as exc:
                raise ApiError(HTTPStatus.BAD_REQUEST, "Choose a preferred day") from exc
            if preferred_day not in range(len(PREFERENCE_DAY_LABELS)):
                raise ApiError(HTTPStatus.BAD_REQUEST, "Choose a valid preferred day")
            if period not in PREFERENCE_PERIODS:
                raise ApiError(HTTPStatus.BAD_REQUEST, "Choose morning or afternoon")
            preferred_period = period
        with SessionLocal() as session:
            patient = session.get(Patient, patient_id)
            if patient is None:
                raise ApiError(HTTPStatus.NOT_FOUND, "Patient not found")
            existing = {
                (item.day_of_week, item.slot_index): item
                for item in session.scalars(select(PatientAvailability).where(
                    PatientAvailability.patient_id == patient.id
                ))
            }
            preferred_slots = set(PREFERENCE_PERIODS.get(preferred_period, ()))
            for day_index in range(len(PREFERENCE_DAY_LABELS)):
                for slot in range(len(SLOTS)):
                    availability = existing.get((day_index, slot))
                    if availability is None:
                        availability = PatientAvailability(
                            patient_id=patient.id,
                            day_of_week=day_index,
                            slot_index=slot,
                            available=True,
                        )
                        session.add(availability)
                    availability.preference_score = (
                        PREFERRED_AVAILABILITY_SCORE
                        if availability.available and day_index == preferred_day and slot in preferred_slots
                        else 0
                    )
            patient.preferred_time = PREFERENCE_PERIOD_LABELS.get(preferred_period)
            session.commit()
            payload = serialize_patient_preferences(session, patient)
        self.send_json({"message": "Patient availability preferences saved", **payload})

    def replacement_candidates(self, appointment_id: int) -> None:
        """Return the current eligible children for one released appointment."""
        with SessionLocal() as session:
            appointment = session.get(Appointment, appointment_id)
            if appointment is None:
                raise ApiError(HTTPStatus.NOT_FOUND, "Appointment not found")
            if appointment.status != AppointmentStatus.NEEDS_RESCHEDULING:
                raise ApiError(HTTPStatus.CONFLICT, "Appointment is not waiting for rescheduling")
            group = replacement_group(
                session,
                appointment,
                find_replacement_candidates(session, appointment, monday(appointment.date)),
            )
        self.send_json(group, HTTPStatus.OK)

    def cancel_appointment(self, appointment_id: int) -> None:
        with SessionLocal() as session:
            appointment = session.get(Appointment, appointment_id)
            if appointment is None:
                raise ApiError(HTTPStatus.NOT_FOUND, "Appointment not found")
            if appointment.status not in ACTIVE_STATUSES:
                raise ApiError(HTTPStatus.CONFLICT, "Only an active appointment can be cancelled")
            vacancy_date = appointment.date
            week_start = monday(vacancy_date)
            cancel_patient_appointment(session, appointment)
            replacements = find_replacement_candidates(session, appointment, week_start)
            replacement_groups = [replacement_group(session, appointment, replacements)]
            pull_forward = find_pull_forward_candidates(
                session, appointment, vacancy_date + timedelta(days=SCHEDULING_WEEK_DAYS - 1)
            )
        self.send_json({
            "message": "Cancellation recorded",
            "replacements": replacements,
            "replacementGroups": replacement_groups,
            "pullForward": pull_forward,
        })

    def move_appointment(self, appointment_id: int, body: dict[str, Any]) -> None:
        new_date = parse_date(body.get("date"))
        try:
            new_slot = int(body.get("slot"))
        except (TypeError, ValueError) as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Choose a valid starting time") from exc
        if new_slot not in range(len(SLOTS)):
            raise ApiError(HTTPStatus.BAD_REQUEST, "Choose a valid starting time")
        with SessionLocal() as session:
            appointment = session.get(Appointment, appointment_id)
            if appointment is None:
                raise ApiError(HTTPStatus.NOT_FOUND, "Appointment not found")
            problem = load_scheduling_problem(
                session, monday(date.today()), preserve_confirmed=True, horizon_days=SCHEDULING_WEEK_DAYS
            )
            moved, messages = move_appointment(
                session, problem, problem.pinned, appointment, new_date, new_slot
            )
        if not moved:
            raise ApiError(HTTPStatus.CONFLICT, " ".join(messages))
        self.send_json({"message": "Appointment moved safely"})

    def generate_schedule(self, body: dict[str, Any]) -> None:
        selected_date = parse_date(body.get("start"), "start")
        run_date = max(selected_date, date.today())
        search_seconds = parse_search_seconds(body.get("searchSeconds"))
        with SessionLocal() as session:
            result, start, planning_end = generate_rolling_schedule(
                session,
                run_date,
                mode=SolverMode.BEST_EFFORT,
                settings=replace(
                    SOLVER_SETTINGS,
                    max_time_seconds=search_seconds,
                ),
            )
        preview_id = str(uuid4())
        horizon_days = (planning_end - start).days + 1
        PREVIEWS[preview_id] = {
            "result": result,
            "start": start,
            "horizon_days": horizon_days,
            "created": datetime.utcnow(),
        }
        payload = serialize_preview(result, start, planning_end)
        # The saved window begins on Monday so existing appointments can be
        # preserved, while new placements begin on the day the planner runs.
        payload["planningStart"] = run_date.isoformat()
        payload["previewId"] = preview_id
        self.send_json(payload, HTTPStatus.CREATED)

    def save_schedule(self, preview_id: str) -> None:
        preview = PREVIEWS.get(preview_id)
        if preview is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "This preview has expired. Generate it again.")
        result = preview["result"]
        if result.status not in ("OPTIMAL", "FEASIBLE") or any(result.validation.values()):
            raise ApiError(HTTPStatus.CONFLICT, "This preview did not pass validation")
        with SessionLocal() as session:
            added = save_proposed_schedule(
                session, result, preview["start"], preview["horizon_days"]
            )
        PREVIEWS.pop(preview_id, None)
        self.send_json({"message": f"Saved {added} new proposed appointments", "added": added})

    def push_appointment_to_zoho(self, appointment_id: int) -> None:
        """Push exactly one saved appointment to Creator, once."""
        settings = settings_from_environment()
        access_token = ZOHO_CREATOR_CONNECTION.get("access_token")
        if not settings.configured:
            raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "Zoho Creator is not configured")
        if not access_token:
            try:
                access_token = refresh_access_token(settings)["access_token"]
            except ValueError as exc:
                raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, str(exc)) from exc
            ZOHO_CREATOR_CONNECTION["access_token"] = access_token
        with SessionLocal() as session:
            existing = session.scalar(select(ZohoAppointmentSync).where(
                ZohoAppointmentSync.appointment_id == appointment_id
            ))
            if existing:
                self.send_json({
                    "message": "This appointment was already pushed to Zoho Creator",
                    "appointmentId": appointment_id,
                    "creatorRecordId": existing.creator_record_id,
                })
                return
            row = session.execute(
                appointment_query(date.min, date.max).where(Appointment.id == appointment_id)
            ).first()
            if row is None:
                raise ApiError(HTTPStatus.NOT_FOUND, "Saved appointment not found")
            fields = creator_appointment_fields(row)
            try:
                response = create_appointment_schedule(settings, access_token, fields)
            except ValueError as exc:
                raise ApiError(HTTPStatus.BAD_GATEWAY, str(exc)) from exc
            data = response.get("data") or {}
            creator_record_id = str(data.get("ID") or data.get("id") or "") or None
            session.add(ZohoAppointmentSync(
                appointment_id=appointment_id,
                creator_record_id=creator_record_id,
            ))
            session.commit()
        self.send_json({
            "message": "One appointment was pushed to Zoho Creator",
            "appointmentId": appointment_id,
            "creatorRecordId": creator_record_id,
        }, HTTPStatus.CREATED)

    def push_appointment_batch_to_zoho(self, body: dict[str, Any]) -> None:
        """Push up to 200 unsynced saved appointments in one Creator request."""
        limit = int(body.get("limit", 200))
        if not 1 <= limit <= 200:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Batch size must be between 1 and 200")
        settings = settings_from_environment()
        if not settings.configured:
            raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "Zoho Creator is not configured")
        access_token = ZOHO_CREATOR_CONNECTION.get("access_token")
        if not access_token:
            try:
                access_token = refresh_access_token(settings)["access_token"]
            except ValueError as exc:
                raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, str(exc)) from exc
            ZOHO_CREATOR_CONNECTION["access_token"] = access_token

        with SessionLocal() as session:
            rows = session.execute(
                appointment_query(date.min, date.max)
                .outerjoin(
                    ZohoAppointmentSync,
                    ZohoAppointmentSync.appointment_id == Appointment.id,
                )
                .where(
                    ZohoAppointmentSync.id.is_(None),
                    Appointment.status.in_(ACTIVE_STATUSES),
                )
                .limit(limit)
            ).all()
            if not rows:
                self.send_json({
                    "message": "No unsynced active appointments are available",
                    "requested": 0,
                    "pushed": 0,
                    "failed": [],
                })
                return

            fields_by_appointment_id = {
                row[0].id: creator_appointment_fields(row) for row in rows
            }
            try:
                response = create_appointment_schedules(
                    settings,
                    access_token,
                    list(fields_by_appointment_id.values()),
                )
            except ValueError as exc:
                raise ApiError(HTTPStatus.BAD_GATEWAY, str(exc)) from exc

            results = response.get("result")
            if not isinstance(results, list):
                raise ApiError(
                    HTTPStatus.BAD_GATEWAY,
                    "Zoho Creator did not return per-appointment batch results",
                )
            pushed = []
            failed = []
            for item in results:
                data = item.get("data") if isinstance(item, dict) else None
                appointment_value = data.get("Appointment_ID") if isinstance(data, dict) else None
                try:
                    appointment_id = int(appointment_value)
                except (TypeError, ValueError):
                    continue
                if appointment_id not in fields_by_appointment_id:
                    continue
                if item.get("code") == 3000:
                    creator_record_id = str(data.get("ID") or data.get("id") or "") or None
                    session.add(ZohoAppointmentSync(
                        appointment_id=appointment_id,
                        creator_record_id=creator_record_id,
                    ))
                    pushed.append(appointment_id)
                else:
                    failed.append({
                        "appointmentId": appointment_id,
                        "message": item.get("message") or "Zoho rejected this appointment",
                    })
            returned_ids = set(pushed) | {item["appointmentId"] for item in failed}
            for appointment_id in fields_by_appointment_id:
                if appointment_id not in returned_ids:
                    failed.append({
                        "appointmentId": appointment_id,
                        "message": "Zoho did not return a result for this appointment",
                    })
            session.commit()

        self.send_json({
            "message": f"Pushed {len(pushed)} of {len(rows)} appointments to Zoho Creator",
            "requested": len(rows),
            "pushed": len(pushed),
            "failed": failed,
        }, HTTPStatus.CREATED)

    def pull_plan_details_from_zoho(self) -> None:
        """Pull the Creator Plan Detail report into PostgreSQL staging rows."""
        settings = settings_from_environment()
        if not settings.configured or not settings.plan_detail_report_link_name:
            raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "Zoho Plan Detail is not configured")
        access_token = ZOHO_CREATOR_CONNECTION.get("access_token")
        if not access_token:
            try:
                access_token = refresh_access_token(settings)["access_token"]
            except ValueError as exc:
                raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, str(exc)) from exc
            ZOHO_CREATOR_CONNECTION["access_token"] = access_token
        try:
            records = get_report_records(
                settings, access_token, settings.plan_detail_report_link_name
            )
            values = [creator_plan_detail_fields(record) for record in records]
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_GATEWAY, str(exc)) from exc

        created = 0
        updated = 0
        with SessionLocal() as session:
            for item in values:
                existing = session.get(ZohoPlanDetailRecord, item["creator_record_id"])
                if existing is None:
                    session.add(ZohoPlanDetailRecord(**item))
                    created += 1
                else:
                    for key, value in item.items():
                        setattr(existing, key, value)
                    existing.pulled_at = datetime.utcnow()
                    updated += 1
            session.commit()
        self.send_json({
            "message": "Plan Detail report pulled into PostgreSQL staging",
            "read": len(values),
            "created": created,
            "updated": updated,
            "table": "zoho_plan_detail_records",
        }, HTTPStatus.CREATED)

    def start_zoho_oauth(self) -> None:
        """Redirect an administrator to Zoho Creator's consent screen."""
        settings = settings_from_environment()
        if not settings.configured:
            raise ApiError(
                HTTPStatus.SERVICE_UNAVAILABLE,
                "Zoho Creator is not configured. Set the client ID and client secret outside the app first.",
            )
        state = str(uuid4())
        ZOHO_OAUTH_STATES[state] = datetime.utcnow()
        self.redirect(authorization_url(settings, state))

    def complete_zoho_oauth(self, query: dict[str, list[str]]) -> None:
        """Validate one-time OAuth consent and store its refresh token locally."""
        error = query.get("error", [""])[0]
        if error:
            self.send_html(
                "<h1>Zoho connection was not approved</h1>"
                "<p>You can close this tab and try Connect Zoho again.</p>",
                HTTPStatus.BAD_REQUEST,
            )
            return
        state = query.get("state", [""])[0]
        started = ZOHO_OAUTH_STATES.pop(state, None)
        if started is None or datetime.utcnow() - started > timedelta(minutes=10):
            raise ApiError(HTTPStatus.BAD_REQUEST, "Zoho connection request expired. Start again from the scheduler.")
        code = query.get("code", [""])[0]
        if not code:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Zoho did not return an authorization code.")
        try:
            ZOHO_CREATOR_CONNECTION.clear()
            token_response = exchange_authorization_code(settings_from_environment(), code)
            save_refresh_token(str(token_response.get("refresh_token") or ""))
            ZOHO_CREATOR_CONNECTION.update(token_response)
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_GATEWAY, str(exc)) from exc
        self.send_html(
            "<h1>Zoho Creator connected</h1>"
            "<p>The scheduler can now reconnect automatically. You may close this tab.</p>"
        )


def run(host: str = "127.0.0.1", port: int = 8765) -> None:
    """Start the local therapy scheduler web application."""
    init_db()
    with SessionLocal() as session:
        initialize_source_database(session)
    server = ThreadingHTTPServer((host, port), TherapySchedulerHandler)
    print(f"EIC Therapy Scheduler is ready at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    run()
