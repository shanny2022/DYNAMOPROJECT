#!/usr/bin/env python3
"""
Point-in-time customer feature builder.
Spec: /app/instruction.md
"""

import csv
import json
import math
import os
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

DATA_DIR = "/app/data"
OUT_DIR = "/app/output"


def parse_instant(s: str) -> datetime:
    s = s.strip()
    # Replace Z with +00:00 for fromisoformat, handle ±HH:MM offsets
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    return dt.astimezone(timezone.utc)


def fmt_ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def norm_id(s: str) -> str:
    if s is None:
        return ""
    return s.strip().upper()


def read_csv(path: str):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


# ── Load & deduplicate customers ───────────────────────────────────────────────
raw_customers = read_csv(os.path.join(DATA_DIR, "customers.csv"))
customers = {}
for row in raw_customers:
    cid = norm_id(row.get("customer_id", ""))
    if not cid:
        continue
    row["_cid"] = cid
    row["_updated_at"] = parse_instant(row["updated_at"])
    row["_signup_time"] = parse_instant(row["signup_time"])
    row["_source_record_id"] = row.get("source_record_id", "")
    existing = customers.get(cid)
    if existing is None:
        customers[cid] = row
    else:
        if (row["_updated_at"], row["_source_record_id"]) > (
            existing["_updated_at"],
            existing["_source_record_id"],
        ):
            customers[cid] = row

# ── Load & deduplicate events ─────────────────────────────────────────────────
raw_events = read_csv(os.path.join(DATA_DIR, "events.csv"))
events = {}
for row in raw_events:
    eid = norm_id(row.get("event_id", ""))
    if not eid:
        continue
    row["_eid"] = eid
    row["_customer_id"] = norm_id(row.get("customer_id", ""))
    row["_event_time"] = parse_instant(row["event_time"])
    row["_ingested_at"] = parse_instant(row["ingested_at"])
    row["_event_record_id"] = row.get("event_record_id", "")
    existing = events.get(eid)
    if existing is None:
        events[eid] = row
    else:
        if (row["_ingested_at"], row["_event_record_id"]) > (
            existing["_ingested_at"],
            existing["_event_record_id"],
        ):
            events[eid] = row

# ── Load status history ────────────────────────────────────────────────────────
raw_statuses = read_csv(os.path.join(DATA_DIR, "account_status_history.csv"))
for row in raw_statuses:
    row["_customer_id"] = norm_id(row.get("customer_id", ""))
    row["_effective_time"] = parse_instant(row["effective_time"])
    row["_updated_time"] = parse_instant(row["updated_time"])
    row["_status_record_id"] = row.get("status_record_id", "")

# ── Load & deduplicate labels ─────────────────────────────────────────────────
raw_labels = read_csv(os.path.join(DATA_DIR, "labels.csv"))
labels = {}
for row in raw_labels:
    cid = norm_id(row.get("customer_id", ""))
    if not cid:
        continue
    row["_cid"] = cid
    row["_cutoff_time"] = parse_instant(row["cutoff_time"])
    row["_label_updated_at"] = parse_instant(row["label_updated_at"])
    row["_label_record_id"] = row.get("label_record_id", "")
    key = (cid, fmt_ts(row["_cutoff_time"]))
    existing = labels.get(key)
    if existing is None:
        labels[key] = row
    else:
        if (row["_label_updated_at"], row["_label_record_id"]) > (
            existing["_label_updated_at"],
            existing["_label_record_id"],
        ):
            labels[key] = row

# ── Load cutoffs ──────────────────────────────────────────────────────────────
raw_cutoffs = read_csv(os.path.join(DATA_DIR, "cutoffs.csv"))
cutoffs = [parse_instant(r["cutoff_time"]) for r in raw_cutoffs]

# ── Build features ─────────────────────────────────────────────────────────────
os.makedirs(OUT_DIR, exist_ok=True)

event_list = list(events.values())
status_list = raw_statuses

output_rows = []
qualifying_count = 0
seen_keys = set()
duplicate_keys = 0
null_label_rows = 0

SEVEN_DAYS = 7 * 86400
THIRTY_DAYS = 30 * 86400

for cid in sorted(customers.keys()):
    cust = customers[cid]
    signup_time = cust["_signup_time"]

    for cutoff_time in sorted(cutoffs):
        if signup_time > cutoff_time:
            continue

        cutoff_str = fmt_ts(cutoff_time)

        # Qualifying events: event_time <= cutoff AND ingested_at <= cutoff
        qualifying_events = [
            e
            for e in event_list
            if e["_customer_id"] == cid
            and e["_event_time"] <= cutoff_time
            and e["_ingested_at"] <= cutoff_time
        ]

        qualifying_count += len(qualifying_events)

        # Account status
        status_candidates = [
            s
            for s in status_list
            if s["_customer_id"] == cid and s["_effective_time"] <= cutoff_time
        ]
        if status_candidates:
            best = max(
                status_candidates,
                key=lambda s: (
                    s["_effective_time"],
                    s["_updated_time"],
                    s["_status_record_id"],
                ),
            )
            account_status = best["status"].strip().lower()
        else:
            account_status = "unknown"

        # Event features
        total_event_count = len(qualifying_events)
        distinct_types = set()
        event_count_7d = 0
        event_count_30d = 0
        purchase_count = 0
        purchase_amount_sum = Decimal("0.00")
        latest_event_time = None

        cutoff_ts = cutoff_time.timestamp()

        for e in qualifying_events:
            et = e["_event_time"]
            etype = (e.get("event_type") or "").strip().lower() or None
            if etype:
                distinct_types.add(etype)

            et_ts = et.timestamp()
            elapsed = cutoff_ts - et_ts
            if elapsed < SEVEN_DAYS:
                event_count_7d += 1
            if elapsed < THIRTY_DAYS:
                event_count_30d += 1

            if etype == "purchase":
                purchase_count += 1
                amt_str = (e.get("amount") or "").strip()
                if amt_str:
                    try:
                        purchase_amount_sum += Decimal(amt_str)
                    except InvalidOperation:
                        pass  # non-numeric amount → contributes zero

            if latest_event_time is None or et > latest_event_time:
                latest_event_time = et

        if latest_event_time is not None:
            elapsed_sec = cutoff_time.timestamp() - latest_event_time.timestamp()
            days_since = math.floor(elapsed_sec / 86400)
        else:
            days_since = -1

        # Label
        label_row = labels.get((cid, cutoff_str))
        label_val = label_row["label"] if label_row else ""

        key = (cid, cutoff_str)
        if key in seen_keys:
            duplicate_keys += 1
        else:
            seen_keys.add(key)

        if not label_val:
            null_label_rows += 1

        output_rows.append(
            {
                "customer_id": cid,
                "cutoff_time": cutoff_str,
                "account_status": account_status,
                "total_event_count": str(total_event_count),
                "distinct_event_type_count": str(len(distinct_types)),
                "event_count_7d": str(event_count_7d),
                "event_count_30d": str(event_count_30d),
                "purchase_count": str(purchase_count),
                "purchase_amount_sum": f"{purchase_amount_sum:.2f}",
                "days_since_last_event": str(days_since),
                "label": label_val,
            }
        )

# ── Write features.csv ────────────────────────────────────────────────────────
fieldnames = [
    "customer_id",
    "cutoff_time",
    "account_status",
    "total_event_count",
    "distinct_event_type_count",
    "event_count_7d",
    "event_count_30d",
    "purchase_count",
    "purchase_amount_sum",
    "days_since_last_event",
    "label",
]

with open(os.path.join(OUT_DIR, "features.csv"), "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(output_rows)

# ── Write validation.json ──────────────────────────────────────────────────────
validation = {
    "input_customer_rows": len(raw_customers),
    "input_status_rows": len(raw_statuses),
    "input_event_rows": len(raw_events),
    "input_label_rows": len(raw_labels),
    "input_cutoff_rows": len(raw_cutoffs),
    "deduplicated_customer_rows": len(customers),
    "deduplicated_event_rows": len(events),
    "qualifying_event_cutoff_rows": qualifying_count,
    "output_feature_rows": len(output_rows),
    "duplicate_feature_keys": duplicate_keys,
    "null_label_rows": null_label_rows,
}

with open(os.path.join(OUT_DIR, "validation.json"), "w", encoding="utf-8") as f:
    json.dump(validation, f, indent=2)

print("Done.")
