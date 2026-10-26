import hashlib
import hmac
from io import BytesIO
import json
from urllib.error import HTTPError

import pytest

from services import whatsapp_service


def test_valid_webhook_signature_rejects_missing_or_modified_signatures():
    body = b'{"object":"whatsapp_business_account"}'
    secret = "app-secret"
    signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()

    assert whatsapp_service.valid_webhook_signature(body, f"sha256={signature}", secret)
    assert not whatsapp_service.valid_webhook_signature(body, None, secret)
    assert not whatsapp_service.valid_webhook_signature(body + b" ", f"sha256={signature}", secret)


def test_webhook_event_summary_reports_statuses_without_contact_data():
    summary = whatsapp_service.webhook_event_summary({
        "object": "whatsapp_business_account",
        "entry": [{
            "changes": [{
                "value": {
                    "statuses": [{
                        "status": "failed",
                        "recipient_id": "918012345678",
                        "errors": [{"code": 131026, "title": "Not delivered"}],
                    }],
                },
            }],
        }],
    })

    assert summary == {
        "object": "whatsapp_business_account",
        "entryCount": 1,
        "statusCounts": {"failed": 1},
        "failureCodes": {"131026": 1},
    }


def test_template_message_payload_normalizes_recipient_and_body_parameters():
    payload = whatsapp_service.template_message_payload(
        "+91 79946-25454",
        "physio_appointment",
        "en_US",
        ("Test", "Tuesday, 14 October at 10:20 AM"),
    )

    assert payload["to"] == "917994625454"
    assert payload["template"]["name"] == "physio_appointment"
    assert payload["template"]["components"][0]["parameters"] == [
        {"type": "text", "text": "Test"},
        {"type": "text", "text": "Tuesday, 14 October at 10:20 AM"},
    ]


def test_template_message_payload_rejects_invalid_recipient_or_template_name():
    with pytest.raises(whatsapp_service.WhatsAppConfigurationError, match="Recipient"):
        whatsapp_service.template_message_payload("not-a-number", "physio_appointment", "en_US", ("Test",))
    with pytest.raises(whatsapp_service.WhatsAppConfigurationError, match="Template name"):
        whatsapp_service.template_message_payload("917994625454", "Physio Appointment", "en_US", ("Test",))


def test_template_message_payload_includes_custom_quick_reply_payload():
    payload = whatsapp_service.template_message_payload(
        "917994625454",
        "physio_appointment",
        "en_US",
        ("Test", "Tuesday, 14 October at 10:20 AM"),
        ("help",),
    )

    assert payload["template"]["components"][1] == {
        "type": "button",
        "sub_type": "quick_reply",
        "index": "0",
        "parameters": [{"type": "payload", "payload": "help"}],
    }


def test_send_template_message_uses_meta_messages_endpoint(monkeypatch):
    captured = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        @staticmethod
        def read():
            return b'{"messages":[{"id":"wamid.test"}]}'

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return FakeResponse()

    monkeypatch.setattr(whatsapp_service, "urlopen", fake_urlopen)
    result = whatsapp_service.send_template_message(
        whatsapp_service.WhatsAppSenderSettings("token", "1265029893366600", "v26.0"),
        "917994625454",
        "physio_appointment",
        "en_US",
        ("Test", "Tuesday, 14 October at 10:20 AM"),
    )

    assert result["messages"][0]["id"] == "wamid.test"
    assert captured["request"].full_url.endswith("/v26.0/1265029893366600/messages")
    assert captured["request"].get_header("Authorization") == "Bearer token"
    assert json.loads(captured["request"].data.decode())["type"] == "template"
    assert captured["timeout"] == 20


def test_send_template_message_reports_meta_error_code_without_response_details(monkeypatch):
    def fake_urlopen(request, timeout):
        raise HTTPError(
            request.full_url,
            400,
            "Bad Request",
            {},
            BytesIO(b'{"error":{"message":"private detail","code":132001,"error_subcode":2494073}}'),
        )

    monkeypatch.setattr(whatsapp_service, "urlopen", fake_urlopen)

    with pytest.raises(
        whatsapp_service.WhatsAppDeliveryError, match=r"Meta error 132001/2494073"
    ) as error:
        whatsapp_service.send_template_message(
            whatsapp_service.WhatsAppSenderSettings("token", "1265029893366600", "v26.0"),
            "917994625454",
            "physio_appointment",
            "en_US",
            ("Test", "Tuesday, 14 October at 10:20 AM"),
        )

    assert "private detail" not in str(error.value)


def test_send_text_message_sends_exact_help_reply(monkeypatch):
    captured = {}
    def fake_send(settings, payload):
        captured.update(payload)
        return {"messages": [{"id": "wamid.reply"}]}
    monkeypatch.setattr(whatsapp_service, "_send_message", fake_send)
    whatsapp_service.send_text_message(
        whatsapp_service.WhatsAppSenderSettings("token", "1265029893366600", "v26.0"),
        "919000000001", "Do you want to reschedule or contact staff?",
    )
    assert captured == {
        "messaging_product": "whatsapp", "to": "919000000001", "type": "text",
        "text": {"body": "Do you want to reschedule or contact staff?", "preview_url": False},
    }
