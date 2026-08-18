#!/usr/bin/env bash
# Smoke test for DeepSWE 1.1 — N instances, real inference.
# Usage: deepswe-1.1-smoke.sh [--limit-new=N]
#   --limit-new=N  (default 0 = all instances; use 1 for the one-shot smoke)
set -euo pipefail
cd "$(cd "$(dirname "$0")/.." && pwd)"
exec ./.venv_wsl/bin/python bin/smoke.py deepswe-1.1 "$@"