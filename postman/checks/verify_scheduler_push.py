"""Real manual push CLI/HTTP checks against the local Zoho Scheduler simulation."""

from datetime import date
from dataclasses import replace
from contextlib import redirect_stdout
from http.server import ThreadingHTTPServer
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import socket
from tempfile import TemporaryDirectory
from threading import Thread
from types import SimpleNamespace
import time
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import ProxyHandler, Request, build_opener

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ["THERAPY_SCHEDULER_DATABASE_URL"] = "sqlite:///:memory:"
os.environ["FLOW_TEST_LIVE"] = "false"
os.environ.update(FLOW_TEST_ZOHO_CLIENT_ID="dummy", FLOW_TEST_ZOHO_CLIENT_SECRET="dummy",
                  FLOW_TEST_ZOHO_REFRESH_TOKEN="dummy", ZOHO_CREATOR_API_URL="http://127.0.0.1:4913/zoho",
                  ZOHO_CREATOR_ACCOUNTS_URL="http://127.0.0.1:4913/zoho")

from flow_test import mount
from flow_test.service import FlowTest
from flow_test.zoho_export import export_preview, setting
from flow_test.zoho_authorize import authorize
from flow_test.zoho_push import export_settings
from web_app import TherapySchedulerHandler


def main():
    with TemporaryDirectory(prefix="scheduler-push-") as directory:
        # Exercise the real one-time callback and token exchange against mock OAuth.
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            callback_port = reservation.getsockname()[1]
        destination = Path(directory) / "test.env"
        destination.write_text("UNCHANGED=dummy\n")
        settings = replace(export_settings(), client_id="dummy", client_secret="dummy",
                           api_url="http://127.0.0.1:4913/zoho", accounts_url="http://127.0.0.1:4913/zoho",
                           redirect_uri=f"http://127.0.0.1:{callback_port}/oauth/zoho/callback")
        output, auth_errors = StringIO(), []
        def connect():
            try:
                with redirect_stdout(output):
                    authorize(settings, destination)
            except Exception as exc:
                auth_errors.append(exc)
        connection = Thread(target=connect, daemon=True)
        connection.start()
        deadline = time.monotonic() + 10
        while "/oauth/v2/auth?" not in output.getvalue() and time.monotonic() < deadline:
            time.sleep(0.05)
        consent = next(line for line in output.getvalue().splitlines() if "/oauth/v2/auth?" in line)
        state = parse_qs(urlsplit(consent).query)["state"][0]
        auth_opener = build_opener(ProxyHandler({}))
        try:
            auth_opener.open(settings.redirect_uri + "?state=invalid&code=dummy", timeout=10)
            raise AssertionError("Untrusted OAuth state was accepted")
        except HTTPError as exc:
            assert exc.code == 400
        with auth_opener.open(settings.redirect_uri + "?" + urlencode({"state":state,"code":"dummy"}), timeout=30) as response:
            assert response.status == 200
        connection.join(timeout=10)
        assert not auth_errors and not connection.is_alive()
        assert destination.read_text() == "UNCHANGED=dummy\nFLOW_TEST_ZOHO_REFRESH_TOKEN=simulation-refresh\n"
        scenario = json.loads((ROOT / "tests/fixtures/flow_scenario_v1.json").read_text())
        flow = FlowTest("sqlite:///" + (Path(directory) / "flow.sqlite").as_posix(), scenario=scenario)
        target = date(2026, 10, 22)
        flow.next_offer_date = lambda: target
        flow.generate(target)
        flow.publish(target)
        flow.drain()
        mount._runtime = SimpleNamespace(flow=flow, token="dummy-admin", previews={})
        server = ThreadingHTTPServer(("127.0.0.1", 0), TherapySchedulerHandler)
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        base = f"http://127.0.0.1:{server.server_port}"
        opener = build_opener(ProxyHandler({}))

        def call(url, body=None):
            request = Request(url, data=json.dumps(body).encode() if body is not None else None,
                              headers={"Authorization":"Bearer dummy-admin", "Content-Type":"application/json"})
            with opener.open(request, timeout=60) as response:
                return json.load(response)

        def api(path, body=None):
            return call(base + "/test/api/" + path, body)

        def push(apply=True, success=True):
            environment = dict(os.environ, FLOW_TEST_APP_URL=base, FLOW_TEST_ADMIN_TOKEN="dummy-admin",
                               FLOW_TEST_ZOHO_CLIENT_ID="dummy", FLOW_TEST_ZOHO_CLIENT_SECRET="dummy",
                               FLOW_TEST_ZOHO_REFRESH_TOKEN="dummy",
                               ZOHO_CREATOR_API_URL="http://127.0.0.1:4913/zoho",
                               ZOHO_CREATOR_ACCOUNTS_URL="http://127.0.0.1:4913/zoho")
            # The simulation's /zoho prefix is routing, not a production base path.
            command = [sys.executable, "-m", "flow_test.zoho_push", "--date", target.isoformat()]
            if apply:
                command.append("--apply")
            result = subprocess.run(command, cwd=ROOT, env=environment, text=True, capture_output=True, timeout=90)
            assert (result.returncode == 0) == success, result.stdout + result.stderr
            return json.loads(result.stdout) if success else result.stdout.strip()

        def remote():
            return call("http://127.0.0.1:4913/zoho/creator/v2.1/data/nidhin.kumar_turnb/test-gokul/report/Scheduler_Report?field_config=all&criteria=ID%20%21%3D%200")["data"]

        def owned():
            with flow.sessions() as session:
                namespace = setting(session, "namespace")
            return [row for row in remote() if row["Scheduler_Key"].startswith(namespace + ":")]

        try:
            call("http://127.0.0.1:4913/zoho/__test__/scheduler-key-unique", {"enabled":False})
            calendar_preview = api("zoho/export/preview", {"date":target.isoformat()})
            assert calendar_preview["wouldAssumeAcceptedVisits"] == 3
            assert "No duplicate values" in push(success=False)
            assert {o["state"] for o in flow.state(target)["offers"]} == {"PENDING"}
            call("http://127.0.0.1:4913/zoho/__test__/scheduler-key-unique", {"enabled":True})
            preview = push(apply=False)
            assert preview["wouldAssumeAcceptedVisits"] == 3
            assert {o["state"] for o in flow.state(target)["offers"]} == {"PENDING"}
            call("http://127.0.0.1:4913/zoho/__test__/fail-next-scheduler-create", {})
            assert "batch retained" in push(success=False)
            assert export_preview(flow, target)["lastPush"] is None
            # Resume via the calendar's server-side route after a CLI failure.
            result = api("zoho/export/push", {"date":target.isoformat()})
            assert result["pushed"] == 4 and result["reconciled"] == 1
            own = owned()
            assert len(own) == len({r["Scheduler_Key"] for r in own}) == 4
            assert {r["Status"] for r in own} == {"ASSUMED_CONFIRMED"}
            assert push()["pushed"] == 0
            token = next(o["token"] for o in flow.state(target)["offers"] if o["patient"] == "TEST-B")
            flow.simulate(token, "NO")
            flow.drain()
            released = next(a for a in flow.state(target)["appointments"] if a["patient"] == "TEST-B")
            d = next(p for p in api("patients")["patients"] if p["code"] == "TEST-D")
            reservation = api(f"appointments/{released['id']}/reservation-preview", {"patientId":d["id"],"wholeVisit":True})
            api(f"appointments/{released['id']}/reserve", {"patientId":d["id"],"wholeVisit":True,
                "mode":"with_messaging","previewToken":reservation["preview_token"],
                "requestId":"push-replacement","overrideWarnings":bool(reservation["warnings"])})
            flow.drain()
            changed = api("zoho/export/push", {"date":target.isoformat()})
            assert changed["created"] == changed["updated"] == 1
            own = owned()
            assert next(r for r in own if r["Reg_ID"] == "TEST-B")["Status"] == "NEEDS_RESCHEDULING"
            assert next(r for r in own if r["Reg_ID"] == "TEST-D")["Status"] == "ASSUMED_CONFIRMED"
            assert len(own) == 5 and push()["pushed"] == 0
            assert next(r for r in remote() if r["Reg_ID"] == "DEMO")["Scheduler_Key"] == ""
            print(json.dumps({"oauth_callback_state_and_atomic_test_token":"passed",
                              "calendar_server_side_push_and_cli_retry":"passed",
                              "manual_cutoff_assumed_visits":result["assumedAcceptedVisits"],
                              "lost_response_reconciled_without_duplicate":True,
                              "second_unchanged_push":0,"decline_and_replacement":"passed",
                              "last_push_IST":changed["completedAtIST"],"real_messages_sent":0},indent=2))
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)
            flow.engine.dispose()
            mount._runtime = None


if __name__ == "__main__":
    main()
