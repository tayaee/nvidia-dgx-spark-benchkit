#!/usr/bin/env bash
# Smoke test for SWE-bench Pro — N instances, real inference.
# Usage: swebench-pro-smoke.sh [--limit-new=N]
#   --limit-new=N  (default 0 = all instances; use 1 for the one-shot smoke)
set -euo pipefail
cd "$(cd "$(dirname "$0")/.." && pwd)"
exec ./.venv/bin/python bin/smoke.py swebench-pro "$@"