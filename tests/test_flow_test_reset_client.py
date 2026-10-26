"""Verify the live-reset command protocol without touching Azure or contacts."""

from io import BytesIO
import json

import pytest

from flow_test import reset_live as client


@pytest.fixture
def fake_live_api(monkeypatch):
    calls = []
    scenario = json.loads((client.Path(__file__).resolve().parents[1] / "flow_test/scenario.json").read_text(encoding="utf-8"))
    before = {"mode": "LIVE test", "scenario": scenario}
    after = {"appointments": [], "offers": [], "jobs": [], "inbox": [],
             "progress": [{"completed": 0, "booked": 0, "remaining": 21}],
             "windows": {"TEST-A": {"open": False}}}
    response = {"reset": True, "schema": "flow_test", "patients": 8,
                "therapists": 2, "messages_sent": 0}
    people = {"patients": [{"code": f"TEST-{c}"} for c in "ABCDEFGH"]}
    values = [before, people, response, after]

    def urlopen(request, timeout):
        assert request.full_url.startswith(client.BASE_URL + "/test/api/")
        assert request.get_header("Authorization") == "Bearer fake-test-key"
        assert timeout == 30
        calls.append(request)
        return BytesIO(json.dumps(values[len(calls) - 1]).encode())

    monkeypatch.setattr(client, "_admin_token", lambda: "fake-test-key")
    monkeypatch.setattr(client, "urlopen", urlopen)
    return calls, values


def test_reset_client_verifies_version_before_post_and_clean_state_after(fake_live_api):
    calls, _ = fake_live_api
    assert client.reset_live("RESET-flow-test")["patients"] == 8
    assert len(calls) == 4
    assert calls[2].get_method() == "POST"
    assert calls[2].full_url.endswith("/test/api/reset")
    assert json.loads(calls[2].data) == {"confirmation": "RESET flow_test"}
    assert all(c.get_method() == "GET" for c in (calls[0], calls[1], calls[3]))


def test_reset_client_refuses_old_revision_without_mutation(fake_live_api):
    calls, values = fake_live_api
    values[0]["scenario"]["version"] = "malaparamba-fictional-v1"
    with pytest.raises(ValueError, match="Deploy the current code first"):
        client.reset_live("RESET-flow-test")
    assert all(c.get_method() == "GET" for c in calls)


def test_reset_client_requires_explicit_confirmation(fake_live_api):
    calls, _ = fake_live_api
    with pytest.raises(ValueError, match="--confirm"):
        client.reset_live("yes")
    assert not calls


def test_reset_client_detects_remaining_activity_without_reset_retry(fake_live_api):
    calls, values = fake_live_api
    values[3]["offers"] = [{"state": "PENDING"}]
    with pytest.raises(ValueError, match="do not blindly repeat"):
        client.reset_live("RESET-flow-test")
    assert len([c for c in calls if c.get_method() == "POST"]) == 1


def test_reset_client_uses_current_source_counts_not_eight_and_two(fake_live_api):
    _, values = fake_live_api
    scenario = values[0]["scenario"]
    scenario["patients"].append({"code": "REG-NEW-009", "requirements": [{"weekly": 1}]})
    scenario["therapists"].append({"code": "ZC-900002"})
    values[1]["patients"].append({"code": "REG-NEW-009"})
    values[2].update(patients=9, therapists=3)
    values[3]["progress"][0]["remaining"] = 22
    result = client.reset_live("RESET-flow-test")
    assert result["patients"] == 9 and result["therapists"] == 3
    assert result["weekly_sessions"] == 22
