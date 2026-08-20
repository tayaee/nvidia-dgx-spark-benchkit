#!/usr/bin/env bash
# Compatibility shim — translate legacy RUN_ID / SCRIPT_VER (TUNE_NO)
# into the new benchkit experiment / script-version identifiers.
# Existing `benchmark-lib.sh` scripts can `source` this and still work,
# but the on-disk artifacts use the new layout (manifest.json, attempts/, etc.).

set -Eeuo pipefail

# Required legacy variable
: "${RUN_ID:?RUN_ID is required (legacy env)}"

# SCRIPT_VER is the new primary name; TUNE_NO is accepted as a legacy alias.
if [[ -z "${SCRIPT_VER:-}" && -n "${TUNE_NO:-}" ]]; then
  # legacy alias
  export SCRIPT_VER="$TUNE_NO"
fi
: "${SCRIPT_VER:?SCRIPT_VER (or legacy TUNE_NO) is required}"

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
export BENCKKIT_TUNE_ALIAS="$SCRIPT_VER"