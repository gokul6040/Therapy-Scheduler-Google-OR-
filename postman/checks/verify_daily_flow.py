"""Run the real CLI and shared HTTP flow against local Postman Zoho simulations.

Start zoho-test.sim.yaml on 4913 and zoho-test-failure.sim.yaml on 4914 first.
All credentials, contacts and records are fictional; storage is temporary SQLite.
No Meta endpoint, Azure database or production application is contacted.
"""

from datetime import date, datetime, timezone
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import time
from types import SimpleNamespace
from urllib.request import ProxyHandler, Request, build_opener
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ["THERAPY_SCHEDULER_DATABASE_URL"] = "sqlite:///:memory:"
os.environ["FLOW_TEST_LIVE"] = "false"

from flow_test import mount
from flow_test.models import FlowSetting
from flow_test.service import FlowTest
from web_app import TherapySchedulerHandler


def main():
    with TemporaryDirectory(prefix="daily-flow-audit-") as directory:
        flow = FlowTest("sqlite:///" + (Path(directory) / "preview.sqlite").as_posix())
        target = date(2026, 10, 23)
        flow.next_offer_date = lambda: target
        mount._runtime = SimpleNamespace(flow=flow, token="dummy-admin", previews={})
        server = ThreadingHTTPServer(("127.0.0.1", 0), TherapySchedulerHandler)
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f"http://127.0.0.1:{server.server_port}"
        opener = build_opener(ProxyHandler({}))

        def api(path, body=None):
            request = Request(base + "/test/api/" + path,
                              data=json.dumps(body).encode() if body is not None else None,
                              headers={"Authorization": "Bearer dummy-admin", "Content-Type": "application/json"})
            with opener.open(request, timeout=60) as response:
                return json.load(response)

        def sync(port=4913, success=True, reconcile=False):
            environment = dict(os.environ, FLOW_TEST_APP_URL=base, FLOW_TEST_ADMIN_TOKEN="dummy-admin",
                               FLOW_TEST_ZOHO_CLIENT_ID="dummy", FLOW_TEST_ZOHO_CLIENT_SECRET="dummy",
                               FLOW_TEST_ZOHO_REFRESH_TOKEN="dummy",
                               ZOHO_CREATOR_ACCOUNTS_URL=f"http://127.0.0.1:{port}/zoho",
                               ZOHO_CREATOR_API_URL=f"http://127.0.0.1:{port}/zoho")
            command = [sys.executable, "-m", "flow_test.zoho_daily", "--apply"]
            if reconcile:
                command.append("--reconcile")
            result = subprocess.run(command,
                                    cwd=ROOT, env=environment, text=True, capture_output=True, timeout=90)
            assert (result.returncode == 0) == success, result.stdout + result.stderr
            return json.loads(result.stdout) if success else result.stdout.strip()

        def process():
            return api("process", {})

        def state():
            return api(f"state?date={target.isoformat()}")

        def reply(token, action):
            api("simulate", {"token": token, "action": action})
            process()

        try:
            imported = sync()
            assert imported["patients"] == 8 and imported["messages_sent"] == 0
            assert api("zoho/status")["sourceTimezones"] == {"Added_Time": "Asia/Kolkata", "Modified_Time": "Asia/Kolkata"}
            assert not state()["appointments"]
            roster = [f"TEST-{c}" for c in "ABCDEFG"]  # H is this scenario's standby.
            draft = api("schedules/generate", {"start": target.isoformat(), "patients": roster, "searchSeconds": 20})
            saved = api(f"schedules/{draft['previewId']}/save", {})
            assert saved["offers"] == 7
            assert api(f"schedules/{draft['previewId']}/save", {}) == saved
            process()
            tokens = {o["patient"]: o["token"] for o in state()["offers"]}
            reply(tokens["TEST-B"], "HELP")
            assert any(i["reason"] == "Parent requested help" for i in state()["inbox"])
            help_item = next(i for i in state()["inbox"] if i["reason"] == "Parent requested help")
            api("resolve", {"id": help_item["id"]})
            reply(tokens["TEST-B"], "YES")
            b = next(a for a in state()["appointments"] if a["patient"] == "TEST-B")
            api("attendance", {"id": b["id"], "status": "COMPLETED"})
            reply(tokens["TEST-F"], "NO")
            released = next(a for a in state()["appointments"] if a["patient"] == "TEST-F")
            assert released["status"] == "NEEDS_RESCHEDULING"
            h = next(p for p in api("patients")["patients"] if p["code"] == "TEST-H")
            replacement = api(f"appointments/{released['id']}/reservation-preview", {"patientId": h["id"], "wholeVisit": True})
            offered = api(f"appointments/{released['id']}/reserve", {
                "patientId": h["id"], "wholeVisit": True, "mode": "with_messaging",
                "previewToken": replacement["preview_token"], "requestId": "http-audit-replacement",
                "overrideWarnings": bool(replacement["warnings"]),
            })
            process()
            reply(offered["offer_token"], "YES")
            replacement_id = offered["appointment_ids"][0]
            assert next(a for a in state()["appointments"] if a["id"] == replacement_id)["status"] == "CONFIRMED"
            api("attendance", {"id": replacement_id, "status": "NO_SHOW"})
            b_progress = next(p for p in state()["progress"] if p["patient"] == "TEST-B")
            assert (b_progress["completed"], b_progress["remaining"]) == (1, 1)
            h_progress = next(p for p in state()["progress"] if p["patient"] == "TEST-H")
            assert (h_progress["completed"], h_progress["booked"], h_progress["remaining"]) == (0, 0, 2)
            bookings = state()["appointments"]
            # Seed a legacy persisted UTC cache, then run the actual recovery CLI.
            with flow.sessions.begin() as session:
                cache = session.get(FlowSetting, "zoho_test_source")
                legacy = json.loads(cache.value)
                for rows in legacy.values():
                    for row in rows:
                        for field in ("Added_Time", "Modified_Time"):
                            row[field] = datetime.strptime(row[field], "%d-%b-%Y %H:%M:%S").replace(
                                tzinfo=ZoneInfo("Asia/Kolkata")).astimezone(timezone.utc).strftime("%d-%b-%Y %H:%M:%S")
                cache.value = json.dumps(legacy)
                session.get(FlowSetting, "zoho_test_source_timezone").value = json.dumps("UTC")
            assert api("zoho/status")["reconciliationRequired"]
            assert "--reconcile" in sync(success=False)
            recovered = sync(reconcile=True)
            assert recovered["patients"] == 8 and not api("zoho/status")["reconciliationRequired"]
            assert state()["appointments"] == bookings
            time.sleep(1.1)
            unchanged = sync()
            assert unchanged["downloaded"] == {"Plan_Report": 0, "All_Consultants": 0}
            assert not unchanged["changed"] and state()["appointments"] == bookings
            checkpoint = api("zoho/status")["checkpoint"]
            time.sleep(1.1)  # Ensure a new whole-second window for the failure check.
            failure = sync(4914, success=False)
            assert api("zoho/status")["checkpoint"] == checkpoint
            assert state()["appointments"] == bookings
            print(json.dumps({"imported": imported["patients"], "saved_offers": saved["offers"],
                              "help_confirm_attendance": "passed", "replacement_confirm_no_show": "passed",
                              "timezone_migration_and_empty_delta": "passed",
                              "failed_fetch_preserves_checkpoint_and_bookings": True,
                              "failure": failure, "real_messages_sent": 0}, indent=2))
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)
            flow.engine.dispose()
            mount._runtime = None


if __name__ == "__main__":
    main()
