"""Authenticated test-only import and automatic draft endpoints; no Zoho writes."""

from dataclasses import replace
from datetime import date, datetime
import json
from uuid import uuid4

from flow_test.models import FlowSetting
from flow_test.zoho_import import CHECKPOINT_KEY, SOURCE_TIMEZONES, TIMEZONE_KEY, apply_reports, put_setting


def daily_draft(instance, target: date) -> dict:
    """Use the same review/save contract as a manually generated daily draft."""
    import web_app

    result, pinned = instance.flow.preview_daily(target)
    result = replace(result, appointments=[item for item in result.appointments if item.date == target])
    preview_id = str(uuid4())
    offer_on_save = target == instance.flow.next_offer_date()
    instance.previews[preview_id] = {"result": result, "start": target, "horizon_days": 1,
                                     "created": datetime.utcnow(), "offer_on_save": offer_on_save}
    payload = web_app.serialize_preview(result, target, target)
    payload.update({"previewId": preview_id, "planningStart": target.isoformat(),
                    "planningMode": "day", "planningDays": 1,
                    "newSessions": sum(item not in pinned for item in result.appointments),
                    "scheduledOnSelectedDay": len(result.appointments),
                    "autoOfferOnSave": offer_on_save, "liveMessaging": instance.flow.live})
    return payload


def dispatch_zoho(request, instance, path: str) -> None:
    import web_app

    flow = instance.flow
    if path == "/api/zoho/status":
        with flow.sessions() as session:
            item = session.get(FlowSetting, "zoho_test_sync")
            checkpoint = session.get(FlowSetting, CHECKPOINT_KEY)
            source_zone = session.get(FlowSetting, TIMEZONE_KEY)
            source_cache = session.get(FlowSetting, "zoho_test_source")
            request.send_json({"source": "nidhin.kumar_turnb/test-gokul",
                               "sync": json.loads(item.value) if item else None,
                               "checkpoint": json.loads(checkpoint.value) if checkpoint else None,
                               "planStartSource": "Added_Time",
                               "sourceTimezones": SOURCE_TIMEZONES,
                               "reconciliationRequired": (checkpoint is not None or source_cache is not None) and (
                                   source_zone is None or json.loads(source_zone.value) != SOURCE_TIMEZONES)})
        return
    if path == "/api/zoho/draft":
        with flow.sessions() as session:
            item = session.get(FlowSetting, "zoho_test_draft_date")
            value = json.loads(item.value) if item else None
            target = date.fromisoformat(value) if value else None
        # Refresh on opening: source, absences or saved bookings may have changed since 3 AM.
        if target is None or target != flow.next_offer_date():
            request.send_json({"draft": None})
        else:
            request.send_json({"draft": daily_draft(instance, target)})
        return
    body = request.read_json()
    try:
        if body.get("syncWindow") is not None and body.get("sourceTimezones") != SOURCE_TIMEZONES:
            raise ValueError("Source timestamp timezone mismatch; deploy the Added Time IST / Modified Time IST job client")
        # The automatic run always drafts tomorrow in IST, never publishes or saves.
        target = flow.next_offer_date()
        if target.weekday() == 6:
            target = None
        result = apply_reports(flow, body["reports"], window=body.get("syncWindow"),
                               full_snapshot=body.get("fullSnapshot", False))
        instance.previews.clear()
        with flow.sessions.begin() as session:
            put_setting(session, "zoho_test_draft_date", target.isoformat() if target else None)
    except (ValueError, KeyError, TypeError) as exc:
        message = str(exc) if isinstance(exc, ValueError) else "Missing or invalid test import fields"
        raise web_app.ApiError(409, message) from None
    # Import and planning are separate outcomes: a valid sync must not be described
    # as rolled back if the solver cannot prepare its draft.
    try:
        draft = daily_draft(instance, target) if target else None
    except ValueError as exc:
        request.send_json({**result, "draftDate": None, "draftError": str(exc), "messages_sent": 0})
        return
    request.send_json({**result, "draftDate": target.isoformat() if target else None,
                       "draftSessions": len(draft["appointments"]) if draft else 0,
                       "messages_sent": 0})
