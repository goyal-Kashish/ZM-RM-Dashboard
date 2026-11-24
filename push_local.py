r"""
Run this once a day from a machine that can reach your Redash instance
(e.g. your work PC, on the office network/VPN).

What it does:
  1. Main L1 performance query (14600)  -> pushed to the dashboard as-is.
  2. App-install query (52336)          -> the ~100k meeting rows are crunched
     HERE on your PC into per-L1 totals (same logic as the dashboard's manual
     upload) and only that small result is pushed. Skipped if
     REDASH_QUERY_ID_APP_INSTALL is not set.
  3. Location-sales query (13308)       -> legacy, only if
     REDASH_QUERY_ID_LOCATION_SALES is set. Not needed any more: sales come
     from the sales dump you upload on the dashboard page.

If a query's cached result in Redash is not from today, the script asks
Redash to re-run it first and waits for it (up to REDASH_REFRESH_TIMEOUT
seconds, default 900), so you always push today's numbers.

The dashboard server never needs Redash credentials -- only this script does.
This file must sit next to ingest.py and dashboard_logic.py (same folder as
the rest of the dashboard code).

Setup (one time):
    pip install requests

Run -- in PowerShell:
    cd $env:USERPROFILE\Downloads\redash-dashboard-v2\redash-dashboard-v2
    $env:REDASH_BASE_URL="https://redash.intermesh.net"
    $env:REDASH_API_KEY="your-key"
    $env:REDASH_QUERY_ID="14600"
    $env:REDASH_QUERY_ID_APP_INSTALL="52336"
    $env:DASHBOARD_URL="https://zm-rm-dashboard.onrender.com"
    $env:PUSH_TOKEN="whatever-secret-you-set-on-the-server"
    python push_local.py

(In the old cmd.exe window use `set NAME=value` instead of `$env:NAME="value"`.)
"""

import datetime
import os
import sys
import time

import requests

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))


def env_or_die(name):
    v = os.environ.get(name, "").strip()
    if not v:
        print(f"ERROR: environment variable {name} is not set.")
        sys.exit(1)
    return v


def _rows_of(query_result):
    data = (query_result or {}).get("data", {}) or {}
    return data.get("rows", []) or []


def _is_from_today(query_result):
    ts = (query_result or {}).get("retrieved_at")
    if not ts:
        return False
    try:
        when = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return False
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return when.astimezone(IST).date() == datetime.datetime.now(IST).date()


def _refresh_query(base_url, api_key, query_id):
    """Ask Redash to re-run the query and wait for the new result."""
    base = base_url.rstrip("/")
    timeout = int(os.environ.get("REDASH_REFRESH_TIMEOUT", "900"))
    print(f"  Cached result is not from today -- asking Redash to re-run query {query_id}...")
    resp = requests.post(f"{base}/api/queries/{query_id}/results",
                         params={"api_key": api_key}, json={"max_age": 0}, timeout=60)
    resp.raise_for_status()
    body = resp.json()
    if "query_result" in body:            # Redash answered straight away
        return body["query_result"]
    job = body.get("job", {})
    started = time.time()
    while True:
        status = job.get("status")
        if status == 3:                   # success
            qr_id = job.get("query_result_id")
            r = requests.get(f"{base}/api/query_results/{qr_id}.json",
                             params={"api_key": api_key}, timeout=300)
            r.raise_for_status()
            return r.json().get("query_result", {})
        if status in (4, 5):              # failed / cancelled
            raise RuntimeError(f"Redash query {query_id} failed: {job.get('error')}")
        if time.time() - started > timeout:
            raise TimeoutError(f"Redash query {query_id} still running after {timeout}s")
        time.sleep(5)
        r = requests.get(f"{base}/api/jobs/{job.get('id')}", params={"api_key": api_key}, timeout=60)
        r.raise_for_status()
        job = r.json().get("job", {})
        print(f"  ...still running ({int(time.time() - started)}s)")


def fetch_from_redash(base_url, api_key, query_id):
    url = f"{base_url.rstrip('/')}/api/queries/{query_id}/results.json"
    print(f"Fetching from Redash: {url}")
    resp = requests.get(url, params={"api_key": api_key}, timeout=300)
    resp.raise_for_status()
    qr = resp.json().get("query_result", {})
    if not _is_from_today(qr):
        try:
            qr = _refresh_query(base_url, api_key, query_id)
        except Exception as e:
            print(f"  WARNING: couldn't refresh ({e}). Using the cached result from {qr.get('retrieved_at')}.")
    print(f"  Result from {qr.get('retrieved_at')} -- {len(_rows_of(qr))} rows")
    return _rows_of(qr)


def push_to_dashboard(dashboard_url, push_token, endpoint, body):
    url = f"{dashboard_url.rstrip('/')}{endpoint}"
    print(f"Pushing to: {url}")
    resp = requests.post(url, json=body, headers={"X-Push-Token": push_token}, timeout=180)
    resp.raise_for_status()
    return resp.json()


APP_REQUIRED = ["glid", "meeting_id", "meeting_date", "week_start", "in_last_3_weeks",
                "in_mtd", "vertical_id", "employee_id", "employee_location", "first_install_date"]


def push_app_install(base_url, api_key, query_id, dashboard_url, push_token):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import ingest   # same parsing logic the dashboard uses for the manual upload

    rows = fetch_from_redash(base_url, api_key, query_id)
    if not rows:
        print("WARNING: app-install query returned 0 rows. Not pushing.")
        return False
    missing = [c for c in APP_REQUIRED if c not in rows[0]]
    if missing:
        print(f"ERROR: app-install query is missing columns {missing}. It must be the "
              f"meeting-level output (option A in the query).")
        return False
    parsed = ingest.parse_appinstall_rows(rows)
    print(f"  Crunched {parsed['stats'].get('meetings_read', 0)} meetings into "
          f"{len(parsed['by_owner'])} employees; periods {parsed['period_labels']}")
    result = push_to_dashboard(dashboard_url, push_token, "/api/push-appinstall", {"parsed": parsed})
    if result.get("ok"):
        print(f"Success -- app-install data updated ({result['employees']} employees).")
        return True
    print(f"Push failed: {result.get('error')}")
    return False


def main():
    base_url = env_or_die("REDASH_BASE_URL")
    api_key = env_or_die("REDASH_API_KEY")
    query_id = env_or_die("REDASH_QUERY_ID")
    app_query_id = os.environ.get("REDASH_QUERY_ID_APP_INSTALL", "").strip()
    location_sales_query_id = os.environ.get("REDASH_QUERY_ID_LOCATION_SALES", "").strip()
    dashboard_url = env_or_die("DASHBOARD_URL")
    push_token = env_or_die("PUSH_TOKEN")
    failed = False

    # --- 1. Main L1 performance query ---
    print("=== Main performance query ===")
    rows = fetch_from_redash(base_url, api_key, query_id)
    if not rows:
        print("WARNING: Redash returned 0 rows for the main query. Not pushing -- check your query.")
        sys.exit(1)
    result = push_to_dashboard(dashboard_url, push_token, "/api/push-data", {"rows": rows})
    if result.get("ok"):
        dropped = result.get("duplicates_dropped") or 0
        extra = f" ({dropped} duplicate rows from the query were dropped)" if dropped else ""
        print(f"Success -- dashboard now has {result['row_count']} L1 rows as of this push{extra}.")
    else:
        print(f"Push failed: {result.get('error')}")
        sys.exit(1)

    # --- 2. App-install query ---
    if app_query_id:
        print()
        print("=== App-install query ===")
        try:
            failed |= not push_app_install(base_url, api_key, app_query_id, dashboard_url, push_token)
        except Exception as e:
            print(f"App-install push failed: {e}")
            failed = True

    # --- 3. Location-level sales query (optional, legacy) ---
    if location_sales_query_id:
        print()
        print("=== Location sales query (legacy) ===")
        location_rows = fetch_from_redash(base_url, api_key, location_sales_query_id)
        if location_rows:
            result2 = push_to_dashboard(dashboard_url, push_token, "/api/push-location-sales",
                                        {"rows": location_rows})
            print("Success." if result2.get("ok") else f"Push failed: {result2.get('error')}")

    print()
    print("Done. Remaining manual step: upload today's sales dump on the dashboard page.")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
