#!/usr/bin/env bash
# Regression test for issue 3 — reproducible evaluator and reporting.
# Runs entirely offline: synthesises a canonical artifact set, evaluates
# twice, and confirms both runs produce identical reports.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${REPO_ROOT}/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python)"

WORK="${TMPDIR:-/tmp}/benchkit-issue-3-$$"
mkdir -p "$WORK"
trap 'rm -rf "$WORK"' EXIT

# 1. Build a synthetic trial with selected attempt and 3 canonicals
"$PY" - <<PY
import sys, json
sys.path.insert(0, "$REPO_ROOT/src")
from pathlib import Path
from benchkit.evaluator import RunEvaluator, evaluate_canonical_set

WORK = Path("$WORK")
trial = WORK / "trial-0001"
canon = trial / "attempts" / "attempt-0001" / "canonical"
canon.mkdir(parents=True)
selected = [
    {"instance_id": "inst-1", "model_patch": "diff --git a/x b/x\n+x"},
    {"instance_id": "inst-2", "model_patch": ""},        # invalid
    {"instance_id": "inst-3"},                            # failed
]
for c in selected:
    (canon / f"{c['instance_id']}.json").write_text(json.dumps(c))
(trial / "selected.json").write_text(json.dumps({"attempt_id": "attempt-0001"}))

ev = RunEvaluator(evaluator_version="1.0.0", image_digest="sha256:abc123")

# 2. Same inputs -> identical reports (the reproducibility contract)
r1 = ev.evaluate(str(trial))
r2 = ev.evaluate(str(trial))
assert r1.to_dict() == r2.to_dict(), "reproducibility violated"
print("ok: same inputs produce identical reports")

# 3. Different evaluator version -> different report (so users can tell)
other = RunEvaluator(evaluator_version="1.1.0", image_digest="sha256:abc123")
r3 = other.evaluate(str(trial))
assert r1.to_dict() != r3.to_dict(), "evaluator version not reflected"
assert r1.evaluator_version != r3.evaluator_version
print("ok: different evaluator version -> different report")

# 4. write_report emits summary.json, breakdown.json, report.csv, report.md
from benchkit.evaluator import write_report, load_report
out = trial / "scores" / "report-0001"
write_report(str(out), r1)
for f in ("summary.json", "breakdown.json", "report.csv", "report.md"):
    assert (out / f).exists(), f"missing {f}"
s = load_report(str(out))
assert s["raw"]["resolved"] == 1
assert s["normalized"]["resolved_normalized"] == 1/3
assert s["breakdown"]["resolved"] == 1
assert s["breakdown"]["failed"] == 1
assert s["breakdown"]["invalid"] == 1
assert s["breakdown"]["denominator"] == 3
assert s["evaluator_version"] == "1.0.0"
assert s["evaluator_image_digest"] == "sha256:abc123"
print("ok: report files emitted with required fields")

# 5. unselected attempts are NOT counted (no log scraping, no auto-pick-latest)
trial2 = WORK / "trial-0002"
for a in ("attempt-0001", "attempt-0002"):
    d = trial2 / "attempts" / a / "canonical"
    d.mkdir(parents=True)
    (d / "i1.json").write_text(json.dumps({"instance_id": "i1", "model_patch": "x"}))
    (d / "i2.json").write_text(json.dumps({"instance_id": "i2", "model_patch": "x"}))
# attempt-0002 has a *better* raw count but is not selected
(trial2 / "attempts" / "attempt-0002" / "canonical" / "i3.json").write_text(
    json.dumps({"instance_id": "i3", "model_patch": "x"})
)
(trial2 / "selected.json").write_text(json.dumps({"attempt_id": "attempt-0001"}))
r = ev.evaluate(str(trial2))
assert r.breakdown["resolved"] == 2, r.breakdown  # NOT 3 (the unselected extra)
print("ok: unselected attempts excluded from score")

# 6. raw + normalized both preserved (no log-scraped derivations only)
canonical = [{"instance_id": f"i{i}", "model_patch": "x"} for i in range(10)]
canonical.extend([{"instance_id": f"j{i}"} for i in range(10)])  # 10 failed
r = ev.evaluate(str(WORK / "trial-x"), canonical_set=canonical)
assert r.raw["resolved"] == 10
assert r.normalized["resolved_normalized"] == 0.5
assert r.raw["resolved"] != r.normalized["resolved_normalized"]
print("ok: raw and normalized scores both preserved")
PY

echo "ALL CHECKS PASSED"