"""Authenticated calendar export using server-held OAuth and short state locks."""

import threading

from flow_test.zoho_export import acknowledge_export, export_preview, finish_export, prepare_export
from flow_test.zoho_push import REQUIRED_SCOPES, export_settings, run_push, verify_target
from services.zoho_creator_service import refresh_access_token


_push_lock = threading.Lock()


def push_calendar(instance, target) -> dict:
    """Push one saved test date without holding the scheduling lock over HTTP."""
    if not _push_lock.acquire(blocking=False):
        raise ValueError("A Zoho push is already running. Wait for it to finish, then refresh the preview.")
    try:
        settings = export_settings()
        verify_target(settings)
        if not all((settings.client_id, settings.client_secret, settings.refresh_token)):
            raise ValueError("Zoho export is not configured on this app. Deploy with the saved test Zoho connection.")
        try:
            connection = refresh_access_token(settings)
        except (ValueError, OSError):
            raise ValueError("Zoho could not reconnect. Check the saved test connection and redeploy; no schedule was finalised.") from None
        if not REQUIRED_SCOPES.issubset(set(connection.get("scope", "").split())):
            raise ValueError("Zoho needs permission to update records and read Scheduler fields. Run python -m flow_test.zoho_authorize once, then redeploy.")

        def exchange(operation, body):
            if operation == "preview":
                return export_preview(instance.flow, target)
            if operation == "prepare":
                result = prepare_export(instance.flow, target)
                with instance.flow.lock:
                    instance.previews.clear()
                return result
            if operation == "ack":
                return acknowledge_export(instance.flow, body["batchId"], body["appointmentId"],
                                          body["fingerprint"], body["recordId"])
            return finish_export(instance.flow, body["batchId"])

        return run_push(settings, connection["access_token"], "", "", target, apply=True, exchange=exchange)
    finally:
        _push_lock.release()


def dispatch_calendar_push(request, instance) -> None:
    import web_app

    body = request.read_json()
    target = web_app.parse_date(body.get("date"), "date")
    try:
        result = push_calendar(instance, target)
    except ValueError as exc:
        raise web_app.ApiError(409, str(exc)) from None
    except OSError:
        raise web_app.ApiError(503, "Zoho is unavailable. Refresh the preview and retry; unfinished batches are retained.") from None
    request.send_json(result)
