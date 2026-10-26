"""Read approved Zoho test reports, then ask the isolated app to sync and draft."""

import argparse
from dataclasses import replace
from datetime import date, datetime, timezone
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import ProxyHandler, Request, build_opener
from zoneinfo import ZoneInfo

from config import load_local_env_file
from flow_test.reset_live import BASE_URL, _admin_token
from flow_test.zoho_import import APP, OWNER, REPORTS, SOURCE_TIMEZONES, checkpoint_time, convert_reports
from services.zoho_creator_service import get_report_records, refresh_access_token, settings_from_environment


def window_criteria(since: str | None, until: str) -> str:
    """Render each field's boundary in its own source zone; checkpoints stay UTC."""
    end = checkpoint_time(until)
    upper = "(" + " && ".join(
        f"{field} < '{end.astimezone(ZoneInfo(zone)):%d-%b-%Y %H:%M:%S}'"
        for field, zone in SOURCE_TIMEZONES.items()) + ")"
    if since is None:
        return upper
    start = checkpoint_time(since)
    if start >= end:
        raise ValueError("Sync cutoff must be after its saved checkpoint")
    lower = " || ".join(
        f"{field} >= '{start.astimezone(ZoneInfo(zone)):%d-%b-%Y %H:%M:%S}'"
        for field, zone in SOURCE_TIMEZONES.items())
    return f"{upper} && ({lower})"


def fetch_reports(*, since: str | None = None, until: str | None = None) -> dict:
    """OAuth is shared configuration; the read target is pinned to the test app."""
    settings = settings_from_environment()
    overrides = {field: os.environ[f"FLOW_TEST_ZOHO_{field.upper()}"]
                 for field in ("client_id", "client_secret", "refresh_token")
                 if os.environ.get(f"FLOW_TEST_ZOHO_{field.upper()}")}
    settings = replace(settings, account_owner=OWNER, app_link_name=APP, **overrides)
    token = refresh_access_token(settings)["access_token"]
    criteria = window_criteria(since, until) if until else None
    return {report: get_report_records(settings, token, report, criteria=criteria) for report in REPORTS}


def test_api(base_url: str, token: str, path: str, body: dict | None = None) -> dict:
    """Call only the existing app's isolated Zoho test endpoints."""
    target = urlsplit(base_url)
    allowed_host = urlsplit(BASE_URL).hostname
    if (target.query or target.fragment or target.username or target.path not in {"", "/"}
            or not ((target.scheme == "https" and target.hostname == allowed_host)
                    or (target.scheme == "http" and target.hostname in {"localhost", "127.0.0.1"}))):
        raise ValueError("Use the existing Azure app or a loopback preview server")
    if path not in {"status", "import", "export/preview", "export/prepare", "export/ack", "export/finish"}:
        raise ValueError("Unsupported test sync operation")
    request = Request(base_url.rstrip("/") + "/test/api/zoho/" + path,
                      data=json.dumps(body).encode() if body is not None else None,
                      headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    try:
        with build_opener(ProxyHandler({})).open(request, timeout=180) as response:
            return json.load(response)
    except HTTPError as exc:
        # The test endpoint returns only sanitised validation messages, not request bodies.
        try:
            message = json.loads(exc.read()).get("error", "Test import failed")
        except (ValueError, AttributeError):
            message = f"Test import returned HTTP {exc.code}"
        raise ValueError(message) from None


def import_to_app(reports: dict, plan_start: date | None, base_url: str, token: str,
                  *, window: dict | None = None, full_snapshot: bool = False) -> dict:
    body = {"reports": reports,
            "sourceTimezones": SOURCE_TIMEZONES, "fullSnapshot": full_snapshot}
    if window is not None:
        body["syncWindow"] = window
    return test_api(base_url, token, "import", body)


def main() -> None:
    load_local_env_file(Path(__file__).resolve().parent / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan-start", help="Deprecated compatibility option; Added Time supplies treatment start")
    parser.add_argument("--apply", action="store_true", help="Import and prepare a draft; never send offers")
    parser.add_argument("--reconcile", action="store_true", help="With --apply, reread all source rows before the cutoff without resetting data")
    parser.add_argument("--capture", type=Path, help="Use an already-captured local JSON snapshot")
    args = parser.parse_args()
    try:
        start = date.fromisoformat(args.plan_start) if args.plan_start else None
        if args.reconcile and not args.apply:
            raise ValueError("Use --reconcile with --apply")
        if args.apply:
            if args.capture:
                raise ValueError("Captured snapshots are validation-only; live sync uses bounded API reads")
            token = os.getenv("FLOW_TEST_ADMIN_TOKEN") or _admin_token()
            base_url = os.getenv("FLOW_TEST_APP_URL", BASE_URL)
            status = test_api(base_url, token, "status")
            if status.get("source") != f"{OWNER}/{APP}" or "checkpoint" not in status:
                raise ValueError("Deploy the incremental test endpoints before syncing")
            if status.get("sourceTimezones") != SOURCE_TIMEZONES:
                raise ValueError("Deploy the Added Time IST / Modified Time IST update before syncing")
            if status.get("planStartSource") != "Added_Time":
                raise ValueError("Deploy the Added Time treatment-start update before syncing")
            if status.get("reconciliationRequired") and not args.reconcile:
                raise ValueError("Run --apply --reconcile once to recover records missed by the old timestamp filter")
            since = status["checkpoint"]
            until = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            if since is not None and checkpoint_time(since) >= checkpoint_time(until):
                print(json.dumps({"mode": "NO NEW WINDOW", "checkpoint": since}))
                return
            reports = fetch_reports(since=None if args.reconcile else since, until=until)
            summary = import_to_app(reports, start, base_url, token, window={"from": since, "to": until},
                                    full_snapshot=args.reconcile)
            summary["downloaded"] = {key: len(value) for key, value in reports.items()}
        else:
            reports = json.loads(args.capture.read_text(encoding="utf-8")) if args.capture else fetch_reports()
            scenario = convert_reports(reports, start)
            summary = {"patients": len(scenario["patients"]), "consultants": len(scenario["therapists"]),
                       "requirements": sum(len(p["requirements"]) for p in scenario["patients"]),
                       "weekly_sessions": sum(r["weekly"] for p in scenario["patients"] for r in p["requirements"]),
                       "branches": len(scenario["locations"]), "plan_start_source": "Added_Time (Asia/Kolkata date)",
                       "mode": "VALIDATION ONLY"}
        print(json.dumps(summary, indent=2))
        if summary.get("draftError"):
            raise SystemExit(1)
    except (ValueError, OSError, URLError) as exc:
        message = str(exc) if isinstance(exc, ValueError) else "Connection or local file operation failed"
        print(f"Test Zoho sync stopped: {message}")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
