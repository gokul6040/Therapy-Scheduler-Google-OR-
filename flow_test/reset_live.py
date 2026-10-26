"""Explicitly start the existing live /test workspace from clean fictional inputs."""

import argparse
from datetime import datetime, timedelta
import json
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


BASE_URL = "https://orscheduler.agreeablemeadow-8055ebc2.eastus2.azurecontainerapps.io"
VERSION = "malaparamba-fictional-v3"


def _admin_token() -> str:
    path = Path(__file__).resolve().parent / "data" / "admin.token"
    return path.read_text(encoding="utf-8").strip()


def reset_live(confirmation: str) -> dict:
    """Use the app's serialized reset API, never an uncoordinated database delete."""
    if confirmation != "RESET-flow-test":
        raise ValueError("Use --confirm RESET-flow-test to clear live test history")
    token = _admin_token()
    if not token:
        raise ValueError("The saved test admin key is empty")

    def api(path: str, body: dict | None = None) -> dict:
        request = Request(
            BASE_URL + "/test/api/" + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {token}",
                     "Content-Type": "application/json"},
        )
        with urlopen(request, timeout=30) as response:
            return json.load(response)

    target = (datetime.now(ZoneInfo("Asia/Kolkata")).date() + timedelta(days=1)).isoformat()
    state_path = f"state?date={target}"
    before = api(state_path)
    people = api("patients")
    codes = {p["code"] for p in people.get("patients", []) if p.get("active", True)}
    scenario = before.get("scenario", {})
    expected_codes = {p["code"] for p in scenario.get("patients", [])}
    expected_therapists = len(scenario.get("therapists", []))
    expected_weekly = sum(r["weekly"] for p in scenario.get("patients", []) for r in p["requirements"])
    if (not str(before.get("mode", "")).startswith("LIVE")
            or before.get("scenario", {}).get("version") != VERSION
            or not expected_codes or not expected_therapists or codes != expected_codes):
        raise ValueError("The live test scenario is not ready. Deploy the current code first; nothing was reset.")
    result = api("reset", {"confirmation": "RESET flow_test"})
    if (result.get("reset") is not True or result.get("schema") != "flow_test"
            or result.get("patients") != len(expected_codes) or result.get("therapists") != expected_therapists
            or result.get("messages_sent") != 0):
        raise ValueError("Reset response needs review. Inspect /test before repeating the reset.")
    after = api(state_path)
    clean = (
        not any(after.get(key) for key in ("appointments", "offers", "jobs", "inbox"))
        and all(p["completed"] == p["booked"] == 0 for p in after["progress"])
        and sum(p["remaining"] for p in after["progress"]) == expected_weekly
        and not any(w["open"] for w in after["windows"].values())
    )
    if not clean:
        raise ValueError("Reset applied, but new activity or unexpected totals appeared. Inspect /test; do not blindly repeat the reset.")
    return {**result, "weekly_sessions": expected_weekly}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", required=True, choices=("RESET-flow-test",))
    args = parser.parse_args()
    try:
        result = reset_live(args.confirm)
    except (HTTPError, URLError, OSError, ValueError, KeyError) as exc:
        # Do not print request objects, headers or response bodies containing keys.
        print(f"Live reset stopped: {type(exc).__name__}")
        if isinstance(exc, ValueError):
            print(str(exc))
        raise SystemExit(1) from None
    print(f"Live /test reset verified: {result['patients']} children, {result['therapists']} therapists.")
    print("Old bookings, offers, replies, attendance, absences and inbox are cleared.")
    print(f"Remaining weekly demand: {result['weekly_sessions']}; no saved appointments or messages sent.")
    print("Send a fresh START from the configured test number, then refresh /test and generate tomorrow's preview.")


if __name__ == "__main__":
    main()
