#!/usr/bin/env bash
# Compatibility shim — translate legacy RUN_ID/TUNE_NO into the new
# benchkit experiment/trial identifiers. Existing `benchmark-lib.sh`
# scripts can `source` this and still work, but the on-disk artifacts
# use the new layout (manifest.json, attempts/, etc.).

set -Eeuo pipefail

# Legacy variables (still required for backward compat)
: "${RUN_ID:?RUN_ID is required (legacy env)}"
: "${TUNE_NO:?TUNE_NO is required (legacy env)}"

export BENCKKIT_ROOT="${BENCKKIT_ROOT:-$(pwd)}"
export RESULTS_ROOT="${RESULTS_ROOT:-${BENCKKIT_ROOT}/results}"

# Generate (or reuse) a canonical experiment id from RUN_ID
if [[ ! -f "${RESULTS_ROOT}/.run-id-map" ]]; then
  echo "run-id-map absent; please run 'benchkit create-experiment' first" >&2
  exit 2
fi

EID="$(awk -v rid="$RUN_ID" '$2==rid {print $1}' "${RESULTS_ROOT}/.run-id-map" | head -1)"
if [[ -z "$EID" ]]; then
  echo "no experiment mapped to RUN_ID=$RUN_ID" >&2
  exit 2
fi
export BENCKKIT_EXPERIMENT_ID="$EID"
export BENCKKIT_TUNE_ALIAS="$TUNE_NO"