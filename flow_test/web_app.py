"""Small authenticated test dashboard and separately authenticated Meta webhook."""

from datetime import date, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from services.whatsapp_service import MAX_WEBHOOK_BODY_BYTES, valid_webhook_signature


WEB = Path(__file__).resolve().parent / "web"
WEBHOOK_PATH = "/webhooks/whatsapp-flow-test"


def handler_for(flow, admin_token: str):
    """Bind a handler to this test instance; never expose production endpoints."""
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # Callback URLs and event bodies can include private data.

        def reply(self, status: int, body, content_type="application/json"):
            if content_type == "application/json":
                body = json.dumps(body, ensure_ascii=False).encode("utf-8")
            elif isinstance(body, str):
                body = body.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            self.wfile.write(body)

        def authorized(self) -> bool:
            supplied = self.headers.get("Authorization", "")
            if not secrets.compare_digest(supplied, f"Bearer {admin_token}"):
                # A rejected POST body remains unread. Close the connection so
                # those bytes cannot be mistaken for the next HTTP request.
                self.close_connection = True
                self.reply(HTTPStatus.UNAUTHORIZED, {"error": "Open the admin URL printed by the test launcher"})
                return False
            return True

        def body(self) -> bytes:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise ValueError("Invalid body length") from None
            if not 0 < length <= MAX_WEBHOOK_BODY_BYTES:
                raise ValueError("Request body is missing or too large")
            return self.rfile.read(length)

        def do_GET(self):
            parsed = urlsplit(self.path)
            if parsed.path == WEBHOOK_PATH:
                query = parse_qs(parsed.query)
                verify = os.getenv("FLOW_TEST_VERIFY_TOKEN") or os.getenv("WHATSAPP_VERIFY_TOKEN", "")
                provided = query.get("hub.verify_token", [""])[0]
                if verify and query.get("hub.mode") == ["subscribe"] and secrets.compare_digest(verify, provided):
                    self.reply(200, query.get("hub.challenge", [""])[0], "text/plain")
                else:
                    self.reply(403, {"error": "Webhook verification failed"})
                return
            if parsed.path == "/api/state":
                if not self.authorized():
                    return
                try:
                    raw_date = parse_qs(parsed.query).get("date", [datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()])[0]
                    self.reply(200, flow.state(date.fromisoformat(raw_date)))
                except ValueError:
                    self.reply(400, {"error": "Use a valid ISO date"})
                return
            files = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "application/javascript"),
                     "/styles.css": ("styles.css", "text/css")}
            if parsed.path in files:
                name, content_type = files[parsed.path]
                self.reply(200, (WEB / name).read_bytes(), content_type)
            else:
                self.reply(404, {"error": "Not found"})

        def do_POST(self):
            parsed = urlsplit(self.path)
            try:
                if parsed.path == WEBHOOK_PATH:
                    raw = self.body()
                    secret = os.getenv("WHATSAPP_APP_SECRET", "")
                    if not secret or not valid_webhook_signature(raw, self.headers.get("X-Hub-Signature-256"), secret):
                        self.reply(401, {"error": "Invalid webhook signature"})
                        return
                    payload = json.loads(raw)
                    if not isinstance(payload, dict):
                        raise ValueError("Webhook must contain a JSON object")
                    if flow.live:
                        flow.receive(payload)
                    self.reply(200, {"received": True})
                    return
                if not self.authorized():
                    return
                payload = json.loads(self.body())
                if not isinstance(payload, dict):
                    raise ValueError("Expected a JSON object")
                path = parsed.path
                if path == "/api/generate":
                    result = flow.generate(date.fromisoformat(payload["date"]), payload.get("patients"))
                elif path == "/api/publish":
                    result = {"offers": flow.publish(date.fromisoformat(payload["date"]), payload.get("appointment_id"))}
                elif path == "/api/simulate":
                    flow.simulate(payload["token"], payload["action"])
                    result = {"queued": True}
                elif path == "/api/attendance":
                    flow.attendance(int(payload["id"]), payload["status"])
                    result = {"saved": True}
                elif path == "/api/release":
                    flow.release_offer(payload["token"])
                    result = {"released": True}
                elif path == "/api/resolve":
                    flow.resolve(int(payload["id"]))
                    result = {"saved": True}
                elif path == "/api/process" and not flow.live:
                    result = {"processed": flow.drain()}
                else:
                    self.reply(404, {"error": "Not found"})
                    return
                self.reply(200, result)
            except (ValueError, KeyError, TypeError) as exc:
                message = str(exc) if isinstance(exc, ValueError) else "Missing or invalid request fields"
                self.reply(400, {"error": message})
            except Exception:
                self.reply(500, {"error": "Test operation failed; inspect the queue and database"})

    return Handler


def create_server(flow, admin_token: str, host="127.0.0.1", port=8767):
    return ThreadingHTTPServer((host, port), handler_for(flow, admin_token))
