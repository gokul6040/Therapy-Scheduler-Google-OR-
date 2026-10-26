"""Run the existing UI handlers against the selected isolated test session."""

from dataclasses import replace
from datetime import datetime
import re
import secrets
from urllib.parse import urlsplit
from uuid import uuid4
from zoneinfo import ZoneInfo

from flow_test.service import SETTINGS


GET_ROUTES = (
    r"/api/(overview|schedule|patients|therapists|operations|health)",
    r"/api/zoho/(status|draft)",
    r"/api/patients/\d+/preferences",
    r"/api/appointments/\d+/(replacement-candidates|reservation-context)",
)
POST_ROUTES = (
    r"/api/reset",
    r"/api/zoho/import",
    r"/api/zoho/export/(preview|prepare|ack|finish|push)",
    r"/api/absences",
    r"/api/patients/\d+/preferences",
    r"/api/appointments/\d+/(cancel|move|reservation-preview|reserve)",
    r"/api/calendar/(reservation-preview|reserve)",
    r"/api/schedules/generate",
    r"/api/schedules/[0-9a-f-]+/save",
)


def generate_daily_preview(request, instance) -> None:
    """Reuse the daily flow's inputs, with the shared UI's review/save contract."""
    import web_app

    body = request.read_json()
    target = web_app.parse_date(body.get("start"), "start")
    seconds = web_app.parse_search_seconds(body.get("searchSeconds"))
    try:
        result, pinned = instance.flow.preview_daily(
            target, body.get("patients"), settings=replace(SETTINGS, max_time_seconds=seconds),
        )
    except ValueError as exc:
        raise web_app.ApiError(400, str(exc)) from exc
    # The solver counts the whole week. Only selected-day rows may be displayed
    # or passed to a one-day save; including other pinned days would duplicate
    # them because the existing saver checks only its requested date range.
    day_result = replace(result, appointments=[a for a in result.appointments if a.date == target])
    new_count = sum(a not in pinned for a in day_result.appointments)
    offer_on_save = target == instance.flow.next_offer_date()
    preview_id = str(uuid4())
    instance.previews[preview_id] = {
        "result": day_result, "start": target, "horizon_days": 1,
        "created": datetime.utcnow(),
        "offer_on_save": offer_on_save,
    }
    payload = web_app.serialize_preview(day_result, target, target)
    payload.update({
        "previewId": preview_id, "planningStart": target.isoformat(),
        "planningMode": "day", "planningDays": 1,
        "newSessions": new_count, "scheduledOnSelectedDay": len(day_result.appointments),
        "autoOfferOnSave": offer_on_save, "liveMessaging": instance.flow.live,
    })
    request.send_json(payload, 201)


def save_daily_preview(request, instance, preview_id: str) -> None:
    """Publish tomorrow's calendar as part of its save, preserving safe retries."""
    import web_app

    request.read_json()
    preview = instance.previews.get(preview_id)
    if preview is None:
        raise web_app.ApiError(404, "This preview has expired. Generate it again.")
    if "saved_response" in preview:
        request.send_json(preview["saved_response"])
        return
    try:
        response = instance.flow.save_calendar(
            preview["result"], preview["start"],
            expected_offer_on_save=preview["offer_on_save"],
        )
    except ValueError as exc:
        raise web_app.ApiError(409, str(exc)) from exc
    preview["saved_response"] = response
    request.send_json(response)


def dispatch_shared_api(request, instance) -> bool:
    """Allow local UI workflows with a request-scoped session and previews."""
    path = urlsplit(request.path).path.removeprefix("/test")
    patterns = GET_ROUTES if request.command == "GET" else POST_ROUTES
    local_route = any(re.fullmatch(pattern, path) for pattern in patterns)
    assistant_status = path == "/api/assistant/status" and request.command == "GET"
    if not local_route and not assistant_status:
        return False
    if not secrets.compare_digest(request.headers.get("Authorization", ""), f"Bearer {instance.token}"):
        request.close_connection = True
        request.send_json({"error": "Enter the test admin key to open this workspace."}, 401)
        return True
    if assistant_status:
        request.send_json({"configured": False})
        return True
    import web_app

    if path == "/api/zoho/export/push":
        from flow_test.zoho_calendar import dispatch_calendar_push
        # State helpers take their own locks. External HTTP must not block
        # incoming replies or other calendar reads through the workflow lock.
        dispatch_calendar_push(request, instance)
        return True

    with instance.flow.lock:
        if not hasattr(instance, "previews"):
            instance.previews = {}
        context = web_app.WORKSPACE_CONTEXT.set({
            "sessions": instance.flow.sessions, "previews": instance.previews,
        })
        adapter = web_app.TherapySchedulerHandler.__new__(web_app.TherapySchedulerHandler)
        adapter.__dict__.update(request.__dict__)
        adapter.path = request.path.removeprefix("/test")
        adapter._headers_buffer = []
        try:
            save = re.fullmatch(r"/api/schedules/([0-9a-f-]+)/save", path)
            reservation = re.fullmatch(r"/api/appointments/(\d+)/reserve", path)
            if path.startswith("/api/zoho/export/"):
                from flow_test.zoho_export import dispatch_export
                dispatch_export(adapter, instance, path.rsplit("/", 1)[-1])
            elif path in {"/api/zoho/import", "/api/zoho/status", "/api/zoho/draft"}:
                from flow_test.zoho_routes import dispatch_zoho
                dispatch_zoho(adapter, instance, path)
            elif request.command == "POST" and path == "/api/reset":
                body = adapter.read_json()
                try:
                    result = instance.flow.reset_test_data(body.get("confirmation"))
                except ValueError as exc:
                    raise web_app.ApiError(409, str(exc)) from exc
                instance.previews.clear()
                adapter.send_json(result)
            elif request.command == "POST" and save:
                save_daily_preview(adapter, instance, save[1])
            elif request.command == "POST" and reservation:
                body = adapter.read_json()
                if body.get("mode") != "with_messaging":
                    adapter.admin_reservation(int(reservation[1]), body, save=True)
                else:
                    patient_id = body.get("patientId")
                    if type(patient_id) is not int or patient_id <= 0:
                        raise web_app.ApiError(400, "Choose a patient from the patient list")
                    for name in ("wholeVisit", "overrideWarnings"):
                        if name in body and type(body[name]) is not bool:
                            raise web_app.ApiError(400, f"{name} must be true or false")
                    try:
                        result = instance.flow.reserve_with_message(
                            int(reservation[1]), patient_id=patient_id,
                            request_id=body.get("requestId"), preview_token=body.get("previewToken"),
                            whole_visit=body.get("wholeVisit", True),
                            override_warnings=body.get("overrideWarnings", False),
                        )
                    except ValueError as exc:
                        raise web_app.ApiError(409, str(exc)) from exc
                    adapter.send_json(result)
            elif request.command == "POST" and path == "/api/schedules/generate":
                generate_daily_preview(adapter, instance)
            elif request.command == "GET" and path == "/api/overview":
                local_today = datetime.now(ZoneInfo("Asia/Kolkata")).date()
                adapter.send_json({**web_app.overview_payload(local_today), "planningMode": "day"})
            elif request.command == "GET":
                adapter.do_GET()
            else:
                adapter.do_POST()
        finally:
            web_app.WORKSPACE_CONTEXT.reset(context)
            request.close_connection = adapter.close_connection
    return True
