#!/usr/bin/env bash
# web.sh — run the benchkit results dashboard (FastAPI).
#
# Usage:
#   ./web.sh [--port 8001] [--host 127.0.0.1]
#
# Reads the results/ directory and serves benchmark progress / scores.
set -Eeuo pipefail
cd "$(cd "$(dirname "$0")" && pwd)"

PORT="${PORT:-8001}"
HOST="${HOST:-0.0.0.0}"
while (($#)); do
  case "$1" in
    --port) PORT="$2"; shift 2;;
    --host) HOST="$2"; shift 2;;
    -h|--help) echo "usage: $0 [--port N] [--host H]"; exit 0;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac
done

export BENCHKIT_RESULTS_ROOT="${BENCHKIT_RESULTS_ROOT:-$PWD/results}"
echo "Results root : $BENCHKIT_RESULTS_ROOT"
echo "Dashboard    : http://$HOST:$PORT"

RUNPY='
import os, uvicorn
from benchkit.webapp.app import create_app
app = create_app(os.environ.get("BENCHKIT_RESULTS_ROOT"))
uvicorn.run(app, host=os.environ.get("BENCHKIT_HOST", "127.0.0.1"), port=int(os.environ.get("BENCHKIT_PORT", "8001")), log_level="warning")
'
export BENCHKIT_HOST="$HOST"
export BENCHKIT_PORT="$PORT"

if [ -d .venv ] && .venv/bin/python -c 'import fastapi, uvicorn, websockets' 2>/dev/null; then
  exec .venv/bin/python -c "$RUNPY"
else
  exec uv run --with fastapi --with 'uvicorn[standard]' --python 3.12 python -c "$RUNPY"
fi
