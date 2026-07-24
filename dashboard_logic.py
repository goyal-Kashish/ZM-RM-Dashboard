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
        "total_meet": "total_meet", "fresh_meet": "fresh_meet",
        "hp_converted": None, "hp_converted_met": None, "hp_tp_sum_met": None, "hp_tp_over_200": None, "working_days": None,
        "combined_total": None, "combined_l2_met": None, "combined_converted": None, "combined_converted_met": None,
    },
    "wtd": {
        "sales_count": "week_sales_done", "sales_value": "annual_week_sale_done",
        "hot_total": "l1_hot_glids_wtd", "hot_self": "l1_hot_self_meet_wtd", "hot_mgr": "l1_hot_with_mgr_meet_wtd",
        "total_meet": "total_meet_wtd", "fresh_meet": "fresh_meet_wtd",
        "hp_converted": "l1_hot_converted_wtd", "hp_converted_met": "l1_hot_converted_met_wtd",
        "hp_tp_sum_met": "l1_hot_tp_sum_met_wtd",
        "hp_tp_over_200": "l1_hot_tp_over_200_count_wtd", "working_days": "working_days_wtd",
        "combined_total": "combined_total_wtd", "combined_l2_met": "combined_l2_met_wtd",
        "combined_converted": "combined_converted_wtd", "combined_converted_met": "combined_converted_met_wtd",
    },
    "mtd": {
        "sales_count": "month_sales_done", "sales_value": "annual_month_sale_done",
        "hot_total": "l1_hot_glids_mtd", "hot_self": "l1_hot_self_meet_mtd", "hot_mgr": "l1_hot_with_mgr_meet_mtd",
        "total_meet": "total_meet_mtd", "fresh_meet": "fresh_meet_mtd",
        "hp_converted": "l1_hot_converted_mtd", "hp_converted_met": "l1_hot_converted_met_mtd",
        "hp_tp_sum_met": "l1_hot_tp_sum_met_mtd",
        "hp_tp_over_200": "l1_hot_tp_over_200_count_mtd", "working_days": "working_days_mtd",
        "combined_total": "combined_total_mtd", "combined_l2_met": "combined_l2_met_mtd",
        "combined_converted": "combined_converted_mtd", "combined_converted_met": "combined_converted_met_mtd",
    },
    "m1": {
        "sales_count": "sales_done_m1", "sales_value": "annual_sale_done_m1",
        "hot_total": "l1_hot_glids_m1", "hot_self": "l1_hot_self_meet_m1", "hot_mgr": "l1_hot_with_mgr_meet_m1",
        "total_meet": "total_meet_m1", "fresh_meet": "fresh_meet_m1",
        "hp_converted": "l1_hot_converted_m1", "hp_converted_met": "l1_hot_converted_met_m1",
        "hp_tp_sum_met": "l1_hot_tp_sum_met_m1",
        "hp_tp_over_200": "l1_hot_tp_over_200_count_m1", "working_days": "working_days_m1",
        "combined_total": "combined_total_m1", "combined_l2_met": "combined_l2_met_m1",
        "combined_converted": "combined_converted_m1", "combined_converted_met": "combined_converted_met_m1",
    },
    "m2": {
        "sales_count": "sales_done_m2", "sales_value": "annual_sale_done_m2",
        "hot_total": None, "hot_self": None, "hot_mgr": None,
        "total_meet": "total_meet_m2", "fresh_meet": "fresh_meet_m2",
        "hp_converted": None, "hp_converted_met": None, "hp_tp_sum_met": None, "hp_tp_over_200": None, "working_days": None,
        "combined_total": None, "combined_l2_met": None, "combined_converted": None, "combined_converted_met": None,
    },
    "m3": {
        "sales_count": "sales_done_m3", "sales_value": "annual_sale_done_m3",
        "hot_total": None, "hot_self": None, "hot_mgr": None,
        "total_meet": "total_meet_m3", "fresh_meet": "fresh_meet_m3",
        "hp_converted": None, "hp_converted_met": None, "hp_tp_sum_met": None, "hp_tp_over_200": None, "working_days": None,
        "combined_total": None, "combined_l2_met": None, "combined_converted": None, "combined_converted_met": None,
    },
    "m4": {
        "sales_count": "sales_done_m4", "sales_value": "annual_sale_done_m4",
        "hot_total": None, "hot_self": None, "hot_mgr": None,
        "total_meet": "total_meet_m4", "fresh_meet": "fresh_meet_m4",
        "hp_converted": None, "hp_converted_met": None, "hp_tp_sum_met": None, "hp_tp_over_200": None, "working_days": None,
        "combined_total": None, "combined_l2_met": None, "combined_converted": None, "combined_converted_met": None,
    },
}

UNDERPERFORMER_METRIC = "sales_done"   # overall sales count; ranks within each location group
UNDERPERFORMER_PCT = 0.20              # bottom 20% flagged, minimum 1 per group if group is non-empty
UNDERPERFORMER_MIN_TENURE_DAYS = 90    # employees newer than this are never flagged
TENURE_FIELD = "tenure"                # assumed to be in days

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
        has_funnel = fields.get("hp_converted") is not None
        if has_funnel:
            agg["hp_converted"] = 0
            agg["hp_converted_met"] = 0
            agg["hp_tp_sum_met"] = 0
            agg["hp_tp_over_200"] = 0
            agg["working_days"] = 0
        has_combined = fields.get("combined_total") is not None
        if has_combined:
            agg["combined_total"] = 0
            agg["combined_l2_met"] = 0
            agg["combined_converted"] = 0
            agg["combined_converted_met"] = 0
        for r in rows:
            agg["sales_count"] += _num(r, fields["sales_count"]) or 0
            agg["sales_value"] += _num(r, fields["sales_value"]) or 0
            agg["total_meet"] += _num(r, fields["total_meet"]) or 0
            if has_hot:
                agg["hot_total"] += _num(r, fields["hot_total"]) or 0
                agg["hot_self"] += _num(r, fields["hot_self"]) or 0
                agg["hot_mgr"] += _num(r, fields["hot_mgr"]) or 0
            if has_funnel:
                agg["hp_converted"] += _num(r, fields["hp_converted"]) or 0
                agg["hp_converted_met"] += _num(r, fields["hp_converted_met"]) or 0
                agg["hp_tp_sum_met"] += _num(r, fields["hp_tp_sum_met"]) or 0
                agg["hp_tp_over_200"] += _num(r, fields["hp_tp_over_200"]) or 0
                agg["working_days"] += _num(r, fields["working_days"]) or 0
            if has_combined:
                agg["combined_total"] += _num(r, fields["combined_total"]) or 0
                agg["combined_l2_met"] += _num(r, fields["combined_l2_met"]) or 0
                agg["combined_converted"] += _num(r, fields["combined_converted"]) or 0
                agg["combined_converted_met"] += _num(r, fields["combined_converted_met"]) or 0
        out[period] = agg
    return out


def flag_underperformers(rows):
    """Within one location group, flag the bottom ~20% by overall sales_done —
    but only among employees who've been here 90+ days. Newer employees are
    never flagged, and don't count toward the group size used for the 20%
    calculation either."""
    if not rows:
        return set()
    eligible = [r for r in rows if (_num(r, TENURE_FIELD) or 0) >= UNDERPERFORMER_MIN_TENURE_DAYS]
    if not eligible:
        return set()
    scored = [(r.get(EMPLOYEE_ID_FIELD), _num(r, UNDERPERFORMER_METRIC) or 0) for r in eligible]
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


EMPLOYEE_SUMMARY_PERIODS = ["wtd", "mtd", "m1"]


TALKTIME_FIELD_BY_PERIOD = {
    "wtd": "avg_tt_hhmmss_wtd",
    "mtd": "avg_tt_hhmmss_mtd",
    "m1": "avg_tt_hhmmss_m1",
}


def build_employee_summary(row):
    """Small per-employee summary used in the tree's employee sub-table --
    only the handful of fields needed there, not the full raw row (which
    stays server-side, fetched on-demand per employee via /api/employee)."""
    summary = {
        "manager_name": row.get("manager_name"),
        "tenure": _num(row, "tenure"),
        "metrics": {},
    }
    for period in EMPLOYEE_SUMMARY_PERIODS:
        fields = METRIC_FIELDS[period]
        summary["metrics"][period] = {
            "sales_count": _num(row, fields["sales_count"]) or 0,
            "combined_total": _num(row, fields["combined_total"]) or 0,
            "combined_l2_met": _num(row, fields["combined_l2_met"]) or 0,
            "total_meet": _num(row, fields["total_meet"]) or 0,
            "fresh_meet": _num(row, fields["fresh_meet"]) or 0,
            "avg_talktime": row.get(TALKTIME_FIELD_BY_PERIOD[period]),
        }
    return summary


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
                            **build_employee_summary(r),
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


# ---------------------------------------------------------------------------
# Authoritative location-level sales (separate Redash query, query 13308).
# Only covers WTD/MTD/M1 and has no vertical breakdown — so this overlay is
# only applied when viewing "All Verticals"; a vertical-filtered view falls
# back to the employee-summed numbers, since applying an all-verticals total
# onto a filtered view would misrepresent it.
# ---------------------------------------------------------------------------
LOCATION_SALES_LOCATION_FIELD = "iil_comp_loc_name"
LOCATION_SALES_PERIOD_FIELDS = {
    "wtd": {"sales_count": "sales_done_wtd", "sales_value": "annual_sales_wtd"},
    "mtd": {"sales_count": "sales_done_mtd", "sales_value": "annual_sales_mtd"},
    "m1": {"sales_count": "sales_done_prev_month", "sales_value": "annual_sales_prev_month"},
}


def _recompute_parent_sales(parent_node, children):
    """Recompute a parent's sales_count/sales_value for the authoritative
    periods only, by summing its (possibly just-overlaid) children — leaves
    every other metric (total_meet, hot_*, other periods) untouched."""
    for period in LOCATION_SALES_PERIOD_FIELDS:
        parent_node["metrics"][period]["sales_count"] = sum(
            c["metrics"][period]["sales_count"] for c in children
        )
        parent_node["metrics"][period]["sales_value"] = sum(
            c["metrics"][period]["sales_value"] for c in children
        )


def compute_company_headcount_by_rm_location(full_tree):
    """Company-wide (ALL verticals combined) headcount for every (rm_name,
    location_name) pair. Used to consistently decide which RM 'truly owns'
    an ambiguous location, independent of which vertical filter is currently
    being viewed — computed once from the complete, unfiltered population."""
    result = {}
    for zm_node in full_tree.values():
        for rm_name, rm_node in zm_node["rm_children"].items():
            for loc_name, loc_node in rm_node["location_children"].items():
                result[(rm_name, loc_name)] = loc_node["headcount"]
    return result


def apply_authoritative_location_sales(tree, location_sales_rows, company_headcount_by_rm_location):
    """Overwrite each Location node's WTD/MTD/M1 sales_count/sales_value with
    the authoritative per-location numbers, matched on iil_comp_loc_name, then
    recompute RM and ZM rollups from the corrected location figures. Mutates
    `tree` in place.

    When the same location name appears under more than one RM branch (within
    whichever vertical-filtered tree is passed in here), the authoritative
    total goes to whichever occurrence has the highest COMPANY-WIDE headcount
    (via `company_headcount_by_rm_location`, computed once from the full,
    unfiltered population) — NOT the headcount within this filtered view.
    This matters: two RMs sharing a location can have very different
    vertical mixes, so "who has more people here" can flip between vertical
    views if computed per-view, silently reassigning (and zeroing out) real
    data depending on which vertical someone happens to be looking at. Using
    a single, vertical-independent reference keeps the assignment consistent
    everywhere. Every other occurrence of the same name gets set to 0 rather
    than left on its old employee-summed guess, since the true total is
    already fully assigned elsewhere and adding anything more would
    double-count it.

    Returns a dict with two lists:
      - "ambiguous": location names that appeared under more than one RM
        branch (informational — the majority-headcount one still got the
        real number; only flagged so you can sanity-check the split).
      - "unmatched": location names present in the tree that were NOT found
        anywhere in the location-sales query's own data at all -- these keep
        their old employee-summed number, silently, unless surfaced here.
        Usually means the location name is spelled/formatted differently
        between the two Redash queries.
    """
    if not location_sales_rows:
        return {"ambiguous": [], "unmatched": []}

    loc_lookup = {}
    for row in location_sales_rows:
        loc_name = row.get(LOCATION_SALES_LOCATION_FIELD)
        if not loc_name:
            continue
        loc_lookup[loc_name] = {
            period: {
                "sales_count": _num(row, fields["sales_count"]) or 0,
                "sales_value": _num(row, fields["sales_value"]) or 0,
            }
            for period, fields in LOCATION_SALES_PERIOD_FIELDS.items()
        }

    # Gather every (rm_name, loc_node) occurrence PRESENT in this (possibly
    # vertical-filtered) tree, for each location name.
    occurrences = defaultdict(list)
    for zm_node in tree.values():
        for rm_name, rm_node in zm_node["rm_children"].items():
            for loc_name, loc_node in rm_node["location_children"].items():
                occurrences[loc_name].append((rm_name, loc_node))

    ambiguous_locations = sorted(name for name, occ in occurrences.items() if len(occ) > 1)

    # Pick the majority occurrence using company-wide headcount, but only
    # among occurrences actually present in THIS view — if the company-wide
    # majority RM has zero employees in the current vertical (so it doesn't
    # even appear here), whichever occurrence IS present just gets treated
    # as majority, rather than everything present getting zeroed out.
    majority_node_by_name = {}
    for loc_name in ambiguous_locations:
        occ_list = occurrences[loc_name]
        majority_node_by_name[loc_name] = max(
            occ_list,
            key=lambda t: company_headcount_by_rm_location.get((t[0], loc_name), t[1]["headcount"]),
        )[1]

    unmatched_set = set()

    for zm_node in tree.values():
        for rm_node in zm_node["rm_children"].values():
            for loc_name, loc_node in rm_node["location_children"].items():
                authoritative = loc_lookup.get(loc_name)
                is_ambiguous = loc_name in majority_node_by_name
                is_majority_occurrence = is_ambiguous and majority_node_by_name[loc_name] is loc_node

                if authoritative is None:
                    if not is_ambiguous or is_majority_occurrence:
                        unmatched_set.add(loc_name)
                    continue  # no authoritative data at all; leave old number as-is

                if is_ambiguous and not is_majority_occurrence:
                    # Minority occurrence of an ambiguous name: the true total
                    # is already fully assigned to the majority occurrence,
                    # so this one gets 0 rather than double-counting.
                    for period in authoritative:
                        loc_node["metrics"][period]["sales_count"] = 0
                        loc_node["metrics"][period]["sales_value"] = 0
                else:
                    for period, vals in authoritative.items():
                        loc_node["metrics"][period]["sales_count"] = vals["sales_count"]
                        loc_node["metrics"][period]["sales_value"] = vals["sales_value"]
            _recompute_parent_sales(rm_node, rm_node["location_children"].values())
        _recompute_parent_sales(zm_node, zm_node["rm_children"].values())

    return {"ambiguous": ambiguous_locations, "unmatched": sorted(unmatched_set)}
