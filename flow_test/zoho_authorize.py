"""One-time local consent for test Scheduler create/read/update permissions."""

from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
import os
from pathlib import Path
import secrets
import tempfile
import time
from urllib.parse import parse_qs, urlsplit

from config import load_local_env_file
from flow_test.zoho_push import REQUIRED_SCOPES, export_settings, verify_target
from services.zoho_creator_service import ZohoCreatorSettings, authorization_url, exchange_authorization_code, refresh_access_token


SCOPES = tuple(sorted(REQUIRED_SCOPES))


def save_test_refresh_token(path: Path, token: str) -> None:
    """Preserve existing local settings and atomically replace only the test token."""
    if not token or any(c in token for c in "\r\n"):
        raise ValueError("Zoho did not return a valid refresh token")
    key = "FLOW_TEST_ZOHO_REFRESH_TOKEN"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    lines = [line for line in lines if not line.startswith(key + "=")]
    lines.append(f"{key}={token}")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = handle.name
            handle.write("\n".join(lines) + "\n")
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)
    os.environ[key] = token


def authorize(settings: ZohoCreatorSettings, destination: Path) -> None:
    """Serve the already-registered loopback callback; the user approves in Zoho."""
    verify_target(settings)
    redirect = urlsplit(settings.redirect_uri)
    if (redirect.scheme != "http" or redirect.hostname not in {"127.0.0.1", "localhost"}
            or redirect.path != "/oauth/zoho/callback" or redirect.query or redirect.fragment):
        raise ValueError("This helper requires the existing registered loopback OAuth callback")
    state, deadline = secrets.token_urlsafe(32), time.monotonic() + 600
    result = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Callback URLs contain a short-lived authorization code.

        def do_GET(self):
            parsed = urlsplit(self.path)
            query = parse_qs(parsed.query)
            valid = (parsed.path == redirect.path and time.monotonic() < deadline
                     and secrets.compare_digest(query.get("state", [""])[0], state))
            status, message = 400, "Invalid or expired Zoho connection callback."
            if valid:
                if query.get("error") or not query.get("code"):
                    result["error"] = "Zoho consent was not completed"
                else:
                    try:
                        payload = exchange_authorization_code(settings, query["code"][0])
                        token = payload.get("refresh_token", "")
                        refreshed = refresh_access_token(replace(settings, refresh_token=token))
                        if not set(SCOPES).issubset(set(refreshed.get("scope", "").split())):
                            raise ValueError("Zoho did not grant the requested permissions")
                        save_test_refresh_token(destination, token)
                        result["ok"] = True
                        status, message = 200, "Test Scheduler connected. You can close this tab."
                    except (ValueError, OSError):
                        result["error"] = "Zoho connection was not saved; check consent and existing client configuration"
            data = message.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    try:
        server = HTTPServer((redirect.hostname, redirect.port or 80), Callback)
    except OSError:
        raise ValueError("The local callback port is busy. Stop the local scheduler server, then rerun this helper") from None
    server.timeout = 1
    try:
        print("Open this Zoho link and approve create/read/update and reading form definitions for the test connection:", flush=True)
        print(authorization_url(replace(settings, scopes=SCOPES), state), flush=True)
        while not result and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    if not result.get("ok"):
        raise ValueError(result.get("error", "Zoho connection timed out; existing settings were kept"))
    print("Test-only refresh token saved in ignored flow_test/.env. No schedule records were pushed.")


def main() -> None:
    path = Path(__file__).resolve().parent / ".env"
    load_local_env_file(path)
    try:
        authorize(export_settings(), path)
    except (ValueError, OSError) as exc:
        print(f"Test Zoho connection stopped: {exc}")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
