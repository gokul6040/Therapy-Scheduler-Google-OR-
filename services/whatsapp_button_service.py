"""Store reply-button clicks without logging contacts or changing appointments."""

from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import re
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database.models import WhatsAppButtonClick
from database.whatsapp_time import KOLKATA, WHATSAPP_TIME_ZONE, as_kolkata, kolkata_now


def _bounded_text(value: Any, maximum: int) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value if value and len(value) <= maximum else None


def record_button_clicks(
    session: Session,
    payload: dict[str, Any],
    phone_number_id: str,
    now: datetime | None = None,
) -> int:
    """Stage unique button clicks; the caller commits with the Help reply queue.

    Call only after validating Meta's signature. Names are WhatsApp profile names,
    not verified patient identities. Old callbacks are retained even when too old
    to send a Help reply. Save aware Asia/Kolkata timestamps; the database also
    persists explicit IST wall-clock columns for direct SQL viewing.
    """
    now = as_kolkata(now) if now is not None else kolkata_now()
    if payload.get("object") != "whatsapp_business_account":
        return 0
    if not re.fullmatch(r"[0-9]{6,20}", phone_number_id):
        return 0
    recorded = 0
    entries = payload.get("entry", [])
    for entry in entries if isinstance(entries, list) else []:
        if not isinstance(entry, dict):
            continue
        changes = entry.get("changes", [])
        for change in changes if isinstance(changes, list) else []:
            if not isinstance(change, dict) or change.get("field") != "messages":
                continue
            value = change.get("value")
            if not isinstance(value, dict) or value.get("messaging_product") != "whatsapp":
                continue
            metadata = value.get("metadata")
            if not isinstance(metadata, dict) or metadata.get("phone_number_id") != phone_number_id:
                continue
            names = {}
            contacts = value.get("contacts", [])
            for contact in contacts if isinstance(contacts, list) else []:
                if not isinstance(contact, dict):
                    continue
                profile = contact.get("profile")
                wa_id = contact.get("wa_id")
                if isinstance(wa_id, str) and isinstance(profile, dict):
                    names[wa_id] = _bounded_text(profile.get("name"), 512)
            messages = value.get("messages", [])
            for message in messages if isinstance(messages, list) else []:
                if not isinstance(message, dict):
                    continue
                if message.get("type") == "button":
                    button = message.get("button")
                    label_key, payload_key = "text", "payload"
                elif message.get("type") == "interactive":
                    interactive = message.get("interactive")
                    if not isinstance(interactive, dict) or interactive.get("type") != "button_reply":
                        continue
                    button = interactive.get("button_reply")
                    label_key, payload_key = "title", "id"
                else:
                    continue
                if not isinstance(button, dict):
                    continue
                label = _bounded_text(button.get(label_key), 512)
                button_payload = _bounded_text(button.get(payload_key), 4096)
                if not label and not button_payload:
                    continue
                # Payload-only callbacks still identify the clicked button.
                clicked_button = label or button_payload
                if len(clicked_button) > 512:
                    continue
                phone = message.get("from")
                message_id = _bounded_text(message.get("id"), 512)
                if not isinstance(phone, str) or not re.fullmatch(r"[0-9]{6,20}", phone):
                    continue
                if not message_id:
                    continue
                try:
                    clicked_at = datetime.fromtimestamp(
                        float(message["timestamp"]), KOLKATA
                    )
                except (KeyError, TypeError, ValueError, OverflowError, OSError):
                    continue
                if clicked_at > now + timedelta(minutes=5):
                    continue
                event_key = hashlib.sha256(f"{phone_number_id}:{message_id}".encode()).hexdigest()
                if session.get(WhatsAppButtonClick, event_key) is not None:
                    continue
                try:
                    with session.begin_nested():
                        session.add(WhatsAppButtonClick(
                            event_key=event_key,
                            message_id=message_id,
                            phone_number_id=phone_number_id,
                            user_name=names.get(phone),
                            phone_number=phone,
                            clicked_button=clicked_button,
                            button_payload=button_payload,
                            clicked_at=clicked_at,
                            recorded_at=now,
                            time_zone=WHATSAPP_TIME_ZONE,
                        ))
                        session.flush()
                except IntegrityError:
                    continue
                recorded += 1
    return recorded
