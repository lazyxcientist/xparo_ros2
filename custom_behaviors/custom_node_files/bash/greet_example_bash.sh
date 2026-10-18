#!/usr/bin/env bash
# Example custom BT Action node (Bash) -- ships with the repo so the
# multi-language custom-node pipeline is testable with zero Django/
# dashboard interaction: a fresh `colcon build --symlink-install` alone
# registers this tag (see engine.py's sync_custom_node_files,
# examples_manifest.json). Genuinely working, not a TODO stub.
#
# Run by XPARO's real Bash runner (BashProcessNode) -- a fresh process
# per tick: inputs arrive as upper-cased env vars, outputs are printed as
# "KEY=value" lines, status is the exit code (0=SUCCESS, 1=FAILURE,
# 2=RUNNING).
set -euo pipefail

NAME="${NAME:-robot}"

GREETING="Hello, ${NAME}! XPARO custom node pipeline is working."
echo "[GreetExample] ${GREETING}" >&2
echo "GREETING=${GREETING}"

exit 0
