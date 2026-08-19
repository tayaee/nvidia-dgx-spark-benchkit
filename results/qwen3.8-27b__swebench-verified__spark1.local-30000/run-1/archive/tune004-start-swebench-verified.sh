# RUN_ID=1 TUNE_NO=4 BENCHMARK=swebench-verified
#!/usr/bin/env bash
# start-swebench-verified.sh — SWE-bench Verified 클라이언트 실행 (로컬 WSL2)
#
# Usage:
#   RUN_ID=1 TUNE_NO=1 PARALLELISM=2 ./start-swebench-verified.sh --limit-new 2
#
# 동작:
#   - mini-swe-agent (litellm 기반) 로 실제 SWE-bench Verified 인스턴스를 푼다.
#   - 모델/엔드포인트는 /v1/models 자동 탐지 (기본 spark1.local:30000)
#   - preds.json(raw) → predictions.jsonl(canonical) 변환
#   - 이미 완료된 인스턴스는 건너뛰고, --limit-new N 만큼 새로 푼다.
#   - 실행 스크립트는 results/run-$RUN_ID/archive/ 에 TUNE_NO 로 보존.
#
# Environment:
#   RUN_ID            — 필수, 양의 정수
#   TUNE_NO           — 필수, 양의 정수 (archive 충돌 시 증가 요구)
#   PARALLELISM       — worker 수 (기본 2)
#   OPENAI_BASE_URL   — 기본 http://spark1.local:30000/v1
#   OPENAI_API_KEY    — 기본 none
#   MODEL_NAME        — 명시적 모델명 (없으면 /v1/models 자동 탐지)
#   PYTHON            — mini-swe-agent 실행 커맨드 (기본 자동 탐지)

set -Eeuo pipefail
cd "$(cd "$(dirname "$0")" && pwd)"

# shellcheck source=./benchmark-lib.sh
source ./benchmark-lib.sh

BENCHMARK="swebench-verified"
DATASET="${DATASET:-SWE-bench/SWE-bench_Verified}"

# target 키 계산에 모델명/URL이 필요하므로 main_common(→ bench_root) 보다 먼저 결정한다.
PARALLELISM="${PARALLELISM:-2}"
export OPENAI_BASE_URL="${OPENAI_BASE_URL:-http://spark1.local:30000/v1}"
export OPENAI_API_KEY="${OPENAI_API_KEY:-none}"
export MSWEA_COST_TRACKING="${MSWEA_COST_TRACKING:-ignore_errors}"

log(){ echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# ── 모델 결정: /v1/models 자동 탐지 ──
if [[ -z "${MODEL_NAME:-}" ]]; then
    log "Auto-discovering model from $OPENAI_BASE_URL/models ..."
    MODEL_NAME="$(curl -sf --max-time 10 "$OPENAI_BASE_URL/models" | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["data"][0]["id"])' 2>/dev/null || true)"
    [[ -n "$MODEL_NAME" ]] || die "no model exposed at $OPENAI_BASE_URL — server up?"
fi
export MODEL_NAME

# main_common 이 --limit-new 파싱 + RUN_ID/TUNE_NO/LIMIT_NEW 검증을 수행한다.
main_common "$@"
log "model=$MODEL_NAME run_id=$RUN_ID tune_no=$TUNE_NO parallelism=$PARALLELISM limit_new=$LIMIT_NEW"

# 실험 메타데이터를 manifest.json 에 기록 (웹 대시보드 표시용)
export SERVER_SCRIPT="${SERVER_SCRIPT:-~/git/dgx-spark-qwen38/run.sh}"
export SERVER_HOST="${SERVER_HOST:-spark1.local}"
update_manifest

# ── Python (mini-swe-agent) 실행 커맨드 결정 ──
if [[ -z "${PYTHON:-}" ]]; then
    if python3 -c 'import minisweagent' 2>/dev/null; then
        PYTHON="python3"
    elif mise x -- python -c 'import minisweagent' 2>/dev/null; then
        PYTHON="mise x -- python"
    else
        PYTHON="uv run --with mini-swe-agent --python 3.14 python"
    fi
fi

# ── 실행 디렉터리: results/run-$RUN_ID/swebench-verified/predictions/raw ──
#    (main_common 이 BENCHMARK 기반 RUN_ROOT 를 계산해 둔다)
RUN_DIR="$RUN_ROOT"
RAW_DIR="$RUN_DIR/predictions/raw"
mkdir -p "$RAW_DIR"
RUN_LEDGER="$RUN_DIR/state.jsonl"

# ── 이미 완료된 인스턴스 수 파악 (state.jsonl 의 solved 레코드) ──
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

# ── archive: canonical 스크립트 보존 ──
archive_script "$0" "start-swebench-verified.sh"

# ── 1회 배치 실행 ──
#    mini-swe-agent 는 --redo-existing 없이 이미 푼 인스턴스(출력 디렉터리에
#    instance 디렉터리가 있는 것)를 자동으로 건너뛴다. slice 를
#    완료수 + 시도수 만큼 넉넉히 잡아, 완료분은 스킵하고 새 인스턴스를 푼다.
#    LIMIT_MODE=any 면 LIMIT_NEW 개, ok 모드면 LIMIT_NEW_OK 개 목표로 시도.
if [[ "$LIMIT_MODE" == "ok" ]]; then
  TRY_COUNT="$LIMIT_NEW"
else
  TRY_COUNT="${LIMIT_NEW:-0}"
fi

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
      --subset "SWE-bench/SWE-bench_Verified" \
      --split test \
      --model "openai/${MODEL_NAME}" \
      --output . \
      -w "$PARALLELISM" \
      --slice "0:${slice_end}"
  set +x
  popd >/dev/null

  # ── state.jsonl 에 완료 레코드 기록 ──
  PREDS_JSON="$RAW_DIR/preds.json"
  if [[ -s "$PREDS_JSON" ]]; then
      python3 - "$PREDS_JSON" "$RUN_LEDGER" "$MODEL_NAME" <<'PY'
import json, sys
preds_path, ledger, model = sys.argv[1], sys.argv[2], sys.argv[3]
with open(preds_path) as f:
    preds = json.load(f)
existing = set()
try:
    for line in open(ledger):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except Exception:
            continue
        if rec.get("stage") == "solved":
            existing.add(rec["instance_id"])
except FileNotFoundError:
    pass
with open(ledger, "a") as f:
    for iid, p in sorted(preds.items()):
        if iid in existing:
            continue
        f.write(json.dumps({
            "stage": "solved",
            "instance_id": iid,
            "model": p.get("model_name_or_path", model),
            "ts": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }, ensure_ascii=False) + "\n")
PY
      log "Wrote $(python3 -c "import json;print(len(json.load(open('$PREDS_JSON'))))") instance(s) to $RUN_LEDGER"
  fi

  # ── canonical predictions.jsonl 변환 ──
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

# 이번 배치에서 새로 solved 된 수 (성공 판정)
_LAST_OK_COUNT=$DONE_COUNT
count_new_ok(){
  local now
  now=$(count_solved)
  echo $(( now - _LAST_OK_COUNT ))
  _LAST_OK_COUNT=$now
}

run_with_limits

log "Done. Next: RUN_ID=$RUN_ID ./eval-swebench-verified.sh"
