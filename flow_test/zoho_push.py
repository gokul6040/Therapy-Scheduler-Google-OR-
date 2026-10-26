"""Manually finalise one test date and incrementally publish it to Scheduler."""

import argparse
from dataclasses import replace
from datetime import date
import json
import os
from pathlib import Path
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request

from config import load_local_env_file
from flow_test.reset_live import BASE_URL, _admin_token
from flow_test.zoho_daily import test_api
from flow_test.zoho_import import APP, OWNER
from services.zoho_creator_service import (
    ZohoCreatorSettings, get_report_records, open_zoho_request, refresh_access_token, settings_from_environment,
)


FORM = "Scheduler"
REPORT = "Scheduler_Report"
REQUIRED_FIELDS = {"Reg_ID", "PatientName", "Therapy_Date", "Doctor", "TherapyTime", "Scheduler_Key", "Status"}
REQUIRED_SCOPES = {"ZohoCreator.form.CREATE", "ZohoCreator.report.READ", "ZohoCreator.report.UPDATE",
                   "ZohoCreator.meta.form.READ"}


def export_settings() -> ZohoCreatorSettings:
    settings = settings_from_environment()
    overrides = {field: os.environ[f"FLOW_TEST_ZOHO_{field.upper()}"]
                 for field in ("client_id", "client_secret", "refresh_token")
                 if os.environ.get(f"FLOW_TEST_ZOHO_{field.upper()}")}
    return replace(settings, account_owner=OWNER, app_link_name=APP,
                   form_link_name=FORM, report_link_name=REPORT, **overrides)


def verify_target(settings: ZohoCreatorSettings) -> None:
    host = urlsplit(settings.api_url)
    if ((settings.account_owner, settings.app_link_name, settings.form_link_name, settings.report_link_name)
            != (OWNER, APP, FORM, REPORT) or host.query or host.fragment or host.username
            or not ((host.scheme == "https" and host.hostname == "www.zohoapis.in" and not host.path)
                    or (host.scheme == "http" and host.hostname in {"localhost", "127.0.0.1"}
                        and host.path in {"", "/zoho"}))):
        raise ValueError("Schedule exports are restricted to the authorized test-gokul Scheduler form")


def write_record(settings: ZohoCreatorSettings, access_token: str, fields: dict, record_id: str | None) -> str:
    """POST a new unique key or PATCH the same owned row; never disable workflows."""
    verify_target(settings)
    if set(fields) != REQUIRED_FIELDS or not fields["Scheduler_Key"].startswith("test-"):
        raise ValueError("Unexpected Scheduler payload")
    if record_id is not None and not record_id.isdigit():
        raise ValueError("Invalid Scheduler record ID")
    suffix = f"report/{REPORT}/{record_id}" if record_id else f"form/{FORM}"
    request = Request(f"{settings.api_url}/creator/v2.1/data/{OWNER}/{APP}/{suffix}",
                      data=json.dumps({"data": fields}).encode(), method="PATCH" if record_id else "POST",
                      headers={"Authorization": f"Zoho-oauthtoken {access_token}", "Content-Type": "application/json"})
    try:
        with open_zoho_request(request, timeout=30) as response:
            payload = json.load(response)
    except HTTPError as exc:
        try:
            code = json.loads(exc.read()).get("code", exc.code)
        except (ValueError, AttributeError):
            code = exc.code
        raise ValueError(f"Scheduler write rejected (code {code}); batch retained for review/retry") from None
    except (URLError, TimeoutError, OSError):
        raise ValueError("Scheduler write outcome is uncertain; batch retained. Rerun to reconcile by Scheduler Key") from None
    result = payload.get("result", payload)
    if isinstance(result, list):
        result = result[0] if len(result) == 1 else {}
    identifier = (result.get("data") or {}).get("ID")
    if payload.get("code") != 3000 or result.get("code", 3000) != 3000 or not str(identifier or "").isdigit():
        raise ValueError("Scheduler did not confirm the record; batch retained for reconciliation")
    return str(identifier)


def validate_schema(settings: ZohoCreatorSettings, access_token: str) -> None:
    """Verify the real field names and uniqueness even when the report is empty."""
    verify_target(settings)
    request = Request(f"{settings.api_url}/creator/v2.1/meta/{OWNER}/{APP}/form/{FORM}/fields",
                      headers={"Authorization": f"Zoho-oauthtoken {access_token}"})
    try:
        with open_zoho_request(request, timeout=30) as response:
            payload = json.load(response)
    except HTTPError:
        raise ValueError("Cannot read Scheduler field definitions. Run python -m flow_test.zoho_authorize once") from None
    except (URLError, OSError):
        raise ValueError("Scheduler field definitions could not be reached; no finalisation or push was attempted") from None
    fields = {field["link_name"]: field for field in payload.get("fields", []) if "link_name" in field}
    if payload.get("code") != 3000 or REQUIRED_FIELDS - fields.keys():
        raise ValueError("Scheduler is missing field link names: " + ", ".join(sorted(REQUIRED_FIELDS - fields.keys())))
    key, status = fields["Scheduler_Key"], fields["Status"]
    if key.get("type") != 1 or key.get("unique") is not True or int(key.get("max_char", 255)) < 60:
        raise ValueError("Scheduler_Key must be Single Line, at least 60 characters, with No duplicate values enabled")
    if status.get("type") != 1:
        raise ValueError("Status must be a Single Line field")


def run_push(settings: ZohoCreatorSettings, access_token: str, base_url: str, admin_token: str, target: date, *, apply: bool,
             exchange: Callable[[str, dict], dict] | None = None) -> dict:
    """Share the export protocol between the CLI and the calendar's local adapter."""
    if exchange is None:
        exchange = lambda operation, body: test_api(base_url, admin_token, f"export/{operation}", body)
    verify_target(settings)
    validate_schema(settings, access_token)
    try:
        existing = get_report_records(settings, access_token, REPORT, criteria="ID != 0")
    except (ValueError, OSError):
        raise ValueError("Could not read Scheduler_Report. Check Zoho access and retry; no new batch was prepared.") from None
    preview = exchange("preview", {"date": target.isoformat()})
    if not apply:
        return {"mode": "PREVIEW ONLY", "date": target.isoformat(), "target": preview["target"],
                "changesReady": len(preview["changes"]), "wouldAssumeAcceptedVisits": preview["wouldAssumeAcceptedVisits"],
                "awaitingFinalisation": preview["awaitingFinalisation"], "lastPush": preview["lastPush"]}
    by_key = {}
    for row in existing:
        if row.get("Scheduler_Key"):
            if row["Scheduler_Key"] in by_key:
                raise ValueError("Duplicate Scheduler Key in Zoho; correct it before resuming")
            by_key[row["Scheduler_Key"]] = row
    batch = exchange("prepare", {"date": target.isoformat()})
    created = updated = recovered = 0
    for change in batch["changes"]:
        appointment_id = change["appointmentId"]
        if str(appointment_id) in batch["acknowledged"]:
            continue
        fields = change["fields"]
        found = by_key.get(fields["Scheduler_Key"])
        if found and all(str(found.get(k, "")) == str(v) for k, v in fields.items()):
            record_id = str(found["ID"])
            recovered += 1
        else:
            record_id = write_record(settings, access_token, fields, str(found["ID"]) if found else None)
            if found:
                updated += 1
            else:
                created += 1
        exchange("ack", {
            "batchId": batch["id"], "appointmentId": appointment_id,
            "fingerprint": change["fingerprint"], "recordId": record_id,
        })
    result = exchange("finish", {"batchId": batch["id"]})
    summary = {**result, "created": created, "updated": updated, "reconciled": recovered,
               "assumedAcceptedVisits": batch["assumedAcceptedVisits"], "mode": "APPLIED"}
    if batch.get("resumed"):
        # Finish the frozen attempt, then catch changes received since its cutoff.
        current = run_push(settings, access_token, base_url, admin_token, target, apply=True, exchange=exchange)
        for field in ("created", "updated", "reconciled", "assumedAcceptedVisits", "pushed"):
            current[field] += summary[field]
        current["resumedBatch"] = batch["id"]
        return current
    return summary


def main() -> None:
    load_local_env_file(Path(__file__).resolve().parent / ".env")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True, help="Therapy schedule date, YYYY-MM-DD (India)")
    parser.add_argument("--apply", action="store_true", help="Manual cutoff: assume unanswered sent offers accepted and push changes")
    args = parser.parse_args()
    try:
        target = date.fromisoformat(args.date)
        settings = export_settings()
        verify_target(settings)
        connection = refresh_access_token(settings)
        if args.apply and not REQUIRED_SCOPES.issubset(set(connection.get("scope", "").split())):
            raise ValueError("The saved connection needs update/field-definition access. Run python -m flow_test.zoho_authorize once, then retry")
        token = connection["access_token"]
        result = run_push(settings, token, os.getenv("FLOW_TEST_APP_URL", BASE_URL),
                          os.getenv("FLOW_TEST_ADMIN_TOKEN") or _admin_token(), target, apply=args.apply)
        print(json.dumps(result, indent=2))
    except (ValueError, OSError) as exc:
        print(f"Test schedule push stopped: {exc}")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
