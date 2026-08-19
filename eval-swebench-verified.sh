#!/usr/bin/env bash
# eval.sh — 공식 SWE-bench evaluator 연결 (로컬 Docker)
#
# Usage:
#   RUN_ID=1 ./eval.sh
#
# 동작:
#   - results/run-$RUN_ID/predictions/canonical/predictions.jsonl 을 공식
#     swebench harness 에 넘겨 평가한다.
#   - 이미 평가된 인스턴스는 건너뛰고 미평가 인스턴스만 평가 (증분).
#   - harness 출력(로그/테스트 결과)은 results/run-$RUN_ID/eval/raw/ 로,
#     최종 요약은 eval/summary.json, eval/breakdown.json 으로 생성.
#
# Environment:
#   RUN_ID   — 필수, 양의 정수
#   MAX_WORKERS — 기본 4
#   TIMEOUT  — 인스턴스당 테스트 타임아웃 (초), 기본 1800

set -Eeuo pipefail
cd "$(cd "$(dirname "$0")" && pwd)"

# shellcheck source=./benchmark-lib.sh
source ./benchmark-lib.sh

BENCHMARK="swebench-verified"
DATASET="${DATASET:-SWE-bench/SWE-bench_Verified}"

# eval-swebench-verified.sh 는 --limit-new/TUNE_NO 가 필요 없으므로 기본값을 인자로 채워 검증을 통과시킨다.
TUNE_NO="${TUNE_NO:-0}"
main_common --limit-new 1

RUN_DIR="$RUN_ROOT"
CANON_DIR="$RUN_DIR/predictions/canonical"
EVAL_RAW_DIR="$RUN_DIR/eval/raw"
EVAL_INPUT_DIR="$RUN_DIR/eval/input"
EVAL_DIR="$RUN_DIR/eval"
mkdir -p "$EVAL_RAW_DIR" "$EVAL_INPUT_DIR"

PRED_JSONL="$CANON_DIR/predictions.jsonl"
if [[ ! -s "$PRED_JSONL" ]]; then
    echo "ERROR: $PRED_JSONL missing or empty. Run start-swebench-verified.sh first." >&2
    exit 1
fi

MAX_WORKERS="${MAX_WORKERS:-4}"
TIMEOUT="${TIMEOUT:-1800}"

log(){ echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# ── 증분 평가 대상 계산: eval/raw 의 harness 리포트에 이미 포함된 인스턴스는 제외 ──
python3 - "$PRED_JSONL" "$EVAL_RAW_DIR" > /tmp/swebv-eval-pending.jsonl <<'PY'
import json, os, sys
preds_path, eval_raw = sys.argv[1], sys.argv[2]

done = set()
if os.path.isdir(eval_raw):
    for fname in os.listdir(eval_raw):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(eval_raw, fname)) as f:
                rep = json.load(f)
        except Exception:
            continue
        for key in ("completed_ids", "submitted_ids", "resolved_ids", "unresolved_ids"):
            for iid in rep.get(key) or []:
                done.add(iid)
    # 레거시: per-instance report.json 스캔
    for root, _, files in os.walk(eval_raw):
        if "report.json" in files:
            try:
                with open(os.path.join(root, "report.json")) as f:
                    for k in json.load(f):
                        done.add(k)
            except Exception:
                pass

pending = []
with open(preds_path) as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        if rec["instance_id"] not in done:
            pending.append(rec)
for rec in pending:
    print(json.dumps(rec, ensure_ascii=False))
print(f"# total={sum(1 for _ in open(preds_path))} pending={len(pending)}", file=sys.stderr)
PY

PENDING_COUNT=$(grep -cv '^#' /tmp/swebv-eval-pending.jsonl || true)
if [[ "$PENDING_COUNT" -eq 0 ]]; then
    log "Nothing to evaluate — all instances already evaluated"
else
    log "Evaluating $PENDING_COUNT instance(s) via swebench harness (max_workers=$MAX_WORKERS, timeout=${TIMEOUT}s)"

    # ── 공식 swebench harness 실행 (uv run --with swebench) ──
    RUN_ID_ARG="benchkit-run-$RUN_ID"
    set -x
    uv run --with swebench python -m swebench.harness.run_evaluation \
        --dataset_name "SWE-bench/SWE-bench_Verified" \
        --split test \
        --predictions_path /tmp/swebv-eval-pending.jsonl \
        --max_workers "$MAX_WORKERS" \
        --timeout "$TIMEOUT" \
        --run_id "$RUN_ID_ARG" \
        --report_dir "$EVAL_RAW_DIR"
    set +x

    log "Harness evaluation done. Outputs in $EVAL_RAW_DIR"
fi

# ── summary.json / breakdown.json 생성 ──
python3 - "$EVAL_RAW_DIR" "$EVAL_DIR" "$PRED_JSONL" <<'PY'
import json, os, sys
eval_raw, eval_dir, preds_path = sys.argv[1], sys.argv[2], sys.argv[3]

# harness 단일 리포트 파일 (openai__<model>.<run_id>.json) 또는 per-instance report.json
reports = {}
harness_report = None
if os.path.isdir(eval_raw):
    for fname in os.listdir(eval_raw):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(eval_raw, fname)) as f:
                rep = json.load(f)
        except Exception:
            continue
        if isinstance(rep, dict) and ("resolved_ids" in rep or "submitted_ids" in rep):
            harness_report = rep
        for root, _, files in os.walk(eval_raw):
            if "report.json" in files:
                try:
                    with open(os.path.join(root, "report.json")) as f:
                        rdata = json.load(f)
                    for iid, res in rdata.items():
                        reports[iid] = res
                except Exception:
                    pass

resolved = []
unresolved = []
missing = []
if harness_report is not None:
    resolved = list(harness_report.get("resolved_ids") or [])
    unresolved = list(harness_report.get("unresolved_ids") or [])
    missing = list(harness_report.get("empty_patch_ids") or [])
    # harness 리포트의 resolved/unresolved 를 per-instance report 로 채움
    for iid in resolved:
        reports.setdefault(iid, {"resolved": True})
    for iid in unresolved:
        reports.setdefault(iid, {"resolved": False})
    for iid in missing:
        reports.setdefault(iid, {"resolved": None})
else:
    for iid, res in reports.items():
        if res.get("resolved") is True:
            resolved.append(iid)
        elif res.get("resolved") is False:
            unresolved.append(iid)
        else:
            missing.append(iid)

pred_ids = []
with open(preds_path) as f:
    for line in f:
        line = line.strip()
        if line:
            pred_ids.append(json.loads(line)["instance_id"])
not_evaluated = [iid for iid in pred_ids if iid not in reports]

summary = {
    "run_id": os.environ.get("RUN_ID"),
    "total_predicted": len(pred_ids),
    "resolved": len(resolved),
    "unresolved": len(unresolved),
    "missing": len(missing),
    "not_evaluated": len(not_evaluated),
    "resolved_ids": sorted(resolved),
    "unresolved_ids": sorted(unresolved),
    "missing_ids": sorted(missing),
    "not_evaluated_ids": sorted(not_evaluated),
}
with open(os.path.join(eval_dir, "summary.json"), "w") as f:
    json.dump(summary, f, indent=2, ensure_ascii=False)
with open(os.path.join(eval_dir, "breakdown.json"), "w") as f:
    json.dump(reports, f, indent=2, ensure_ascii=False)

print(f"summary: resolved={len(resolved)} unresolved={len(unresolved)} "
      f"missing={len(missing)} not_evaluated={len(not_evaluated)} (of {len(pred_ids)} predicted)")
print(f"wrote {eval_dir}/summary.json and {eval_dir}/breakdown.json")
PY

log "Done. Next: RUN_ID=$RUN_ID ./report.sh"
