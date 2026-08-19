#!/usr/bin/env bash
set -Eeuo pipefail
die(){ echo "ERROR: $*" >&2; exit 2; }

# ──────────────────────────────────────────────────────────────
# 타깃(테스트 목표) = bench official name + model name + model url
# results/
# └── <target-key>/                  예: qwen3.8-27b__swebench-verified__spark1.local-30000
#     ├── target.json                { bench, model, model_url, active_run_id, last_run_at }
#     ├── comments.json              { "run-1": "...", ... }
#     └── run-1/, run-2/, ...        ← 벤치 결과 누적
#
# RUN_ID 미지정 → target 의 active_run_id 자동 사용.
# RUN_ID 명시   → 해당 run 재개 (없으면 생성).
# New Run(웹, 미래) → active_run_id += 1.
# ──────────────────────────────────────────────────────────────

# target 키 계산. BENCHMARK / MODEL_NAME / OPENAI_BASE_URL 이 필요하다.
# url 은 호스트:포트 형태로 줄이고, 나머지는 안전한 파일명 문자만 남긴다.
target_key(){
  local model="${MODEL_NAME:-${BENCKKIT_MODEL:-unknown}}"
  local url="${OPENAI_BASE_URL:-${BENCKKIT_ENDPOINT:-unknown}}"
  # http://spark1.local:30000/v1 → spark1.local-30000
  local host_port
  host_port="$(printf '%s' "$url" | sed -E 's#^[a-z]+://##; s#/.*$##; s#[:/]#-#g')"
  printf '%s__%s__%s' "$model" "$BENCHMARK" "$host_port" \
    | tr ' /' '__' | tr -cd 'A-Za-z0-9_.-'
}

# target 디렉터리 경로 결정 (존재하면 반환, 없으면 생성 + target.json 초기화)
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

# active_run_id 를 target.json 에서 읽는다 (없으면 1).
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

# run 번호 결정: RUN_ID 미지정 → active, 명시 → 해당 값.
resolve_run_id(){
  if [[ -n "${RUN_ID:-}" ]]; then
    [[ "$RUN_ID" =~ ^[0-9]+$ ]] || die 'RUN_ID must be a non-negative integer'
  else
    RUN_ID="$(active_run_id)"
  fi
  export RUN_ID
}

# run 루트 결정 + 디렉터리/state/manifest 준비.
bench_root(){
  [[ -n "${BENCHMARK:-}" ]] || die 'BENCHMARK is not set'
  target_root
  resolve_run_id
  RUN_ROOT="$TARGET_ROOT/run-$RUN_ID"
  mkdir -p "$RUN_ROOT"/{predictions/raw,predictions/canonical,eval/input,eval/raw,logs,archive}
  touch "$RUN_ROOT/state.jsonl"
  [[ -e "$RUN_ROOT/manifest.json" ]] || printf '{"run_id":%s,"benchmark":"%s","dataset":"%s","created_at":"%s","status":"active"}\n' "$RUN_ID" "${BENCHMARK:-unknown}" "${DATASET:-}" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$RUN_ROOT/manifest.json"
  # target.last_run_at 갱신
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
  LIMIT_NEW=""          # --limit-new / --limit-new-any: 몇 개 시도할지 (0/비어있으면 무제한)
  LIMIT_NEW_OK=0        # --limit-new-ok N: 성공이 N개 나올 때까지 시도 (0 = 비활성)
  LIMIT_MAX_TRY=0       # --limit-max-try N: 최대 N번 시도 (0 = 무제한)
  LIMIT_MODE="any"      # any | ok
  LIMIT_ANY_SET=0       # --limit-new/--limit-new-any 지정 여부
  LIMIT_OK_SET=0        # --limit-new-ok 지정 여부
  while (($#)); do
    case "$1" in
      --limit-new|--limit-new-any) (($#>1)) || die 'missing --limit-new value'; LIMIT_NEW="$2"; LIMIT_MODE="any"; LIMIT_ANY_SET=1; shift 2;;
      --limit-new=*|--limit-new-any=*) LIMIT_NEW="${1#*=}"; LIMIT_MODE="any"; LIMIT_ANY_SET=1; shift;;
      --limit-new-ok) (($#>1)) || die 'missing --limit-new-ok value'; LIMIT_NEW="$2"; LIMIT_MODE="ok"; LIMIT_OK_SET=1; shift 2;;
      --limit-new-ok=*) LIMIT_NEW="${1#*=}"; LIMIT_MODE="ok"; LIMIT_OK_SET=1; shift;;
      --limit-max-try) (($#>1)) || die 'missing --limit-max-try value'; LIMIT_MAX_TRY="$2"; shift 2;;
      --limit-max-try=*) LIMIT_MAX_TRY="${1#*=}"; shift;;
      -h|--help) echo "usage: RUN_ID=N TUNE_NO=N $0 [--limit-new[-any] N | --limit-new-ok N] [--limit-max-try N]"; exit 0;;
      *) die "unknown argument: $1";;
    esac
  done
  [[ "${TUNE_NO:-}" =~ ^[0-9]+$ ]] || die 'TUNE_NO is required'
  # 미지정 시 LIMIT_NEW=0 (시도 제한 없음)
  LIMIT_NEW="${LIMIT_NEW:-0}"
  [[ "$LIMIT_NEW" =~ ^[0-9]+$ ]] || die '--limit-new must be a non-negative integer'
  [[ "$LIMIT_MAX_TRY" =~ ^[0-9]+$ ]] || die '--limit-max-try must be a non-negative integer'
  # --limit-new-ok 와 --limit-new/--limit-new-any 는 상호배타: 둘 다 지정하면 에러
  if (( LIMIT_ANY_SET && LIMIT_OK_SET )); then
    die '--limit-new/--limit-new-any and --limit-new-ok are mutually exclusive'
  fi
  bench_root
}

# manifest.json 에 실험 메타데이터를 갱신한다 (기존 키는 보존).
# 벤치 스크립트는 실행 시점의 모델/서버 정보를 env 로 넘겨준다:
#   MODEL_NAME / BENCKKIT_MODEL — 모델명
#   OPENAI_BASE_URL / BENCKKIT_ENDPOINT — 모델 URL
#   SERVER_SCRIPT — 서버 기동 스크립트 이름 (spark1 의 run.sh 등)
#   SERVER_HOST — 서버 호스트
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

# run 주석 설정 (웹 Update 버튼 → API → 이 함수)
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

archive_script(){
  local src="$1" name="$2"
  local dst="$RUN_ROOT/archive/tune$(printf '%03d' "$TUNE_NO")-$name"
  [[ ! -e "$dst" ]] || die "archive exists: $dst; increment TUNE_NO"
  { echo "# RUN_ID=$RUN_ID TUNE_NO=$TUNE_NO BENCHMARK=${BENCHMARK:-}"; cat "$src"; } > "$dst"
  chmod +x "$dst"
}

# --limit-new-ok / --limit-max-try 루프.
# 벤치 스크립트는 다음 두 함수를 정의해야 한다:
#   run_batch()            — 1회 배치 실행 (여러 인스턴스를 동시에 시도)
#   count_new_ok()         — 이번 배치에서 새로 "성공"한 인스턴스 수를 stdout 으로 출력
#
# 동작:
#   - LIMIT_MODE=ok  : 성공이 LIMIT_NEW 개 쌓일 때까지 반복 (run_batch 호출)
#   - LIMIT_MODE=any : run_batch 를 1회 호출 (기존 --limit-new 동작)
#   - LIMIT_MAX_TRY  : run_batch 총 호출 수 상한 (0 = 무제한)
run_with_limits(){
  local batches=0
  local ok_total=0
  while :; do
    # 최대 시도 횟수 초과 시 중단
    if (( LIMIT_MAX_TRY > 0 && batches >= LIMIT_MAX_TRY )); then
      log "Stopping: reached --limit-max-try $LIMIT_MAX_TRY batch(es)"
      return 0
    fi
    # ok 모드에서 목표 달성 시 중단
    if [[ "$LIMIT_MODE" == "ok" && "$ok_total" -ge "$LIMIT_NEW" ]]; then
      log "Stopping: reached --limit-new-ok $LIMIT_NEW (ok so far: $ok_total)"
      return 0
    fi
    # any 모드: 1회 실행 후 중단 (기존 --limit-new 동작)
    if [[ "$LIMIT_MODE" == "any" && "$batches" -ge 1 ]]; then
      return 0
    fi
    (( batches += 1 ))
    log "=== batch $batches (ok so far: $ok_total) ==="
    # 배치 실패(모델/검증 오류 등)는 다음 배치로 넘어간다
    run_batch || log "batch $batches finished with errors; continuing"
    local got
    got="$(count_new_ok)"
    ok_total=$(( ok_total + got ))
  done
}
