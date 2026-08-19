# RUN_ID=1 TUNE_NO=2 BENCHMARK=terminal-bench-2.0
#!/usr/bin/env bash
# start-terminal-bench-2.0.sh — Terminal-Bench 2.0 실행 (로컬 WSL2)
#
# Usage:
#   RUN_ID=1 TUNE_NO=1 ./start-terminal-bench-2.0.sh --limit-new 1
#
# 동작:
#   - harborframework/terminal-bench-2.0 (HF) 에서 태스크를 가져와 실제로 푼다.
#   - 모델은 spark1.local:30000 의 qwen3.8-27b (자동 탐지).
#   - 각 태스크는 전용 docker 이미지에서 검증 (tests/test.sh + test_outputs.py).
#   - 완료된 인스턴스는 .solved 트래커로 건너뛰고, --limit-new N 만큼 새로 푼다.
#   - 실행 스크립트는 results/run-$RUN_ID/terminal-bench-2.0/archive/ 에 보존.
#
# Environment:
#   RUN_ID            — 필수, 양의 정수
#   TUNE_NO           — 필수, 양의 정수 (archive 충돌 시 증가 요구)
#   OPENAI_BASE_URL / BENCKKIT_ENDPOINT — 기본 http://spark1.local:30000/v1
#   MODEL_NAME / BENCKKIT_MODEL         — 기본 qwen3.8-27b (자동 탐지)

set -Eeuo pipefail
cd "$(cd "$(dirname "$0")" && pwd)"

# shellcheck source=./benchmark-lib.sh
source ./benchmark-lib.sh

BENCHMARK="terminal-bench-2.0"
DATASET="${DATASET:-harborframework/terminal-bench-2.0}"

# main_common 이 --limit-new 파싱 + RUN_ID/TUNE_NO/LIMIT_NEW 검증을 수행한다.
main_common "$@"

export BENCKKIT_ENDPOINT="${BENCKKIT_ENDPOINT:-http://spark1.local:30000/v1}"
export BENCKKIT_MODEL="${BENCKKIT_MODEL:-qwen3.8-27b}"
# smoke.py 는 results/<run_id>/<benchmark>/ 구조를 쓰므로,
# run_id=run-$RUN_ID 로 두면 벤치별 레이아웃(results/run-1/terminal-bench-2.0/)과 일치한다.
export BENCKKIT_RUN_ID="${BENCKKIT_RUN_ID:-run-$RUN_ID}"
export BENCKKIT_RESULTS="${BENCKKIT_RESULTS:-$PWD/results}"

log(){ echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# ── archive: canonical 스크립트 보존 ──
archive_script "$0" "start-terminal-bench-2.0.sh"

SOLVED_FILE="results/.solved/terminal-bench-2.0.solved"
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

# ── 1회 배치 실행: smoke.py (실제 inference + docker 검증) ──
run_batch(){
  local try_n=1
  if (( TRY_COUNT > 0 )); then try_n=$TRY_COUNT; fi
  log "model=$BENCKKIT_MODEL endpoint=$BENCKKIT_ENDPOINT run_root=$RUN_ROOT trying up to $try_n new instance(s)"
  # 결과는 results/run-$RUN_ID/terminal-bench-2.0/ 에 쌓인다.
  ./.venv/bin/python bin/smoke.py "$BENCHMARK" --limit-new "$try_n"
}

# 이번 배치에서 새로 PASS(성공)된 수 — smoke.py 는 PASS 시 .solved 트래커에 기록한다
_LAST_OK_COUNT=$DONE_COUNT
count_new_ok(){
  local now
  now=$(count_solved)
  echo $(( now - _LAST_OK_COUNT ))
  _LAST_OK_COUNT=$now
}

run_with_limits

log "Done."
