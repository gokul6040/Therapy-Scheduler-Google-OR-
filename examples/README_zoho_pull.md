# Standalone Zoho Creator report downloader

Send `zoho_creator_pull.py` to your friend. It is independent of this scheduling
project and uses only the Python standard library (Python 3.10 or later).
It reads one Zoho Creator report and saves the raw records as JSON. It does not
write to Zoho, connect to our database, or run the scheduler.

You need your own OAuth Client ID, Client Secret and Refresh Token, with
`ZohoCreator.report.READ` permission and access to the target report.

In PowerShell, in the folder containing the script:

```powershell
$env:ZOHO_CLIENT_ID = "YOUR_CLIENT_ID"
$env:ZOHO_CLIENT_SECRET = "YOUR_CLIENT_SECRET"
$env:ZOHO_REFRESH_TOKEN = "YOUR_REFRESH_TOKEN"

python .\zoho_creator_pull.py --owner YOUR_OWNER --app YOUR_APP --report YOUR_REPORT --output records.json
```

`YOUR_OWNER`, `YOUR_APP` and `YOUR_REPORT` are the Creator account username,
application link name and report link name, respectively. For our test URL,
these are `nidhin.kumar_turnb`, `test-gokul` and `Plan_Report`. To download
consultants, use `All_Consultants` and a separate output filename.

India data-centre endpoints are the defaults (`accounts.zoho.in` and
`www.zohoapis.in`). For another data centre, set `ZOHO_ACCOUNTS_URL` and
`ZOHO_API_URL` to the matching official endpoints before running.

The script refreshes the access token and follows every `record_cursor` page.
An optional `--criteria` accepts Creator filter syntax. Date/time criteria must
use the SOURCE application's format and timezone; don't assume your computer's
timezone. Downloaded JSON may contain personal information; keep it private.

Official references:
- https://www.zoho.com/creator/help/api/v2.1/get-records.html
- https://www.zoho.com/developer/oauth/non-browser-apps/refresh-access-token.html
