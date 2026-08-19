#!/usr/bin/env bash
# Smoke test for Terminal-Bench 2.0 — N instances, real inference.
# Usage: terminal-bench-2.0-smoke.sh [--limit-new=N]
#   --limit-new=N  (default 0 = all instances; use 1 for the one-shot smoke)
set -euo pipefail
cd "$(cd "$(dirname "$0")/.." && pwd)"
exec ./.venv/bin/python bin/smoke.py terminal-bench-2.0 "$@"
