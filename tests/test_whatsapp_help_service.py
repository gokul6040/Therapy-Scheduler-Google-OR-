from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from http import HTTPStatus

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from database.models import WhatsAppHelpReply
from services import whatsapp_help_service as help_service
from services.whatsapp_service import WhatsAppDeliveryError, WhatsAppWebhookSettings
import web_app


NOW = datetime(2026, 10, 13, 10, 0)


def help_event(message_id="wamid.help-test", phone="1265029893366600", kind="button"):
    return {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{
            "field": "messages",
            "value": {
                "messaging_product": "whatsapp",
                "metadata": {"phone_number_id": phone},
                "messages": [{
                    "id": message_id,
                    "from": "919000000001",
                    "timestamp": str(int(NOW.replace(tzinfo=timezone.utc).timestamp())),
                    "type": kind,
                    "button": {"payload": "help", "text": "Help"},
                }],
            },
        }]}],
    }


@pytest.fixture
def session_factory(db_session):
    return sessionmaker(bind=db_session.get_bind(), expire_on_commit=False)


def test_help_tap_replies_to_sender_once_across_sessions(session_factory, monkeypatch):
    sent = []
    monkeypatch.setattr(help_service, "sender_settings_from_environment", lambda: "settings")
    monkeypatch.setattr(help_service, "send_text_message", lambda *args: sent.append(args))
    payload = help_event()
    with session_factory() as session:
        assert help_service.queue_help_replies(session, payload, "1265029893366600", NOW) == 1
    # A second connection simulates both Meta redelivery and an app restart.
    with session_factory() as session:
        assert help_service.queue_help_replies(session, payload, "1265029893366600", NOW) == 0
    assert help_service.deliver_next_help_reply(session_factory, NOW) == "accepted"
    assert help_service.deliver_next_help_reply(session_factory, NOW) is None
    assert sent == [("settings", "919000000001", "Do you want to reschedule or contact staff?")]
    with session_factory() as session:
        reply = session.scalar(select(WhatsAppHelpReply))
        assert reply.recipient is None
        assert reply.state == "accepted"
        assert len(reply.event_key) == 64
        assert "wamid" not in reply.event_key
        assert help_service.queue_help_replies(session, payload, "1265029893366600", NOW) == 0


@pytest.mark.parametrize("variant", [
    "text", "status", "other_phone", "other_button", "missing_id", "invalid_sender",
    "stale", "future", "missing_timestamp", "malformed", "other_object",
])
def test_non_help_or_invalid_events_do_not_queue(db_session, variant):
    payload = help_event()
    value = payload["entry"][0]["changes"][0]["value"]
    message = value["messages"][0]
    if variant == "text":
        message["type"] = "text"
        message["text"] = {"body": "Help"}
    elif variant == "status":
        value.pop("messages")
        value["statuses"] = [{"status": "read"}]
    elif variant == "other_phone":
        value["metadata"]["phone_number_id"] = "other"
    elif variant == "other_button":
        message["button"] = {"payload": "confirm", "text": "Confirm"}
    elif variant == "missing_id":
        message.pop("id")
    elif variant == "invalid_sender":
        message["from"] = "not-a-number"
    elif variant == "stale":
        message["timestamp"] = str(int((NOW - timedelta(days=2)).replace(tzinfo=timezone.utc).timestamp()))
    elif variant == "future":
        message["timestamp"] = str(int((NOW + timedelta(hours=1)).replace(tzinfo=timezone.utc).timestamp()))
    elif variant == "missing_timestamp":
        message.pop("timestamp")
    elif variant == "malformed":
        value["messages"] = [None, "bad", {"type": "button", "button": []}]
    elif variant == "other_object":
        payload["object"] = "another_object"
    assert help_service.queue_help_replies(db_session, payload, "1265029893366600", NOW) == 0
    assert db_session.scalar(select(WhatsAppHelpReply)) is None


def test_batch_and_label_only_help_buttons_are_supported(db_session):
    payload = help_event()
    messages = payload["entry"][0]["changes"][0]["value"]["messages"]
    messages[0]["button"] = {"text": "Help"}
    messages.append(dict(messages[0], id="wamid.second-help"))
    assert help_service.queue_help_replies(db_session, payload, "1265029893366600", NOW) == 2


def test_expired_pending_reply_does_not_send(session_factory, monkeypatch):
    monkeypatch.setattr(help_service, "send_text_message", lambda *_args: pytest.fail("expired send"))
    with session_factory() as session:
        help_service.queue_help_replies(session, help_event(), "1265029893366600", NOW)
    assert help_service.deliver_next_help_reply(session_factory, NOW + timedelta(days=2)) == "expired"


def test_ambiguous_send_failure_is_not_retried(session_factory, monkeypatch):
    attempts = []
    monkeypatch.setattr(help_service, "sender_settings_from_environment", lambda: "settings")
    def fail(*args):
        attempts.append(args)
        raise WhatsAppDeliveryError("Request timed out")
    monkeypatch.setattr(help_service, "send_text_message", fail)
    with session_factory() as session:
        help_service.queue_help_replies(session, help_event(), "1265029893366600", NOW)
    assert help_service.deliver_next_help_reply(session_factory, NOW) == "unconfirmed"
    assert help_service.deliver_next_help_reply(session_factory, NOW) is None
    assert len(attempts) == 1
    with session_factory() as session:
        assert session.scalar(select(WhatsAppHelpReply)).recipient is None


@pytest.mark.parametrize("valid_signature", [True, False])
def test_http_handler_only_queues_authenticated_buttons(session_factory, monkeypatch, valid_signature):
    payload = help_event()
    payload["entry"][0]["changes"][0]["value"]["messages"][0]["timestamp"] = str(
        int(datetime.now(timezone.utc).timestamp())
    )
    body = json.dumps(payload).encode()
    signature = hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
    monkeypatch.setattr(web_app, "webhook_settings_from_environment", lambda: WhatsAppWebhookSettings("verify", "app-secret"))
    monkeypatch.setattr(web_app, "SessionLocal", session_factory)
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", "1265029893366600")
    class Handler:
        headers = {"X-Hub-Signature-256": "sha256=" + (signature if valid_signature else "wrong")}
        response = None
        def read_raw_body(self):
            return body
        def send_json(self, payload):
            self.response = payload
    handler = Handler()
    if valid_signature:
        web_app.TherapySchedulerHandler.handle_whatsapp_webhook(handler)
        assert handler.response == {"ok": True}
    else:
        with pytest.raises(web_app.ApiError) as error:
            web_app.TherapySchedulerHandler.handle_whatsapp_webhook(handler)
        assert error.value.status == HTTPStatus.UNAUTHORIZED
    with session_factory() as session:
        assert (session.scalar(select(WhatsAppHelpReply)) is not None) == valid_signature
