import argparse
import json
import os
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


def read_json(request: Request) -> tuple[dict, str | None]:
    """Return the response and next-page cursor without printing credentials."""
    try:
        with urlopen(request, timeout=60) as response:
            return json.load(response), response.headers.get("record_cursor")
    except HTTPError as exc:
        # Creator reports return HTTP 400 / code 9280 when no rows match.
        try:
            payload = json.load(exc)
        except (ValueError, UnicodeError):
            payload = {}
        if exc.code == 400 and payload.get("code") in (3100, 9280):
            return payload, None
        raise RuntimeError(f"Zoho returned HTTP {exc.code}; check credentials and report access") from None
    except URLError:
        raise RuntimeError("Could not connect to Zoho") from None


def refresh_access_token() -> str:
    """Exchange your refresh token for a temporary access token."""
    names = ("ZOHO_CLIENT_ID", "ZOHO_CLIENT_SECRET", "ZOHO_REFRESH_TOKEN")
    if any(not os.getenv(name) for name in names):
        raise ValueError("Set " + ", ".join(names))
    accounts_url = os.getenv("ZOHO_ACCOUNTS_URL", "https://accounts.zoho.in").rstrip("/")
    request = Request(accounts_url + "/oauth/v2/token", method="POST", data=urlencode({
        "grant_type": "refresh_token",
        "client_id": os.environ["ZOHO_CLIENT_ID"],
        "client_secret": os.environ["ZOHO_CLIENT_SECRET"],
        "refresh_token": os.environ["ZOHO_REFRESH_TOKEN"],
    }).encode("utf-8"))
    payload, _ = read_json(request)
    if not payload.get("access_token"):
        raise RuntimeError("OAuth refresh failed; check the OAuth client, token and data centre")
    return payload["access_token"]


def fetch_report(owner: str, app: str, report: str, criteria: str | None = None) -> list[dict]:
    """Fetch every page of a report, optionally using Creator search criteria."""
    token = refresh_access_token()
    api_url = os.getenv("ZOHO_API_URL", "https://www.zohoapis.in").rstrip("/")
    owner, app, report = (quote(value, safe="") for value in (owner, app, report))
    endpoint = f"{api_url}/creator/v2.1/data/{owner}/{app}/report/{report}"
    parameters = {"max_records": 1000, "field_config": "all"}
    if criteria:
        parameters["criteria"] = criteria
    records, seen_cursors = [], set()
    cursor = None
    while True:
        headers = {"Authorization": f"Zoho-oauthtoken {token}", "Accept": "application/json"}
        if cursor:
            headers["record_cursor"] = cursor
        payload, next_cursor = read_json(Request(endpoint + "?" + urlencode(parameters), headers=headers))
        if payload.get("code") in (3100, 9280) and cursor is None:
            return []
        if payload.get("code") != 3000:
            raise RuntimeError("Zoho did not return a successful report response")
        rows = payload.get("data")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise RuntimeError("Zoho returned invalid report data")
        records.extend(rows)
        if not next_cursor:
            return records
        if next_cursor in seen_cursors:
            raise RuntimeError("Zoho repeated a page cursor; refusing an incomplete download")
        seen_cursors.add(next_cursor)
        cursor = next_cursor


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--app", required=True)
    parser.add_argument("--report", required=True)
    parser.add_argument("--criteria", help="Optional Creator filter, in the source app's date/time format and timezone")
    parser.add_argument("--output", type=Path, default=Path("zoho_records.json"))
    args = parser.parse_args()
    try:
        records = fetch_report(args.owner, args.app, args.report, args.criteria)
        args.output.write_text(json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8")
    except (ValueError, RuntimeError, OSError) as exc:
        parser.exit(1, f"Pull failed: {exc}\n")
    print(f"Downloaded {len(records)} records to {args.output}")


if __name__ == "__main__":
    main()
