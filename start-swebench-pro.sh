#!/usr/bin/env bash
# start-swebench-pro.sh — run SWE-bench Pro client (local WSL2)
#
# Usage:
#   RUN_ID=1 SCRIPT_VER=1 PARALLELISM=2 ./start-swebench-pro.sh --limit-new 1
#
# Behavior:
#   - Solves real SWE-bench Pro instances via mini-swe-agent (litellm).
#   - Uses local parquet (data/swebench-pro/swebench-pro.parquet) and injects
#     dockerhub_tag as the docker_image column so mini-swe-agent launches the
#     correct Docker Hub image (jefzda/sweap-images).
#   - preds.json (raw) → predictions.jsonl (canonical) conversion.
#   - Completed instances are skipped; new instances run up to --limit-new N.
#   - Launch script archived under results/run-$RUN_ID/archive/ (archive/vNNN-...).
#
# Environment:
#   RUN_ID            — non-negative integer. Defaults to the last-used value
#                       from .cache/start-swebench-pro.sh.env, or 1.
#   SCRIPT_VER        — non-negative integer. Config (server/client settings)
#                       version number. Defaults to the last-used cache value,
#                       or 1. Increment only when the config changes; plain
#                       reruns keep the same value.
#   PARALLELISM       — worker count (default 2)
#   OPENAI_BASE_URL   — default http://spark1.local:30000/v1
#   OPENAI_API_KEY    — default none
#   MODEL_NAME        — explicit model name (autodetected via /v1/models if unset)
#   PYTHON            — mini-swe-agent invocation command (autodetected if unset)

set -Eeuo pipefail
cd "$(cd "$(dirname "$0")" && pwd)"

# shellcheck source=./benchmark-lib.sh
source ./benchmark-lib.sh

BENCHMARK="swebench-pro"
DATASET="${DATASET:-ScaleAI/SWE-bench_Pro}"
# Local dataset directory readable by mini-swe-agent.load_dataset().
# If absent, fetch from HF and materialize as <root>/train/ (dataset_info.json).
PRO_LOCAL="${PRO_LOCAL:-data/swebench-pro/pro-local}"
PRO_LOCAL_ABS="$(cd "$(dirname "$PRO_LOCAL")" 2>/dev/null && pwd)/$(basename "$PRO_LOCAL")"

# target_key needs model/url, so resolve them before main_common(→ bench_root).
PARALLELISM="${PARALLELISM:-2}"
export OPENAI_BASE_URL="${OPENAI_BASE_URL:-http://spark1.local:30000/v1}"
export OPENAI_API_KEY="${OPENAI_API_KEY:-none}"
export MSWEA_COST_TRACKING="${MSWEA_COST_TRACKING:-ignore_errors}"

# SWE-bench Pro images (jefzda/sweap-images) declare ENTRYPOINT=/bin/bash, so
# mini-swe-agent's `docker run ... <image> sleep 2h` fails. The wrapper
# bin/docker-pro-entrypoint-fix.sh injects --entrypoint /usr/bin/sleep.
if [[ -z "${MSWEA_DOCKER_EXECUTABLE:-}" ]]; then
    export MSWEA_DOCKER_EXECUTABLE="$(pwd)/bin/docker-pro-entrypoint-fix.sh"
fi

# (log() is provided by benchmark-lib.sh)

# ── Model resolution: autodetect via /v1/models ──
if [[ -z "${MODEL_NAME:-}" ]]; then
    log "Auto-discovering model from $OPENAI_BASE_URL/models ..."
    MODEL_NAME="$(curl -sf --max-time 10 "$OPENAI_BASE_URL/models" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["data"][0]["id"])' 2>/dev/null || true)"
    [[ -n "$MODEL_NAME" ]] || die "no model exposed at $OPENAI_BASE_URL — server up?"
fi
export MODEL_NAME

# main_common parses --limit-new and validates RUN_ID / SCRIPT_VER / LIMIT_NEW.
main_common "$@"

# ── Local dataset prep (HF → add docker_image column → save_to_disk) ──
if [[ ! -d "$PRO_LOCAL_ABS/train" ]]; then
    log "Preparing local Pro dataset at $PRO_LOCAL_ABS ..."
    python3 - "$PRO_LOCAL_ABS" <<'PY'
import os, sys
import pandas as pd
from datasets import load_dataset

out_root = sys.argv[1]
ds = load_dataset("ScaleAI/SWE-bench_Pro", split="test")
df = ds.to_pandas()
df["docker_image"] = df["dockerhub_tag"]  # field mini-swe-agent reads
# Python instances first (qutebrowser etc.), other languages keep original order
df["_lang_rank"] = (df["repo_language"] != "python").astype(int)
df = df.sort_values("_lang_rank", kind="stable").drop(columns=["_lang_rank"])
from datasets import Dataset
Dataset.from_pandas(df).save_to_disk(os.path.join(out_root, "train"))
print(f"wrote {len(df)} instances -> {out_root}/train")
PY
fi
[[ -d "$PRO_LOCAL_ABS/train" ]] || die "local Pro dataset missing at $PRO_LOCAL_ABS/train"

log "model=$MODEL_NAME run_id=$RUN_ID script_ver=$SCRIPT_VER parallelism=$PARALLELISM limit_new=$LIMIT_NEW"

# Record experiment metadata in manifest.json (web dashboard display).
export SERVER_SCRIPT="${SERVER_SCRIPT:-~/git/dgx-spark-qwen38/run.sh}"
export SERVER_HOST="${SERVER_HOST:-spark1.local}"
update_manifest

# ── Python (mini-swe-agent) invocation ──
if [[ -z "${PYTHON:-}" ]]; then
    if python3 -c 'import minisweagent' 2>/dev/null; then
        PYTHON="python3"
    elif mise x -- python -c 'import minisweagent' 2>/dev/null; then
        PYTHON="mise x -- python"
    else
        PYTHON="uv run --with mini-swe-agent --python 3.14 python"
    fi
fi

# ── Output directory: results/run-$RUN_ID/swebench-pro/predictions/raw ──
#    (main_common computes the BENCHMARK-based RUN_ROOT.)
RUN_DIR="$RUN_ROOT"
RAW_DIR="$RUN_DIR/predictions/raw"
mkdir -p "$RAW_DIR"
RUN_LEDGER="$RUN_DIR/state.jsonl"

# ── Number of already-completed instances (solved records in state.jsonl) ──
count_solved(){
  python3 - "$RUN_LEDGER" <<'PY'
import json, sys
ids = set()
try:
    for line in open(sys.argv[1]):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("stage") == "solved":
            ids.add(rec["instance_id"])
except FileNotFoundError:
    pass
print(len(ids))
PY
}
DONE_COUNT=$(count_solved)

# ── archive: canonical launch script ──
archive_script "$0" "start-swebench-pro.sh"

if [[ "$LIMIT_MODE" == "ok" ]]; then
  TRY_COUNT="$LIMIT_NEW"
else
  TRY_COUNT="${LIMIT_NEW:-0}"
fi

# ── Single batch ──
# Completed instances are auto-skipped, so the slice is set generously
# (done + try_n).
run_batch(){
  local done_before
  done_before=$(count_solved)
  local try_n=1
  if (( TRY_COUNT > 0 )); then try_n=$TRY_COUNT; fi
  local slice_end=$(( done_before + try_n ))
  log "Done so far: $done_before. Running up to $try_n new instance(s) (slice 0:$slice_end, existing auto-skipped)..."

  pushd "$RAW_DIR" >/dev/null
  set -x
  $PYTHON -m minisweagent.run.benchmarks.swebench \
      --subset "$PRO_LOCAL_ABS" \
      --split train \
      --model "openai/${MODEL_NAME}" \
      --output . \
      -w "$PARALLELISM" \
      --slice "0:${slice_end}"
  set +x
  popd >/dev/null

  # ── Append completion records to state.jsonl ──
  # patch present → solved; empty patch (agent failure) → failed.
  PREDS_JSON="$RAW_DIR/preds.json"
  if [[ -s "$PREDS_JSON" ]]; then
      python3 - "$PREDS_JSON" "$RUN_LEDGER" "$MODEL_NAME" <<'PY'
import json, sys
preds_path, ledger, model = sys.argv[1], sys.argv[2], sys.argv[3]
with open(preds_path) as f:
    preds = json.load(f)
existing = {}
try:
    for line in open(ledger):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        existing[rec["instance_id"]] = rec.get("stage")
except FileNotFoundError:
    pass
with open(ledger, "a") as f:
    for iid, p in sorted(preds.items()):
        if iid in existing:
            continue
        patch = p.get("model_patch") or ""
        stage = "solved" if patch.strip() else "failed"
        f.write(json.dumps({
            "stage": stage,
            "instance_id": iid,
            "model": p.get("model_name_or_path", model),
            "ts": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }, ensure_ascii=False) + "\n")
PY
      log "Wrote $(python3 -c "import json;print(len(json.load(open('$PREDS_JSON'))))") instance(s) to $RUN_LEDGER"
  fi

  # ── Convert to canonical predictions.jsonl ──
  CANON_DIR="$RUN_DIR/predictions/canonical"
  mkdir -p "$CANON_DIR"
  python3 - "$PREDS_JSON" "$CANON_DIR/predictions.jsonl" <<'PY'
import json, sys
preds_path, out_path = sys.argv[1], sys.argv[2]
if not __import__("os").path.exists(preds_path) or __import__("os").path.getsize(preds_path) == 0:
    print("no predictions to convert (preds.json missing/empty)", file=__import__("sys").stderr)
    raise SystemExit(0)
with open(preds_path) as f:
    preds = json.load(f)
with open(out_path, "w") as f:
    for iid, p in sorted(preds.items()):
        f.write(json.dumps(p, ensure_ascii=False) + "\n")
print(f"wrote {len(preds)} predictions -> {out_path}")
PY
}

# Newly solved in this batch (success criterion: patch generated).
_LAST_OK_COUNT=$DONE_COUNT
count_new_ok(){
  local now
  now=$(count_solved)
  echo $(( now - _LAST_OK_COUNT ))
  _LAST_OK_COUNT=$now
}

run_with_limits

log "Done. Next: RUN_ID=$RUN_ID ./eval-swebench-pro.sh"
