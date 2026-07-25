from __future__ import annotations

import csv
import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

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
    "days_since_last_event",
    "label",
]

VALIDATION_FIELDS = [
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
]


def read_csv_rows(filename: str) -> list[dict[str, str]]:
    with (DATA / filename).open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def normalize_identifier(value: str | None) -> str | None:
    normalized = (value or "").strip().upper()
    return normalized if normalized else None


def parse_instant(value: str) -> datetime:
    # Accept trailing Z and offsets, always normalize to UTC-aware datetime.
    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    return parsed.astimezone(timezone.utc)


def format_utc(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def normalize_event_type(value: str | None) -> str | None:
    normalized = (value or "").strip().lower()
    return normalized if normalized else None


def safe_decimal(value: str | None) -> Decimal:
    raw = (value or "").strip()
    if not raw:
        return Decimal("0")
    try:
        return Decimal(raw)
    except (InvalidOperation, ValueError):
        # Contract says valid amounts for purchases; invalid behaves as missing -> zero.
        return Decimal("0")


def choose_latest(
    rows: list[dict[str, str]],
    key_fn: Callable[[dict[str, str]], Any],
    rank_fn: Callable[[dict[str, str]], Any],
) -> dict[Any, dict[str, str]]:
    chosen: dict[Any, dict[str, str]] = {}
    for row in rows:
        key = key_fn(row)
        parts = key if isinstance(key, tuple) else (key,)
        if any(part is None for part in parts):
            # Null normalized IDs cannot form output/join keys.
            continue
        incumbent = chosen.get(key)
        if incumbent is None or rank_fn(row) > rank_fn(incumbent):
            chosen[key] = row
    return chosen


def main() -> None:
    customers_raw = read_csv_rows("customers.csv")
    statuses_raw = read_csv_rows("account_status_history.csv")
    events_raw = read_csv_rows("events.csv")
    labels_raw = read_csv_rows("labels.csv")
    cutoffs_raw = read_csv_rows("cutoffs.csv")

    customers = choose_latest(
        customers_raw,
        key_fn=lambda r: normalize_identifier(r.get("customer_id")),
        rank_fn=lambda r: (parse_instant(r["updated_at"]), r["source_record_id"]),
    )

    events = choose_latest(
        events_raw,
        key_fn=lambda r: normalize_identifier(r.get("event_id")),
        rank_fn=lambda r: (parse_instant(r["ingested_at"]), r["event_record_id"]),
    )

    labels = choose_latest(
        labels_raw,
        key_fn=lambda r: (
            normalize_identifier(r.get("customer_id")),
            parse_instant(r["cutoff_time"]),
        ),
        rank_fn=lambda r: (parse_instant(r["label_updated_at"]), r["label_record_id"]),
    )

    # Precompute normalized/parsed fields for status rows.
    for row in statuses_raw:
        row["_customer_id"] = normalize_identifier(row.get("customer_id")) # type: ignore
        row["_effective_time"] = parse_instant(row["effective_time"]) # type: ignore
        row["_updated_time"] = parse_instant(row["updated_time"]) # type: ignore
        row["_status_norm"] = (row.get("status") or "").strip().lower()

    # Precompute normalized/parsed fields for deduplicated events.
    for row in events.values():
        row["_customer_id"] = normalize_identifier(row.get("customer_id")) # type: ignore
        row["_event_time"] = parse_instant(row["event_time"]) # type: ignore
        row["_event_type_norm"] = normalize_event_type(row.get("event_type")) # type: ignore
        row["_amount_dec"] = safe_decimal(row.get("amount")) # type: ignore

    output_rows: list[dict[str, str]] = []
    qualifying_event_cutoff_rows = 0

    # Build one output row per deduped customer x physical cutoff row where signup <= cutoff.
    for customer_id, customer_row in customers.items():
        signup_time = parse_instant(customer_row["signup_time"])

        for cutoff_row in cutoffs_raw:
            cutoff_time = parse_instant(cutoff_row["cutoff_time"])
            if signup_time > cutoff_time:
                continue

            qualifying_events = [
                e
                for e in events.values()
                if e["_customer_id"] == customer_id and e["_event_time"] <= cutoff_time
            ]
            qualifying_event_cutoff_rows += len(qualifying_events)

            status_candidates = [
                s
                for s in statuses_raw
                if s["_customer_id"] == customer_id and s["_effective_time"] <= cutoff_time
            ]
            if status_candidates:
                chosen_status = max(
                    status_candidates,
                    key=lambda s: (
                        s["_effective_time"],
                        s["_updated_time"],
                        s["status_record_id"],
                    ),
                )
                account_status = chosen_status["_status_norm"] or "unknown"
            else:
                account_status = "unknown"

            purchase_events = [
                e for e in qualifying_events if e["_event_type_norm"] == "purchase"
            ]
            purchase_sum = sum((e["_amount_dec"] for e in purchase_events), Decimal("0"))

            latest_event_time = max(
                (e["_event_time"] for e in qualifying_events),
                default=None,
            )
            days_since_last_event = (
                -1
                if latest_event_time is None
                else int((cutoff_time - latest_event_time).total_seconds() // 86400)
            )

            label_row = labels.get((customer_id, cutoff_time))
            label_value = "" if label_row is None else (label_row.get("label") or "").strip()

            event_types = {
                e["_event_type_norm"] for e in qualifying_events if e["_event_type_norm"] is not None
            }

            row = {
                "customer_id": customer_id,
                "cutoff_time": format_utc(cutoff_time),
                "account_status": account_status,
                "total_event_count": str(len(qualifying_events)),
                "distinct_event_type_count": str(len(event_types)),
                "event_count_7d": str(
                    sum(
                        cutoff_time - timedelta(days=7) < e["_event_time"] <= cutoff_time
                        for e in qualifying_events
                    )
                ),
                "event_count_30d": str(
                    sum(
                        cutoff_time - timedelta(days=30) < e["_event_time"] <= cutoff_time
                        for e in qualifying_events
                    )
                ),
                "purchase_count": str(len(purchase_events)),
                "purchase_amount_sum": f"{purchase_sum:.2f}",
                "days_since_last_event": str(days_since_last_event),
                "label": label_value,
            }
            output_rows.append(row)

    output_rows.sort(key=lambda r: (r["customer_id"], r["cutoff_time"]))

    OUTPUT.mkdir(parents=True, exist_ok=True)

    with (OUTPUT / "features.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FEATURE_COLUMNS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(output_rows)

    keys = [(r["customer_id"], r["cutoff_time"]) for r in output_rows]
    validation = {
        "input_customer_rows": len(customers_raw),
        "input_status_rows": len(statuses_raw),
        "input_event_rows": len(events_raw),
        "input_label_rows": len(labels_raw),
        "input_cutoff_rows": len(cutoffs_raw),
        "deduplicated_customer_rows": len(customers),
        "deduplicated_event_rows": len(events),
        "qualifying_event_cutoff_rows": qualifying_event_cutoff_rows,
        "output_feature_rows": len(output_rows),
        "duplicate_feature_keys": len(keys) - len(set(keys)),
        "null_label_rows": sum(1 for r in output_rows if r["label"] == ""),
    }

    ordered_validation = {k: int(validation[k]) for k in VALIDATION_FIELDS}

    with (OUTPUT / "validation.json").open("w", encoding="utf-8") as handle:
        json.dump(ordered_validation, handle, indent=2)
        handle.write("\n")


if __name__ == "__main__":
    main()
