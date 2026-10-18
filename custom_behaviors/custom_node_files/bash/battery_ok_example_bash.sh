#!/usr/bin/env bash
# Example custom BT Condition node (Bash) -- see greet_example.sh's
# comment for why this ships in the repo. A real deployment would read
# this robot's actual battery telemetry (no such subsystem exists in this
# repo yet) -- this uses a fixed simulated reading so the example is
# deterministic and testable without any hardware at all.
set -euo pipefail

# TODO(hardware): replace with a real battery-telemetry read.
SIMULATED_BATTERY_PERCENT=76.0
MIN_LEVEL="${MIN_LEVEL:-20}"

# A Condition should never exit 2 (RUNNING) -- only 0 (SUCCESS) or 1
# (FAILURE). awk avoids depending on bc/bash's own integer-only arithmetic
# for a fractional comparison.
if awk -v a="$SIMULATED_BATTERY_PERCENT" -v b="$MIN_LEVEL" 'BEGIN { exit !(a >= b) }'; then
  echo "[BatteryOkExample] battery=${SIMULATED_BATTERY_PERCENT}% min_level=${MIN_LEVEL} -> SUCCESS" >&2
  exit 0
else
  echo "[BatteryOkExample] battery=${SIMULATED_BATTERY_PERCENT}% min_level=${MIN_LEVEL} -> FAILURE" >&2
  exit 1
fi
