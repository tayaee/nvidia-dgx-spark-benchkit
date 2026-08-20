#!/usr/bin/env bash
# report-swebench-pro.sh — SWE-bench Pro report
#
# Usage:
#   RUN_ID=1 ./report-swebench-pro.sh
#
# Behavior:
#   - Reads results/run-$RUN_ID/swebench-pro/eval/summary.json instead of scraping logs.
#   - Displays resolved / unresolved / missing / not-evaluated and resolved/total.
#
# Environment:
#   RUN_ID   — required, positive integer

set -Eeuo pipefail
cd "$(cd "$(dirname "$0")" && pwd)"

# shellcheck source=./benchmark-lib.sh
source ./benchmark-lib.sh

BENCHMARK="swebench-pro"
DATASET="${DATASET:-ScaleAI/SWE-bench_Pro}"

# report script needs no --limit-new/SCRIPT_VER; fill defaults to pass validation.
SCRIPT_VER="${SCRIPT_VER:-0}"
main_common --limit-new 1

RUN_DIR="$RUN_ROOT"
EVAL_DIR="$RUN_DIR/eval"
SUMMARY_JSON="$EVAL_DIR/summary.json"

if [[ ! -s "$SUMMARY_JSON" ]]; then
    echo "ERROR: $SUMMARY_JSON missing. Run ./eval-swebench-pro.sh first." >&2
    exit 1
fi

python3 - "$SUMMARY_JSON" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    s = json.load(f)

total = s["total_predicted"]
resolved = s["resolved"]
unresolved = s["unresolved"]
missing = s["missing"]
not_eval = s["not_evaluated"]

W = 72
print("=" * W)
print(f"SWE-bench Pro — run-{s.get('run_id')}")
print("=" * W)
print(f"  resolved        : {resolved}  / {total}")
print(f"  unresolved      : {unresolved}")
print(f"  missing         : {missing}")
print(f"  not evaluated   : {not_eval}")
pct = (resolved / total * 100) if total else 0.0
print(f"  score           : {resolved}/{total} = {pct:.1f}%")
print("-" * W)
if s.get("resolved_ids"):
    print("resolved:")
    for iid in s["resolved_ids"]:
        print(f"  + {iid}")
if s.get("unresolved_ids"):
    print("unresolved:")
    for iid in s["unresolved_ids"]:
        print(f"  - {iid}")
if s.get("missing_ids"):
    print("missing (empty patch / no output):")
    for iid in s["missing_ids"]:
        print(f"  ? {iid}")
if s.get("not_evaluated_ids"):
    print("not evaluated:")
    for iid in s["not_evaluated_ids"]:
        print(f"  ~ {iid}")
print("=" * W)
PY
