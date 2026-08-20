#!/usr/bin/env bash
# eval-swebench-pro.sh — official SWE-bench Pro evaluator (local Docker)
#
# Usage:
#   RUN_ID=1 ./eval-swebench-pro.sh
#
# Behavior:
#   - Run the ScaleAI/SWE-bench_Pro official eval script (swe_bench_pro_eval.py)
#     with --use_local_docker.
#   - Raw sample (instance_id, before_repo_set_cmd, selected_test_files_to_run,
#     base_commit, FAIL_TO_PASS, PASS_TO_PASS, etc.) is built from the HF dataset.
#   - Patch is read from results/run-$RUN_ID/swebench-pro/predictions/canonical/predictions.jsonl
#     and converted to a [{instance_id, patch}] list.
#   - Docker Hub image: jefzda/sweap-images (override via dockerhub_username)
#
# Environment:
#   RUN_ID   — required, positive integer
#   PRO_HARNESS — official repo path (default: clone location)
#   DOCKERHUB_USERNAME — sweap-images owner (default jefzda)
#   NUM_WORKERS — parallel workers (default 4)

set -Eeuo pipefail
cd "$(cd "$(dirname "$0")" && pwd)"

# shellcheck source=./benchmark-lib.sh
source ./benchmark-lib.sh

BENCHMARK="swebench-pro"
DATASET="${DATASET:-ScaleAI/SWE-bench_Pro}"

# eval script needs no --limit-new/SCRIPT_VER; fill defaults to pass validation.
SCRIPT_VER="${SCRIPT_VER:-0}"
main_common --limit-new 1

RUN_DIR="$(cd "$RUN_ROOT" && pwd)"  # absolute path so it stays valid after cd $PRO_HARNESS
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

# ── 1) generate raw sample (jsonl): HF dataset → required columns only ──
#    (use absolute path so it stays valid after cd $PRO_HARNESS)
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
        # JSON-encode list fields (format expected by the eval script)
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

# ── 2) generate patch json: canonical predictions.jsonl → [{instance_id, patch}] ──
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

# ── 3) run official evaluation (local Docker) ──
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

# ── 4) generate summary.json / breakdown.json ──
python3 - "$EVAL_RAW_DIR" "$EVAL_DIR" "$PRED_JSONL" <<'PY'
import json, os, sys
eval_raw, eval_dir, preds_path = sys.argv[1], sys.argv[2], sys.argv[3]

reports = {}
# official swe_bench_pro_eval.py output: eval_results.json = {instance_id: true/false}
er_path = os.path.join(eval_raw, "eval_results.json")
if os.path.isfile(er_path):
    with open(er_path) as f:
        er = json.load(f)
    for iid, ok in er.items():
        reports[iid] = {"resolved": True if ok else False}
# legacy: also accept reports in resolved_ids format
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
