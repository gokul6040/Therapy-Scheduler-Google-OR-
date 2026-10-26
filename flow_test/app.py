"""Run with python -m flow_test.app [--live] from the repository root."""

import argparse
import os
import secrets
import shutil
import threading
import webbrowser

from config import load_local_env_file
from flow_test.service import FlowTest, ROOT, load_recipient_names, load_recipients
from flow_test.web_app import WEBHOOK_PATH, create_server


def main() -> None:
    """Start an isolated single-process admin workspace and durable worker."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Send to configured coworkers after publishing")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--init-db", action="store_true", help="Initialize test storage without starting a worker or sending messages")
    args = parser.parse_args()
    load_local_env_file(ROOT / ".env")
    recipients_path = ROOT / "recipients.local.json"
    if not recipients_path.exists():
        shutil.copyfile(ROOT / "recipients.example.json", recipients_path)
    recipients = load_recipients(recipients_path)
    recipient_names = load_recipient_names(recipients_path)
    if args.live and not args.init_db and not (os.getenv("WHATSAPP_APP_SECRET") and (
        os.getenv("FLOW_TEST_VERIFY_TOKEN") or os.getenv("WHATSAPP_VERIFY_TOKEN")
    )):
        parser.error("Live mode requires WHATSAPP_APP_SECRET and a webhook verify token")
    try:
        def unavailable_transport(*_args):
            raise RuntimeError("Database initialization cannot send messages")

        # Live storage settings never redirect the default preview into live data.
        prefix = "FLOW_TEST" if args.live else "FLOW_TEST_PREVIEW"
        flow = FlowTest(
            os.getenv(f"{prefix}_DATABASE_URL") or None, live=args.live,
            recipients=recipients, recipient_names=recipient_names,
            database_schema=os.getenv(f"{prefix}_DATABASE_SCHEMA") or None,
            send=unavailable_transport if args.init_db else None,
        )
    except ValueError as exc:
        parser.error(str(exc))
    if args.init_db:
        print(f"Test storage initialized: {flow.database_schema or 'isolated SQLite'}")
        print("No worker started and no messages sent.")
        flow.engine.dispose()
        return
    token = os.getenv("FLOW_TEST_ADMIN_TOKEN", "").strip() or secrets.token_urlsafe(32)
    server = create_server(flow, token, args.host, args.port)
    stop = threading.Event()

    def worker():
        while not stop.wait(1.0):
            try:
                flow.expire_offers()
                flow.process_batch()
            except Exception:
                print("Test worker paused one cycle after a database error")

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/#token={token}"
    print(f"Mode: {'LIVE coworker messages' if args.live else 'PREVIEW (no network sends)'}")
    print(f"Admin URL (private): {url}")
    print(f"Webhook path: {WEBHOOK_PATH}")
    print("No Zoho integration. Stop with Ctrl+C.")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
        thread.join(timeout=25)
        flow.engine.dispose()


if __name__ == "__main__":
    main()
