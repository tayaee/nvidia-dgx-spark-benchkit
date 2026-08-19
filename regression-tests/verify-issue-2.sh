#!/usr/bin/env bash
# Regression test for issue 2 — common runner, plugin contract, fake endpoint,
# SWE-bench adapter. Runs entirely offline with the in-process FakeOpenAIEndpoint.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${REPO_ROOT}/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python)"

WORK="${TMPDIR:-/tmp}/benchkit-issue-2-$$"
mkdir -p "$WORK"
trap 'rm -rf "$WORK"' EXIT

cd "$WORK"
export BENCKKIT_ROOT="$WORK"

# 1. Fake endpoint + echo plugin: full attempt completes, all artifacts on disk
"$PY" - <<PY
import sys, json, os
sys.path.insert(0, "$REPO_ROOT/src")

from pathlib import Path
from benchkit.runner import AttemptRunner, FakeOpenAIEndpoint
from benchkit.runner.plugin import BenchmarkPlugin, InstanceSpec
from benchkit.runner.swebench import SwebenchAdapter, SwebenchPrediction

class EchoPlugin(BenchmarkPlugin):
    name = "echo"
    def enumerate(self, dataset_revision):
        return [InstanceSpec(instance_id=f"inst-{i}", input={"text": f"hi {i}"}) for i in range(3)]
    def prepare(self, instance, config):
        return {"prompt": instance.input["text"]}
    def run(self, endpoint, prepared, runtime):
        r = endpoint.chat([{"role":"user","content":prepared["prompt"]}])
        return r["choices"][0]["message"]["content"]
    def parse(self, raw_artifact):
        return {"prediction": raw_artifact.strip()}
    def evaluate(self, canonical_set):
        return {"resolved": len(canonical_set)}

WORK = Path("$WORK")
ep = FakeOpenAIEndpoint()
runner = AttemptRunner(ep, EchoPlugin(), output_dir=WORK, concurrency=2)
report = runner.run(limit_new=None)
assert report["completed"] == 3, report
assert report["failed"] == 0, report
attempt_dirs = [p for p in WORK.iterdir() if p.is_dir()]
assert len(attempt_dirs) == 1
a = attempt_dirs[0]
for sub in ("raw", "canonical", "logs", "checkpoints"):
    assert (a / sub).is_dir(), sub
assert (a / "events.jsonl").exists()
assert (a / "state.jsonl").exists()
for iid in ("inst-0", "inst-1", "inst-2"):
    raw = a / "raw" / f"{iid}.json"
    can = a / "canonical" / f"{iid}.json"
    assert raw.exists(), f"raw missing {iid}"
    assert can.exists(), f"canonical missing {iid}"
print("ok: full attempt writes raw + canonical + ledgers")
PY

# 2. --limit-new 1 with two already-completed instances: only 1 attempt
"$PY" - <<PY
import sys
sys.path.insert(0, "$REPO_ROOT/src")
from pathlib import Path
from benchkit.runner import AttemptRunner, FakeOpenAIEndpoint
from benchkit.runner.plugin import BenchmarkPlugin, InstanceSpec
from benchkit.store import Store
from benchkit.ids import new_experiment_id, new_trial_id

class EchoPlugin(BenchmarkPlugin):
    name = "echo"
    def enumerate(self, dataset_revision):
        return [InstanceSpec(instance_id=f"inst-{i}", input={"text": f"hi {i}"}) for i in range(3)]
    def prepare(self, instance, config): return {"prompt": instance.input["text"]}
    def run(self, endpoint, prepared, runtime):
        return endpoint.chat([{"role":"user","content":prepared["prompt"]}])["choices"][0]["message"]["content"]
    def parse(self, raw): return {"prediction": raw.strip()}
    def evaluate(self, cs): return {"resolved": len(cs)}

WORK = Path("$WORK")
store = Store(WORK / "store.db")
eid = new_experiment_id(); tid = new_trial_id()
store.create_experiment(eid, {})
store.create_trial(eid, tid, {})
store.mark_instance_completed(tid, "inst-0")
store.mark_instance_completed(tid, "inst-1")

runner = AttemptRunner(
    FakeOpenAIEndpoint(), EchoPlugin(),
    output_dir=WORK / "limit", store=store, trial_id=tid, concurrency=1,
)
report = runner.run(limit_new=10)
assert report["completed"] == 1, f"expected 1 completed, got {report['completed']}"
assert report["skipped"] == 2, f"expected 2 skipped, got {report['skipped']}"
print("ok: --limit-new skips already-completed instances")
PY

# 3. Retry: flaky plugin creates a fresh attempt after the first one fails
"$PY" - <<PY
import sys
sys.path.insert(0, "$REPO_ROOT/src")
from pathlib import Path
from benchkit.runner import AttemptRunner, FakeOpenAIEndpoint
from benchkit.runner.plugin import BenchmarkPlugin, InstanceSpec, RunnerError

class FlakyPlugin(BenchmarkPlugin):
    name = "flaky"
    calls = []
    def enumerate(self, dataset_revision):
        return [InstanceSpec(instance_id="inst-1", input={"text": "hi"})]
    def prepare(self, instance, config): return {"prompt": instance.input["text"]}
    def run(self, endpoint, prepared, runtime):
        FlakyPlugin.calls.append(prepared["prompt"])
        if len(FlakyPlugin.calls) == 1:
            raise RunnerError("simulated transient failure")
        return "ok"
    def parse(self, raw): return {"prediction": raw}
    def evaluate(self, cs): return {"resolved": 1 if cs[0]["prediction"] == "ok" else 0}

WORK = Path("$WORK")
FlakyPlugin.calls = []
ep = FakeOpenAIEndpoint()
# seed the output dir with a fake attempt-0001 so retry picks attempt-0002
(WORK / "retry" / "attempt-0001").mkdir(parents=True)
runner = AttemptRunner(ep, FlakyPlugin(), output_dir=WORK / "retry", concurrency=1, max_retries=1)
report = runner.run(limit_new=None)
assert report["completed"] == 1, report
attempts = sorted((WORK / "retry").iterdir())
ids = [p.name for p in attempts]
assert len(ids) >= 2, f"expected >=2 attempts, got {ids}"
print(f"ok: retry produced {len(ids)} attempt dir(s): {ids}")
PY

# 4. SWE-bench adapter: canonical prediction schema
"$PY" - <<PY
import sys, json
sys.path.insert(0, "$REPO_ROOT/src")
from pathlib import Path
from benchkit.runner.swebench import SwebenchAdapter, SwebenchPrediction

# parse raw trajectory text with <patch> markers
adapter = SwebenchAdapter()
canonical = adapter.parse({
    "instance_id": "django__django-12345",
    "prediction_text": "<patch>\n--- a/foo.py\n+++ b/foo.py\n@@\n-x\n+y\n</patch>",
})
assert isinstance(canonical, SwebenchPrediction), type(canonical)
d = canonical.to_dict()
assert d["instance_id"] == "django__django-12345"
assert d["model_patch"].startswith("--- a/foo.py")
assert d["model_name"] == "benchkit"
print("ok: swebench adapter emits {instance_id, model_patch, model_name}")

# validate against the published schema (no extra dependency)
import re
for required in ("instance_id", "model_patch", "model_name"):
    assert required in d, required
assert re.match(r"^[A-Za-z0-9_-]+__[A-Za-z0-9_.-]+-[0-9]+$", d["instance_id"]), d["instance_id"]
print("ok: canonical passes schema validation")
PY

# 5. Plugin manifest parse + required fields
"$PY" - <<PY
import sys, tempfile
sys.path.insert(0, "$REPO_ROOT/src")
from pathlib import Path
from benchkit.runner import parse_plugin_manifest

with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
    f.write("id: swebench-verified\nversion: 1.0.0\nadapter: benchkit.runner.swebench\nexecution:\n  protocol: openai-chat\n  timeout_seconds: 60\n")
    path = f.name
m = parse_plugin_manifest(Path(path))
assert m["id"] == "swebench-verified"
assert m["execution"]["protocol"] == "openai-chat"
assert m["execution"]["timeout_seconds"] == 60
print("ok: plugin manifest parsed")
PY

echo "ALL CHECKS PASSED"