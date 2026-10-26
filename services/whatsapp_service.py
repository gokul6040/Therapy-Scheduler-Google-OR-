"""Small, dependency-free helpers for the WhatsApp Cloud API integration.

This module intentionally keeps Meta transport and credential handling separate
from the browser HTTP handler and from scheduler business rules.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import hmac
import json
import os
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


DEFAULT_GRAPH_API_VERSION = "v26.0"
MAX_WEBHOOK_BODY_BYTES = 1_000_000
_GRAPH_API_VERSION_PATTERN = re.compile(r"v\d+\.\d+")
_PHONE_NUMBER_PATTERN = re.compile(r"\d{6,20}")
_TEMPLATE_NAME_PATTERN = re.compile(r"[a-z0-9_]{1,512}")


class WhatsAppConfigurationError(ValueError):
    """Raised when an outbound WhatsApp request lacks safe configuration."""


class WhatsAppDeliveryError(RuntimeError):
    """Raised when Meta rejects or cannot receive an outbound request."""


@dataclass(frozen=True)
class WhatsAppWebhookSettings:
    """Secrets necessary to verify inbound Meta webhook traffic."""

    verify_token: str
    app_secret: str


@dataclass(frozen=True)
class WhatsAppSenderSettings:
    """Configuration necessary to send a Cloud API template message."""

    access_token: str
    phone_number_id: str
    graph_api_version: str


def webhook_settings_from_environment() -> WhatsAppWebhookSettings | None:
    """Return inbound webhook settings only when both required secrets exist."""
    verify_token = os.getenv("WHATSAPP_VERIFY_TOKEN", "").strip()
    app_secret = os.getenv("WHATSAPP_APP_SECRET", "").strip()
    if not verify_token or not app_secret:
        return None
    return WhatsAppWebhookSettings(verify_token=verify_token, app_secret=app_secret)


def sender_settings_from_environment() -> WhatsAppSenderSettings:
    """Load and validate the minimum configuration for a Meta send request."""
    access_token = os.getenv("WHATSAPP_ACCESS_TOKEN", "").strip()
    phone_number_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID", "").strip()
    graph_api_version = os.getenv(
        "WHATSAPP_GRAPH_API_VERSION", DEFAULT_GRAPH_API_VERSION
    ).strip()
    if not access_token:
        raise WhatsAppConfigurationError("WHATSAPP_ACCESS_TOKEN is not configured")
    if not _PHONE_NUMBER_PATTERN.fullmatch(phone_number_id):
        raise WhatsAppConfigurationError("WHATSAPP_PHONE_NUMBER_ID must contain only digits")
    if not _GRAPH_API_VERSION_PATTERN.fullmatch(graph_api_version):
        raise WhatsAppConfigurationError("WHATSAPP_GRAPH_API_VERSION must look like v26.0")
    return WhatsAppSenderSettings(
        access_token=access_token,
        phone_number_id=phone_number_id,
        graph_api_version=graph_api_version,
    )


def valid_webhook_signature(
    raw_body: bytes, signature_header: str | None, app_secret: str
) -> bool:
    """Check Meta's SHA-256 HMAC header without parsing untrusted JSON first."""
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    supplied_signature = signature_header.removeprefix("sha256=")
    expected_signature = hmac.new(
        app_secret.encode("utf-8"), raw_body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(supplied_signature, expected_signature)


def webhook_event_summary(payload: dict[str, Any]) -> dict[str, Any]:
    """Return log-safe event metadata without message text or contact details."""
    entries = payload.get("entry")
    summary: dict[str, Any] = {
        "object": str(payload.get("object", "unknown")),
        "entryCount": len(entries) if isinstance(entries, list) else 0,
    }
    status_counts: dict[str, int] = {}
    failure_codes: dict[str, int] = {}
    if not isinstance(entries, list):
        return summary

    for entry in entries:
        if not isinstance(entry, dict):
            continue
        changes = entry.get("changes")
        if not isinstance(changes, list):
            continue
        for change in changes:
            if not isinstance(change, dict):
                continue
            value = change.get("value")
            if not isinstance(value, dict):
                continue
            statuses = value.get("statuses")
            if not isinstance(statuses, list):
                continue
            for status_event in statuses:
                if not isinstance(status_event, dict):
                    continue
                status = str(status_event.get("status", "unknown"))
                status_counts[status] = status_counts.get(status, 0) + 1
                errors = status_event.get("errors")
                if not isinstance(errors, list):
                    continue
                for error in errors:
                    if not isinstance(error, dict) or "code" not in error:
                        continue
                    code = str(error["code"])
                    failure_codes[code] = failure_codes.get(code, 0) + 1

    if status_counts:
        summary["statusCounts"] = status_counts
    if failure_codes:
        summary["failureCodes"] = failure_codes
    return summary


def template_message_payload(
    recipient: str,
    template_name: str,
    language: str,
    body_parameters: tuple[str, ...],
    quick_reply_payloads: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Build a Cloud API template request with a text-only body component."""
    normalized_recipient = re.sub(r"[\s+()-]", "", recipient)
    if not _PHONE_NUMBER_PATTERN.fullmatch(normalized_recipient):
        raise WhatsAppConfigurationError("Recipient must be an international phone number in digits")
    normalized_template_name = template_name.strip()
    if not _TEMPLATE_NAME_PATTERN.fullmatch(normalized_template_name):
        raise WhatsAppConfigurationError("Template name must use lowercase letters, digits, and underscores")
    normalized_language = language.strip()
    if not normalized_language:
        raise WhatsAppConfigurationError("Template language is required")
    if not body_parameters:
        raise WhatsAppConfigurationError("At least one template body parameter is required")
    if any(not value.strip() for value in body_parameters):
        raise WhatsAppConfigurationError("Template body parameters cannot be blank")
    components: list[dict[str, Any]] = [{
        "type": "body",
        "parameters": [{"type": "text", "text": value} for value in body_parameters],
    }]
    for index, payload in enumerate(quick_reply_payloads):
        if not payload.strip():
            raise WhatsAppConfigurationError("Quick-reply payloads cannot be blank")
        components.append({
            "type": "button",
            "sub_type": "quick_reply",
            "index": str(index),
            "parameters": [{"type": "payload", "payload": payload}],
        })
    return {
        "messaging_product": "whatsapp",
        "to": normalized_recipient,
        "type": "template",
        "template": {
            "name": normalized_template_name,
            "language": {"code": normalized_language},
            "components": components,
        },
    }


def send_template_message(
    settings: WhatsAppSenderSettings,
    recipient: str,
    template_name: str,
    language: str,
    body_parameters: tuple[str, ...],
    quick_reply_payloads: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Send an approved template and return Meta's response without logging secrets."""
    payload = template_message_payload(
        recipient, template_name, language, body_parameters, quick_reply_payloads
    )
    return _send_message(settings, payload)


def send_text_message(
    settings: WhatsAppSenderSettings, recipient: str, text: str
) -> dict[str, Any]:
    """Send a plain-text reply inside the recipient's customer-service window."""
    if not _PHONE_NUMBER_PATTERN.fullmatch(recipient):
        raise WhatsAppConfigurationError("Recipient must contain only international phone digits")
    if not text.strip() or len(text) > 4096:
        raise WhatsAppConfigurationError("Text must contain between 1 and 4096 characters")
    return _send_message(settings, {
        "messaging_product": "whatsapp",
        "to": recipient,
        "type": "text",
        "text": {"body": text, "preview_url": False},
    })


def send_reply_buttons(
    settings: WhatsAppSenderSettings, recipient: str, text: str,
    buttons: tuple[tuple[str, str], ...],
) -> dict[str, Any]:
    """Send 1–3 interactive replies; the caller must verify an open service window."""
    if not _PHONE_NUMBER_PATTERN.fullmatch(recipient):
        raise WhatsAppConfigurationError("Recipient must contain only international phone digits")
    if not text.strip() or len(text) > 1024:
        raise WhatsAppConfigurationError("Interactive body must contain 1–1024 characters")
    if not 1 <= len(buttons) <= 3:
        raise WhatsAppConfigurationError("Interactive messages need 1–3 reply buttons")
    if any(not key.strip() or len(key) > 256 or not title.strip() or len(title) > 20
           for key, title in buttons):
        raise WhatsAppConfigurationError("Invalid reply button ID or title")
    if len({key for key, _ in buttons}) != len(buttons):
        raise WhatsAppConfigurationError("Reply button IDs must be unique")
    return _send_message(settings, {
        "messaging_product": "whatsapp", "to": recipient, "type": "interactive",
        "interactive": {
            "type": "button", "body": {"text": text},
            "action": {"buttons": [
                {"type": "reply", "reply": {"id": key, "title": title}}
                for key, title in buttons
            ]},
        },
    })


def _send_message(settings: WhatsAppSenderSettings, payload: dict[str, Any]) -> dict[str, Any]:
    """Submit a message without including recipient or Meta response details in errors."""
    encoded_payload = json.dumps(payload).encode("utf-8")
    request = Request(
        f"https://graph.facebook.com/{settings.graph_api_version}/"
        f"{settings.phone_number_id}/messages",
        data=encoded_payload,
        headers={
            "Authorization": f"Bearer {settings.access_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=20) as response:
            response_body = response.read().decode("utf-8")
    except HTTPError as exc:
        error_suffix = ""
        try:
            error_payload = json.loads(exc.read().decode("utf-8"))
            error_details = error_payload.get("error", {})
            error_code = error_details.get("code")
            error_subcode = error_details.get("error_subcode")
            if error_code is not None:
                error_suffix = f" (Meta error {error_code}"
                if error_subcode is not None:
                    error_suffix += f"/{error_subcode}"
                error_suffix += ")"
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            pass
        raise WhatsAppDeliveryError(
            f"Meta rejected the WhatsApp message{error_suffix}"
        ) from exc
    except URLError as exc:
        raise WhatsAppDeliveryError("Could not reach Meta to send the WhatsApp message") from exc
    try:
        result = json.loads(response_body)
    except json.JSONDecodeError as exc:
        raise WhatsAppDeliveryError("Meta returned an invalid response") from exc
    if not isinstance(result, dict) or not result.get("messages"):
        raise WhatsAppDeliveryError("Meta did not confirm the WhatsApp message")
    return result
