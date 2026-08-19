#!/usr/bin/env bash
# Run all 5 smoke tests in sequence. Returns non-zero if any fail.
# Usage: smoke-all.sh [--limit-new=N]
#   --limit-new=N  (default 0 = all instances per benchmark; 1 = one-shot)
set -uo pipefail
cd "$(cd "$(dirname "$0")/.." && pwd)"

BENCHMARKS=(swebench-verified swebench-pro terminal-bench-2.0 deepswe-1.1)
RESULTS=()
for b in "${BENCHMARKS[@]}"; do
  echo "==================================================================="
  echo "SMOKE: $b"
  echo "==================================================================="
  if bin/$b-smoke.sh "$@"; then
    RESULTS+=("$b=PASS")
  else
    RESULTS+=("$b=FAIL")
  fi
  echo
done

echo "==================================================================="
echo "SUMMARY"
echo "==================================================================="
for r in "${RESULTS[@]}"; do
  echo "  $r"
done
! grep -q FAIL <<<"${RESULTS[*]}"