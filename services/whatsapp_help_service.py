"""Queue and send the single approved response to incoming Help button taps."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import re
from threading import Event, Thread
from typing import Any, Callable

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from database.models import WhatsAppHelpReply
from services.whatsapp_service import (
    WhatsAppConfigurationError,
    WhatsAppDeliveryError,
    send_text_message,
    sender_settings_from_environment,
)


HELP_REPLY_TEXT = "Do you want to reschedule or contact staff?"
SERVICE_WINDOW = timedelta(hours=24)


def queue_help_replies(
    session: Session,
    payload: dict[str, Any],
    phone_number_id: str,
    now: datetime | None = None,
) -> int:
    """Persist fresh Help taps from this sender, skipping duplicate event IDs."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    if payload.get("object") != "whatsapp_business_account":
        return 0
    queued = 0
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
            messages = value.get("messages", [])
            for message in messages if isinstance(messages, list) else []:
                if not isinstance(message, dict) or message.get("type") != "button":
                    continue
                button = message.get("button")
                if not isinstance(button, dict):
                    continue
                if not any(
                    isinstance(button.get(key), str) and button[key].strip().casefold() == "help"
                    for key in ("payload", "text")
                ):
                    continue
                recipient, message_id = message.get("from"), message.get("id")
                if not isinstance(recipient, str) or not re.fullmatch(r"\d{6,20}", recipient):
                    continue
                if not isinstance(message_id, str) or not message_id or len(message_id) > 512:
                    continue
                try:
                    timestamp = float(message["timestamp"])
                    received_at = datetime.fromtimestamp(timestamp, timezone.utc).replace(tzinfo=None)
                except (KeyError, TypeError, ValueError, OverflowError, OSError):
                    continue
                if not now - SERVICE_WINDOW < received_at <= now + timedelta(minutes=5):
                    continue
                event_key = hashlib.sha256(f"{phone_number_id}:{message_id}".encode()).hexdigest()
                if session.get(WhatsAppHelpReply, event_key) is not None:
                    continue
                try:
                    with session.begin_nested():
                        session.add(WhatsAppHelpReply(
                            event_key=event_key, recipient=recipient, received_at=received_at,
                        ))
                        session.flush()
                except IntegrityError:
                    continue
                queued += 1
    session.commit()
    return queued


def deliver_next_help_reply(
    session_factory: Callable[[], Session], now: datetime | None = None
) -> str | None:
    """Claim one pending reply atomically; never resend an ambiguous send attempt."""
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    with session_factory() as session:
        reply = session.scalar(select(WhatsAppHelpReply).where(
            WhatsAppHelpReply.state == "pending"
        ).order_by(WhatsAppHelpReply.received_at).limit(1))
        if reply is None:
            return None
        event_key, recipient = reply.event_key, reply.recipient
        claimed = session.execute(update(WhatsAppHelpReply).where(
            WhatsAppHelpReply.event_key == event_key,
            WhatsAppHelpReply.state == "pending",
        ).values(state="sending"))
        if claimed.rowcount != 1:
            session.rollback()
            return None
        session.commit()
        expired = reply.received_at <= now - SERVICE_WINDOW

    state = "expired" if expired else "accepted"
    if not expired:
        try:
            send_text_message(sender_settings_from_environment(), recipient or "", HELP_REPLY_TEXT)
        except (WhatsAppConfigurationError, WhatsAppDeliveryError, TimeoutError, OSError):
            # A network timeout may have occurred after Meta accepted the request.
            # Keep the event claimed instead of risking a duplicate reply.
            state = "unconfirmed"
    with session_factory() as session:
        session.execute(update(WhatsAppHelpReply).where(
            WhatsAppHelpReply.event_key == event_key
        ).values(state=state, recipient=None))
        session.commit()
    return state


def start_help_reply_worker(session_factory: Callable[[], Session]) -> tuple[Event, Thread]:
    """Process durable pending replies off the HTTP thread for prompt acknowledgments."""
    stop = Event()

    def work() -> None:
        while not stop.is_set():
            try:
                state = deliver_next_help_reply(session_factory)
                if state is not None:
                    print(f"WhatsApp Help auto-reply state={state}")
                    continue
            except Exception as exc:
                # Database errors can contain SQL parameters; log only the class.
                print(f"WhatsApp Help worker error type={type(exc).__name__}")
            stop.wait(2)

    thread = Thread(target=work, name="whatsapp-help-replies", daemon=True)
    thread.start()
    return stop, thread
