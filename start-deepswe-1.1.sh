#!/usr/bin/env bash
# start-deepswe-1.1.sh — run DeepSWE 1.1 (local WSL2)
#
# Usage:
#   RUN_ID=1 SCRIPT_VER=1 ./start-deepswe-1.1.sh --limit-new 1
#   RUN_ID=1 SCRIPT_VER=1 ./start-deepswe-1.1.sh --limit-new-ok 1 --limit-max-try 10
#
# Behavior:
#   - Pull tasks from datacurve/deep-swe (HF) and run them.
#   - Model: qwen3.8-27b on spark1.local:30000 (auto-detected).
#   - Each task is verified by its dedicated docker image (verifier_script).
#   - Completed instances are skipped via the .solved tracker; new instances
#     are run up to --limit-new N.
#   - The launch script is archived under
#     results/run-$RUN_ID/deepswe-1.1/archive/ (archive/vNNN-...sh).
#
# Environment:
#   RUN_ID            — non-negative integer. Defaults to the last-used value
#                       from .cache/start-deepswe-1.1.sh.env, or 1.
#   SCRIPT_VER        — non-negative integer. Config (server/client settings)
#                       version number. Defaults to the last-used cache value,
#                       or 1. Increment only when the config changes; plain
#                       reruns keep the same value.
#   HF_TOKEN          — required for datacurve/deep-swe
#   OPENAI_BASE_URL / BENCKKIT_ENDPOINT — default http://spark1.local:30000/v1
#   MODEL_NAME / BENCKKIT_MODEL         — default qwen3.8-27b (auto-detected)

set -Eeuo pipefail
cd "$(cd "$(dirname "$0")" && pwd)"

# shellcheck source=./benchmark-lib.sh
source ./benchmark-lib.sh

BENCHMARK="deepswe-1.1"
DATASET="${DATASET:-datacurve/deep-swe}"

# main_common parses --limit-new and validates RUN_ID / SCRIPT_VER / LIMIT_NEW.
main_common "$@"

[[ -n "${HF_TOKEN:-}" ]] || die 'HF_TOKEN is required for datacurve/deep-swe'

# Model/endpoint already resolved by main_common → resolve_model_target
# (BENCKKIT_ENDPOINT / BENCKKIT_MODEL / OPENAI_BASE_URL / MODEL_NAME).
# Re-resolving here would race the target path computation and split results.
# smoke.py expects results/<run_id>/<benchmark>/, so BENCKKIT_RUN_ID is the
# bench-specific subdir (results/run-1/deepswe-1.1/).
export BENCKKIT_RUN_ID="${BENCKKIT_RUN_ID:-run-$RUN_ID}"
export BENCKKIT_RESULTS="${BENCKKIT_RESULTS:-$PWD/results}"

# Record experiment metadata in manifest.json (web dashboard display).
export SERVER_SCRIPT="${SERVER_SCRIPT:-~/git/dgx-spark-qwen38/run.sh}"
export SERVER_HOST="${SERVER_HOST:-spark1.local}"
update_manifest

# (log() is provided by benchmark-lib.sh)

# ── archive: canonical launch script ──
archive_script "$0" "start-deepswe-1.1.sh"

SOLVED_FILE="results/.solved/deepswe-1.1.solved"
count_solved(){
  if [[ -s "$SOLVED_FILE" ]]; then
    grep -c . "$SOLVED_FILE" || true
  else
    echo 0
  fi
}
DONE_COUNT=$(count_solved)

if [[ "$LIMIT_MODE" == "ok" ]]; then
  TRY_COUNT="$LIMIT_NEW"
else
  TRY_COUNT="${LIMIT_NEW:-0}"
fi

# Track every instance attempted in this run (success or failure) so the next
# batch skips them instead of retrying.
ATTEMPTED_FILE="$RUN_ROOT/.attempted"
touch "$ATTEMPTED_FILE"

# ── Single batch: smoke.py (real inference + docker verifier) ──
run_batch(){
  local try_n=1
  if (( TRY_COUNT > 0 )); then try_n=$TRY_COUNT; fi
  # Skip instances already attempted this run.
  local skip
  skip=$(paste -sd ' ' "$ATTEMPTED_FILE")
  log "model=$BENCKKIT_MODEL endpoint=$BENCKKIT_ENDPOINT run_root=$RUN_ROOT trying up to $try_n new instance(s)"
  # Results accumulate at results/run-$RUN_ID/deepswe-1.1/.
  local out
  out="$(
    ./.venv/bin/python bin/smoke.py "$BENCHMARK" --limit-new "$try_n" ${skip:+--skip-ids "$skip"} 2>&1
  )" || true
  echo "$out"
  # Record attempted instance ids from the per-instance progress lines.
  echo "$out" | sed -nE 's/^--- \[[0-9]+\/[0-9]+\] .* \/ ([^ ]+) ---$/\1/p' \
    | while read -r iid; do
        grep -qxF "$iid" "$ATTEMPTED_FILE" 2>/dev/null || echo "$iid" >> "$ATTEMPTED_FILE"
      done
}

# Newly PASSed in this batch — smoke.py appends to .solved on PASS.
_LAST_OK_COUNT=$DONE_COUNT
count_new_ok(){
  local now
  now=$(count_solved)
  echo $(( now - _LAST_OK_COUNT ))
  _LAST_OK_COUNT=$now
}

run_with_limits

log "Done."
