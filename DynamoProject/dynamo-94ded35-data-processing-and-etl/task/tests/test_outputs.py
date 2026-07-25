"""Independent verifier for the point-in-time customer ETL artifacts.

This verifier is intentionally strict and independent:
- Normalizes IDs and timestamps to UTC instants
- Reconstructs expected outputs from source contracts
- Enforces exact schema, ordering, sorting, and key uniqueness
- Adds adversarial checks for leakage and boundary behavior
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pytest

DATA = Path("/app/data")
OUTPUT = Path("/app/output")

FEATURE_COLUMNS = [
    "customer_id",
    "cutoff_time",
    "account_status",
    "total_event_count",
    "distinct_event_type_count",
    "event_count_7d",
    "event_count_30d",
    "purchase_count",
    "purchase_amount_sum",
    "purchase_amount_sum_7d",
    "days_since_last_event",
    "label",
]

VALIDATION_FIELDS = {
    "input_customer_rows",
    "input_status_rows",
    "input_event_rows",
    "input_label_rows",
    "input_cutoff_rows",
    "deduplicated_customer_rows",
    "deduplicated_event_rows",
    "qualifying_event_cutoff_rows",
    "output_feature_rows",
    "duplicate_feature_keys",
    "null_label_rows",
}


def read_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def norm_id(value: str | None) -> str | None:
    cleaned = (value or "").strip().upper()
    return cleaned or None


def norm_event_type(value: str | None) -> str | None:
    cleaned = (value or "").strip().lower()
    return cleaned or None


def instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc)


def canonical(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_decimal(value: str | None) -> Decimal:
    raw = (value or "").strip()
    if not raw:
        return Decimal("0")
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError):
        return Decimal("0")


def choose_latest(rows, key_fn, rank_fn):
    chosen = {}
    for row in rows:
        key = key_fn(row)
        parts = key if isinstance(key, tuple) else (key,)
        if any(part is None for part in parts):
            continue
        prev = chosen.get(key)
        if prev is None or rank_fn(row) > rank_fn(prev):
            chosen[key] = row
    return chosen


@pytest.fixture(scope="module")
def expected():
    customers_raw = read_rows(DATA / "customers.csv")
    status_raw = read_rows(DATA / "account_status_history.csv")
    events_raw = read_rows(DATA / "events.csv")
    labels_raw = read_rows(DATA / "labels.csv")
    cutoffs_raw = read_rows(DATA / "cutoffs.csv")

    customers = choose_latest(
        customers_raw,
        key_fn=lambda r: norm_id(r.get("customer_id")),
        rank_fn=lambda r: (instant(r["updated_at"]), r["source_record_id"]),
    )

    events = choose_latest(
        events_raw,
        key_fn=lambda r: norm_id(r.get("event_id")),
        rank_fn=lambda r: (instant(r["ingested_at"]), r["event_record_id"]),
    )

    labels = choose_latest(
        labels_raw,
        key_fn=lambda r: (norm_id(r.get("customer_id")), instant(r["cutoff_time"])),
        rank_fn=lambda r: (instant(r["label_updated_at"]), r["label_record_id"]),
    )

    # Precompute status fields.
    for row in status_raw:
        row["_customer_id"] = norm_id(row.get("customer_id")) # type: ignore
        row["_effective_time"] = instant(row["effective_time"]) # type: ignore
        row["_updated_time"] = instant(row["updated_time"])
        row["_status_norm"] = (row.get("status") or "").strip().lower()

    # Precompute event fields on deduplicated events.
    for row in events.values():
        row["_customer_id"] = norm_id(row.get("customer_id"))
        row["_event_time"] = instant(row["event_time"])
        row["_event_type_norm"] = norm_event_type(row.get("event_type"))
        row["_amount_dec"] = safe_decimal(row.get("amount"))

    expected_rows = []
    qualifying_event_cutoff_rows = 0

    for customer_id, customer in customers.items():
        signup_time = instant(customer["signup_time"])
        for cutoff_row in cutoffs_raw:  # physical cutoffs are not deduplicated
            cutoff_time = instant(cutoff_row["cutoff_time"])
            if signup_time > cutoff_time:
                continue

            qualifying = [
                e
                for e in events.values()
                if e["_customer_id"] == customer_id and e["_event_time"] <= cutoff_time
            ]
            qualifying_event_cutoff_rows += len(qualifying)

            status_candidates = [
                s
                for s in status_raw
                if s["_customer_id"] == customer_id and s["_effective_time"] <= cutoff_time
            ]
            if status_candidates:
                selected = max(
                    status_candidates,
                    key=lambda s: (
                        s["_effective_time"],
                        s["_updated_time"],
                        s["status_record_id"],
                    ),
                )
                account_status = selected["_status_norm"] or "unknown"
            else:
                account_status = "unknown"

            purchases = [e for e in qualifying if e["_event_type_norm"] == "purchase"]
            purchase_sum = sum((e["_amount_dec"] for e in purchases), Decimal("0"))

            purchases_7d = [
                e for e in purchases
                if cutoff_time - timedelta(days=7) < e["_event_time"] <= cutoff_time
            ]
            purchase_sum_7d = sum((e["_amount_dec"] for e in purchases_7d), Decimal("0"))

            latest_event_time = max((e["_event_time"] for e in qualifying), default=None)
            days_since_last_event = (
                -1
                if latest_event_time is None
                else int((cutoff_time - latest_event_time).total_seconds() // 86400)
            )

            distinct_types = {
                e["_event_type_norm"] for e in qualifying if e["_event_type_norm"] is not None
            }

            label_row = labels.get((customer_id, cutoff_time))
            label_value = "" if label_row is None else (label_row.get("label") or "").strip()

            expected_rows.append(
                {
                    "customer_id": customer_id,
                    "cutoff_time": canonical(cutoff_time),
                    "account_status": account_status,
                    "total_event_count": str(len(qualifying)),
                    "distinct_event_type_count": str(len(distinct_types)),
                    "event_count_7d": str(
                        sum(
                            cutoff_time - timedelta(days=7) < e["_event_time"] <= cutoff_time
                            for e in qualifying
                        )
                    ),
                    "event_count_30d": str(
                        sum(
                            cutoff_time - timedelta(days=30) < e["_event_time"] <= cutoff_time
                            for e in qualifying
                        )
                    ),
                    "purchase_count": str(len(purchases)),
                    "purchase_amount_sum": f"{purchase_sum:.2f}",
                    "purchase_amount_sum_7d": f"{purchase_sum_7d:.2f}",
                    "days_since_last_event": str(days_since_last_event),
                    "label": label_value,
                }
            )

    expected_rows.sort(key=lambda r: (r["customer_id"], r["cutoff_time"]))

    validation_expected = {
        "input_customer_rows": len(customers_raw),
        "input_status_rows": len(status_raw),
        "input_event_rows": len(events_raw),
        "input_label_rows": len(labels_raw),
        "input_cutoff_rows": len(cutoffs_raw),
        "deduplicated_customer_rows": len(customers),
        "deduplicated_event_rows": len(events),
        "qualifying_event_cutoff_rows": qualifying_event_cutoff_rows,
        "output_feature_rows": len(expected_rows),
        "duplicate_feature_keys": 0,
        "null_label_rows": sum(1 for r in expected_rows if r["label"] == ""),
    }

    return expected_rows, validation_expected


@pytest.fixture(scope="module")
def actual():
    return read_rows(OUTPUT / "features.csv")


def test_required_output_files_exist():
    assert (OUTPUT / "features.csv").is_file()
    assert (OUTPUT / "validation.json").is_file()


def test_feature_schema_and_column_order(actual):
    assert actual, "features.csv must contain at least one data row"
    assert list(actual[0].keys()) == FEATURE_COLUMNS


def test_key_population_order_and_uniqueness(actual, expected):
    wanted_rows, _ = expected
    actual_keys = [(r["customer_id"], r["cutoff_time"]) for r in actual]
    expected_keys = [(r["customer_id"], r["cutoff_time"]) for r in wanted_rows]
    assert actual_keys == expected_keys
    assert len(actual_keys) == len(set(actual_keys)), "duplicate feature keys are not allowed"


def test_exact_feature_values(actual, expected):
    wanted_rows, _ = expected
    assert actual == wanted_rows


def test_timestamp_canonical_utc(actual):
    for row in actual:
        assert canonical(instant(row["cutoff_time"])) == row["cutoff_time"]


def test_integer_fields_are_base10_strings(actual):
    int_fields = [
        "total_event_count",
        "distinct_event_type_count",
        "event_count_7d",
        "event_count_30d",
        "purchase_count",
        "days_since_last_event",
    ]
    for row in actual:
        for f in int_fields:
            value = row[f]
            # Accept "0", "-1", etc.; reject empty, decimals, scientific notation.
            assert value != ""
            parsed = int(value, 10)
            assert str(parsed) == value


def test_purchase_amount_sum_has_two_decimals(actual):
    for row in actual:
        for col in ("purchase_amount_sum", "purchase_amount_sum_7d"):
            val = row[col]
            assert "." in val
            whole, frac = val.split(".", 1)
            assert whole.lstrip("-").isdigit()
            assert len(frac) == 2 and frac.isdigit()


def test_adversarial_boundary_and_dedup_cases(actual):
    keyed = {(r["customer_id"], r["cutoff_time"]): r for r in actual}

    # 1) Event exactly at cutoff must be included; E039 (Dec 20, from winning record R41)
    #    also qualifies at Jan 15 => total is 6.
    assert keyed[("C001", "2026-01-15T00:00:00Z")]["total_event_count"] == "6"

    # 2) Event dedup by latest ingested_at then event_record_id:
    #    C002 E009 should use amount=12.50 (R09 ingested later than R10).
    assert keyed[("C002", "2026-01-15T00:00:00Z")]["purchase_amount_sum"] == "13.50"

    # 3) Zero-event customer rows preserved.
    assert keyed[("C005", "2026-01-15T00:00:00Z")]["total_event_count"] == "0"
    assert keyed[("C005", "2026-01-15T00:00:00Z")]["purchase_amount_sum"] == "0.00"

    # 4) Exact-cutoff event on signup boundary.
    assert keyed[("C007", "2026-01-15T00:00:00Z")]["days_since_last_event"] == "0"

    # 5) UTC-equivalent label time normalization for C002 @ 2026-01-15.
    #    L18 (with -05:00) and L19 target same UTC cutoff; latest label_updated_at wins => "0".
    assert keyed[("C002", "2026-01-15T00:00:00Z")]["label"] == "0"

    # 6) Status tie break at same effective_time for C003 cutoff 2026-01-15:
    #    S08 updated later than S07 => active.
    assert keyed[("C003", "2026-01-15T00:00:00Z")]["account_status"] == "active"

    # 7) Status UTC conversion case C004:
    #    S10 updated_time has -04:00 offset and should be parsed correctly.
    assert keyed[("C004", "2026-02-01T00:00:00Z")]["account_status"] == "review"

    # 8) Dec-15 cutoff: C001 had no events before Dec 15 (first event is Dec 16).
    assert keyed[("C001", "2025-12-15T00:00:00Z")]["total_event_count"] == "0"
    assert keyed[("C001", "2025-12-15T00:00:00Z")]["account_status"] == "active"

    # 9) Dec-15 cutoff: C002 has exactly one login (E008, Dec 10) — 4 full days before cutoff.
    assert keyed[("C002", "2025-12-15T00:00:00Z")]["total_event_count"] == "1"
    assert keyed[("C002", "2025-12-15T00:00:00Z")]["days_since_last_event"] == "4"

    # 10) E039 dedup: R41 (ingested 00:03, event_time Dec 20) beats R42 (ingested 00:02,
    #     event_time Jan 15). Only purchases with event_time IN the 7d window count.
    #     E039's event_time is Dec 20, NOT in the Jan-15 7d window => 7d sum stays at 20.00
    #     (only E004, Jan 8T00:00:01, is a qualifying purchase in the 7d window).
    assert keyed[("C001", "2026-01-15T00:00:00Z")]["purchase_amount_sum_7d"] == "20.00"

    # 11) C002@Jan15: E009 (Jan 15, 12.50) in 7d; E032 (Jan 2, 1.00) is not.
    assert keyed[("C002", "2026-01-15T00:00:00Z")]["purchase_amount_sum_7d"] == "12.50"

    # 12) C008@Jan15: E038 (Jan 15T00:00, 1.75 from tie-break R40>R39) is in 7d.
    assert keyed[("C008", "2026-01-15T00:00:00Z")]["purchase_amount_sum_7d"] == "1.75"


def test_window_boundaries(actual):
    keyed = {(r["customer_id"], r["cutoff_time"]): r for r in actual}

    # C001 @ 2026-01-15:
    # E003 is exactly at the exclusive lower bound; E004 and E005 qualify => 2.
    # E039 (Dec 20) is NOT in the 7d window even though it qualifies overall.
    assert keyed[("C001", "2026-01-15T00:00:00Z")]["event_count_7d"] == "2"

    # C001 @ 2026-01-15: 30d window (> Dec 16). E039 (Dec 20) IS in 30d => 5 events.
    assert keyed[("C001", "2026-01-15T00:00:00Z")]["event_count_30d"] == "5"

    # C001 @ 2026-02-01:
    # 30d window (> Jan 2): six Jan events qualify; E039 (Dec 20) and Dec-16 events do not.
    assert keyed[("C001", "2026-02-01T00:00:00Z")]["event_count_30d"] == "6"


def test_validation_schema_and_values(expected):
    _, wanted = expected
    with (OUTPUT / "validation.json").open(encoding="utf-8") as handle:
        report = json.load(handle)

    assert set(report.keys()) == VALIDATION_FIELDS
    assert all(type(v) is int for v in report.values())
    assert report == wanted
