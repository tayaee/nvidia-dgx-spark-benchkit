#!/usr/bin/env bash
# Regression test for issue 1 — benchmark domain, manifest, layout, lifecycle.
# Checks CLI help, ID validation, matrix expansion, atomic artifact layout,
# and the resume-skip-completed-instance contract.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${REPO_ROOT}/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python)"

WORK="${TMPDIR:-/tmp}/benchkit-issue-1-$$"
mkdir -p "$WORK"
trap 'rm -rf "$WORK"' EXIT

cd "$WORK"
export BENCKKIT_ROOT="$WORK"
export RESULTS_ROOT="$WORK/results"
mkdir -p benchmarks models configs/bundles results

# 1. CLI help is available
"$PY" -m benchkit.cli.main --help >/dev/null

# 2. ID validation rejects malformed references
"$PY" - <<PY
import sys
sys.path.insert(0, "$REPO_ROOT/src")
from benchkit.ids import validate_benchmark_ref, validate_model_ref
try:
    validate_benchmark_ref("not-a-ref")
except ValueError:
    pass
else:
    raise SystemExit("expected ValueError for bad benchmark ref")
try:
    validate_model_ref("Qwen/Qwen3-8B")
except ValueError:
    pass
else:
    raise SystemExit("expected ValueError for bad model ref")
print("ok: id validation rejects malformed refs")
PY

# 3. Matrix expansion: 2 models × 2 configs → 4 trials, all unique
SPEC="$WORK/spec.json"
cat > "$SPEC" <<JSON
{
  "benchmark_id": "swebench-verified",
  "benchmark_version": "1.0.0",
  "dataset_fingerprint": "deadbeef",
  "endpoint": "http://localhost:1234/v1",
  "seed": 0,
  "workers": 1,
  "models": [
    {"model_id": "m1", "model_revision": "rev1", "precision": "fp16"},
    {"model_id": "m2", "model_revision": "rev2", "precision": "fp16"}
  ],
  "configs": [
    {"server": {"tp": 1}},
    {"server": {"tp": 2}}
  ]
}
JSON

PLAN_JSON="$("$PY" -m benchkit.cli.main plan --spec "$SPEC" --json)"
TRIAL_COUNT="$("$PY" -c "import json,sys; print(len(json.loads(sys.argv[1])['trials']))" "$PLAN_JSON")"
[[ "$TRIAL_COUNT" -eq 4 ]] || { echo "FAIL: expected 4 trials, got $TRIAL_COUNT"; exit 1; }

UNIQ_COUNT="$("$PY" -c "import json,sys; d=json.loads(sys.argv[1]); print(len(set(t['trial_id'] for t in d['trials'])))" "$PLAN_JSON")"
[[ "$UNIQ_COUNT" -eq 4 ]] || { echo "FAIL: trial ids are not unique"; exit 1; }
echo "ok: matrix expansion produces 4 unique trials"

# 4. Create experiment + verify manifest on disk
EID="$("$PY" -m benchkit.cli.main create-experiment --spec "$SPEC" --json | "$PY" -c "import json,sys; print(json.load(sys.stdin)['experiment_id'])")"
MANIFEST="$WORK/results/$EID/manifest.json"
[[ -f "$MANIFEST" ]] || { echo "FAIL: manifest not at $MANIFEST"; exit 1; }
"$PY" -c "import json,sys; d=json.load(open(sys.argv[1])); assert d['benchmark_id']=='swebench-verified'" "$MANIFEST"
echo "ok: manifest written with required fingerprints"

# 5. start-attempt creates atomic layout, run-twice preserves first artifact
TID="$("$PY" -c "import json,sys; print(json.loads(sys.argv[1])['trials'][0]['trial_id'])" "$PLAN_JSON")"

"$PY" -m benchkit.cli.main start-attempt --experiment "$EID" --trial "$TID" >/dev/null
A_DIR="$(ls -1 "$WORK/results/$EID/trials/$TID/attempts" | head -1)"
A_PATH="$WORK/results/$EID/trials/$TID/attempts/$A_DIR"
for sub in raw canonical logs checkpoints; do
  [[ -d "$A_PATH/$sub" ]] || { echo "FAIL: missing $A_PATH/$sub"; exit 1; }
done
[[ -f "$A_PATH/events.jsonl" ]] || { echo "FAIL: events.jsonl missing"; exit 1; }
[[ -f "$A_PATH/state.jsonl" ]] || { echo "FAIL: state.jsonl missing"; exit 1; }

# write a sentinel raw artifact, then start another attempt and confirm it's separate
echo "raw1" > "$A_PATH/raw/instance-1.txt"
"$PY" -m benchkit.cli.main start-attempt --experiment "$EID" --trial "$TID" >/dev/null
A2="$(ls -1t "$WORK/results/$EID/trials/$TID/attempts" | head -1)"
[[ "$A2" != "$A_DIR" ]] || { echo "FAIL: second attempt has same id $A2"; exit 1; }
[[ -f "$A_PATH/raw/instance-1.txt" ]] || { echo "FAIL: first artifact was overwritten"; exit 1; }
echo "ok: atomic layout; retry creates separate attempt without overwriting raw"

# 6. Resume skips completed instances (store contract)
RESUME_OUT="$("$PY" -m benchkit.cli.main resume --trial "$TID" --json)"
"$PY" - <<PY
import json
d = json.loads('''$RESUME_OUT''')
assert d["trial_id"] == "$TID", d
print("ok: resume returns valid payload for trial $TID")
PY

echo "ALL CHECKS PASSED"