#!/usr/bin/env bash
# eval-swebench-pro.sh — 공식 SWE-bench Pro evaluator 연결 (로컬 Docker)
#
# Usage:
#   RUN_ID=1 ./eval-swebench-pro.sh
#
# 동작:
#   - ScaleAI/SWE-bench_Pro 의 공식 평가 스크립트(swe_bench_pro_eval.py)를
#     --use_local_docker 로 실행한다.
#   - raw sample(instance_id, before_repo_set_cmd, selected_test_files_to_run,
#     base_commit, FAIL_TO_PASS, PASS_TO_PASS 등)은 HF 데이터셋에서 생성.
#   - patch 는 results/run-$RUN_ID/swebench-pro/predictions/canonical/predictions.jsonl
#     에서 읽어 {instance_id, patch} 목록으로 변환.
#   - Docker Hub 이미지: jefzda/sweap-images (dockerhub_username 으로 변경 가능)
#
# Environment:
#   RUN_ID   — 필수, 양의 정수
#   PRO_HARNESS — 공식 저장소 경로 (기본: 클론 위치)
#   DOCKERHUB_USERNAME — sweap-images 가 있는 사용자명 (기본 jefzda)
#   NUM_WORKERS — 병렬 worker 수 (기본 4)

set -Eeuo pipefail
cd "$(cd "$(dirname "$0")" && pwd)"

# shellcheck source=./benchmark-lib.sh
source ./benchmark-lib.sh

BENCHMARK="swebench-pro"
DATASET="${DATASET:-ScaleAI/SWE-bench_Pro}"

# eval-swebench-pro.sh 는 --limit-new/TUNE_NO 가 필요 없으므로 기본값을 인자로 채워 검증을 통과시킨다.
TUNE_NO="${TUNE_NO:-0}"
main_common --limit-new 1

RUN_DIR="$(cd "$RUN_ROOT" && pwd)"  # cd $PRO_HARNESS 후에도 유효하도록 절대경로
CANON_DIR="$RUN_DIR/predictions/canonical"
EVAL_RAW_DIR="$RUN_DIR/eval/raw"
EVAL_INPUT_DIR="$RUN_DIR/eval/input"
EVAL_DIR="$RUN_DIR/eval"
mkdir -p "$EVAL_RAW_DIR" "$EVAL_INPUT_DIR"

PRO_HARNESS="${PRO_HARNESS:-$HOME/src/SWE-bench_Pro-os}"
DOCKERHUB_USERNAME="${DOCKERHUB_USERNAME:-jefzda}"
NUM_WORKERS="${NUM_WORKERS:-4}"

PRED_JSONL="$CANON_DIR/predictions.jsonl"
if [[ ! -s "$PRED_JSONL" ]]; then
    echo "ERROR: $PRED_JSONL missing or empty. Run start-swebench-pro.sh first." >&2
    exit 1
fi
[[ -d "$PRO_HARNESS" ]] || die "PRO_HARNESS not found: $PRO_HARNESS (clone https://github.com/scaleapi/SWE-bench_Pro-os.git)"
[[ -d "$PRO_HARNESS/run_scripts" ]] || die "run_scripts missing in $PRO_HARNESS"

log(){ echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"; }

# ── 1) raw sample (jsonl) 생성: HF 데이터셋 → 필요한 컬럼만 ──
#    (cd $PRO_HARNESS 후에도 유효하도록 절대경로 사용)
RAW_SAMPLE="$EVAL_INPUT_DIR/raw_sample.jsonl"
python3 - "$RAW_SAMPLE" <<'PY'
import json, sys
from datasets import load_dataset

out = sys.argv[1]
ds = load_dataset("ScaleAI/SWE-bench_Pro", split="test")
fields = ["instance_id", "before_repo_set_cmd", "selected_test_files_to_run",
          "base_commit", "repo", "fail_to_pass", "pass_to_pass", "dockerhub_tag"]
with open(out, "w") as f:
    for r in ds:
        rec = {k: r.get(k, "") for k in fields}
        # 목록 필드를 JSON 문자열로 (eval 스크립트가 읽는 형식)
        for k in ("selected_test_files_to_run", "fail_to_pass", "pass_to_pass"):
            v = rec[k]
            if isinstance(v, list):
                rec[k] = json.dumps(v)
            elif isinstance(v, str) and v and not v.startswith("["):
                try:
                    rec[k] = json.dumps(json.loads(v))
                except Exception:
                    rec[k] = json.dumps([v])
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
print(f"wrote {len(ds)} rows -> {out}")
PY

# ── 2) patch json 생성: canonical predictions.jsonl → [{instance_id, patch}] ──
PATCH_JSON="$EVAL_INPUT_DIR/patches.json"
python3 - "$PRED_JSONL" "$PATCH_JSON" <<'PY'
import json, sys
preds_path, out_path = sys.argv[1], sys.argv[2]
patches = []
with open(preds_path) as f:
    for line in f:
        line = line.strip()
        if not line:
            continue
        rec = json.loads(line)
        patches.append({
            "instance_id": rec["instance_id"],
            "patch": rec.get("model_patch", ""),
        })
with open(out_path, "w") as f:
    json.dump(patches, f, ensure_ascii=False, indent=2)
print(f"wrote {len(patches)} patches -> {out_path}")
PY

log "Evaluating $(python3 -c "import json;print(len(json.load(open('$PATCH_JSON'))))") instance(s) via swe_bench_pro_eval.py (local docker, workers=$NUM_WORKERS)"

# ── 3) 공식 평가 실행 (로컬 Docker) ──
set -x
cd "$PRO_HARNESS"
python3 swe_bench_pro_eval.py \
    --raw_sample_path "$RAW_SAMPLE" \
    --patch_path "$PATCH_JSON" \
    --output_dir "$EVAL_RAW_DIR" \
    --scripts_dir "$PRO_HARNESS/run_scripts" \
    --num_workers "$NUM_WORKERS" \
    --dockerhub_username "$DOCKERHUB_USERNAME" \
    --use_local_docker
set +x
cd - >/dev/null

# ── 4) summary.json / breakdown.json 생성 ──
python3 - "$EVAL_RAW_DIR" "$EVAL_DIR" "$PRED_JSONL" <<'PY'
import json, os, sys
eval_raw, eval_dir, preds_path = sys.argv[1], sys.argv[2], sys.argv[3]

reports = {}
# 공식 swe_bench_pro_eval.py 의 출력: eval_results.json = {instance_id: true/false}
er_path = os.path.join(eval_raw, "eval_results.json")
if os.path.isfile(er_path):
    with open(er_path) as f:
        er = json.load(f)
    for iid, ok in er.items():
        reports[iid] = {"resolved": True if ok else False}
# 레거시: resolved_ids 형식 리포트도 허용
if os.path.isdir(eval_raw):
    for fname in os.listdir(eval_raw):
        if not fname.endswith(".json") or fname == "eval_results.json":
            continue
        try:
            with open(os.path.join(eval_raw, fname)) as f:
                rep = json.load(f)
        except Exception:
            continue
        if isinstance(rep, dict) and "resolved_ids" in rep:
            for iid in rep.get("resolved_ids") or []:
                reports.setdefault(iid, {"resolved": True})
            for iid in rep.get("unresolved_ids") or []:
                reports.setdefault(iid, {"resolved": False})
            for iid in rep.get("empty_patch_ids") or []:
                reports.setdefault(iid, {"resolved": None})

resolved = [iid for iid, r in reports.items() if r.get("resolved") is True]
unresolved = [iid for iid, r in reports.items() if r.get("resolved") is False]
missing = [iid for iid, r in reports.items() if r.get("resolved") is None]

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

log "Done. Next: RUN_ID=$RUN_ID ./report-swebench-pro.sh"
