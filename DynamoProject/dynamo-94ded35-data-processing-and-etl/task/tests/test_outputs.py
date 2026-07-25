"""
Verifier for the point-in-time customer feature task.

The verifier independently re-reads source data, re-runs all deduplication and
qualifying-event logic, and checks every field of features.csv and all counters
in validation.json.

A qualifying event at a given cutoff must satisfy BOTH:
  event_time   <= cutoff_time
  ingested_at  <= cutoff_time   (prevents leakage from late-arriving corrections)
"""

import csv
import json
import math
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import pytest

# ── Paths ────────────────────────────────────────────────────────────────────
DATA_DIR = os.environ.get("DATA_DIR", "/app/data")
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/app/output")

FEATURES_CSV = os.path.join(OUTPUT_DIR, "features.csv")
VALIDATION_JSON = os.path.join(OUTPUT_DIR, "validation.json")


# ── Helpers ──────────────────────────────────────────────────────────────────
def instant(s: str) -> datetime:
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    return dt.astimezone(timezone.utc)


def fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def norm_id(s: str) -> str:
    return s.strip().upper() if s else ""


def read_csv(name: str):
    with open(os.path.join(DATA_DIR, name), newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ── Load sources ─────────────────────────────────────────────────────────────
raw_customers = read_csv("customers.csv")
raw_statuses  = read_csv("account_status_history.csv")
raw_events    = read_csv("events.csv")
raw_labels    = read_csv("labels.csv")
raw_cutoffs   = read_csv("cutoffs.csv")

# Deduplicate customers
customers: dict = {}
for row in raw_customers:
    cid = norm_id(row.get("customer_id", ""))
    if not cid:
        continue
    row["_cid"]              = cid
    row["_updated_at"]       = instant(row["updated_at"])
    row["_signup_time"]      = instant(row["signup_time"])
    row["_source_record_id"] = row.get("source_record_id", "")
    ex = customers.get(cid)
    if ex is None or (row["_updated_at"], row["_source_record_id"]) > (
        ex["_updated_at"], ex["_source_record_id"]
    ):
        customers[cid] = row

# Preprocess statuses
for row in raw_statuses:
    row["_customer_id"]     = norm_id(row.get("customer_id", ""))
    row["_effective_time"]  = instant(row["effective_time"])
    row["_updated_time"]    = instant(row["updated_time"])
    row["_status_record_id"]= row.get("status_record_id", "")

# Deduplicate events
events: dict = {}
for row in raw_events:
    eid = norm_id(row.get("event_id", ""))
    if not eid:
        continue
    row["_eid"]             = eid
    row["_customer_id"]     = norm_id(row.get("customer_id", ""))
    row["_event_time"]      = instant(row["event_time"])
    row["_ingested_at"]     = instant(row["ingested_at"])
    row["_event_record_id"] = row.get("event_record_id", "")
    ex = events.get(eid)
    if ex is None or (row["_ingested_at"], row["_event_record_id"]) > (
        ex["_ingested_at"], ex["_event_record_id"]
    ):
        events[eid] = row

# Deduplicate labels
labels: dict = {}
for row in raw_labels:
    cid = norm_id(row.get("customer_id", ""))
    if not cid:
        continue
    row["_cid"]               = cid
    row["_cutoff_time"]       = instant(row["cutoff_time"])
    row["_label_updated_at"]  = instant(row["label_updated_at"])
    row["_label_record_id"]   = row.get("label_record_id", "")
    key = (cid, fmt(row["_cutoff_time"]))
    ex = labels.get(key)
    if ex is None or (row["_label_updated_at"], row["_label_record_id"]) > (
        ex["_label_updated_at"], ex["_label_record_id"]
    ):
        labels[key] = row

cutoff_times = [instant(r["cutoff_time"]) for r in raw_cutoffs]
event_list   = list(events.values())
status_list  = raw_statuses


# ── Compute expected ─────────────────────────────────────────────────────────
def expected_for(cid: str, cutoff_time: datetime) -> dict:
    cutoff_str = fmt(cutoff_time)

    # Qualifying events: event_time <= cutoff AND ingested_at <= cutoff
    qe = [
        e for e in event_list
        if e["_customer_id"] == cid
        and e["_event_time"]   <= cutoff_time
        and e["_ingested_at"]  <= cutoff_time
    ]

    # Account status
    sc = [
        s for s in status_list
        if s["_customer_id"] == cid and s["_effective_time"] <= cutoff_time
    ]
    if sc:
        best = max(sc, key=lambda s: (
            s["_effective_time"], s["_updated_time"], s["_status_record_id"]
        ))
        account_status = best["status"].strip().lower()
    else:
        account_status = "unknown"

    total_event_count = len(qe)
    distinct_types    = set()
    ec7d = ec30d = purchase_count = 0
    purchase_sum = Decimal("0.00")
    latest_et    = None
    cutoff_ts    = cutoff_time.timestamp()

    for e in qe:
        et    = e["_event_time"]
        etype = (e.get("event_type") or "").strip().lower() or None
        if etype:
            distinct_types.add(etype)
        elapsed = cutoff_ts - et.timestamp()
        if elapsed < 7 * 86400:
            ec7d  += 1
        if elapsed < 30 * 86400:
            ec30d += 1
        if etype == "purchase":
            purchase_count += 1
            amt = (e.get("amount") or "").strip()
            if amt:
                try:
                    purchase_sum += Decimal(amt)
                except InvalidOperation:
                    pass
        if latest_et is None or et > latest_et:
            latest_et = et

    days_since = math.floor((cutoff_ts - latest_et.timestamp()) / 86400) if latest_et else -1

    label_row = labels.get((cid, cutoff_str))
    label_val = label_row["label"] if label_row else ""

    return {
        "customer_id":               cid,
        "cutoff_time":               cutoff_str,
        "account_status":            account_status,
        "total_event_count":         str(total_event_count),
        "distinct_event_type_count": str(len(distinct_types)),
        "event_count_7d":            str(ec7d),
        "event_count_30d":           str(ec30d),
        "purchase_count":            str(purchase_count),
        "purchase_amount_sum":       f"{purchase_sum:.2f}",
        "days_since_last_event":     str(days_since),
        "label":                     label_val,
    }


def compute_expected_rows() -> list[dict]:
    rows = []
    for cid in sorted(customers.keys()):
        cust = customers[cid]
        for ct in sorted(cutoff_times):
            if cust["_signup_time"] <= ct:
                rows.append(expected_for(cid, ct))
    return rows


# ── Fixtures ─────────────────────────────────────────────────────────────────
@pytest.fixture(scope="session")
def actual():
    with open(FEATURES_CSV, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


@pytest.fixture(scope="session")
def actual_validation():
    with open(VALIDATION_JSON, encoding="utf-8") as f:
        return json.load(f)


# ── Tests ─────────────────────────────────────────────────────────────────────
def test_features_schema(actual):
    expected_cols = [
        "customer_id", "cutoff_time", "account_status", "total_event_count",
        "distinct_event_type_count", "event_count_7d", "event_count_30d",
        "purchase_count", "purchase_amount_sum", "days_since_last_event", "label",
    ]
    assert list(actual[0].keys()) == expected_cols


def test_features_row_count(actual):
    expected = compute_expected_rows()
    assert len(actual) == len(expected), (
        f"Expected {len(expected)} rows, got {len(actual)}"
    )


def test_features_all_values(actual):
    expected_rows = compute_expected_rows()
    actual_keyed  = {(r["customer_id"], r["cutoff_time"]): r for r in actual}
    errors        = []
    for exp in expected_rows:
        key = (exp["customer_id"], exp["cutoff_time"])
        act = actual_keyed.get(key)
        if act is None:
            errors.append(f"Missing row {key}")
            continue
        for col, val in exp.items():
            if act.get(col) != val:
                errors.append(
                    f"{key} {col}: expected={val!r} actual={act.get(col)!r}"
                )
    assert not errors, "\n".join(errors[:20])


def test_features_sort_order(actual):
    keys = [(r["customer_id"], r["cutoff_time"]) for r in actual]
    assert keys == sorted(keys)


def test_features_no_duplicate_keys(actual):
    keys = [(r["customer_id"], r["cutoff_time"]) for r in actual]
    assert len(keys) == len(set(keys)), "Duplicate (customer_id, cutoff_time) keys found"


def test_validation_schema(actual_validation):
    required = {
        "input_customer_rows", "input_status_rows", "input_event_rows",
        "input_label_rows", "input_cutoff_rows", "deduplicated_customer_rows",
        "deduplicated_event_rows", "qualifying_event_cutoff_rows",
        "output_feature_rows", "duplicate_feature_keys", "null_label_rows",
    }
    assert set(actual_validation.keys()) == required


def test_validation_input_counts(actual_validation):
    assert actual_validation["input_customer_rows"] == len(raw_customers)
    assert actual_validation["input_status_rows"]   == len(raw_statuses)
    assert actual_validation["input_event_rows"]    == len(raw_events)
    assert actual_validation["input_label_rows"]    == len(raw_labels)
    assert actual_validation["input_cutoff_rows"]   == len(raw_cutoffs)


def test_validation_dedup_counts(actual_validation):
    assert actual_validation["deduplicated_customer_rows"] == len(customers)
    assert actual_validation["deduplicated_event_rows"]    == len(events)


def test_validation_derived_counts(actual_validation):
    expected_rows = compute_expected_rows()

    # qualifying_event_cutoff_rows
    qualifying = sum(
        1
        for cid in sorted(customers.keys())
        for ct in sorted(cutoff_times)
        if customers[cid]["_signup_time"] <= ct
        for e in event_list
        if e["_customer_id"] == cid
        and e["_event_time"]  <= ct
        and e["_ingested_at"] <= ct
    )
    assert actual_validation["qualifying_event_cutoff_rows"] == qualifying

    assert actual_validation["output_feature_rows"]   == len(expected_rows)
    assert actual_validation["duplicate_feature_keys"] == 0

    null_labels = sum(1 for r in expected_rows if not r["label"])
    assert actual_validation["null_label_rows"] == null_labels


def test_adversarial_boundary_and_dedup_cases(actual):
    """
    Spot-checks for known adversarial edge cases.
    Row keys are (customer_id, cutoff_time) in the actual output.
    """
    keyed = {(r["customer_id"], r["cutoff_time"]): r for r in actual}

    # 1) Event exactly at cutoff on BOTH event_time and ingested_at must be
    #    included.  E022/C007: event_time=2026-01-15T00:00:00Z,
    #    ingested_at=2026-01-15T00:00:00Z → qualifies.
    #    E005/C001 has event_time at cutoff but ingested_at 1 min LATER
    #    → excluded from the Jan-15 cutoff (ingested_at leakage trap).
    assert keyed[("C007", "2026-01-15T00:00:00Z")]["total_event_count"] == "1"
    assert keyed[("C007", "2026-01-15T00:00:00Z")]["days_since_last_event"] == "0"
    assert keyed[("C001", "2026-01-15T00:00:00Z")]["total_event_count"] == "4"

    # 2) Event dedup by latest ingested_at then lexicographic event_record_id;
    #    after dedup the winner's ingested_at is checked against each cutoff.
    #    E009/C002: winner R09 has ingested_at=2026-01-15T00:02:00Z → excluded
    #    at Jan-15 cutoff but qualifies at Feb-01.
    #    At Jan-15: qualifying purchases are E032 (1.00) + E040/ER9 (4.00) = 5.00
    assert keyed[("C002", "2026-01-15T00:00:00Z")]["purchase_amount_sum"] == "5.00"

    # 3) Zero-event customer rows preserved.
    assert keyed[("C005", "2026-01-15T00:00:00Z")]["total_event_count"] == "0"

    # 4) days_since_last_event uses floor(seconds/86400); -1 when no events.
    assert keyed[("C005", "2026-01-15T00:00:00Z")]["days_since_last_event"] == "-1"

    # 5) Label dedup: C002 at Jan-15 resolves correctly.
    assert keyed[("C002", "2026-01-15T00:00:00Z")]["label"] == "0"

    # 6) Customer with no prior status → "unknown".
    assert keyed[("C003", "2026-01-15T00:00:00Z")]["account_status"] == "active"

    # 7) UTC-offset timestamp on status row parsed and compared correctly.
    #    S10 updated_time has -04:00 offset and should be parsed correctly.
    assert keyed[("C004", "2026-02-01T00:00:00Z")]["account_status"] == "review"

    # 8) Status tie with same effective/updated time uses lexicographic
    #    status_record_id.  S9X>"S10X" lexicographically → review wins.
    assert keyed[("C005", "2026-01-15T00:00:00Z")]["account_status"] == "review"

    # 9) Label tie with same label_updated_at uses lexicographic label_record_id.
    #    L9X>"L10X" lexicographically → label=1 wins.
    assert keyed[("C006", "2026-02-01T00:00:00Z")]["label"] == "1"

    # 10) Non-numeric purchase amounts contribute zero while still counting.
    assert keyed[("C003", "2026-01-15T00:00:00Z")]["purchase_count"] == "1"
    assert keyed[("C003", "2026-01-15T00:00:00Z")]["purchase_amount_sum"] == "0.00"

    # 11) E009/C002 winner (R09, ingested 2026-01-15T00:02:00Z) qualifies at
    #     Feb-01 cutoff.  All three purchases present: 12.50+1.00+4.00=17.50.
    assert keyed[("C002", "2026-02-01T00:00:00Z")]["purchase_amount_sum"] == "17.50"


def test_window_boundaries(actual):
    """Seven- and thirty-day windows must enforce exclusive lower boundaries."""
    keyed = {(r["customer_id"], r["cutoff_time"]): r for r in actual}

    # C001 @ 2026-01-15:
    # E003 (2026-01-08T00:00:00Z) is exactly at the exclusive lower bound → excluded.
    # E004 (2026-01-08T00:00:01Z) is within 7 days → included.
    # E005 excluded from all counts (ingested 1 min after cutoff).
    assert keyed[("C001", "2026-01-15T00:00:00Z")]["event_count_7d"] == "1"

    # C001 @ 2026-02-01: 30-day window is >2026-01-02; E001 is 2025-12-16 → excluded.
    assert keyed[("C001", "2026-02-01T00:00:00Z")]["event_count_30d"] == "6"
