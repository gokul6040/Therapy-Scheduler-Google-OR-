"""Local web application for the EIC therapy scheduler.

This module deliberately uses the Python standard library for HTTP serving so the
existing scheduling project can offer a modern browser UI without adding a second
application framework. Business rules remain in ``scheduling`` and ``services``.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import asdict, replace
from datetime import date, datetime, timedelta
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
from threading import Lock
from typing import Any
from urllib.parse import parse_qs, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

from sqlalchemy import func, select

from config import SCHEDULING_WEEK_DAYS, SOLVER_SEARCH_TIME_OPTIONS, SOLVER_SETTINGS
from database.db import SessionLocal as ProductionSessionLocal
from database.db import init_admin_booking_tables, init_db
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
    TherapistAvailability,
    TreatmentRequirement,
    ZohoAppointmentSync,
    ZohoPlanDetailRecord,
)
from database.repository import load_scheduling_problem, save_proposed_schedule
from importers.initialize_source import initialize_source_database
from scheduling.cp_sat_scheduler import generate_weekly_schedule
from scheduling.slot_config import CONSECUTIVE_TO_NEXT, SLOTS, slot_label
from scheduling.types import ScheduleResult, SolverMode
from services.absence_service import mark_patient_absent, mark_therapist_absent
from services.appointment_service import move_appointment
from services.admin_reservation_service import (
    EmptySlot, RELEASED_STATUSES, is_reserved_source, preview_admin_reservation,
    preview_empty_slot, reserve_admin_slot, reserved_source_ids,
)
from services.cancellation_service import cancel_patient_appointment
from services.replacement_service import find_pull_forward_candidates, find_replacement_candidates
from services.rolling_schedule_service import generate_rolling_schedule
from services.whatsapp_button_service import record_button_clicks
from services.whatsapp_help_service import queue_help_replies, start_help_reply_worker
from services.whatsapp_service import (
    MAX_WEBHOOK_BODY_BYTES,
    valid_webhook_signature,
    webhook_event_summary,
    webhook_settings_from_environment,
)
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


def sanitized_request_line(request_line: str) -> str:
    """Remove query parameters from access logs to avoid logging credentials."""
    parts = request_line.split(" ", 2)
    if len(parts) != 3 or "?" not in parts[1]:
        return request_line
    return f"{parts[0]} {urlparse(parts[1]).path} {parts[2]}"


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
WORKSPACE_CONTEXT: ContextVar[dict | None] = ContextVar("workspace_context", default=None)


def SessionLocal(*args, **kwargs):
    """Use this request's workspace, defaulting to the production session."""
    context = WORKSPACE_CONTEXT.get()
    factory = context["sessions"] if context is not None else ProductionSessionLocal
    return factory(*args, **kwargs)


def workspace_previews() -> dict:
    """Keep test previews separate even when appointment IDs overlap."""
    context = WORKSPACE_CONTEXT.get()
    return context["previews"] if context is not None else PREVIEWS


ZOHO_OAUTH_STATES: dict[str, datetime] = {}
# OAuth responses are deliberately memory-only until deployment secrets are
# configured in Azure Key Vault. Never log or return these token values.
ZOHO_CREATOR_CONNECTION: dict[str, Any] = {}
ASSISTANT_SESSIONS: dict[str, dict[str, Any]] = {}
ASSISTANT_SESSION_LOCK = Lock()
ASSISTANT_SESSION_TTL = timedelta(minutes=30)
ASSISTANT_NAME_LIST_LIMIT = 10
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


def whatsapp_webhook_challenge(query: dict[str, list[str]]) -> str:
    """Validate Meta's initial webhook request and return its raw challenge."""
    settings = webhook_settings_from_environment()
    if settings is None:
        raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "WhatsApp webhook is not configured")
    if query.get("hub.mode", [""])[0] != "subscribe":
        raise ApiError(HTTPStatus.BAD_REQUEST, "Unsupported WhatsApp webhook mode")
    if query.get("hub.verify_token", [""])[0] != settings.verify_token:
        raise ApiError(HTTPStatus.FORBIDDEN, "WhatsApp webhook verification failed")
    challenge = query.get("hub.challenge", [""])[0]
    if not challenge:
        raise ApiError(HTTPStatus.BAD_REQUEST, "WhatsApp webhook challenge is required")
    return challenge


AZURE_CHAT_ACTION_SCHEMA = {
    "name": "therapy_scheduler_action",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "cancel_appointment", "day_summary", "centre_summary",
                    "therapist_summary", "therapy_summary", "patient_summary", "needs_attention",
                    "absence_summary", "empty_slots", "list_summary_names",
                    "last_patient_details", "mark_absent", "unsupported", "clarify",
                ],
            },
            "patient_query": {"type": "string"},
            "entity_query": {"type": "string"},
            "absence_kind": {"type": "string", "enum": ["patient", "therapist", "unknown"]},
            "date": {"type": "string"},
            "reply": {"type": "string"},
        },
        "required": ["action", "patient_query", "entity_query", "absence_kind", "date", "reply"],
    },
}


def azure_openai_settings() -> dict[str, str] | None:
    """Read the Azure OpenAI connection without exposing its secret."""
    endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "").rstrip("/")
    api_key = os.getenv("AZURE_OPENAI_API_KEY", "")
    deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", "")
    if not all((endpoint, api_key, deployment)):
        return None
    return {
        "endpoint": endpoint,
        "api_key": api_key,
        "deployment": deployment,
        "api_version": os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-21"),
        "responses_url": os.getenv("AZURE_OPENAI_RESPONSES_URL", "").strip(),
    }


def relative_date_from_message(message: str, reference_date: date) -> date | None:
    """Resolve one unambiguous operator-relative date without relying on the LLM."""
    normalized = message.casefold()
    if re.search(r"\bday\s+after\s+(?:tomorrow|tmrw)\b", normalized):
        return reference_date + timedelta(days=2)
    if re.search(r"\b(?:tomorrow|tmrw)\b", normalized):
        return reference_date + timedelta(days=1)
    if re.search(r"\byesterday\b", normalized):
        return reference_date - timedelta(days=1)
    if re.search(r"\btoday\b", normalized):
        return reference_date
    return None


def local_assistant_action(message: str, reference_date: date) -> dict[str, str] | None:
    """Handle common read-only requests when the model response is unavailable."""
    normalized = message.casefold().strip()
    action_date = relative_date_from_message(message, reference_date)
    if action_date is None:
        date_match = re.search(r"\b\d{4}-\d{2}-\d{2}\b", message)
        try:
            action_date = date.fromisoformat(date_match.group()) if date_match else reference_date
        except ValueError:
            action_date = reference_date
    common = {
        "patient_query": "",
        "absence_kind": "unknown",
        "date": action_date.isoformat(),
        "reply": "",
    }
    patient_summary = re.search(r"\bsummary\b.*\bpatient\s+(.+?)(?:\s+on\s+\d{4}-\d{2}-\d{2})?[?!.]*$", message, re.IGNORECASE)
    if patient_summary:
        return {
            "action": "patient_summary",
            "entity_query": patient_summary.group(1).strip(),
            **common,
        }
    available_slots = re.search(
        r"\b(?:available|free|open)\s+(?:therapist\s+)?(?:slots?|availability)\s+(?:of|for)\s+(.+?)[?!.]*$",
        message, re.IGNORECASE,
    )
    if available_slots:
        return {
            "action": "empty_slots",
            "entity_query": available_slots.group(1).strip(),
            **common,
        }
    if re.fullmatch(r"(?:hi|hello|hey)[!?. ]*", normalized):
        return {
            "action": "unsupported",
            "entity_query": "",
            "reply": "Hello. I can help with patient summaries, therapist availability, absences, and scheduling updates.",
            **{key: value for key, value in common.items() if key != "reply"},
        }
    return None


def professional_assistant_text(value: str) -> str:
    """Keep model fallback copy suitable for a clinical operations workspace."""
    without_symbols = re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", "", value)
    return re.sub(r"\s+", " ", without_symbols).strip()


def assistant_session_id(value: Any) -> str | None:
    """Accept a browser-local assistant session identifier, never an account ID."""
    candidate = str(value or "").strip()
    return candidate if re.fullmatch(r"[A-Za-z0-9_-]{8,80}", candidate) else None


def assistant_memory(session_id: str | None) -> dict[str, Any]:
    """Return a short-lived in-memory context for one browser tab."""
    if session_id is None:
        return {}
    now = datetime.utcnow()
    with ASSISTANT_SESSION_LOCK:
        expired = [key for key, value in ASSISTANT_SESSIONS.items()
                   if value.get("expires_at", now) <= now]
        for key in expired:
            ASSISTANT_SESSIONS.pop(key, None)
        context = ASSISTANT_SESSIONS.setdefault(session_id, {})
        context["expires_at"] = now + ASSISTANT_SESSION_TTL
        return context


def clear_assistant_memory(session_id: str | None) -> None:
    """Discard only the temporary context associated with one browser tab."""
    if session_id is None:
        return
    with ASSISTANT_SESSION_LOCK:
        ASSISTANT_SESSIONS.pop(session_id, None)


def restore_assistant_summary_memory(context: dict[str, Any], value: Any) -> None:
    """Restore only read-only browser-session context after an app restart."""
    if context.get("summary") or not isinstance(value, dict):
        return
    summary = value.get("summary")
    if not isinstance(summary, dict):
        return
    action = str(summary.get("action", ""))
    summary_date = str(summary.get("date", ""))
    entity_query = str(summary.get("entity_query", "")).strip()
    if action not in {
        "day_summary", "centre_summary", "therapist_summary", "therapy_summary", "patient_summary",
    }:
        return
    try:
        date.fromisoformat(summary_date)
    except ValueError:
        return
    context["summary"] = {
        "action": action, "date": summary_date, "entity_query": entity_query,
    }
    patient_ids = value.get("lastPatientIds")
    if isinstance(patient_ids, list):
        context["last_patient_ids"] = [
            item for item in patient_ids[:ASSISTANT_NAME_LIST_LIMIT]
            if isinstance(item, int) and item > 0
        ]


def assistant_memory_snapshot(context: dict[str, Any]) -> dict[str, Any]:
    """Return only non-mutating context that the browser may retain for this tab."""
    snapshot: dict[str, Any] = {}
    if context.get("summary"):
        snapshot["summary"] = context["summary"]
    if context.get("last_patient_ids"):
        snapshot["lastPatientIds"] = context["last_patient_ids"]
    return snapshot


def is_name_list_request(message: str) -> bool:
    """Recognise an ordinary follow-up asking for names after a summary."""
    return bool(re.search(
        r"\b(?:give|show|list)\s+(?:me\s+)?(?:the\s+)?(?:names|patients|people|children)\b"
        r"|\bnames\s+please\b|\b(?:name|identify)\s+(?:that|the)?\s*(?:person|patient|people)\b",
        message.casefold(),
    ))


def is_memory_patient_question(message: str) -> bool:
    """Recognise a follow-up about a patient shown in the prior name list."""
    normalized = message.casefold()
    return bool(re.search(
        r"\b(?:his|her|their|that patient(?:'s)?|the patient(?:'s)?)\b.*\b"
        r"(?:therapist|doctor|clinician|therapy|centre|center|location|schedule|appointment|details)\b"
        r"|\bwho\s+(?:is|treats)\s+(?:him|her|them)\b",
        normalized,
    ))


def is_explicit_patient_summary_request(message: str) -> bool:
    """Keep a named patient-summary request out of the prior-name-list shortcut."""
    normalized = message.casefold()
    return "summary" in normalized and bool(re.search(r"\bpatient\b|\bp-[a-z0-9_-]+\b", normalized))


def normalized_summary_entity(value: str) -> str:
    """Normalise display labels while allowing an optional therapy 'Session' suffix."""
    normalized = re.sub(r"[^a-z0-9]+", " ", value.casefold()).strip()
    return re.sub(r"\bsession$", "", normalized).strip()


def summary_entity_matches(actual: str | None, requested: str) -> bool:
    """Match a summary selection without silently broadening it to a partial name."""
    actual_value = (actual or "").casefold().strip()
    requested_value = requested.casefold().strip()
    return actual_value == requested_value or (
        normalized_summary_entity(actual_value) == normalized_summary_entity(requested_value)
    )


def therapist_display_name(value: str) -> str:
    """Remove a trailing therapist identifier from a source-system display name."""
    return re.sub(r"\s+(?:T-[A-Za-z0-9_-]+|THER[_-][A-Za-z0-9_-]+)$", "", value).strip()


def matching_therapists(session, entity_query: str) -> list[Therapist]:
    """Resolve an exact therapist code/display label, then a uniquely safe base name."""
    requested = entity_query.casefold().strip()
    therapists = list(session.scalars(select(Therapist).where(Therapist.active.is_(True))))
    exact = [item for item in therapists if requested in {
        item.therapist_code.casefold(), item.name.casefold(),
    }]
    if exact:
        return exact
    # The browser commonly displays a therapist as ``Name · Code``. Treat that
    # paired label as the same exact selection rather than widening the lookup.
    requested_parts = {
        part.strip() for part in re.split(r"\s*[·|]\s*", requested) if part.strip()
    }
    paired = [item for item in therapists if {
        item.therapist_code.casefold(), item.name.casefold(),
    }.issubset(requested_parts)]
    if paired:
        return paired
    return [item for item in therapists if therapist_display_name(item.name).casefold() == requested]


def matching_patients(session, entity_query: str) -> list[Patient]:
    """Resolve an exact patient code or displayed patient name without partial matching."""
    requested = entity_query.casefold().strip()
    patients = list(session.scalars(select(Patient)))
    exact = [item for item in patients if requested in {
        item.patient_code.casefold(), (item.name or "").casefold(),
        f"{item.name or ''} {item.patient_code}".casefold().strip(),
    }]
    if exact:
        return exact
    requested_parts = {
        part.strip() for part in re.split(r"\s*[·|]\s*", requested) if part.strip()
    }
    paired = [item for item in patients if {
        item.patient_code.casefold(), (item.name or "").casefold(),
    }.issubset(requested_parts)]
    if paired:
        return paired
    return [item for item in patients if (item.name or "").casefold() == requested]


def interpret_chat_action(message: str, today: date | None = None) -> dict[str, str]:
    """Ask Azure OpenAI to translate one operator message into a limited action."""
    settings = azure_openai_settings()
    if settings is None:
        raise ApiError(
            HTTPStatus.SERVICE_UNAVAILABLE,
            "Scheduling assistant is not configured. Add the Azure OpenAI endpoint, key, and deployment.",
        )
    reference_date = today or date.today()
    relative_date = relative_date_from_message(message, reference_date)
    instructions = (
        "You translate one therapy-scheduler operator message into JSON. "
        "Today is " + reference_date.isoformat() + ". "
        "For a cancellation, return cancel_appointment only when the operator explicitly asks to cancel a "
        "patient's therapy appointment; use the patient's stated name or code as patient_query. "
        "For a general daily summary return day_summary. For a named centre, therapist, therapy, or patient summary, "
        "return centre_summary, therapist_summary, therapy_summary, or patient_summary and put the stated name or code "
        "in entity_query. A request to summarize a named patient must use patient_summary, not another summary action. "
        "Use needs_attention for disrupted or rescheduling work, absence_summary for absences, empty_slots "
        "for available-slot requests, and preserve a named therapist or therapist code in entity_query for a "
        "therapist-specific available-slot request; otherwise leave entity_query empty. A therapist-specific "
        "available-slot request must return only that therapist's openings. Use list_summary_names when the operator asks for names from the most recent "
        "summary, and last_patient_details when the operator asks about a patient previously shown in that name list. "
        "Use mark_absent when explicitly asked to mark a patient or therapist unavailable or absent. "
        "For mark_absent, put the person's name or code in entity_query and patient, therapist, or unknown in "
        "absence_kind. For every summary or absence action, use the requested date or today when no date is "
        "specified. Interpret today, yesterday, tomorrow/tmrw, and day after tomorrow/day after tmrw relative "
        "to today's supplied date. Normalize every date to YYYY-MM-DD. For every other request use unsupported or clarify. "
        "Do not invent patient names, centre names, therapist names, therapy names, dates, "
        "appointment IDs, availability, replacements, or scheduling decisions. "
        "The reply must be a short, formal plain-language explanation with no emojis, icons, or casual language, "
        "and must not claim anything was changed."
    )
    request_body = {
        "model": settings["deployment"],
        "input": [
            {"role": "system", "content": instructions},
            {"role": "user", "content": message},
        ],
        # GPT-5 models use part of the response budget for reasoning. Leave
        # enough room for both that and the small structured action payload.
        "max_output_tokens": 700,
        "reasoning": {"effort": "minimal"},
        "text": {"format": {"type": "json_schema", **AZURE_CHAT_ACTION_SCHEMA}},
    }
    url = settings["responses_url"] or (
        f"{settings['endpoint']}/openai/responses?api-version={settings['api_version']}"
    )
    request = Request(
        url,
        data=json.dumps(request_body).encode("utf-8"),
        headers={"Content-Type": "application/json", "api-key": settings["api_key"]},
        method="POST",
    )
    try:
        with urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
        content = payload.get("output_text")
        if not content:
            content = next(
                item["text"]
                for output in payload.get("output", [])
                for item in output.get("content", [])
                if item.get("type") == "output_text"
            )
        action = json.loads(content)
    except HTTPError as exc:
        fallback = local_assistant_action(message, reference_date)
        if fallback is not None:
            return fallback
        raise ApiError(HTTPStatus.BAD_GATEWAY, "Azure OpenAI could not interpret that request") from exc
    except (URLError, TimeoutError, KeyError, IndexError, StopIteration, TypeError, json.JSONDecodeError) as exc:
        fallback = local_assistant_action(message, reference_date)
        if fallback is not None:
            return fallback
        raise ApiError(HTTPStatus.BAD_GATEWAY, "Scheduling assistant could not interpret that request") from exc
    if not isinstance(action, dict):
        raise ApiError(HTTPStatus.BAD_GATEWAY, "Scheduling assistant returned an invalid action")
    return {
        "action": str(action.get("action", "unsupported")),
        "patient_query": str(action.get("patient_query", "")).strip(),
        "entity_query": str(action.get("entity_query", "")).strip(),
        "absence_kind": str(action.get("absence_kind", "unknown")).strip(),
        "date": relative_date.isoformat() if relative_date else str(action.get("date", "")).strip(),
        "reply": professional_assistant_text(
            str(action.get("reply", "I could not understand that request."))
        ),
    }


def matching_active_appointments(session, patient_query: str, appointment_date: date) -> list[Any]:
    """Find active appointments using a case-insensitive patient name or code match."""
    normalized = patient_query.casefold()
    return list(session.execute(appointment_query(appointment_date, appointment_date).where(
        Appointment.status.in_(ACTIVE_STATUSES),
        (func.lower(Patient.name) == normalized) | (func.lower(Patient.patient_code) == normalized),
    )))


def cancellation_payload(session, appointment: Appointment) -> dict[str, Any]:
    """Cancel one validated appointment and calculate advisory replacement options."""
    vacancy_date = appointment.date
    cancel_patient_appointment(session, appointment)
    replacements = find_replacement_candidates(session, appointment, monday(vacancy_date))
    replacement_groups = [replacement_group(session, appointment, replacements)]
    pull_forward = find_pull_forward_candidates(
        session, appointment, vacancy_date + timedelta(days=SCHEDULING_WEEK_DAYS - 1)
    )
    return {
        "message": "Cancellation recorded",
        "replacements": replacements,
        "replacementGroups": replacement_groups,
        "pullForward": pull_forward,
    }


def assistant_summary_payload(
    session,
    action: str,
    summary_date: date,
    entity_query: str = "",
) -> dict[str, Any]:
    """Build read-only, locally calculated operational answers for the chat."""
    if action == "therapist_summary":
        therapist_matches = matching_therapists(session, entity_query)
        if len(therapist_matches) > 1:
            return {
                "outcome": "needs_clarification",
                "message": "I found more than one therapist with that name. Which therapist do you mean?",
                "itemsLabel": "Matching therapists",
                "items": [
                    {"name": item.name, "detail": item.therapist_code}
                    for item in therapist_matches
                ],
                "choices": [
                    {
                        "number": index + 1,
                        "label": f"{item.name} · {item.therapist_code}",
                    }
                    for index, item in enumerate(therapist_matches)
                ],
            }
    if action == "patient_summary":
        patient_matches = matching_patients(session, entity_query)
        if not patient_matches:
            return {
                "outcome": "not_found",
                "message": f"I could not find a patient matching {entity_query}.",
            }
        if len(patient_matches) > 1:
            return {
                "outcome": "needs_clarification",
                "message": "I found more than one patient with that name. Which patient do you mean?",
                "itemsLabel": "Matching patients",
                "items": [
                    {"name": item.name or item.patient_code, "detail": item.patient_code}
                    for item in patient_matches
                ],
                "choices": [
                    {
                        "number": index + 1,
                        "label": f"{item.name or item.patient_code} · {item.patient_code}",
                    }
                    for index, item in enumerate(patient_matches)
                ],
            }
    rows = summary_rows(session, action, summary_date, entity_query)
    scope_labels = {
        "day_summary": "Overall schedule",
        "centre_summary": "Centre",
        "therapist_summary": "Therapist",
        "therapy_summary": "Therapy",
        "patient_summary": "Patient",
    }
    if action in ("centre_summary", "therapist_summary", "therapy_summary", "patient_summary") and not rows:
        return {
            "outcome": "not_found",
            "message": f"I could not find scheduled appointments for {entity_query or 'that selection'} on {summary_date.isoformat()}.",
        }

    active_rows = [row for row in rows if row[0].status in ACTIVE_STATUSES]
    status_counts: dict[str, int] = {}
    therapy_counts: dict[str, int] = {}
    for row in rows:
        status = row[0].status.value
        status_counts[status] = status_counts.get(status, 0) + 1
    for row in active_rows:
        therapy = row.department_name
        therapy_counts[therapy] = therapy_counts.get(therapy, 0) + 1
    scope = scope_labels.get(action, "Schedule")
    if entity_query:
        scope = f"{scope}: {entity_query}"
    return {
        "outcome": "summary",
        "message": f"{scope} summary for {summary_date.isoformat()}.",
        "summary": {
            "date": summary_date.isoformat(),
            "scope": scope,
            "scheduled": len(active_rows),
            "completed": status_counts.get(AppointmentStatus.COMPLETED.value, 0),
            "cancelled": (
                status_counts.get(AppointmentStatus.CANCELLED_PATIENT.value, 0)
                + status_counts.get(AppointmentStatus.CANCELLED_OTHER.value, 0)
            ),
            "needsRescheduling": status_counts.get(AppointmentStatus.NEEDS_RESCHEDULING.value, 0),
            "therapists": len({row[0].therapist_id for row in active_rows}),
            "patients": len({row[0].patient_id for row in active_rows}),
            "byTherapy": [
                {"name": name, "count": count}
                for name, count in sorted(therapy_counts.items(), key=lambda item: (-item[1], item[0]))
            ],
        },
    }


def summary_rows(session, action: str, summary_date: date, entity_query: str = "") -> list[Any]:
    """Return the locally filtered rows used by a summary and its name follow-up."""
    rows = list(session.execute(appointment_query(summary_date, summary_date)))
    if action == "centre_summary":
        rows = [row for row in rows if summary_entity_matches(row.location_name, entity_query)]
    elif action == "therapist_summary":
        therapist_matches = matching_therapists(session, entity_query)
        rows = [row for row in rows if row[0].therapist_id in {
            item.id for item in therapist_matches
        }]
    elif action == "therapy_summary":
        rows = [row for row in rows if summary_entity_matches(row.department_name, entity_query)]
    elif action == "patient_summary":
        patient_matches = matching_patients(session, entity_query)
        rows = [row for row in rows if row[0].patient_id in {
            item.id for item in patient_matches
        }]
    return rows


def assistant_summary_names_payload(session, context: dict[str, Any]) -> dict[str, Any]:
    """List no more than ten distinct patients from the remembered summary scope."""
    summary = context.get("summary")
    if not summary:
        return {
            "outcome": "needs_clarification",
            "message": "Please request a daily, centre, therapist, therapy, or patient summary before asking for names.",
        }
    summary_date = date.fromisoformat(summary["date"])
    rows = summary_rows(session, summary["action"], summary_date, summary.get("entity_query", ""))
    active_rows = [row for row in rows if row[0].status in ACTIVE_STATUSES]
    people: dict[int, dict[str, Any]] = {}
    for row in active_rows:
        people.setdefault(row[0].patient_id, {
            "patientId": row[0].patient_id,
            "name": row.patient_name or row.patient_code,
            "detail": f"{row.patient_code} · {row.department_name} · {row.location_name}",
        })
    ordered = sorted(people.values(), key=lambda item: item["name"].casefold())
    shown = ordered[:ASSISTANT_NAME_LIST_LIMIT]
    return {
        "outcome": "list",
        "message": (
            f"Showing {len(shown)} of {len(ordered)} patients from the most recent summary."
            if len(ordered) > ASSISTANT_NAME_LIST_LIMIT
            else f"Patients from the most recent summary: {len(ordered)}."
        ),
        "itemsLabel": "Patients",
        "items": [{key: value for key, value in item.items() if key != "patientId"} for item in shown],
        "patientIds": [item["patientId"] for item in shown],
    }


def assistant_memory_patient_payload(
    session, context: dict[str, Any], message: str,
) -> dict[str, Any]:
    """Answer a safe follow-up about a patient retained from the prior name list."""
    patient_ids = context.get("last_patient_ids", [])
    if not patient_ids:
        return {
            "outcome": "needs_clarification",
            "message": "Please ask for names from a summary before referring to a patient in that list.",
        }
    patients = [session.get(Patient, patient_id) for patient_id in patient_ids]
    patients = [patient for patient in patients if patient is not None]
    normalized = message.casefold()
    selected = [patient for patient in patients if (
        patient.patient_code.casefold() in normalized
        or (patient.name and patient.name.casefold() in normalized)
    )]
    if not selected and len(patients) == 1:
        selected = patients
    if len(selected) != 1:
        return {
            "outcome": "needs_clarification",
            "message": "The last name list has more than one patient. Please provide the patient's full name or code.",
        }
    patient = selected[0]
    summary = context.get("summary", {})
    summary_date = date.fromisoformat(summary.get("date", date.today().isoformat()))
    rows = [row for row in session.execute(appointment_query(summary_date, summary_date))
            if row[0].patient_id == patient.id]
    if not rows:
        return {
            "outcome": "not_found",
            "message": f"I could not find a scheduled appointment for {patient.name or patient.patient_code} on {summary_date.isoformat()}.",
        }
    therapists = sorted({row.therapist_name for row in rows})
    therapies = sorted({row.department_name for row in rows})
    centres = sorted({row.location_name for row in rows})
    if re.search(r"\b(?:therapist|doctor|clinician|treats)\b", normalized):
        detail = ", ".join(therapists)
        return {
            "outcome": "patient_detail",
            "message": f"{patient.name or patient.patient_code}'s therapist for {summary_date.isoformat()} is {detail}.",
        }
    if re.search(r"\b(?:centre|center|location)\b", normalized):
        return {
            "outcome": "patient_detail",
            "message": f"{patient.name or patient.patient_code}'s appointment location for {summary_date.isoformat()} is {', '.join(centres)}.",
        }
    if re.search(r"\btherapy\b", normalized):
        return {
            "outcome": "patient_detail",
            "message": f"{patient.name or patient.patient_code}'s therapy for {summary_date.isoformat()} is {', '.join(therapies)}.",
        }
    return {
        "outcome": "patient_detail",
        "message": (
            f"{patient.name or patient.patient_code}: {', '.join(therapies)} with {', '.join(therapists)} "
            f"at {', '.join(centres)} on {summary_date.isoformat()}."
        ),
    }


def absence_entity_candidates(
    session, entity_query: str, requested_kind: str,
) -> list[dict[str, Any]]:
    """Find exact active patient or therapist matches for an absence request."""
    normalized = entity_query.casefold()
    candidates: list[dict[str, Any]] = []
    if requested_kind in ("patient", "unknown"):
        for patient in session.scalars(select(Patient).where(Patient.active.is_(True))):
            if normalized in {(patient.name or "").casefold(), patient.patient_code.casefold()}:
                candidates.append({
                    "kind": "patient", "id": patient.id,
                    "name": patient.name or patient.patient_code, "code": patient.patient_code,
                })
    if requested_kind in ("therapist", "unknown"):
        for therapist in session.scalars(select(Therapist).where(Therapist.active.is_(True))):
            if normalized in {therapist.name.casefold(), therapist.therapist_code.casefold()}:
                candidates.append({
                    "kind": "therapist", "id": therapist.id,
                    "name": therapist.name, "code": therapist.therapist_code,
                })
    return sorted(candidates, key=lambda item: (item["kind"], item["name"].casefold(), item["code"]))


def absence_affected_items(session, affected: list[Appointment]) -> list[dict[str, str]]:
    """Describe the appointments released by one absence without listing more than needed."""
    items = []
    for appointment in sorted(affected, key=lambda item: (item.date, item.slot_index, item.id)):
        patient = session.get(Patient, appointment.patient_id)
        therapist = session.get(Therapist, appointment.therapist_id)
        department = session.get(Department, appointment.department_id)
        location = session.get(Location, appointment.location_id)
        items.append({
            "name": patient.name if patient and patient.name else (patient.patient_code if patient else "Patient"),
            "detail": " · ".join(filter(None, [
                department.name if department else None,
                therapist.name if therapist else None,
                location.name if location else None,
                appointment.date.isoformat(),
                slot_label(appointment.slot_index),
            ])),
        })
    return items


def record_assistant_absence(
    session, candidate: dict[str, Any], absence_date: date,
) -> dict[str, Any]:
    """Use the existing absence services and return their operational consequences."""
    if candidate["kind"] == "therapist":
        affected = mark_therapist_absent(session, candidate["id"], absence_date, "Recorded by scheduling assistant")
        return {
            "outcome": "absence_recorded",
            "message": (
                f"{candidate['name']} has been marked unavailable on {absence_date.isoformat()}. "
                f"{len(affected)} appointment(s) now need rescheduling."
            ),
            "itemsLabel": "Appointments released for rescheduling",
            "items": absence_affected_items(session, affected),
            "affected": len(affected),
        }

    affected = mark_patient_absent(session, candidate["id"], absence_date, "Recorded by scheduling assistant")
    groups = [
        replacement_group(session, appointment, find_replacement_candidates(
            session, appointment, monday(absence_date)
        ))
        for appointment in affected
    ]
    replacements = [
        person for group in groups for person in group["candidates"]
    ][:ASSISTANT_NAME_LIST_LIMIT]
    return {
        "outcome": "absence_recorded",
        "message": (
            f"{candidate['name']} has been marked absent on {absence_date.isoformat()}. "
            f"{len(affected)} appointment(s) now need rescheduling."
        ),
        "itemsLabel": "Appointments released for rescheduling",
        "items": absence_affected_items(session, affected),
        "replacementGroups": groups,
        "replacements": replacements,
        "affected": len(affected),
    }


def assistant_absence_choice(context: dict[str, Any], message: str) -> dict[str, Any] | None:
    """Resolve a numbered, code, or role-specific response to an absence choice."""
    pending = context.get("pending_absence")
    if not pending:
        return None
    normalized = message.casefold().strip()
    candidates = pending["candidates"]
    number = re.fullmatch(r"(?:option\s+)?(\d+)", normalized)
    if number:
        index = int(number.group(1)) - 1
        if index in range(len(candidates)):
            return candidates[index]
    matches = [item for item in candidates if normalized == item["code"].casefold()]
    if len(matches) == 1:
        return matches[0]
    role_matches = [item for item in candidates if normalized in {item["kind"], f"the {item['kind']}"}]
    return role_matches[0] if len(role_matches) == 1 else None


def assistant_therapist_summary_choice(
    context: dict[str, Any], message: str,
) -> dict[str, str] | None:
    """Resolve a follow-up to an ambiguous therapist-summary request."""
    pending = context.get("pending_therapist_summary")
    if not pending:
        return None
    normalized = message.casefold().strip()
    choices = pending["choices"]
    positions = {"first": 0, "second": 1, "third": 2, "fourth": 3, "fifth": 4}
    if normalized in positions and positions[normalized] < len(choices):
        return choices[positions[normalized]]
    number = re.fullmatch(r"(?:option\s+)?(\d+)", normalized)
    if number:
        index = int(number.group(1)) - 1
        if index in range(len(choices)):
            return choices[index]
    matches = [item for item in choices if normalized in {
        item["code"].casefold(), item["name"].casefold(),
    }]
    return matches[0] if len(matches) == 1 else None


def assistant_patient_summary_choice(
    context: dict[str, Any], message: str,
) -> dict[str, str] | None:
    """Resolve a follow-up to an ambiguous patient-summary request."""
    pending = context.get("pending_patient_summary")
    if not pending:
        return None
    normalized = message.casefold().strip()
    choices = pending["choices"]
    number = re.fullmatch(r"(?:option\s+)?(\d+)", normalized)
    if number:
        index = int(number.group(1)) - 1
        if index in range(len(choices)):
            return choices[index]
    matches = [item for item in choices if normalized in {
        item["code"].casefold(), item["name"].casefold(),
    }]
    return matches[0] if len(matches) == 1 else None


def assistant_available_slots_choice(
    context: dict[str, Any], message: str,
) -> dict[str, str] | None:
    """Resolve a therapist choice requested for an ambiguous availability lookup."""
    pending = context.get("pending_available_slots")
    if not pending:
        return None
    normalized = message.casefold().strip()
    choices = pending["choices"]
    number = re.fullmatch(r"(?:option\s+)?(\d+)", normalized)
    if number:
        index = int(number.group(1)) - 1
        if index in range(len(choices)):
            return choices[index]
    matches = [item for item in choices if normalized in {
        item["code"].casefold(), item["name"].casefold(),
    }]
    return matches[0] if len(matches) == 1 else None


def assistant_attention_payload(
    session, summary_date: date, action: str, entity_query: str = "",
) -> dict[str, Any]:
    """Return disrupted appointments or recorded absences without changing data."""
    if action == "absence_summary":
        therapist_absences = list(session.scalars(select(TherapistAbsence).where(
            TherapistAbsence.date == summary_date
        )))
        patient_absences = list(session.scalars(select(PatientAbsence).where(
            PatientAbsence.date == summary_date
        )))
        items = [
            {"kind": "Therapist", "name": (session.get(Therapist, item.therapist_id).name), "reason": item.reason or "No reason recorded"}
            for item in therapist_absences
        ] + [
            {"kind": "Patient", "name": (session.get(Patient, item.patient_id).name or session.get(Patient, item.patient_id).patient_code), "reason": item.reason or "No reason recorded"}
            for item in patient_absences
        ]
        return {"outcome": "list", "message": f"Absences for {summary_date.isoformat()}.", "items": items}

    if action == "needs_attention":
        rows = list(session.execute(appointment_query(summary_date, summary_date).where(
            Appointment.status == AppointmentStatus.NEEDS_RESCHEDULING
        )))
        items = [{
            "name": row.patient_name or row.patient_code,
            "detail": f"{row.department_name} · {row.therapist_name} · {slot_label(row[0].slot_index)}",
        } for row in rows]
        return {"outcome": "list", "message": f"Appointments needing rescheduling on {summary_date.isoformat()}.", "items": items}

    rows = list(session.execute(appointment_query(summary_date, summary_date).where(
        Appointment.status.in_(ACTIVE_STATUSES)
    )))
    occupied = {(row[0].therapist_id, row[0].slot_index) for row in rows}
    absent_therapists = set(session.scalars(select(TherapistAbsence.therapist_id).where(
        TherapistAbsence.date == summary_date
    )))
    available_slots: dict[int, set[int]] = {}
    for item in session.scalars(select(TherapistAvailability).where(
        TherapistAvailability.day_of_week == summary_date.weekday(),
        TherapistAvailability.available.is_(True),
    )):
        available_slots.setdefault(item.therapist_id, set()).add(item.slot_index)
    therapists = list(session.scalars(
        select(Therapist).where(Therapist.active.is_(True)).order_by(Therapist.name)
    ))
    if entity_query:
        matched_therapists = matching_therapists(session, entity_query)
        if not matched_therapists:
            return {
                "outcome": "not_found",
                "message": (
                    f"I could not find an active therapist matching {entity_query} "
                    f"on {summary_date.isoformat()}."
                ),
            }
        if len(matched_therapists) > 1:
            return {
                "outcome": "needs_clarification",
                "message": "I found more than one therapist with that name. Which therapist do you mean?",
                "itemsLabel": "Matching therapists",
                "items": [
                    {"name": item.name, "detail": item.therapist_code}
                    for item in matched_therapists
                ],
                "choices": [
                    {
                        "number": index + 1,
                        "label": f"{item.name} · {item.therapist_code}",
                    }
                    for index, item in enumerate(matched_therapists)
                ],
            }
        therapists = matched_therapists
    items = []
    for therapist in therapists:
        if therapist.id in absent_therapists:
            continue
        for slot in range(len(SLOTS)):
            if therapist.id in available_slots and slot not in available_slots[therapist.id]:
                continue
            if (therapist.id, slot) not in occupied:
                items.append({"name": therapist.name, "detail": f"{therapist.location.name} · {slot_label(slot)}"})
                if len(items) == 20:
                    break
        if len(items) == 20:
            break
    if entity_query:
        therapist = therapists[0]
        return {
            "outcome": "list",
            "message": (
                f"Available slots for {therapist.name} on {summary_date.isoformat()}: "
                f"{len(items)}."
            ),
            "itemsLabel": "Available slots",
            "items": items,
        }
    return {
        "outcome": "list",
        "message": f"First available therapist slots on {summary_date.isoformat()}.",
        "itemsLabel": "Available slots",
        "items": items,
    }


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


def overview_payload(today: date | None = None) -> dict[str, Any]:
    """Return the counts and operational health shown in the shell."""
    today = today or date.today()
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
            Appointment.status == AppointmentStatus.NEEDS_RESCHEDULING,
            Appointment.id.notin_(reserved_source_ids()),
        )) or 0
        # The isolated fixture has no import staging tables or import workflow.
        warning_count = 0 if WORKSPACE_CONTEXT.get() is not None else (
            session.scalar(select(func.count(ImportWarning.id))) or 0
        )
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
            Appointment.status.in_(ACTIVE_STATUSES) | (
                (Appointment.status == AppointmentStatus.NEEDS_RESCHEDULING)
                & Appointment.id.notin_(reserved_source_ids())
            )
        )))
        therapists = [{
            "id": item.id,
            "code": item.therapist_code,
            "name": item.name,
            "department": item.department.name,
            "departmentId": item.department_id,
            "location": item.location.name,
            "locationId": item.location_id,
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
        "calendarBreaks": [{
            "afterSlot": before.slot_index,
            "start": before.end_time.strftime("%H:%M"),
            "end": after.start_time.strftime("%H:%M"),
            "label": "Lunch break",
        } for before, after in zip(SLOTS, SLOTS[1:])
            if before.end_time < after.start_time
            and not CONSECUTIVE_TO_NEXT[before.slot_index]],
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
            .where(
                Appointment.status.in_((AppointmentStatus.NEEDS_RESCHEDULING, AppointmentStatus.CANCELLED_PATIENT)),
                Appointment.id.notin_(reserved_source_ids()),
            )
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
    # Azure Container Instances forwards the public endpoint through an HTTP/1.1
    # proxy.  Match that protocol so compact JSON API responses are flushed and
    # forwarded consistently alongside static assets.
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, directory=str(WEB_DIR), **kwargs)

    def log_message(self, format_string: str, *args) -> None:
        safe_args = list(args)
        if safe_args and isinstance(safe_args[0], str):
            safe_args[0] = sanitized_request_line(safe_args[0])
        print(f"[{self.log_date_time_string()}] {format_string % tuple(safe_args)}")

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

    def send_text(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        """Send a compact plain-text response required by Meta verification."""
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
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

    def read_raw_body(self, maximum_bytes: int = MAX_WEBHOOK_BODY_BYTES) -> bytes:
        """Read a bounded raw request body before signature verification."""
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Invalid content length") from exc
        if length < 1:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Request body is required")
        if length > maximum_bytes:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request body is too large")
        return self.rfile.read(length)

    def handle_whatsapp_webhook(self) -> None:
        """Authenticate and acknowledge Meta events without scheduling side effects."""
        settings = webhook_settings_from_environment()
        if settings is None:
            raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "WhatsApp webhook is not configured")
        raw_body = self.read_raw_body()
        if not valid_webhook_signature(
            raw_body, self.headers.get("X-Hub-Signature-256"), settings.app_secret
        ):
            raise ApiError(HTTPStatus.UNAUTHORIZED, "Invalid WhatsApp webhook signature")
        try:
            payload = json.loads(raw_body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, "WhatsApp webhook body must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "WhatsApp webhook body must be an object")
        if os.getenv("FLOW_TEST_LIVE", "false").lower() == "true":
            from flow_test.mount import route_webhook

            try:
                payload = route_webhook(payload)
            except Exception:
                raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "Could not save test webhook event") from None
        summary = webhook_event_summary(payload)
        print(
            "Received authenticated WhatsApp webhook "
            f"object={summary['object']} entries={summary['entryCount']} "
            f"statusCounts={summary.get('statusCounts', {})} "
            f"failureCodes={summary.get('failureCodes', {})}"
        )
        phone_number_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
        if phone_number_id:
            try:
                with SessionLocal() as session:
                    recorded = record_button_clicks(session, payload, phone_number_id)
                    queued = queue_help_replies(session, payload, phone_number_id)
            except Exception as exc:
                print(f"WhatsApp webhook storage error type={type(exc).__name__}")
                raise ApiError(HTTPStatus.SERVICE_UNAVAILABLE, "Could not save webhook event") from None
            if recorded:
                print(f"WhatsApp button clicks recorded={recorded}")
            if queued:
                print(f"WhatsApp Help auto-replies queued={queued}")
        self.send_json({"ok": True})

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/test" or parsed.path.startswith("/test/"):
                from flow_test.mount import dispatch

                dispatch(self)
                return
            if parsed.path == "/webhooks/whatsapp":
                self.send_text(whatsapp_webhook_challenge(parse_qs(parsed.query)))
                return
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
            if parsed.path == "/api/assistant/status":
                self.send_json({"configured": azure_openai_settings() is not None})
                return
            match = re.fullmatch(r"/api/appointments/(\d+)/replacement-candidates", parsed.path)
            if match:
                self.replacement_candidates(int(match.group(1)))
                return
            match = re.fullmatch(r"/api/appointments/(\d+)/reservation-context", parsed.path)
            if match:
                self.reservation_context(int(match.group(1)))
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
            if parsed.path == "/test" or parsed.path.startswith("/test/"):
                from flow_test.mount import dispatch

                dispatch(self)
                return
            if parsed.path == "/webhooks/whatsapp":
                self.handle_whatsapp_webhook()
                return
            body = self.read_json()
            if parsed.path == "/api/source/initialize":
                with SessionLocal() as session:
                    report = initialize_source_database(session)
                self.send_json(report)
                return
            if parsed.path == "/api/absences":
                self.record_absence(body)
                return
            if parsed.path == "/api/assistant/messages":
                self.handle_assistant_message(body)
                return
            if parsed.path == "/api/assistant/memory/clear":
                self.clear_assistant_memory(body)
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
            match = re.fullmatch(r"/api/appointments/(\d+)/(reservation-preview|reserve)", parsed.path)
            if match:
                self.admin_reservation(int(match.group(1)), body, match.group(2) == "reserve")
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
            if parsed.path in ("/api/calendar/reservation-preview", "/api/calendar/reserve"):
                self.empty_slot_reservation(body, parsed.path.endswith("/reserve"))
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

    def clear_assistant_memory(self, body: dict[str, Any]) -> None:
        """Clear only short-lived chat context; the visible transcript remains in the browser."""
        clear_assistant_memory(assistant_session_id(body.get("sessionId")))
        self.send_json({"message": "Temporary assistant memory cleared"})

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
            if appointment.status not in RELEASED_STATUSES:
                raise ApiError(HTTPStatus.CONFLICT, "Appointment is not waiting for rescheduling")
            group = replacement_group(
                session,
                appointment,
                find_replacement_candidates(session, appointment, monday(appointment.date)),
            )
        self.send_json(group, HTTPStatus.OK)

    def reservation_context(self, appointment_id: int) -> None:
        """Return the existing replacement context and continuous-visit extent."""
        with SessionLocal() as session:
            source = session.get(Appointment, appointment_id)
            if source is None:
                raise ApiError(HTTPStatus.NOT_FOUND, "Appointment not found")
            if source.status not in RELEASED_STATUSES or is_reserved_source(session, source.id):
                raise ApiError(HTTPStatus.CONFLICT, "This opening is already reserved or is not released")
            slots = [source]
            if source.bundle_id:
                slots = list(session.scalars(select(Appointment).where(
                    Appointment.patient_id == source.patient_id,
                    Appointment.date == source.date,
                    Appointment.bundle_id == source.bundle_id,
                    Appointment.status.in_(RELEASED_STATUSES),
                    Appointment.id.notin_(reserved_source_ids()),
                ).order_by(Appointment.slot_index)))
            group = replacement_group(session, source, find_replacement_candidates(session, source, monday(source.date)))
            group["visitSessions"] = len(slots)
        self.send_json(group)

    def admin_reservation(self, appointment_id: int, body: dict[str, Any], save: bool) -> None:
        """Preview warnings or explicitly reserve the chosen replacement locally."""
        patient_id = body.get("patientId")
        if type(patient_id) is not int or patient_id <= 0:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Choose a patient from the patient list")
        for name in ("wholeVisit", "overrideWarnings"):
            if name in body and type(body[name]) is not bool:
                raise ApiError(HTTPStatus.BAD_REQUEST, f"{name} must be true or false")
        try:
            with SessionLocal() as session:
                if not save:
                    result = preview_admin_reservation(session, appointment_id, patient_id, body.get("wholeVisit", True))
                else:
                    result = reserve_admin_slot(
                        session, source_id=appointment_id, patient_id=patient_id,
                        whole_visit=body.get("wholeVisit", True),
                        request_id=body.get("requestId"), preview_token=body.get("previewToken"),
                        override_warnings=body.get("overrideWarnings", False),
                        mode=body.get("mode", "without_messaging"), admin_user="scheduler_manager",
                    )
                    session.commit()
                    result["message"] = "Reservation confirmed without messaging"
        except ValueError as exc:
            raise ApiError(HTTPStatus.CONFLICT, str(exc)) from exc
        self.send_json(result)

    def empty_slot_reservation(self, body: dict[str, Any], save: bool) -> None:
        """Preview or save a manual appointment from an empty calendar cell."""
        for name, label in (
            ("patientId", "patient"), ("therapistId", "doctor"),
            ("departmentId", "therapy"), ("locationId", "branch"), ("slot", "session time"),
        ):
            if type(body.get(name)) is not int or body[name] < (0 if name == "slot" else 1):
                raise ApiError(HTTPStatus.BAD_REQUEST, f"Choose a valid {label}")
        if "overrideWarnings" in body and type(body["overrideWarnings"]) is not bool:
            raise ApiError(HTTPStatus.BAD_REQUEST, "overrideWarnings must be true or false")
        if not isinstance(body.get("date"), str):
            raise ApiError(HTTPStatus.BAD_REQUEST, "Choose a valid appointment date")
        opening = EmptySlot(
            date=parse_date(body.get("date")), slot_index=body["slot"],
            therapist_id=body["therapistId"], department_id=body["departmentId"],
            location_id=body["locationId"],
        )
        try:
            with SessionLocal() as session:
                if save:
                    result = reserve_admin_slot(
                        session, source_id=None, opening=opening, whole_visit=False,
                        patient_id=body["patientId"], request_id=body.get("requestId"),
                        preview_token=body.get("previewToken"),
                        override_warnings=body.get("overrideWarnings", False),
                        mode=body.get("mode", "without_messaging"), admin_user="scheduler_manager",
                    )
                    session.commit()
                    result["message"] = "Appointment confirmed without messaging"
                else:
                    result = preview_empty_slot(session, opening, body["patientId"])
        except ValueError as exc:
            raise ApiError(HTTPStatus.CONFLICT, str(exc)) from exc
        self.send_json(result)

    def cancel_appointment(self, appointment_id: int) -> None:
        with SessionLocal() as session:
            appointment = session.get(Appointment, appointment_id)
            if appointment is None:
                raise ApiError(HTTPStatus.NOT_FOUND, "Appointment not found")
            if appointment.status not in ACTIVE_STATUSES:
                raise ApiError(HTTPStatus.CONFLICT, "Only an active appointment can be cancelled")
            payload = cancellation_payload(session, appointment)
        self.send_json(payload)

    def handle_assistant_message(self, body: dict[str, Any]) -> None:
        """Execute a constrained assistant request using short-lived browser context."""
        message = str(body.get("message", "")).strip()
        if not message:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Enter a scheduling request")
        if len(message) > 1_000:
            raise ApiError(HTTPStatus.BAD_REQUEST, "Keep the scheduling request under 1,000 characters")
        context = assistant_memory(assistant_session_id(body.get("sessionId")))
        restore_assistant_summary_memory(context, body.get("memory"))
        chosen_therapist = assistant_therapist_summary_choice(context, message)
        if chosen_therapist is not None:
            try:
                summary_date = parse_date(
                    context["pending_therapist_summary"]["date"], "summary date"
                )
            except ApiError:
                context.pop("pending_therapist_summary", None)
                self.send_json({
                    "message": "The pending therapist choice expired. Please request the summary again.",
                    "outcome": "needs_clarification",
                })
                return
            context.pop("pending_therapist_summary", None)
            with SessionLocal() as session:
                payload = assistant_summary_payload(
                    session, "therapist_summary", summary_date, chosen_therapist["name"]
                )
            if payload["outcome"] == "summary":
                context["summary"] = {
                    "action": "therapist_summary", "date": summary_date.isoformat(),
                    "entity_query": chosen_therapist["name"],
                }
                context.pop("last_patient_ids", None)
            payload["memoryContext"] = assistant_memory_snapshot(context)
            self.send_json(payload)
            return
        chosen_patient = assistant_patient_summary_choice(context, message)
        if chosen_patient is not None:
            try:
                summary_date = parse_date(
                    context["pending_patient_summary"]["date"], "summary date"
                )
            except ApiError:
                context.pop("pending_patient_summary", None)
                self.send_json({
                    "message": "The pending patient choice expired. Please request the summary again.",
                    "outcome": "needs_clarification",
                })
                return
            context.pop("pending_patient_summary", None)
            with SessionLocal() as session:
                payload = assistant_summary_payload(
                    session, "patient_summary", summary_date, chosen_patient["code"]
                )
            if payload["outcome"] == "summary":
                context["summary"] = {
                    "action": "patient_summary", "date": summary_date.isoformat(),
                    "entity_query": chosen_patient["code"],
                }
                context.pop("last_patient_ids", None)
            payload["memoryContext"] = assistant_memory_snapshot(context)
            self.send_json(payload)
            return
        chosen_available_therapist = assistant_available_slots_choice(context, message)
        if chosen_available_therapist is not None:
            try:
                summary_date = parse_date(
                    context["pending_available_slots"]["date"], "summary date"
                )
            except ApiError:
                context.pop("pending_available_slots", None)
                self.send_json({
                    "message": "The pending therapist choice expired. Please request availability again.",
                    "outcome": "needs_clarification",
                })
                return
            context.pop("pending_available_slots", None)
            with SessionLocal() as session:
                payload = assistant_attention_payload(
                    session, summary_date, "empty_slots", chosen_available_therapist["code"]
                )
            self.send_json(payload)
            return
        chosen_absence = assistant_absence_choice(context, message)
        if chosen_absence is not None:
            try:
                absence_date = parse_date(context["pending_absence"]["date"], "absence date")
            except ApiError:
                context.pop("pending_absence", None)
                self.send_json({
                    "message": "The pending absence choice expired. Please make the absence request again.",
                    "outcome": "needs_clarification",
                })
                return
            context.pop("pending_absence", None)
            with SessionLocal() as session:
                self.send_json(record_assistant_absence(session, chosen_absence, absence_date))
            return

        if is_name_list_request(message):
            with SessionLocal() as session:
                payload = assistant_summary_names_payload(session, context)
            context["last_patient_ids"] = payload.pop("patientIds", [])
            payload["memoryContext"] = assistant_memory_snapshot(context)
            self.send_json(payload)
            return

        if is_memory_patient_question(message) and not is_explicit_patient_summary_request(message):
            with SessionLocal() as session:
                self.send_json(assistant_memory_patient_payload(session, context, message))
            return

        action = interpret_chat_action(message)
        summary_actions = {
            "day_summary", "centre_summary", "therapist_summary", "therapy_summary",
            "patient_summary",
        }
        if action["action"] in summary_actions | {"needs_attention", "absence_summary", "empty_slots"}:
            try:
                summary_date = parse_date(action["date"], "summary date")
            except ApiError:
                self.send_json({
                    "message": "Which date should I use? Please give a calendar date.",
                    "outcome": "needs_clarification",
                })
                return
            with SessionLocal() as session:
                if action["action"] in summary_actions:
                    payload = assistant_summary_payload(
                        session, action["action"], summary_date, action["entity_query"]
                    )
                    if action["action"] == "therapist_summary" and payload["outcome"] == "needs_clarification":
                        matches = matching_therapists(session, action["entity_query"])
                        if len(matches) > 1:
                            context["pending_therapist_summary"] = {
                                "date": summary_date.isoformat(),
                                "choices": [
                                    {"name": item.name, "code": item.therapist_code}
                                    for item in matches
                                ],
                            }
                    if action["action"] == "patient_summary" and payload["outcome"] == "needs_clarification":
                        matches = matching_patients(session, action["entity_query"])
                        if len(matches) > 1:
                            context["pending_patient_summary"] = {
                                "date": summary_date.isoformat(),
                                "choices": [
                                    {"name": item.name or item.patient_code, "code": item.patient_code}
                                    for item in matches
                                ],
                            }
                    if payload["outcome"] == "summary":
                        context["summary"] = {
                            "action": action["action"],
                            "date": summary_date.isoformat(),
                            "entity_query": action["entity_query"],
                        }
                        context.pop("last_patient_ids", None)
                        context.pop("pending_therapist_summary", None)
                        context.pop("pending_patient_summary", None)
                else:
                    payload = assistant_attention_payload(
                        session, summary_date, action["action"], action["entity_query"]
                    )
                    if action["action"] == "empty_slots" and payload["outcome"] == "needs_clarification":
                        matches = matching_therapists(session, action["entity_query"])
                        if len(matches) > 1:
                            context["pending_available_slots"] = {
                                "date": summary_date.isoformat(),
                                "choices": [
                                    {"name": item.name, "code": item.therapist_code}
                                    for item in matches
                                ],
                            }
            if payload["outcome"] == "summary":
                payload["memoryContext"] = assistant_memory_snapshot(context)
            self.send_json(payload)
            return
        if action["action"] == "list_summary_names":
            with SessionLocal() as session:
                payload = assistant_summary_names_payload(session, context)
            context["last_patient_ids"] = payload.pop("patientIds", [])
            payload["memoryContext"] = assistant_memory_snapshot(context)
            self.send_json(payload)
            return
        if action["action"] == "last_patient_details":
            with SessionLocal() as session:
                self.send_json(assistant_memory_patient_payload(session, context, message))
            return
        if action["action"] == "mark_absent":
            if not action["entity_query"]:
                self.send_json({
                    "message": "Which patient or therapist should I mark absent?",
                    "outcome": "needs_clarification",
                })
                return
            try:
                absence_date = parse_date(action["date"], "absence date")
            except ApiError:
                self.send_json({
                    "message": "Which date should I use for the absence? Please give a calendar date.",
                    "outcome": "needs_clarification",
                })
                return
            requested_kind = action.get("absence_kind", "unknown")
            if requested_kind not in {"patient", "therapist", "unknown"}:
                requested_kind = "unknown"
            with SessionLocal() as session:
                candidates = absence_entity_candidates(
                    session, action["entity_query"], requested_kind
                )
                if not candidates:
                    self.send_json({
                        "message": (
                            f"I could not find an active {requested_kind if requested_kind != 'unknown' else 'patient or therapist'} "
                            f"named {action['entity_query']}. Nothing was changed."
                        ),
                        "outcome": "not_found",
                    })
                    return
                if len(candidates) == 1:
                    context.pop("pending_absence", None)
                    self.send_json(record_assistant_absence(session, candidates[0], absence_date))
                    return
            context["pending_absence"] = {
                "date": absence_date.isoformat(), "candidates": candidates,
            }
            self.send_json({
                "message": "I found more than one active match. Please choose the person to mark absent.",
                "outcome": "needs_clarification",
                "choices": [
                    {
                        "number": index + 1,
                        "label": f"{item['name']} · {item['code']} · {item['kind'].title()}",
                    }
                    for index, item in enumerate(candidates)
                ],
            })
            return
        if action["action"] != "cancel_appointment":
            self.send_json({
                "message": action.get("reply") or "I can help with summaries, names from the last summary, absences, available slots, and one dated cancellation.",
                "outcome": "not_executed",
            })
            return
        if not action["patient_query"]:
            self.send_json({
                "message": "Which patient's appointment should I cancel?",
                "outcome": "needs_clarification",
            })
            return
        try:
            appointment_date = parse_date(action["date"], "appointment date")
        except ApiError:
            self.send_json({
                "message": "Which appointment date should I use? Please give it as a calendar date.",
                "outcome": "needs_clarification",
            })
            return
        with SessionLocal() as session:
            matches = matching_active_appointments(session, action["patient_query"], appointment_date)
            if not matches:
                self.send_json({
                    "message": (
                        f"I could not find an active appointment for {action['patient_query']} "
                        f"on {appointment_date.isoformat()}. Nothing was changed."
                    ),
                    "outcome": "not_found",
                })
                return
            if len(matches) > 1:
                self.send_json({
                    "message": "I found more than one active appointment. Please use the appointment controls to choose one.",
                    "outcome": "needs_clarification",
                    "appointments": [serialize_appointment(item) for item in matches],
                })
                return
            appointment = matches[0][0]
            payload = cancellation_payload(session, appointment)
            cancelled = serialize_appointment(matches[0])
        candidates = len(payload["replacements"])
        payload.update({
            "outcome": "cancelled",
            "appointment": cancelled,
            "message": (
                f"{cancelled['patientName']}'s {cancelled['therapy']} session on "
                f"{cancelled['date']} at {cancelled['time']} has been cancelled. "
                + (
                    f"I found {candidates} possible replacement {('child' if candidates == 1 else 'children')}."
                    if candidates else "There are no eligible replacement children right now."
                )
            ),
        })
        self.send_json(payload)

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
        workspace_previews()[preview_id] = {
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
        preview = workspace_previews().get(preview_id)
        if preview is None:
            raise ApiError(HTTPStatus.NOT_FOUND, "This preview has expired. Generate it again.")
        result = preview["result"]
        if result.status not in ("OPTIMAL", "FEASIBLE") or any(result.validation.values()):
            raise ApiError(HTTPStatus.CONFLICT, "This preview did not pass validation")
        with SessionLocal() as session:
            added = save_proposed_schedule(
                session, result, preview["start"], preview["horizon_days"]
            )
        workspace_previews().pop(preview_id, None)
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


class ParentResponseDemoHandler(SimpleHTTPRequestHandler):
    """Serve only the isolated fictional-response workspace on a loopback server."""

    def log_message(self, format_string: str, *args) -> None:
        pass

    def _send_demo_json(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _demo_booking_payload(self, needs_patient: bool = True) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 8192:
                raise ValueError("A small booking JSON request is required")
            payload = json.loads(self.rfile.read(length))
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Invalid booking JSON request") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("run_id"), str):
            raise ValueError("A demo run ID is required")
        for key in ("source_id", "patient_id") if needs_patient else ("source_id",):
            if type(payload.get(key)) is not int or payload[key] <= 0:
                raise ValueError(f"A valid {key} is required")
        for key in ("whole_visit", "override_warnings"):
            if key in payload and type(payload[key]) is not bool:
                raise ValueError(f"{key} must be true or false")
        return payload

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/demo/state":
            self._send_demo_json(self.server.demo_workspace.state())
            return
        assets = {
            "/": ("response_demo.html", "text/html; charset=utf-8"),
            "/response_demo.css": ("response_demo.css", "text/css; charset=utf-8"),
            "/response_demo.js": ("response_demo.js", "text/javascript; charset=utf-8"),
        }
        if path not in assets:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        filename, content_type = assets[path]
        body = (WEB_DIR / filename).read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        # A local demo may only be changed by its own page or a trusted local CLI.
        origin = self.headers.get("Origin")
        expected_origin = f"http://127.0.0.1:{self.server.server_port}"
        if origin and origin != expected_origin:
            self._send_demo_json({"error": "Use the local demo page"}, 403)
            return
        try:
            if self.path == "/api/demo/run":
                self.server.demo_workspace.start()
                self._send_demo_json(self.server.demo_workspace.state(), 202)
            elif self.path == "/api/demo/reset":
                self.server.demo_workspace.reset()
                self._send_demo_json(self.server.demo_workspace.state())
            elif self.path == "/api/demo/slot":
                payload = self._demo_booking_payload(needs_patient=False)
                self._send_demo_json(self.server.demo_workspace.slot_details(
                    payload["run_id"], payload["source_id"],
                ))
            elif self.path == "/api/demo/booking-preview":
                self._send_demo_json(self.server.demo_workspace.preview_booking(
                    self._demo_booking_payload(),
                ))
            elif self.path == "/api/demo/reserve":
                payload = self._demo_booking_payload()
                if not isinstance(payload.get("preview_token"), str) or not isinstance(payload.get("request_id"), str):
                    raise ValueError("Review a booking preview before reserving")
                result = self.server.demo_workspace.reserve_booking(payload)
                self._send_demo_json({
                    "reservation": result, "state": self.server.demo_workspace.state(),
                })
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            self._send_demo_json({"error": str(exc)}, 409)


def run_response_demo(port: int = 8766) -> None:
    """Launch the fictional-data screen without initializing the live database."""
    from services.parent_response_demo_workspace import ResponseDemoWorkspace

    server = ThreadingHTTPServer(("127.0.0.1", port), ParentResponseDemoHandler)
    server.demo_workspace = ResponseDemoWorkspace(BASE_DIR / "outputs" / "response_demo_live")
    print(f"Parent response demo is ready at http://127.0.0.1:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def run(host: str = "127.0.0.1", port: int = 8765, *, local_ui_only: bool = False) -> None:
    """Start the local therapy scheduler web application."""
    if local_ui_only:
        # Use the configured original records without workbook imports, unrelated
        # migrations, or automatic WhatsApp sends during local UI review.
        init_admin_booking_tables()
    else:
        init_db()
        with SessionLocal() as session:
            initialize_source_database(session)
    server = ThreadingHTTPServer((host, port), TherapySchedulerHandler)
    help_worker_stop, help_worker = (None, None)
    if not local_ui_only:
        help_worker_stop, help_worker = start_help_reply_worker(SessionLocal)
    print(f"EIC Therapy Scheduler is ready at http://{host}:{port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # Imported lazily so test-only models never enter the main startup path.
        import sys

        if "flow_test.mount" in sys.modules:
            sys.modules["flow_test.mount"].close()
        if help_worker_stop is not None:
            help_worker_stop.set()
            help_worker.join(timeout=2)
        server.server_close()


if __name__ == "__main__":
    run()
