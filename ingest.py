"""
Ingestion of the two manual dumps that replaced Redash-side calculations:

  * SALES dump   (receipt-level export)  -> per-claimer WTD / MTD / M1 sales
  * APP-INSTALL dump (meeting-level export) -> per-owner unique-GLID install buckets

Both are parsed once at upload time into small dicts (never keeping the raw
rows around), persisted by app.py, and then layered onto the Redash-driven
ZM/RM/Location tree at compute time by the functions below.

Pure Python + openpyxl only (no pandas) so it runs on the Render free tier and
on locked-down Windows machines alike.
"""

import io
import datetime
from collections import Counter, defaultdict

from dashboard_logic import METRIC_FIELDS, normalize_id, EMPLOYEE_ID_FIELD

# ---------------------------------------------------------------------------
# Sales dump rules (as specified by the business)
# ---------------------------------------------------------------------------
RECEIPT_STATUS_OK = {"cleared", "pending"}
SALE_STATUS_OK = {"hosted", "unused"}

# The dump names verticals in words; the dashboard works on vertical ids.
# Anything not listed (Tele, Inbound, ...) is outside this dashboard's scope.
SALES_VERTICAL_IDS = {
    "nsd top cities - inhouse fsf": "1",
    "nsd emerging markets - channel fsf": "42",
    "nsd top cities - channel fsf": "56",
    "nsd top cities fsf - high turnover": "65",
}

# Turnover slab text in the dump -> the dashboard's three buckets.
# Blank / "NAM" / anything unrecognised -> no_gst (same rule as everywhere else).
TURNOVER_UNDER = {"0 - 40 l", "40 l - 1.5 cr"}
TURNOVER_OVER = {"1.5 - 5 cr", "5 - 25 cr", "25 - 100 cr", "100 - 500 cr", "> 500 cr"}

SALES_PERIODS = ["wtd", "mtd", "m1"]
# Order of the counters stored per (claimer, period); mapped to the logical
# metric names used everywhere in dashboard_logic.
SALES_KEYS = ["sales_count", "sales_value", "sales_count_under", "sales_value_under",
              "sales_count_over", "sales_value_over"]

APP_PERIODS = ["aw1", "aw2", "aw3", "al3w", "amtd"]
APP_KEYS = ["app_met", "app_before", "app_after", "app_none"]


def ist_today():
    """Today's date in IST regardless of the server's timezone (Render = UTC)."""
    return (datetime.datetime.utcnow() + datetime.timedelta(hours=5, minutes=30)).date()


def _to_date(v):
    if v is None:
        return None
    if isinstance(v, datetime.datetime):
        return v.date()
    if isinstance(v, datetime.date):
        return v
    s = str(v).strip()
    if not s or s.lower() in ("nan", "nat", "none"):
        return None
    try:
        return datetime.date.fromisoformat(s[:10])
    except ValueError:
        return None


def _norm_text(v):
    return " ".join(str(v).strip().lower().split()) if v is not None else ""


def _iter_dicts(file_bytes, required):
    """Stream an .xlsx as dicts keyed by header name. Tolerates rows that are
    shorter than the header (openpyxl read-only mode can truncate them)."""
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    ws = wb.active
    it = ws.iter_rows(values_only=True)
    header = next(it, None)
    if header is None:
        raise ValueError("The uploaded sheet is empty.")
    header = [str(h).strip() if h is not None else "" for h in header]
    missing = [c for c in required if c not in header]
    if missing:
        raise ValueError(f"Uploaded sheet is missing expected column(s): {missing}")
    idx = {name: header.index(name) for name in required}
    n = len(header)

    def gen():
        for row in it:
            if row is None:
                continue
            yield {k: (row[i] if i < len(row) else None) for k, i in idx.items()}
    return gen()


# ---------------------------------------------------------------------------
# SALES
# ---------------------------------------------------------------------------
def _sales_periods_for(d, as_of):
    """Which of wtd / mtd / m1 a sale date falls in. Same convention as the
    Redash query: today itself is never included (date < as_of)."""
    if d is None or d >= as_of:
        return []
    out = []
    monday = as_of - datetime.timedelta(days=as_of.weekday())
    month_start = as_of.replace(day=1)
    prev_month_start = (month_start - datetime.timedelta(days=1)).replace(day=1)
    if monday <= d:
        out.append("wtd")
    if month_start <= d:
        out.append("mtd")
    if prev_month_start <= d < month_start:
        out.append("m1")
    return out


SALES_COLUMNS = ["Receipt", "Entered On", "Receipt Status", "Sale Status", "Claimed By ID",
                 "Vertical", "mode", "turnover", "Claimed Location", "Regional Manager"]


def parse_sales_dump(file_bytes, as_of=None):
    return parse_sales_rows(_iter_dicts(file_bytes, SALES_COLUMNS), as_of)


def parse_sales_rows(row_iter, as_of=None):
    as_of = as_of or ist_today()
    stats = Counter()
    out_of_scope = Counter()
    unknown_turnover = Counter()
    seen_receipts = set()
    by_claimer = {}

    for r in row_iter:
        stats["rows_read"] += 1
        if _norm_text(r["Receipt Status"]) not in RECEIPT_STATUS_OK or \
           _norm_text(r["Sale Status"]) not in SALE_STATUS_OK:
            stats["dropped_by_status"] += 1
            continue
        receipt = normalize_id(r["Receipt"])
        if receipt and receipt in seen_receipts:
            stats["dropped_duplicate_receipt"] += 1   # multi-service receipt = one sale
            continue
        if receipt:
            seen_receipts.add(receipt)
        vname = _norm_text(r["Vertical"])
        vid = SALES_VERTICAL_IDS.get(vname)
        if not vid:
            stats["out_of_scope_vertical"] += 1
            out_of_scope[str(r["Vertical"]).strip()] += 1
            continue
        cid = normalize_id(r["Claimed By ID"])
        if not cid:
            stats["no_claimer"] += 1
            continue
        periods = _sales_periods_for(_to_date(r["Entered On"]), as_of)
        if not periods:
            stats["outside_periods"] += 1
            continue

        t = _norm_text(r["turnover"])
        if t in TURNOVER_UNDER:
            bucket = "under"
        elif t in TURNOVER_OVER:
            bucket = "over"
        else:
            bucket = "no_gst"
            if t and t not in ("nam", "na", "-", "nan"):
                unknown_turnover[str(r["turnover"]).strip()] += 1
        annual = 1 if _norm_text(r["mode"]) == "normal" else 0

        rec = by_claimer.get(cid)
        if rec is None:
            rec = by_claimer[cid] = {
                "vertical_id": vid,
                "location": r["Claimed Location"],
                "rm": r["Regional Manager"],
                "p": {},
            }
        for p in periods:
            acc = rec["p"].setdefault(p, [0] * len(SALES_KEYS))
            acc[0] += 1
            acc[1] += annual
            if bucket == "under":
                acc[2] += 1
                acc[3] += annual
            elif bucket == "over":
                acc[4] += 1
                acc[5] += annual
        stats["sales_kept"] += 1

    return {
        "as_of": as_of.isoformat(),
        "by_claimer": by_claimer,
        "stats": dict(stats),
        "out_of_scope_verticals": dict(out_of_scope),
        "unknown_turnover_values": dict(unknown_turnover),
    }


# ---------------------------------------------------------------------------
# APP INSTALL
# ---------------------------------------------------------------------------
APPINSTALL_COLUMNS = ["glid", "meeting_id", "meeting_date", "week_start", "in_last_3_weeks",
                      "in_mtd", "vertical_id", "employee_id", "employee_location", "first_install_date"]


def parse_appinstall_dump(file_bytes):
    return parse_appinstall_rows(_iter_dicts(file_bytes, APPINSTALL_COLUMNS))


def parse_appinstall_rows(row_iter):
    """Meeting-level dump -> per (period, GLID, vertical): the FIRST meeting in
    that period decides who gets credit and whether the app was already there.

      before : first install date  <  first meeting date   (already had the app)
      after  : first install date >= first meeting date    (installed after we met)
      none   : never installed

    Each GLID is counted once per period, so totals roll up by plain addition.
    """
    stats = Counter()
    first = {}   # (period_key, glid, vertical) -> (meeting_date, meeting_id, employee_id, location, install_date)
    weeks = set()

    def _mid(v):
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return 0

    for r in row_iter:
        stats["meetings_read"] += 1
        md = _to_date(r["meeting_date"])
        glid = normalize_id(r["glid"])
        eid = normalize_id(r["employee_id"])
        if md is None or not glid or not eid:
            stats["dropped_bad_row"] += 1
            continue
        vid = normalize_id(r["vertical_id"])
        keys = []
        if _norm_text(r["in_last_3_weeks"]) == "yes":
            ws = _to_date(r["week_start"])
            if ws is not None:
                weeks.add(ws)
                keys.append("wk:" + ws.isoformat())
            keys.append("al3w")
        if _norm_text(r["in_mtd"]) == "yes":
            keys.append("amtd")
        if not keys:
            stats["outside_periods"] += 1
            continue
        cand = (md, _mid(r["meeting_id"]), eid, r["employee_location"], _to_date(r["first_install_date"]))
        for k in keys:
            key = (k, glid, vid)
            cur = first.get(key)
            if cur is None or cand[:2] < cur[:2]:
                first[key] = cand

    # Most recent complete week = aw1, then aw2, aw3.
    week_order = sorted(weeks, reverse=True)[:3]
    week_key_to_period = {"wk:" + w.isoformat(): f"aw{i + 1}" for i, w in enumerate(week_order)}
    labels = {"al3w": "Last 3 weeks", "amtd": "MTD"}
    for i, w in enumerate(week_order):
        end = w + datetime.timedelta(days=6)
        if w.month == end.month:
            labels[f"aw{i + 1}"] = f"{w.day}–{end.day} {end.strftime('%b')}"
        else:
            labels[f"aw{i + 1}"] = f"{w.day} {w.strftime('%b')}–{end.day} {end.strftime('%b')}"

    by_owner = {}
    for (k, glid, vid), (md, _m, eid, loc, inst) in first.items():
        p = week_key_to_period.get(k, k if k in ("al3w", "amtd") else None)
        if p is None:
            continue
        rec = by_owner.get(eid)
        if rec is None:
            rec = by_owner[eid] = {"vertical_id": vid, "location": loc, "p": {}}
        acc = rec["p"].setdefault(p, [0, 0, 0, 0])
        acc[0] += 1
        if inst is None:
            acc[3] += 1
        elif inst < md:
            acc[1] += 1
        else:
            acc[2] += 1
        stats["glid_periods"] += 1

    return {
        "by_owner": by_owner,
        "period_labels": {p: labels[p] for p in APP_PERIODS if p in labels},
        "stats": dict(stats),
    }


# ---------------------------------------------------------------------------
# Layering onto the Redash-driven data
# ---------------------------------------------------------------------------
def overlay_sales_on_rows(rows, sales):
    """Return (new_rows, extras). Each L1 row gets its sales written into the
    same columns the old Redash query used (so every existing metric / filter
    / rollup keeps working untouched). Claimers who are NOT an L1 row (TLs,
    pool IDs, managers) can't be shown as an employee, so they come back as
    `extras` to be added at location / RM / ZM level instead."""
    by = (sales or {}).get("by_claimer", {}) if sales else {}
    l1_ids = set()
    new_rows = []
    for r in rows:
        cid = normalize_id(r.get(EMPLOYEE_ID_FIELD))
        l1_ids.add(cid)
        rec = by.get(cid)
        nr = dict(r)
        for period in SALES_PERIODS:
            fields = METRIC_FIELDS[period]
            acc = (rec["p"].get(period) if rec else None) or [0] * len(SALES_KEYS)
            for key, val in zip(SALES_KEYS, acc):
                col = fields.get(key)
                if col:
                    nr[col] = val
        new_rows.append(nr)

    extras = []
    for cid, rec in by.items():
        if cid in l1_ids:
            continue
        metrics = {p: dict(zip(SALES_KEYS, acc)) for p, acc in rec["p"].items()}
        extras.append({"vertical_id": rec["vertical_id"], "location": rec["location"],
                       "rm": rec.get("rm"), "metrics": metrics})
    return new_rows, extras


def app_by_employee(rows, app):
    """Split app-install owners into (L1 employee metrics, extras)."""
    by = (app or {}).get("by_owner", {}) if app else {}
    l1_ids = {normalize_id(r.get(EMPLOYEE_ID_FIELD)) for r in rows}
    emp = {}
    extras = []
    for eid, rec in by.items():
        metrics = {p: dict(zip(APP_KEYS, acc)) for p, acc in rec["p"].items()}
        if eid in l1_ids:
            emp[eid] = metrics
        else:
            extras.append({"vertical_id": rec["vertical_id"], "location": rec["location"],
                           "rm": None, "metrics": metrics})
    return emp, extras


def _zero_app():
    return {k: 0 for k in APP_KEYS}


def apply_app_install(tree, emp_metrics):
    """Attach app-install metrics (under the keys aw1..amtd) to every employee
    summary and roll them up Location -> RM -> ZM by plain addition."""
    for zm_node in tree.values():
        zm_tot = {p: _zero_app() for p in APP_PERIODS}
        for rm_node in zm_node["rm_children"].values():
            rm_tot = {p: _zero_app() for p in APP_PERIODS}
            for loc_node in rm_node["location_children"].values():
                loc_tot = {p: _zero_app() for p in APP_PERIODS}
                for emp in loc_node["employees"]:
                    em = emp_metrics.get(emp["id"], {})
                    for p in APP_PERIODS:
                        vals = em.get(p) or _zero_app()
                        emp["metrics"][p] = dict(vals)
                        for k in APP_KEYS:
                            loc_tot[p][k] += vals[k]
                for p in APP_PERIODS:
                    loc_node["metrics"][p] = loc_tot[p]
                    for k in APP_KEYS:
                        rm_tot[p][k] += loc_tot[p][k]
            for p in APP_PERIODS:
                rm_node["metrics"][p] = rm_tot[p]
                for k in APP_KEYS:
                    zm_tot[p][k] += rm_tot[p][k]
        for p in APP_PERIODS:
            zm_node["metrics"][p] = zm_tot[p]


OUTSIDE_LABEL = "(Partner / outside hierarchy)"


def apply_extras(tree, extras, vertical):
    """Add non-L1 sales / app numbers at Location -> RM -> ZM level. Returns
    {"placed": n, "unplaced": [location names]} so nothing vanishes silently.
    The location is matched by name; if the same name sits under several RMs,
    the RM named in the dump wins, else the branch with the most people."""
    occ = defaultdict(list)
    for zm_node in tree.values():
        for rm_name, rm_node in zm_node["rm_children"].items():
            for loc_name, loc_node in rm_node["location_children"].items():
                occ[loc_name].append((zm_node, rm_name, rm_node, loc_node))

    placed = 0
    unplaced = Counter()
    unplaced_units = Counter()   # how many sales / GLIDs that represents, per period
    for ex in extras:
        if vertical not in (None, "", "all") and normalize_id(ex["vertical_id"]) != normalize_id(vertical):
            continue
        cands = occ.get(ex["location"])
        if not cands or cands[0][1] == OUTSIDE_LABEL:
            # Location not in the hierarchy (partner / channel offices). Park
            # it under a visible "outside hierarchy" ZM so the company totals
            # still equal the dump exactly instead of silently dropping it.
            loc = str(ex["location"] or "(blank)")
            unplaced[loc] += 1
            for period, vals in ex["metrics"].items():
                unplaced_units[period] += vals.get("sales_count", vals.get("app_met", 0))
            zm_node = tree.setdefault(OUTSIDE_LABEL, {"metrics": {}, "rm_children": {}, "headcount": 0})
            rm_node = zm_node["rm_children"].setdefault(
                OUTSIDE_LABEL, {"metrics": {}, "location_children": {}, "headcount": 0})
            loc_node = rm_node["location_children"].setdefault(
                loc, {"metrics": {}, "headcount": 0, "employees": []})
            occ[loc] = [(zm_node, OUTSIDE_LABEL, rm_node, loc_node)]
            for period, vals in ex["metrics"].items():
                for node in (loc_node, rm_node, zm_node):
                    m = node["metrics"].setdefault(period, {})
                    for k, v in vals.items():
                        m[k] = (m.get(k) or 0) + v
            continue
        if len(cands) > 1:
            hinted = [c for c in cands if ex.get("rm") and c[1] == ex["rm"]]
            cands = hinted or sorted(cands, key=lambda c: -c[3]["headcount"])
        zm_node, _rm, rm_node, loc_node = cands[0]
        for period, vals in ex["metrics"].items():
            for node in (loc_node, rm_node, zm_node):
                m = node["metrics"].setdefault(period, {})
                for k, v in vals.items():
                    m[k] = (m.get(k) or 0) + v
        placed += 1
    return {"placed": placed, "unplaced": dict(unplaced), "unplaced_units": dict(unplaced_units)}


def available_periods(rows, candidates):
    """A period is offered only if the pushed query actually contains its
    columns -- so dropping M1 from the query drops the M1 tab, not zeros."""
    if not rows:
        return []
    sample = rows[0]
    return [p for p in candidates
            if (METRIC_FIELDS[p].get("total_meet") or "") in sample]
