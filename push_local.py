"""
Run this once a day from a machine that can reach your Redash instance
(e.g. your work PC, on the office network/VPN).

It fetches the latest cached result from your Redash query, then pushes it
to your publicly-hosted dashboard. The dashboard server itself never needs
Redash credentials or network access — only this script does.

Setup (one time):
    pip install requests

Run (every day, or whenever you want fresh numbers):
    set REDASH_BASE_URL=https://redash.intermesh.net
    set REDASH_API_KEY=your-key
    set REDASH_QUERY_ID=12849
    set DASHBOARD_URL=https://your-app.onrender.com
    set PUSH_TOKEN=whatever-secret-you-set-on-the-server
    python push_local.py
"""

import os
import sys
import requests


def env_or_die(name):
    v = os.environ.get(name, "").strip()
    if not v:
        print(f"ERROR: environment variable {name} is not set.")
        sys.exit(1)
    return v


def fetch_from_redash(base_url, api_key, query_id):
    url = f"{base_url.rstrip('/')}/api/queries/{query_id}/results.json"
    print(f"Fetching from Redash: {url}")
    resp = requests.get(url, params={"api_key": api_key}, timeout=60)
    resp.raise_for_status()
    payload = resp.json()
    result = payload.get("query_result", {})
    data = result.get("data", {}) or {}
    rows = data.get("rows", []) or []
    return rows


def push_to_dashboard(dashboard_url, push_token, rows):
    url = f"{dashboard_url.rstrip('/')}/api/push-data"
    print(f"Pushing {len(rows)} rows to: {url}")
    resp = requests.post(
        url,
        json={"rows": rows},
        headers={"X-Push-Token": push_token},
        timeout=60,
    )
    resp.raise_for_status()
    return resp.json()


def main():
    base_url = env_or_die("REDASH_BASE_URL")
    api_key = env_or_die("REDASH_API_KEY")
    query_id = env_or_die("REDASH_QUERY_ID")
    dashboard_url = env_or_die("DASHBOARD_URL")
    push_token = env_or_die("PUSH_TOKEN")

    rows = fetch_from_redash(base_url, api_key, query_id)
    if not rows:
        print("WARNING: Redash returned 0 rows. Not pushing — check your query.")
        sys.exit(1)

    result = push_to_dashboard(dashboard_url, push_token, rows)
    if result.get("ok"):
        print(f"Success — dashboard now has {result['row_count']} rows as of this push.")
    else:
        print(f"Push failed: {result.get('error')}")
        sys.exit(1)


if __name__ == "__main__":
    main()
