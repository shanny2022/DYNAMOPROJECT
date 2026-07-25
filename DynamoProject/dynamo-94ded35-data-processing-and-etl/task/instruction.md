Build a deterministic point-in-time customer feature dataset from:

- `/app/data/customers.csv`
- `/app/data/account_status_history.csv`
- `/app/data/events.csv`
- `/app/data/labels.csv`
- `/app/data/cutoffs.csv`

Write `/app/output/features.csv` and `/app/output/validation.json`, creating `/app/output` if needed.

Normalize every `customer_id` and `event_id` by trimming surrounding whitespace and uppercasing it. Empty normalized IDs are null and cannot form output keys. Parse timestamps as timezone-aware instants, convert them to UTC, and format output timestamps as `YYYY-MM-DDTHH:MM:SSZ`.

Resolve source versions before computing features:

- Customers: one row per normalized `customer_id`; greatest `updated_at` wins, then greatest `source_record_id`.
- Events: one row per normalized non-null `event_id`; greatest `ingested_at` wins, then greatest `event_record_id`.
- Labels: one row per normalized `customer_id` and UTC `cutoff_time`; greatest `label_updated_at` wins, then greatest `label_record_id`.

Timestamp comparisons use actual instants. Create one output row for every deduplicated customer crossed with every physical cutoff row where `signup_time <= cutoff_time`. Cutoff rows are not deduplicated. Each supplied cutoff is unique. A qualifying event belongs to the same normalized customer and has `event_time <= cutoff_time`. An event exactly at cutoff is included. Events after cutoff are excluded.

For `account_status`, choose status rows for the customer with `effective_time <= cutoff_time`; greatest `effective_time` wins, then greatest `updated_time`, then greatest `status_record_id`. Trim and lowercase the selected status. Use `unknown` if none qualifies.

Trim and lowercase event types; empty values are null. The normative `/app/output/features.csv` column order and definitions are:

`customer_id,cutoff_time,account_status,total_event_count,distinct_event_type_count,event_count_7d,event_count_30d,purchase_count,purchase_amount_sum,purchase_amount_sum_7d,days_since_last_event,label`

- `total_event_count`: qualifying deduplicated events.
- `distinct_event_type_count`: distinct non-null normalized types among them.
- `event_count_7d`: events with `event_time > cutoff_time - 7 days` and `event_time <= cutoff_time`.
- `event_count_30d`: events with `event_time > cutoff_time - 30 days` and `event_time <= cutoff_time`.
- `purchase_count`: qualifying events with normalized type `purchase`.
- `purchase_amount_sum`: decimal sum of valid amounts for those purchases; a missing amount contributes zero. Format with exactly two decimal places.
- `purchase_amount_sum_7d`: decimal sum of valid amounts for purchase events with `event_time > cutoff_time - 7 days` and `event_time <= cutoff_time`; a missing amount contributes zero. Format with exactly two decimal places.
- `days_since_last_event`: floor of elapsed seconds from the latest qualifying event to cutoff divided by 86,400; use `-1` when none qualifies.
- `label`: the deduplicated label joined on normalized `customer_id` plus UTC `cutoff_time`; write an empty CSV field when missing.

All count and day fields are base-10 integers. Preserve eligible zero-event customers with zero counts and `0.00` purchase sum. Sort rows ascending by `customer_id`, then `cutoff_time`, and emit each key exactly once.

The normative `/app/output/validation.json` is one JSON object containing exactly these integer fields:

`input_customer_rows,input_status_rows,input_event_rows,input_label_rows,input_cutoff_rows,deduplicated_customer_rows,deduplicated_event_rows,qualifying_event_cutoff_rows,output_feature_rows,duplicate_feature_keys,null_label_rows`

Input counts are physical data rows excluding headers. Deduplicated counts use the rules above. `qualifying_event_cutoff_rows` counts qualifying deduplicated event/customer/cutoff combinations represented in the eligible output population. `duplicate_feature_keys` counts output rows beyond the first for repeated `customer_id,cutoff_time` keys. `null_label_rows` counts output rows with an empty label. No additional JSON fields are allowed.

Success requires both artifacts to match this contract exactly.
