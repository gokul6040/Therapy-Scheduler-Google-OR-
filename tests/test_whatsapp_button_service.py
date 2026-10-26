from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
from http import HTTPStatus
import json

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from database.models import WhatsAppButtonClick, WhatsAppHelpReply
from database.whatsapp_time import KOLKATA, as_kolkata
from services.whatsapp_button_service import record_button_clicks
from services.whatsapp_help_service import deliver_next_help_reply, queue_help_replies
from services.whatsapp_service import WhatsAppWebhookSettings
import web_app


NOW = datetime(2026, 10, 14, 10)
PHONE_ID = "1265029893366600"
USER_PHONE = "919000000001"


def button_event():
    return {
        "object": "whatsapp_business_account",
        "entry": [{"changes": [{"field": "messages", "value": {
            "messaging_product": "whatsapp",
            "metadata": {"phone_number_id": PHONE_ID},
            "contacts": [{"wa_id": USER_PHONE, "profile": {"name": "Test Person"}}],
            "messages": [{
                "id": "wamid.button-test", "from": USER_PHONE,
                "timestamp": str(int(NOW.replace(tzinfo=timezone.utc).timestamp())),
                "type": "button", "button": {"text": "Help", "payload": "help"},
            }],
        }}]}],
    }


def test_stores_name_phone_button_and_timestamp_once_across_sessions(db_session):
    factory = sessionmaker(bind=db_session.get_bind())
    for expected in (1, 0):
        with factory() as session:
            assert record_button_clicks(session, button_event(), PHONE_ID, NOW) == expected
            session.commit()
    row = db_session.scalar(select(WhatsAppButtonClick))
    assert row.user_name == "Test Person"
    assert row.phone_number == USER_PHONE
    assert row.clicked_button == "Help"
    assert row.button_payload == "help"
    assert row.message_id == "wamid.button-test"
    assert row.phone_number_id == PHONE_ID
    assert row.clicked_at == as_kolkata(NOW)
    assert row.recorded_at == as_kolkata(NOW)
    assert row.clicked_at.utcoffset() == timedelta(hours=5, minutes=30)
    assert row.time_zone == "Asia/Kolkata"
    assert row.clicked_at_ist == datetime(2026, 10, 14, 15, 30)
    assert row.recorded_at_ist == datetime(2026, 10, 14, 15, 30)


@pytest.mark.parametrize("contacts", [None, [], [None], [
    {"wa_id": "919000000002", "profile": {"name": "Someone Else"}},
]])
def test_missing_or_unmatched_name_does_not_invent_identity(db_session, contacts):
    payload = button_event()
    payload["entry"][0]["changes"][0]["value"]["contacts"] = contacts
    assert record_button_clicks(db_session, payload, PHONE_ID, NOW) == 1
    assert db_session.scalar(select(WhatsAppButtonClick)).user_name is None


def test_all_reply_buttons_and_matching_contacts_are_stored(db_session):
    payload = button_event()
    value = payload["entry"][0]["changes"][0]["value"]
    value["contacts"].insert(0, {"wa_id": "919000000002", "profile": {"name": "Other Person"}})
    second = deepcopy(value["messages"][0])
    second.update(id="wamid.confirm", type="interactive", **{"from": "919000000002"})
    second.pop("button")
    second["interactive"] = {"type": "button_reply", "button_reply": {
        "id": "confirm-123", "title": "Confirm",
    }}
    value["messages"] += [second, second]
    assert record_button_clicks(db_session, payload, PHONE_ID, NOW) == 2
    rows = {row.clicked_button: row for row in db_session.scalars(select(WhatsAppButtonClick))}
    assert rows["Help"].user_name == "Test Person"
    assert rows["Confirm"].user_name == "Other Person"
    assert rows["Confirm"].button_payload == "confirm-123"
    assert queue_help_replies(db_session, payload, PHONE_ID, NOW) == 1


def test_old_click_is_retained_without_sending_expired_help(db_session):
    payload = button_event()
    assert record_button_clicks(db_session, payload, PHONE_ID, NOW + timedelta(days=2)) == 1
    assert queue_help_replies(db_session, payload, PHONE_ID, NOW + timedelta(days=2)) == 0
    assert db_session.scalar(select(WhatsAppButtonClick)).clicked_at == as_kolkata(NOW)


def test_ist_storage_handles_day_rollover_and_aware_now(db_session):
    payload = button_event()
    utc_time = datetime(2026, 10, 14, 23, 45, tzinfo=timezone.utc)
    payload["entry"][0]["changes"][0]["value"]["messages"][0]["timestamp"] = str(int(utc_time.timestamp()))
    local_now = utc_time.astimezone(KOLKATA)
    assert record_button_clicks(db_session, payload, PHONE_ID, local_now) == 1
    row = db_session.scalar(select(WhatsAppButtonClick))
    assert row.clicked_at == local_now
    assert row.clicked_at_ist == datetime(2026, 10, 15, 5, 15)
    assert row.recorded_at_ist == datetime(2026, 10, 15, 5, 15)


def test_ist_recording_retains_microseconds(db_session):
    now = NOW.replace(microsecond=123456)
    assert record_button_clicks(db_session, button_event(), PHONE_ID, now) == 1
    row = db_session.scalar(select(WhatsAppButtonClick))
    assert row.recorded_at_ist == datetime(2026, 10, 14, 15, 30, 0, 123456)


def test_click_details_survive_help_reply_recipient_cleanup(db_session, monkeypatch):
    factory = sessionmaker(bind=db_session.get_bind(), expire_on_commit=False)
    monkeypatch.setattr("services.whatsapp_help_service.sender_settings_from_environment", lambda: None)
    monkeypatch.setattr("services.whatsapp_help_service.send_text_message", lambda *_args: {})
    assert record_button_clicks(db_session, button_event(), PHONE_ID, NOW) == 1
    assert queue_help_replies(db_session, button_event(), PHONE_ID, NOW) == 1
    assert deliver_next_help_reply(factory, NOW) == "accepted"
    db_session.expire_all()
    assert db_session.scalar(select(WhatsAppHelpReply)).recipient is None
    assert db_session.scalar(select(WhatsAppButtonClick)).phone_number == USER_PHONE


@pytest.mark.parametrize("variant", [
    "text", "status", "wrong_phone", "wrong_object", "wrong_product", "list_reply",
    "invalid_phone", "missing_id", "missing_time", "bad_time", "future", "bad_button",
    "no_button_label", "malformed_entries", "unicode_phone",
])
def test_unrelated_or_malformed_events_are_not_stored(db_session, variant):
    payload = button_event()
    value = payload["entry"][0]["changes"][0]["value"]
    message = value["messages"][0]
    if variant == "text":
        message["type"] = "text"
    elif variant == "status":
        value.pop("messages")
        value["statuses"] = [{"status": "delivered"}]
    elif variant == "wrong_phone":
        value["metadata"]["phone_number_id"] = "other"
    elif variant == "wrong_object":
        payload["object"] = "other"
    elif variant == "wrong_product":
        value["messaging_product"] = "other"
    elif variant == "list_reply":
        message.update(type="interactive", interactive={"type": "list_reply"})
    elif variant == "invalid_phone":
        message["from"] = "not-a-number"
    elif variant == "unicode_phone":
        message["from"] = "９１９００００００００１"
    elif variant == "missing_id":
        message.pop("id")
    elif variant == "missing_time":
        message.pop("timestamp")
    elif variant == "bad_time":
        message["timestamp"] = "nan"
    elif variant == "future":
        message["timestamp"] = str(int((NOW + timedelta(hours=1)).replace(tzinfo=timezone.utc).timestamp()))
    elif variant == "bad_button":
        message["button"] = []
    elif variant == "no_button_label":
        message["button"] = {}
    elif variant == "malformed_entries":
        payload["entry"] = [None, {"changes": [None, {"field": "messages", "value": None}]}]
    assert record_button_clicks(db_session, payload, PHONE_ID, NOW) == 0
    assert db_session.scalar(select(WhatsAppButtonClick)) is None


@pytest.mark.parametrize("valid_signature", [True, False])
def test_webhook_persists_only_authenticated_clicks(db_session, monkeypatch, capsys, valid_signature):
    payload = button_event()
    payload["entry"][0]["changes"][0]["value"]["messages"][0]["timestamp"] = str(
        int(datetime.now(timezone.utc).timestamp())
    )
    body = json.dumps(payload).encode()
    signature = hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
    monkeypatch.setattr(web_app, "webhook_settings_from_environment", lambda: WhatsAppWebhookSettings("verify", "app-secret"))
    monkeypatch.setattr(web_app, "SessionLocal", sessionmaker(bind=db_session.get_bind()))
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", PHONE_ID)

    class Handler:
        headers = {"X-Hub-Signature-256": "sha256=" + (signature if valid_signature else "wrong")}
        response = None

        def read_raw_body(self):
            return body

        def send_json(self, data):
            self.response = data

    handler = Handler()
    if valid_signature:
        web_app.TherapySchedulerHandler.handle_whatsapp_webhook(handler)
        assert handler.response == {"ok": True}
    else:
        with pytest.raises(web_app.ApiError) as error:
            web_app.TherapySchedulerHandler.handle_whatsapp_webhook(handler)
        assert error.value.status == HTTPStatus.UNAUTHORIZED
    assert (db_session.scalar(select(WhatsAppButtonClick)) is not None) == valid_signature
    output = capsys.readouterr().out
    assert USER_PHONE not in output
    assert "Test Person" not in output


def test_storage_failure_returns_retryable_error_without_exposing_contact(db_session, monkeypatch, capsys):
    body = json.dumps(button_event()).encode()
    signature = hmac.new(b"app-secret", body, hashlib.sha256).hexdigest()
    monkeypatch.setattr(web_app, "webhook_settings_from_environment", lambda: WhatsAppWebhookSettings("verify", "app-secret"))
    monkeypatch.setattr(web_app, "SessionLocal", sessionmaker(bind=db_session.get_bind()))
    monkeypatch.setenv("WHATSAPP_PHONE_NUMBER_ID", PHONE_ID)

    def fail(*_args):
        raise RuntimeError(USER_PHONE)

    monkeypatch.setattr(web_app, "record_button_clicks", fail)

    class Handler:
        headers = {"X-Hub-Signature-256": "sha256=" + signature}

        def read_raw_body(self):
            return body

    with pytest.raises(web_app.ApiError) as error:
        web_app.TherapySchedulerHandler.handle_whatsapp_webhook(Handler())
    assert error.value.status == HTTPStatus.SERVICE_UNAVAILABLE
    assert USER_PHONE not in str(error.value)
    assert USER_PHONE not in capsys.readouterr().out
