from urllib.parse import parse_qs, urlparse

import json

from services.zoho_creator_service import (
    authorization_url,
    create_appointment_schedule,
    create_appointment_schedules,
    get_report_records,
    refresh_access_token,
    settings_from_environment,
)


def test_creator_oauth_url_requests_only_appointment_sync_scopes(monkeypatch):
    monkeypatch.setenv("ZOHO_CREATOR_CLIENT_ID", "client-id")
    monkeypatch.setenv("ZOHO_CREATOR_CLIENT_SECRET", "client-secret")
    settings = settings_from_environment()

    url = authorization_url(settings, "safe-state")
    parsed = urlparse(url)
    query = parse_qs(parsed.query)

    assert parsed.netloc == "accounts.zoho.in"
    assert query["client_id"] == ["client-id"]
    assert query["state"] == ["safe-state"]
    assert query["access_type"] == ["offline"]
    assert query["scope"] == ["ZohoCreator.form.CREATE,ZohoCreator.report.READ"]


def test_creator_settings_use_existing_creator_target_without_secrets(monkeypatch):
    monkeypatch.delenv("ZOHO_CREATOR_CLIENT_ID", raising=False)
    monkeypatch.delenv("ZOHO_CREATOR_CLIENT_SECRET", raising=False)

    settings = settings_from_environment()

    assert not settings.configured
    assert settings.account_owner == "christina.sunil_turnb"
    assert settings.app_link_name == "therapy-scheduler"
    assert settings.form_link_name == "All_Therapy_Schedules"
    assert settings.report_link_name == "All_Therapy_Schedules_Report"


def test_create_appointment_posts_only_to_appointment_form(monkeypatch):
    monkeypatch.setenv("ZOHO_CREATOR_CLIENT_ID", "client-id")
    monkeypatch.setenv("ZOHO_CREATOR_CLIENT_SECRET", "client-secret")
    captured = {}

    class Response:
        def read(self):
            return json.dumps({"code": 3000, "data": {"ID": "creator-1"}}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_open_zoho_request(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode())
        captured["auth"] = request.get_header("Authorization")
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(
        "services.zoho_creator_service.open_zoho_request", fake_open_zoho_request
    )

    response = create_appointment_schedule(
        settings_from_environment(), "token", {"appointment_id": 1}
    )

    assert captured["url"].endswith(
        "/creator/v2.1/data/christina.sunil_turnb/therapy-scheduler/form/All_Therapy_Schedules"
    )
    assert captured["body"] == {
        "data": {"appointment_id": 1},
        "skip_workflow": ["form_workflow"],
    }
    assert captured["auth"] == "Zoho-oauthtoken token"
    assert response["data"]["ID"] == "creator-1"


def test_refresh_access_token_uses_saved_creator_token(monkeypatch):
    monkeypatch.setenv("ZOHO_CREATOR_CLIENT_ID", "client-id")
    monkeypatch.setenv("ZOHO_CREATOR_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv("ZOHO_CREATOR_REFRESH_TOKEN", "refresh-token")
    captured = {}

    class Response:
        def read(self):
            return json.dumps({"access_token": "new-access-token"}).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_open_zoho_request(request, timeout):
        captured["url"] = request.full_url
        captured["body"] = request.data.decode()
        return Response()

    monkeypatch.setattr(
        "services.zoho_creator_service.open_zoho_request", fake_open_zoho_request
    )

    assert refresh_access_token(settings_from_environment())["access_token"] == "new-access-token"
    assert captured["url"] == "https://accounts.zoho.in/oauth/v2/token"
    assert "grant_type=refresh_token" in captured["body"]


def test_create_appointment_schedules_posts_a_creator_batch(monkeypatch):
    monkeypatch.setenv("ZOHO_CREATOR_CLIENT_ID", "client-id")
    monkeypatch.setenv("ZOHO_CREATOR_CLIENT_SECRET", "client-secret")
    captured = {}

    class Response:
        def read(self):
            return json.dumps({
                "code": 3000,
                "result": [
                    {"code": 3000, "data": {"Appointment_ID": 2, "ID": "creator-2"}},
                    {"code": 3000, "data": {"Appointment_ID": 3, "ID": "creator-3"}},
                ],
            }).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_open_zoho_request(request, timeout):
        captured["body"] = json.loads(request.data.decode())
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(
        "services.zoho_creator_service.open_zoho_request", fake_open_zoho_request
    )

    response = create_appointment_schedules(
        settings_from_environment(), "token", [{"Appointment_ID": 2}, {"Appointment_ID": 3}]
    )

    assert response["result"][1]["data"]["ID"] == "creator-3"
    assert captured["body"] == {
        "data": [{"Appointment_ID": 2}, {"Appointment_ID": 3}],
        "skip_workflow": ["form_workflow"],
        "result": {"fields": ["Appointment_ID"]},
    }
    assert captured["timeout"] == 90


def test_get_report_records_reads_creator_report(monkeypatch):
    monkeypatch.setenv("ZOHO_CREATOR_CLIENT_ID", "client-id")
    monkeypatch.setenv("ZOHO_CREATOR_CLIENT_SECRET", "client-secret")
    captured = {}

    class Response:
        headers = {}

        def read(self):
            return json.dumps({
                "code": 3000,
                "data": [{"ID": "creator-plan-1", "Registration_Id": "P-001"}],
            }).encode()

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    def fake_open_zoho_request(request, timeout):
        captured["url"] = request.full_url
        captured["auth"] = request.get_header("Authorization")
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(
        "services.zoho_creator_service.open_zoho_request", fake_open_zoho_request
    )

    records = get_report_records(
        settings_from_environment(), "token", "Plan_Detail_Report"
    )

    assert records == [{"ID": "creator-plan-1", "Registration_Id": "P-001"}]
    assert "/report/Plan_Detail_Report?" in captured["url"]
    assert "max_records=1000" in captured["url"]
    assert captured["auth"] == "Zoho-oauthtoken token"
    assert captured["timeout"] == 60
