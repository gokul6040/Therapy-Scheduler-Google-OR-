"""OAuth helpers for the Zoho Creator appointment-schedule integration.

Credentials are deliberately read from the process environment.  They are never
written to the database, source tree, audit log, or HTTP responses.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener


DEFAULT_SCOPES = (
    "ZohoCreator.form.CREATE",
    "ZohoCreator.report.READ",
)


def open_zoho_request(request: Request, timeout: int):
    """Open a Zoho request without the unavailable inherited local proxy.

    The desktop runtime injects a proxy at ``127.0.0.1:9``.  It is not running,
    and retrying an already-opened ``Request`` against another opener leaves its
    proxy destination mutated.  A clean direct opener avoids both behaviours.
    """
    return build_opener(ProxyHandler({})).open(request, timeout=timeout)


@dataclass(frozen=True)
class ZohoCreatorSettings:
    """Non-persistent configuration for a Zoho Creator OAuth client."""

    client_id: str
    client_secret: str
    redirect_uri: str
    accounts_url: str
    api_url: str
    account_owner: str
    app_link_name: str
    form_link_name: str
    report_link_name: str
    plan_detail_report_link_name: str
    refresh_token: str = ""
    scopes: tuple[str, ...] = DEFAULT_SCOPES

    @property
    def configured(self) -> bool:
        """Return whether the minimum secrets and target are available."""
        return bool(
            self.client_id
            and self.client_secret
            and self.redirect_uri
            and self.account_owner
            and self.app_link_name
            and self.form_link_name
        )


def settings_from_environment() -> ZohoCreatorSettings:
    """Load the India Creator target and OAuth client without exposing secrets."""
    return ZohoCreatorSettings(
        client_id=os.environ.get("ZOHO_CREATOR_CLIENT_ID", ""),
        client_secret=os.environ.get("ZOHO_CREATOR_CLIENT_SECRET", ""),
        redirect_uri=os.environ.get(
            "ZOHO_CREATOR_REDIRECT_URI",
            "http://127.0.0.1:8765/oauth/zoho/callback",
        ),
        accounts_url=os.environ.get(
            "ZOHO_CREATOR_ACCOUNTS_URL", "https://accounts.zoho.in"
        ).rstrip("/"),
        api_url=os.environ.get(
            "ZOHO_CREATOR_API_URL", "https://www.zohoapis.in"
        ).rstrip("/"),
        account_owner=os.environ.get(
            "ZOHO_CREATOR_ACCOUNT_OWNER", "christina.sunil_turnb"
        ),
        app_link_name=os.environ.get(
            "ZOHO_CREATOR_APP_LINK_NAME", "therapy-scheduler"
        ),
        form_link_name=os.environ.get(
            "ZOHO_CREATOR_FORM_LINK_NAME", "All_Therapy_Schedules"
        ),
        report_link_name=os.environ.get(
            "ZOHO_CREATOR_REPORT_LINK_NAME", "All_Therapy_Schedules_Report"
        ),
        plan_detail_report_link_name=os.environ.get(
            "ZOHO_CREATOR_PLAN_DETAIL_REPORT_LINK_NAME", "Plan_Detail_Report"
        ),
        refresh_token=os.environ.get("ZOHO_CREATOR_REFRESH_TOKEN", ""),
    )


def authorization_url(settings: ZohoCreatorSettings, state: str) -> str:
    """Build the consent URL for the minimum appointment-sync permissions."""
    query = urlencode({
        "response_type": "code",
        "client_id": settings.client_id,
        "scope": ",".join(settings.scopes),
        "redirect_uri": settings.redirect_uri,
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    })
    return f"{settings.accounts_url}/oauth/v2/auth?{query}"


def exchange_authorization_code(
    settings: ZohoCreatorSettings,
    authorization_code: str,
) -> dict[str, Any]:
    """Exchange a short-lived authorization code for Zoho OAuth tokens."""
    request = Request(
        f"{settings.accounts_url}/oauth/v2/token",
        data=urlencode({
            "grant_type": "authorization_code",
            "client_id": settings.client_id,
            "client_secret": settings.client_secret,
            "redirect_uri": settings.redirect_uri,
            "code": authorization_code,
        }).encode("utf-8"),
        method="POST",
    )
    try:
        with open_zoho_request(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            message = json.loads(detail).get("error") or json.loads(detail).get("message")
        except json.JSONDecodeError:
            message = detail
        raise ValueError(f"Zoho authorization failed: {message or 'unknown error'}") from exc
    except URLError as exc:
        raise ValueError("Zoho authorization service could not be reached") from exc
    if not payload.get("access_token"):
        message = payload.get("error") or payload.get("message") or "no access token returned"
        raise ValueError(f"Zoho authorization failed: {message}")
    return payload


def refresh_access_token(settings: ZohoCreatorSettings) -> dict[str, Any]:
    """Exchange the locally stored Creator refresh token for an access token."""
    if not settings.refresh_token:
        raise ValueError("Zoho automatic reconnection is not configured")
    request = Request(
        f"{settings.accounts_url}/oauth/v2/token",
        data=urlencode({
            "grant_type": "refresh_token",
            "client_id": settings.client_id,
            "client_secret": settings.client_secret,
            "refresh_token": settings.refresh_token,
        }).encode("utf-8"),
        method="POST",
    )
    try:
        with open_zoho_request(request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            message = json.loads(detail).get("error") or json.loads(detail).get("message")
        except json.JSONDecodeError:
            message = detail
        raise ValueError(f"Zoho automatic reconnection failed: {message or 'unknown error'}") from exc
    except URLError as exc:
        raise ValueError("Zoho authorization service could not be reached") from exc
    if not payload.get("access_token"):
        message = payload.get("error") or payload.get("message") or "no access token returned"
        raise ValueError(f"Zoho automatic reconnection failed: {message}")
    return payload


def save_refresh_token(refresh_token: str) -> None:
    """Persist a Creator refresh token in the ignored local .env secret store."""
    if not refresh_token:
        return
    env_path = Path(__file__).resolve().parent.parent / ".env"
    key = "ZOHO_CREATOR_REFRESH_TOKEN"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    replacement = f"{key}={refresh_token}"
    for index, line in enumerate(lines):
        if line.startswith(f"{key}="):
            lines[index] = replacement
            break
    else:
        lines.extend(["", "# Zoho refresh token; generated after one-time consent.", replacement])
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.environ[key] = refresh_token


def create_appointment_schedule(
    settings: ZohoCreatorSettings,
    access_token: str,
    fields: dict[str, Any],
) -> dict[str, Any]:
    """Create one record in the configured Creator appointment form only."""
    endpoint = (
        f"{settings.api_url}/creator/v2.1/data/{settings.account_owner}/"
        f"{settings.app_link_name}/form/{settings.form_link_name}"
    )
    request = Request(
        endpoint,
        # The manager explicitly approved bypassing Creator's obsolete form
        # validation workflow.  This applies only to this API-created
        # appointment row; it does not alter or disable the workflow itself.
        data=json.dumps({
            "data": fields,
            "skip_workflow": ["form_workflow"],
        }).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Zoho-oauthtoken {access_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with open_zoho_request(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            message = json.loads(detail).get("message", detail)
        except json.JSONDecodeError:
            message = detail
        raise ValueError(f"Zoho rejected the appointment: {message}") from exc
    except URLError as exc:
        raise ValueError("Zoho Creator could not be reached") from exc

    if payload.get("code") not in (None, 3000):
        response_data = payload.get("data")
        nested_message = (
            response_data.get("message")
            or response_data.get("error")
            or response_data.get("error_message")
        ) if isinstance(response_data, dict) else None
        message = payload.get("message") or payload.get("error") or nested_message
        if not message:
            message = f"Creator error code {payload.get('code', 'unknown')}"
        raise ValueError(f"Zoho rejected the appointment: {message}")
    return payload


def create_appointment_schedules(
    settings: ZohoCreatorSettings,
    access_token: str,
    appointments: list[dict[str, Any]],
) -> dict[str, Any]:
    """Create up to 200 appointment rows in one Creator API request."""
    if not appointments:
        raise ValueError("Choose at least one appointment to push")
    if len(appointments) > 200:
        raise ValueError("Zoho Creator accepts at most 200 appointments per request")
    endpoint = (
        f"{settings.api_url}/creator/v2.1/data/{settings.account_owner}/"
        f"{settings.app_link_name}/form/{settings.form_link_name}"
    )
    request = Request(
        endpoint,
        data=json.dumps({
            "data": appointments,
            "skip_workflow": ["form_workflow"],
            # Asking Creator to echo the local ID lets the caller record exactly
            # the rows that succeeded, including a partially accepted batch.
            "result": {"fields": ["Appointment_ID"]},
        }).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Zoho-oauthtoken {access_token}",
            "Content-Type": "application/json",
        },
    )
    try:
        with open_zoho_request(request, timeout=90) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        try:
            message = json.loads(detail).get("message", detail)
        except json.JSONDecodeError:
            message = detail
        raise ValueError(f"Zoho rejected the appointment batch: {message}") from exc
    except URLError as exc:
        raise ValueError("Zoho Creator could not be reached for the appointment batch") from exc

    if payload.get("code") not in (None, 3000):
        message = payload.get("message") or payload.get("error")
        if not message:
            message = f"Creator error code {payload.get('code', 'unknown')}"
        raise ValueError(
            f"Zoho rejected the appointment batch: {message}"
        )
    return payload


def get_report_records(
    settings: ZohoCreatorSettings,
    access_token: str,
    report_link_name: str,
    *, criteria: str | None = None,
) -> list[dict[str, Any]]:
    """Read every record in one Creator report, following its cursor if needed."""
    endpoint = (
        f"{settings.api_url}/creator/v2.1/data/{settings.account_owner}/"
        f"{settings.app_link_name}/report/{report_link_name}"
    )
    records: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    parameters = {"max_records": 1000, "field_config": "all"}
    if criteria:
        parameters["criteria"] = criteria
    while True:
        headers = {
            "Authorization": f"Zoho-oauthtoken {access_token}",
            "Accept": "application/json",
        }
        if cursor:
            headers["record_cursor"] = cursor
        request = Request(
            f"{endpoint}?{urlencode(parameters)}",
            method="GET",
            headers=headers,
        )
        try:
            with open_zoho_request(request, timeout=60) as response:
                payload = json.loads(response.read().decode("utf-8"))
                next_cursor = response.headers.get("record_cursor")
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            try:
                error = json.loads(detail)
                if criteria and error.get("code") in (3100, 9280) and not cursor:
                    return []  # Valid filtered window with no matching records.
                message = error.get("message", detail)
            except json.JSONDecodeError:
                message = detail
            raise ValueError(f"Zoho rejected the report read: {message}") from exc
        except URLError as exc:
            raise ValueError("Zoho Creator could not be reached for the report read") from exc
        if criteria and payload.get("code") in (3100, 9280) and not cursor:
            return []
        if payload.get("code") not in (None, 3000):
            message = payload.get("message") or payload.get("error")
            if not message:
                message = f"Creator error code {payload.get('code', 'unknown')}"
            raise ValueError(f"Zoho rejected the report read: {message}")
        data = payload.get("data") or []
        if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
            raise ValueError("Zoho returned an invalid report response")
        records.extend(data)
        if not next_cursor:
            return records
        if next_cursor in seen_cursors:
            raise ValueError("Zoho repeated a pagination cursor; incomplete report read")
        seen_cursors.add(next_cursor)
        cursor = next_cursor
