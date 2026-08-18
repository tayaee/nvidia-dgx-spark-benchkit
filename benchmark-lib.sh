#!/usr/bin/env bash
set -Eeuo pipefail
die(){ echo "ERROR: $*" >&2; exit 2; }
main_common(){
  LIMIT_NEW=""
  while (($#)); do
    case "$1" in
      --limit-new) (($#>1)) || die 'missing --limit-new value'; LIMIT_NEW="$2"; shift 2;;
      --limit-new=*) LIMIT_NEW="${1#*=}"; shift;;
      -h|--help) echo "RUN_ID=N TUNE_NO=N $0 --limit-new N"; exit 0;;
      *) die "unknown argument: $1";;
    esac
  done
  [[ "${RUN_ID:-}" =~ ^[0-9]+$ ]] || die 'RUN_ID is required'
  [[ "${TUNE_NO:-}" =~ ^[0-9]+$ ]] || die 'TUNE_NO is required'
  [[ "$LIMIT_NEW" =~ ^[1-9][0-9]*$ ]] || die '--limit-new must be positive'
  RUN_ROOT="${RESULTS_ROOT:-results}/run-$RUN_ID"
  mkdir -p "$RUN_ROOT"/{predictions/raw,predictions/canonical,eval/input,eval/raw,logs,archive}
  touch "$RUN_ROOT/state.jsonl"
  [[ -e "$RUN_ROOT/manifest.json" ]] || printf '{"run_id":%s,"benchmark":"%s","dataset":"%s","created_at":"%s","status":"active"}\n' "$RUN_ID" "${BENCHMARK:-unknown}" "${DATASET:-}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$RUN_ROOT/manifest.json"
}
archive_script(){
  local src="$1" name="$2" dst="$RUN_ROOT/archive/tune$(printf '%03d' "$TUNE_NO")-$name"
  [[ ! -e "$dst" ]] || die "archive exists: $dst; increment TUNE_NO"
  { echo "# RUN_ID=$RUN_ID TUNE_NO=$TUNE_NO BENCHMARK=${BENCHMARK:-}"; cat "$src"; } > "$dst"
  chmod +x "$dst"
}
