"""
Core business logic for the ZM/RM/Location drill-down dashboard.
Kept separate from Flask so it can be tested independently of any web server.
"""

from collections import defaultdict

# ---------------------------------------------------------------------------
# Which Redash columns feed each metric, per time period.
# `None` means that metric genuinely doesn't exist for that period in the
# source query (e.g. the hot-meeting self/manager split isn't tracked
# per-month, only Overall/WTD/MTD).
# ---------------------------------------------------------------------------
PERIODS = ["overall", "wtd", "mtd", "m1", "m2", "m3", "m4"]

PERIOD_LABELS = {
    "overall": "Overall", "wtd": "WTD", "mtd": "MTD",
    "m1": "M1", "m2": "M2", "m3": "M3", "m4": "M4",
}

METRIC_FIELDS = {
    "overall": {
        "sales_count": "sales_done", "sales_value": "annual_sale_done",
        "hot_total": "l1_hot_glids", "hot_self": "l1_hot_self_meet", "hot_mgr": "l1_hot_with_mgr_meet",
        "total_meet": "total_meet",
    },
    "wtd": {
        "sales_count": "week_sales_done", "sales_value": "annual_week_sale_done",
        "hot_total": "l1_hot_glids_wtd", "hot_self": "l1_hot_self_meet_wtd", "hot_mgr": "l1_hot_with_mgr_meet_wtd",
        "total_meet": "total_meet_wtd",
    },
    "mtd": {
        "sales_count": "month_sales_done", "sales_value": "annual_month_sale_done",
        "hot_total": "l1_hot_glids_mtd", "hot_self": "l1_hot_self_meet_mtd", "hot_mgr": "l1_hot_with_mgr_meet_mtd",
        "total_meet": "total_meet_mtd",
    },
    "m1": {
        "sales_count": "sales_done_m1", "sales_value": "annual_sale_done_m1",
        "hot_total": "l1_hot_glids_m1", "hot_self": "l1_hot_self_meet_m1", "hot_mgr": "l1_hot_with_mgr_meet_m1",
        "total_meet": "total_meet_m1",
    },
    "m2": {
        "sales_count": "sales_done_m2", "sales_value": "annual_sale_done_m2",
        "hot_total": None, "hot_self": None, "hot_mgr": None,
        "total_meet": "total_meet_m2",
    },
    "m3": {
        "sales_count": "sales_done_m3", "sales_value": "annual_sale_done_m3",
        "hot_total": None, "hot_self": None, "hot_mgr": None,
        "total_meet": "total_meet_m3",
    },
    "m4": {
        "sales_count": "sales_done_m4", "sales_value": "annual_sale_done_m4",
        "hot_total": None, "hot_self": None, "hot_mgr": None,
        "total_meet": "total_meet_m4",
    },
}

UNDERPERFORMER_METRIC = "sales_done"   # overall sales count; ranks within each location group
UNDERPERFORMER_PCT = 0.20              # bottom 20% flagged, minimum 1 per group if group is non-empty

LOCATION_FIELD = "iil_comp_loc_name"
EMPLOYEE_ID_FIELD = "fk_employeeid"
EMPLOYEE_NAME_FIELD = "employeename"

UNMAPPED_LABEL = "(Unmapped — not found in hierarchy sheet)"


def normalize_id(value):
    """Make ID matching robust to formatting differences between the two
    sources — e.g. Redash sometimes serializes integer columns as '125270.0'
    or with thousands-separator commas like '1,25,270', while the HR sheet
    has a clean '125270'. Strips whitespace, commas, and any trailing '.0'
    so all sides compare equal."""
    if value is None:
        return ""
    s = str(value).strip()
    s = s.replace(",", "")
    if s.endswith(".0"):
        try:
            s = str(int(float(s)))
        except (ValueError, OverflowError):
            pass
    return s


def _num(row, field):
    """Safely pull a numeric value out of a Redash row; treats missing/None as 0."""
    if field is None:
        return None
    v = row.get(field)
    if v is None:
        return 0
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0


def build_node_metrics(rows):
    """Given a list of raw Redash rows belonging to one tree node, sum every
    metric for every period."""
    out = {}
    for period in PERIODS:
        fields = METRIC_FIELDS[period]
        agg = {"sales_count": 0, "sales_value": 0.0, "total_meet": 0}
        has_hot = fields["hot_total"] is not None
        if has_hot:
            agg["hot_total"] = 0
            agg["hot_self"] = 0
            agg["hot_mgr"] = 0
        for r in rows:
            agg["sales_count"] += _num(r, fields["sales_count"]) or 0
            agg["sales_value"] += _num(r, fields["sales_value"]) or 0
            agg["total_meet"] += _num(r, fields["total_meet"]) or 0
            if has_hot:
                agg["hot_total"] += _num(r, fields["hot_total"]) or 0
                agg["hot_self"] += _num(r, fields["hot_self"]) or 0
                agg["hot_mgr"] += _num(r, fields["hot_mgr"]) or 0
        out[period] = agg
    return out


def flag_underperformers(rows):
    """Within one location group, flag the bottom ~20% by overall sales_done.
    Returns a set of fk_employeeid values that are flagged."""
    if not rows:
        return set()
    scored = [(r.get(EMPLOYEE_ID_FIELD), _num(r, UNDERPERFORMER_METRIC) or 0) for r in rows]
    scored.sort(key=lambda x: x[1])
    n_flag = max(1, round(len(scored) * UNDERPERFORMER_PCT))
    return set(eid for eid, _ in scored[:n_flag])


def tree_payload(result):
    """The lightweight response sent to the browser on every load — tree,
    rollups, flags, but not the full per-employee raw rows."""
    return {
        "tree": result["tree"],
        "flags": result["flags"],
        "unmapped_count": result["unmapped_count"],
        "periods": result["periods"],
        "period_labels": result["period_labels"],
    }


def merge_and_build_tree(redash_rows, hierarchy_map):
    """
    redash_rows: list of dicts (one per employee, full raw Redash row)
    hierarchy_map: dict of employee_id (str or int) -> {"am":..,"rm":..,"zm":..}

    Returns a dict:
      {
        "tree": { zm: { "metrics": {...}, "rm_children": { rm: { "metrics":{...},
                   "location_children": { loc: { "metrics":{...}, "employees":[ids] } } } } } },
        "employees": { employee_id: full_raw_row },
        "flags": { employee_id: True }   # underperformer flags
        "unmapped_count": N
      }
    """
    # Normalize hierarchy map keys to strings for safe lookup regardless of
    # whether IDs came through as int or str from either source.
    hmap = {normalize_id(k): v for k, v in hierarchy_map.items()}

    zm_groups = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    employees = {}
    unmapped_count = 0

    for row in redash_rows:
        eid = row.get(EMPLOYEE_ID_FIELD)
        eid_key = normalize_id(eid)
        employees[eid_key] = row

        h = hmap.get(eid_key)
        if h:
            zm, rm = h.get("zm") or "(blank)", h.get("rm") or "(blank)"
        else:
            zm, rm = UNMAPPED_LABEL, UNMAPPED_LABEL
            unmapped_count += 1

        location = row.get(LOCATION_FIELD) or "(blank)"
        zm_groups[zm][rm][location].append(row)

    flags = {}
    tree = {}
    for zm, rm_groups in zm_groups.items():
        zm_all_rows = []
        rm_children = {}
        for rm, loc_groups in rm_groups.items():
            rm_all_rows = []
            location_children = {}
            for location, rows in loc_groups.items():
                flagged_ids = flag_underperformers(rows)
                for eid in flagged_ids:
                    flags[normalize_id(eid)] = True
                location_children[location] = {
                    "metrics": build_node_metrics(rows),
                    "headcount": len(rows),
                    "employees": [
                        {
                            "id": normalize_id(r.get(EMPLOYEE_ID_FIELD)),
                            "name": r.get(EMPLOYEE_NAME_FIELD),
                            "flagged": normalize_id(r.get(EMPLOYEE_ID_FIELD)) in {normalize_id(x) for x in flagged_ids},
                        }
                        for r in rows
                    ],
                }
                rm_all_rows.extend(rows)
            rm_children[rm] = {
                "metrics": build_node_metrics(rm_all_rows),
                "location_children": location_children,
                "headcount": len(rm_all_rows),
            }
            zm_all_rows.extend(rm_all_rows)
        tree[zm] = {
            "metrics": build_node_metrics(zm_all_rows),
            "rm_children": rm_children,
            "headcount": len(zm_all_rows),
        }

    return {
        "tree": tree,
        "employees": employees,   # full raw rows — kept server-side only; not sent as-is to the browser
        "flags": flags,
        "unmapped_count": unmapped_count,
        "periods": PERIODS,
        "period_labels": PERIOD_LABELS,
    }
