# ZM / RM / Location Performance Dashboard

A drill-down report: ZM → RM → Location → L1, with Sales and Hot-Meeting
numbers rolled up at every level, switchable across Overall/WTD/MTD/M1-M4.

## New architecture: push, not pull

Earlier versions had this server call Redash directly — which meant it had
to run somewhere with access to Redash's internal-only network (your PC, via
a tunnel). That's fragile: the dashboard goes down whenever your PC does.

This version flips it:

```
Your PC (has Redash access)              Public server (no Redash access needed)
┌─────────────────────┐                  ┌──────────────────────────┐
│  push_local.py       │  ── pushes ──▶  │  app.py                   │
│  (run once a day)     │   data over     │  - stores whatever it's   │
│  fetches from Redash  │   HTTPS         │    given                  │
└─────────────────────┘                  │  - serves the dashboard   │
                                          │    to everyone, always on │
                                          └──────────────────────────┘
```

- The **server** (`app.py`) now holds **no Redash credentials at all**. It
  just waits to receive data via `/api/push-data`, protected by a shared
  secret (`PUSH_TOKEN`).
- **You** run `push_local.py` once a day from your PC (or any machine with
  Redash access) to fetch fresh data and push it out.
- The **dashboard itself is permanently reachable** by everyone — it doesn't
  go down when your PC does, since it isn't running there anymore.
- The **hierarchy sheet** still uploads directly to the server each Monday,
  same as before — that part didn't change, since a plain Excel file isn't
  network-restricted the way Redash is.

## 1. Deploy the server (Render, free tier)

1. Push this folder to a GitHub repo.
2. On [render.com](https://render.com): New + → Web Service → connect the repo.
3. Build command: `pip install -r requirements.txt`
   Start command: `gunicorn app:app`
4. Environment variables — note there's **no Redash config here anymore**:
   | Key | Value |
   |---|---|
   | `PUSH_TOKEN` | any long random string you make up — this is the password that lets `push_local.py` push data in |
   | `DASHBOARD_TITLE` | optional, whatever title you want shown |
5. Deploy. You'll get a permanent URL like `https://your-app.onrender.com`.
   **This is the link you share with your team.**

## 2. Run the daily push from your PC

```cmd
pip install requests
set REDASH_BASE_URL=https://redash.intermesh.net
set REDASH_API_KEY=your-redash-api-key
set REDASH_QUERY_ID=12849
set DASHBOARD_URL=https://your-app.onrender.com
set PUSH_TOKEN=the-same-secret-you-set-on-render
python push_local.py
```

You should see:
```
Fetching from Redash: https://redash.intermesh.net/api/queries/12849/results.json
Pushing 2916 rows to: https://your-app.onrender.com/api/push-data
Success — dashboard now has 2916 rows as of this push.
```

Run this once a day (or whenever you want fresh numbers). Nothing else on
your PC needs to stay open — once the push completes, you can close
everything; the dashboard keeps serving that data to everyone until your
next push.

## 3. Upload the hierarchy sheet

Same as before — open the dashboard URL, click **"Upload weekly sheet"**,
choose your `Required_HC_...xlsx`. Do this once a week, whenever the sheet
updates. This goes straight to the public server, not through your PC.

## What stays the same day-to-day

- **You**: run `push_local.py` daily, upload the hierarchy sheet weekly.
- **Everyone else**: just opens the permanent URL — no PC dependency, no
  ngrok warning page, no "is Kashish's laptop on" uncertainty.

## Security notes

- `PUSH_TOKEN` is effectively a password for writing data into your public
  dashboard — treat it like one (don't post it publicly, rotate it if it
  leaks).
- Your Redash API key now lives **only** on whatever machine runs
  `push_local.py` — it's never sent to or stored on the public server,
  which is a meaningful security improvement over the earlier design.
