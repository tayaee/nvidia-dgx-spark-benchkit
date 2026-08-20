#!/usr/bin/env bash
set -Eeuo pipefail
die(){ echo "ERROR: $*" >&2; exit 2; }
log(){ echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# ──────────────────────────────────────────────────────────────
# target (test goal) = bench official name + model name + model url
# results/
# └── <target-key>/                  e.g. qwen3.8-27b__swebench-verified__spark1.local-30000
#     ├── target.json                { bench, model, model_url, active_run_id, last_run_at }
#     ├── comments.json              { "run-1": "...", ... }
#     └── run-1/, run-2/, ...        ← bench results accumulate here
#
# RUN_ID / SCRIPT_VER resolution (priority high → low):
#   1. environment variable on the command line
#   2. last-used value persisted in .cache/<script-basename>.env
#   3. literal default (1)
# After a successful run, the resolved values are written back to the cache
# so the next invocation of the same script (without env overrides) re-opens
# the same RUN_ID / SCRIPT_VER.
#
# RUN_ID was previously resolved from target.active_run_id when unset. The
# cache replaces that fallback so the CLI behaviour does not depend on the
# target's web-managed active_run_id.
# ──────────────────────────────────────────────────────────────

# Server defaults. Scripts and callers may override via env.
DEFAULT_ENDPOINT="${DEFAULT_ENDPOINT:-http://spark1.local:30000/v1}"
DEFAULT_MODEL="${DEFAULT_MODEL:-qwen3.8-27b}"

# .cache/<basename of $0>.env holds last-used RUN_ID / SCRIPT_VER per script.
# Eval/report scripts do not use it (they don't take SCRIPT_VER); start-*.sh
# does. Override the path with BENCHKIT_CACHE_FILE for tests.
DEFAULT_CACHE_FILE="${BENCHKIT_CACHE_FILE:-.cache/$(basename "$0").env}"

# Echo the resolved value for a single integer variable.
#
# Priority: env var (already set) > .cache/<script>.env > literal default (1).
# Falls back to 1 silently if the cache file is absent, missing the key, or
# has a non-integer value (a warning is logged to stderr in that case).
#
# Usage:  val="$(resolve_default VAR_NAME [DEFAULT] [CACHE_FILE])"
resolve_default(){
  local var="$1"
  local default="${2:-1}"
  local cf="${3:-$DEFAULT_CACHE_FILE}"
  # Already set in env: caller wins.
  if [[ -n "${!var:-}" ]]; then
    printf '%s' "${!var}"
    return 0
  fi
  # Cache file missing: literal default.
  if [[ ! -r "$cf" ]]; then
    printf '%s' "$default"
    return 0
  fi
  # Pick first matching key=value (whitespace-tolerant, comment-tolerant).
  local val
  val="$(awk -F= -v k="$var" '
    /^[[:space:]]*#/ {next}
    /^[[:space:]]*$/ {next}
    $1==k {print $2; exit}
  ' "$cf" 2>/dev/null || true)"
  if [[ -z "$val" ]]; then
    printf '%s' "$default"
    return 0
  fi
  if [[ "$val" =~ ^[0-9]+$ ]]; then
    printf '%s' "$val"
    return 0
  fi
  echo "warning: $cf has invalid $var='$val'; falling back to $default" >&2
  printf '%s' "$default"
}

# Persist current values of the named variables to a .cache/<script>.env file.
# Overwrites the file (atomic via a tmp + mv). Values are not validated here;
# main_common validates before saving.
#
# Usage:  write_defaults CACHE_FILE VAR1 VAR2 ...
write_defaults(){
  local cf="$1"; shift
  [[ -n "$cf" ]] || { echo "write_defaults: cache file path required" >&2; return 2; }
  mkdir -p "$(dirname "$cf")"
  local tmp="$cf.tmp.$$"
  {
    echo "# last-used values for $(basename "$0")"
    echo "# auto-written by benchmark-lib.sh; edit by hand if needed."
    local var
    for var in "$@"; do
      printf '%s=%s\n' "$var" "${!var:-}"
    done
  } > "$tmp"
  mv "$tmp" "$cf"
}

# Resolve and export the model/endpoint.
#
# target_key derives the results directory from model name + url, so this must
# run BEFORE target_root (→ bench_root). Skipping it leaks results into
# results/unknown__<bench>__unknown/ and splits one experiment in two.
# main_common calls it, so individual scripts need not bother.
#
# Idempotent: already-set values are kept, so it is safe even when a script
# detected and exported the model itself.
resolve_model_target(){
  local url="${OPENAI_BASE_URL:-${BENCKKIT_ENDPOINT:-$DEFAULT_ENDPOINT}}"
  export OPENAI_BASE_URL="$url" BENCKKIT_ENDPOINT="$url"

  local model="${MODEL_NAME:-${BENCKKIT_MODEL:-}}"
  if [[ -z "$model" ]]; then
    # Autodetect via /v1/models. Fall back to DEFAULT_MODEL so a down server
    # does not divert the results path to "unknown".
    model="$(curl -sf --max-time 10 "$url/models" \
      | python3 -c 'import json,sys; print(json.load(sys.stdin)["data"][0]["id"])' 2>/dev/null || true)"
    [[ -n "$model" ]] || model="$DEFAULT_MODEL"
  fi
  export MODEL_NAME="$model" BENCKKIT_MODEL="$model"
}

# Compute the target key. Needs BENCHMARK / MODEL_NAME / OPENAI_BASE_URL.
# The url is reduced to host-port; only filename-safe chars survive.
target_key(){
  local model="${MODEL_NAME:-${BENCKKIT_MODEL:-unknown}}"
  local url="${OPENAI_BASE_URL:-${BENCKKIT_ENDPOINT:-unknown}}"
  # http://spark1.local:30000/v1 → spark1.local-30000
  local host_port
  host_port="$(printf '%s' "$url" | sed -E 's#^[a-z]+://##; s#/.*$##; s#[:/]#-#g')"
  printf '%s__%s__%s' "$model" "$BENCHMARK" "$host_port" \
    | tr ' /' '__' | tr -cd 'A-Za-z0-9_.-'
}

# Resolve the target directory (return if present, else create + init target.json).
target_root(){
  local key
  key="$(target_key)"
  TARGET_ROOT="${RESULTS_ROOT:-results}/$key"
  mkdir -p "$TARGET_ROOT"
  export TARGET_ROOT
  [[ -f "$TARGET_ROOT/target.json" ]] || printf '{"bench":"%s","model":"%s","model_url":"%s","active_run_id":1,"created_at":"%s","last_run_at":""}\n' \
      "$BENCHMARK" "${MODEL_NAME:-${BENCKKIT_MODEL:-}}" "${OPENAI_BASE_URL:-${BENCKKIT_ENDPOINT:-}}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
      > "$TARGET_ROOT/target.json"
  [[ -f "$TARGET_ROOT/comments.json" ]] || echo '{}' > "$TARGET_ROOT/comments.json"
}

# Read active_run_id from target.json (default 1).
active_run_id(){
  python3 - "$TARGET_ROOT/target.json" <<'PY'
import json, sys
try:
    m = json.load(open(sys.argv[1]))
except Exception:
    m = {}
print(int(m.get("active_run_id", 1)))
PY
}

# Pick the run number: RUN_ID unset → active, set → that value.
resolve_run_id(){
  if [[ -n "${RUN_ID:-}" ]]; then
    [[ "$RUN_ID" =~ ^[0-9]+$ ]] || die 'RUN_ID must be a non-negative integer'
  else
    RUN_ID="$(active_run_id)"
  fi
  export RUN_ID
}

# Resolve the run root and prepare directories/state/manifest.
bench_root(){
  [[ -n "${BENCHMARK:-}" ]] || die 'BENCHMARK is not set'
  target_root
  resolve_run_id
  RUN_ROOT="$TARGET_ROOT/run-$RUN_ID"
  mkdir -p "$RUN_ROOT"/{predictions/raw,predictions/canonical,eval/input,eval/raw,logs,archive}
  touch "$RUN_ROOT/state.jsonl"
  [[ -e "$RUN_ROOT/manifest.json" ]] || printf '{"run_id":%s,"benchmark":"%s","dataset":"%s","created_at":"%s","status":"active"}\n' "$RUN_ID" "${BENCHMARK:-unknown}" "${DATASET:-}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$RUN_ROOT/manifest.json"
  # Refresh target.last_run_at
  python3 - "$TARGET_ROOT/target.json" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" <<'PY'
import json, sys
path, ts = sys.argv[1], sys.argv[2]
try:
    m = json.load(open(path))
except Exception:
    m = {}
m["last_run_at"] = ts
json.dump(m, open(path, "w"), ensure_ascii=False, indent=2)
PY
  export RUN_ROOT
}

main_common(){
  LIMIT_NEW=""          # --limit-new / --limit-new-any: how many to try (0/empty = unlimited)
  LIMIT_NEW_OK=0        # --limit-new-ok N: retry until N pass (0 = disabled)
  LIMIT_MAX_TRY=0       # --limit-max-try N: at most N batches (0 = unlimited)
  LIMIT_MODE="any"      # any | ok
  LIMIT_ANY_SET=0       # whether --limit-new/--limit-new-any was given
  LIMIT_OK_SET=0        # whether --limit-new-ok was given
  while (($#)); do
    case "$1" in
      --limit-new|--limit-new-any) (($#>1)) || die 'missing --limit-new value'; LIMIT_NEW="$2"; LIMIT_MODE="any"; LIMIT_ANY_SET=1; shift 2;;
      --limit-new=*|--limit-new-any=*) LIMIT_NEW="${1#*=}"; LIMIT_MODE="any"; LIMIT_ANY_SET=1; shift;;
      --limit-new-ok) (($#>1)) || die 'missing --limit-new-ok value'; LIMIT_NEW="$2"; LIMIT_MODE="ok"; LIMIT_OK_SET=1; shift 2;;
      --limit-new-ok=*) LIMIT_NEW="${1#*=}"; LIMIT_MODE="ok"; LIMIT_OK_SET=1; shift;;
      --limit-max-try) (($#>1)) || die 'missing --limit-max-try value'; LIMIT_MAX_TRY="$2"; shift 2;;
      --limit-max-try=*) LIMIT_MAX_TRY="${1#*=}"; shift;;
      -h|--help)
        cat <<EOF
usage: RUN_ID=N SCRIPT_VER=N $0 [opts]

  RUN_ID     non-negative integer experiment-bundle ID
  SCRIPT_VER non-negative integer config (server/client settings) version
             Both default to the last-used value from $DEFAULT_CACHE_FILE
             (or 1 if neither env nor cache is set).

opts:
  --limit-new[-any] N | --limit-new-ok N
  --limit-max-try N
EOF
        exit 0;;
      *) die "unknown argument: $1";;
    esac
  done
  # Resolve SCRIPT_VER (env > cache > 1) and validate.
  SCRIPT_VER="$(resolve_default SCRIPT_VER 1)"
  [[ "$SCRIPT_VER" =~ ^[0-9]+$ ]] || die "SCRIPT_VER must be a non-negative integer (got: '$SCRIPT_VER')"
  export SCRIPT_VER
  # Resolve RUN_ID (env > cache > 1) BEFORE bench_root so the run directory is
  # correct. resolve_run_id() inside bench_root will only re-validate+export
  # since RUN_ID is already set.
  RUN_ID="$(resolve_default RUN_ID 1)"
  [[ "$RUN_ID" =~ ^[0-9]+$ ]] || die "RUN_ID must be a non-negative integer (got: '$RUN_ID')"
  export RUN_ID
  # Unset → LIMIT_NEW=0 (no attempt cap)
  LIMIT_NEW="${LIMIT_NEW:-0}"
  [[ "$LIMIT_NEW" =~ ^[0-9]+$ ]] || die '--limit-new must be a non-negative integer'
  [[ "$LIMIT_MAX_TRY" =~ ^[0-9]+$ ]] || die '--limit-max-try must be a non-negative integer'
  # --limit-new-ok and --limit-new/--limit-new-any are mutually exclusive
  if (( LIMIT_ANY_SET && LIMIT_OK_SET )); then
    die '--limit-new/--limit-new-any and --limit-new-ok are mutually exclusive'
  fi
  # The results path depends on model/endpoint, so resolve before bench_root.
  resolve_model_target
  bench_root
  # Persist the resolved values for next time. Atomic write so partial files
  # can't confuse a future run.
  write_defaults "$DEFAULT_CACHE_FILE" RUN_ID SCRIPT_VER
}

# Update experiment metadata in manifest.json (existing keys preserved).
# Bench scripts pass the run-time model/server info via env:
#   MODEL_NAME / BENCKKIT_MODEL — model name
#   OPENAI_BASE_URL / BENCKKIT_ENDPOINT — model url
#   SERVER_SCRIPT — server launch script (e.g. run.sh on spark1)
#   SERVER_HOST — server host
update_manifest(){
  local manifest="$RUN_ROOT/manifest.json"
  python3 - "$manifest" "${MODEL_NAME:-${BENCKKIT_MODEL:-}}" \
      "${OPENAI_BASE_URL:-${BENCKKIT_ENDPOINT:-}}" \
      "${SERVER_SCRIPT:-}" "${SERVER_HOST:-}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" <<'PY'
import json, sys
path, model, model_url, server_script, server_host, ts = sys.argv[1:]
try:
    m = json.load(open(path))
except Exception:
    m = {}
m["model"] = model or m.get("model", "")
m["model_url"] = model_url or m.get("model_url", "")
if server_script:
    m["server_script"] = server_script
if server_host:
    m["server_host"] = server_host
m["last_run_at"] = ts
json.dump(m, open(path, "w"), ensure_ascii=False, indent=2)
PY
}

# Set a run comment (web Update button → API → this function).
set_run_comment(){
  local run_id="${1:?run_id required}" comment="${2:-}"
  [[ -n "${TARGET_ROOT:-}" ]] || target_root
  python3 - "$TARGET_ROOT/comments.json" "$run_id" "$comment" <<'PY'
import json, sys
path, run_id, comment = sys.argv[1:]
try:
    c = json.load(open(path))
except Exception:
    c = {}
if comment:
    c[run_id] = comment
else:
    c.pop(run_id, None)
json.dump(c, open(path, "w"), ensure_ascii=False, indent=2)
PY
}

# Preserve the launch script as archive/vNNN-<name>.
#
# SCRIPT_VER is the config (server/client settings) version number, so:
#   - same SCRIPT_VER + identical script → plain rerun; keep the archive, pass.
#   - same SCRIPT_VER + changed script   → config changed; demand a bump.
# Bumping SCRIPT_VER for a plain rerun would pollute the config history.
archive_script(){
  local src="$1" name="$2"
  local ver dst prev
  ver="v$(printf '%03d' "$SCRIPT_VER")"
  dst="$RUN_ROOT/archive/$ver-$name"
  # Existing archive for this version: the new vNNN name, or the legacy
  # tuneNNN name written before the rename.
  prev="$dst"
  [[ -e "$prev" ]] || prev="$RUN_ROOT/archive/tune$(printf '%03d' "$SCRIPT_VER")-$name"
  if [[ -e "$prev" ]]; then
    # Line 1 is the meta header this function prepends; skip it when diffing.
    if diff -q <(tail -n +2 "$prev") "$src" >/dev/null 2>&1; then
      echo "[archive] $(basename "$prev") unchanged — same config, rerun (SCRIPT_VER kept)"
      return 0
    fi
    die "$src differs from archived $(basename "$prev"): config changed, increment SCRIPT_VER"
  fi
  { echo "# RUN_ID=$RUN_ID SCRIPT_VER=$SCRIPT_VER BENCHMARK=${BENCHMARK:-}"; cat "$src"; } > "$dst"
  chmod +x "$dst"
}

# --limit-new-ok / --limit-max-try loop.
# Bench scripts must define these two functions:
#   run_batch()            — run one batch (may try several instances at once)
#   count_new_ok()         — print how many instances newly passed in this batch
#
# Behavior:
#   - LIMIT_MODE=ok  : repeat until LIMIT_NEW passes accumulate
#   - LIMIT_MODE=any : call run_batch once (original --limit-new behavior)
#   - LIMIT_MAX_TRY  : cap on total run_batch calls (0 = unlimited)
run_with_limits(){
  local batches=0
  local ok_total=0
  while :; do
    # Stop once the batch cap is reached
    if (( LIMIT_MAX_TRY > 0 && batches >= LIMIT_MAX_TRY )); then
      log "Stopping: reached --limit-max-try $LIMIT_MAX_TRY batch(es)"
      return 0
    fi
    # ok mode: stop when the target is met
    if [[ "$LIMIT_MODE" == "ok" && "$ok_total" -ge "$LIMIT_NEW" ]]; then
      log "Stopping: reached --limit-new-ok $LIMIT_NEW (ok so far: $ok_total)"
      return 0
    fi
    # any mode: one run, then stop (original --limit-new behavior)
    if [[ "$LIMIT_MODE" == "any" && "$batches" -ge 1 ]]; then
      return 0
    fi
    (( batches += 1 ))
    log "=== batch $batches (ok so far: $ok_total) ==="
    # A failed batch (model/verifier error) moves on to the next one
    run_batch || log "batch $batches finished with errors; continuing"
    local got
    got="$(count_new_ok)"
    ok_total=$(( ok_total + got ))
  done
}
