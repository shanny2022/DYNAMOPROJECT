# Point-in-Time Customer Feature Pipeline

This benchmark evaluates deterministic ETL over versioned customer, status,
event, label, and cutoff data. Its difficulty comes from interacting
point-in-time, deduplication, boundary, normalization, and population rules.

The verifier independently reconstructs expected results from the public seed
inputs rather than importing the oracle implementation.
