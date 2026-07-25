#!/usr/bin/env bash
set -uo pipefail
mkdir -p /logs/verifier
if pytest -q --ctrf /logs/verifier/ctrf.json /tests/test_outputs.py; then
  printf '1\n' > /logs/verifier/reward.txt
else
  printf '0\n' > /logs/verifier/reward.txt
fi
exit 0
