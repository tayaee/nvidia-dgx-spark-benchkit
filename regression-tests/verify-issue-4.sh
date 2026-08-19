#!/usr/bin/env bash
# Regression test for issue 4 — scheduler + Web UI control API.
# Boots the Flask app in-process, drives a fake-runner golden path
# (create -> claim -> complete -> evaluate -> report), and confirms the
# state survives a "refresh" via a fresh app instance against the same
# on-disk store.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${REPO_ROOT}/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python)"

WORK="${TMPDIR:-/tmp}/benchkit-issue-4-$$"
mkdir -p "$WORK/store" "$WORK/artifacts"
trap 'rm -rf "$WORK"' EXIT

cd "$REPO_ROOT"
PYTHONPATH="$REPO_ROOT/src" "$PY" - <<PY
import json, sys
sys.path.insert(0, "$REPO_ROOT/src")
from pathlib import Path
from benchkit.api.app import create_app
from benchkit.artifact import atomic_write_text

app = create_app(store_root=Path("$WORK/store"), artifact_root=Path("$WORK/artifacts"))
c = app.test_client()

# 1. Health
assert c.get("/api/health").get_json()["status"] == "ok"
print("ok: health")

# 2. Builder matrix preview
preview = c.post("/api/builder/matrix", json={
    "benchmark_id": "swebench@1.0.0",
    "benchmark_version": "1.0.0",
    "dataset_fingerprint": "ds-abc",
    "endpoint": {"kind": "openai-compatible"},
    "models": [
        {"model_id": "huggingface:Qwen/Qwen3-8B@rev1", "model_revision": "rev1", "precision": "fp16"},
        {"model_id": "huggingface:Qwen/Qwen3-8B@rev1", "model_revision": "rev2", "precision": "fp16"},
    ],
    "configs": [{"max_tokens": 256}, {"max_tokens": 512}],
}).get_json()
assert preview["expected_trial_count"] == 4
print(f"ok: matrix preview -> {preview['expected_trial_count']} trials")

# 3. Create experiment
payload = {
    "benchmark_id": "swebench@1.0.0",
    "benchmark_version": "1.0.0",
    "dataset_fingerprint": "ds-abc",
    "endpoint": {"kind": "openai-compatible"},
    "trials": [
        {
            "model_id": "huggingface:Qwen/Qwen3-8B@rev1",
            "model_revision": "rev1",
            "precision": "fp16",
            "config_bundle": {"max_tokens": 256},
            "evaluator_version": "1.0.0",
            "evaluator_image_digest": "sha256:abc",
            "instance_ids": ["inst-1", "inst-2"],
        },
        {
            "model_id": "huggingface:Qwen/Qwen3-8B@rev2",
            "model_revision": "rev2",
            "precision": "fp16",
            "config_bundle": {"max_tokens": 512},
            "evaluator_version": "1.0.0",
            "evaluator_image_digest": "sha256:abc",
            "instance_ids": ["inst-1", "inst-2"],
        },
    ],
}
created = c.post("/api/experiments", json=payload).get_json()
eid, tids, aids = created["experiment_id"], created["trial_ids"], created["attempt_ids"]
assert len(tids) == 2
print(f"ok: created experiment {eid}")

# 4. Start -> enqueue
start = c.post(f"/api/experiments/{eid}/start").get_json()
assert len(start["started_trial_ids"]) == 2
print(f"ok: started {len(start['started_trial_ids'])} trials")

# 5. Worker claim -> returns a lease
claim = c.post(f"/api/experiments/{eid}/workers/fake-worker/claim").get_json()
assert claim["trial_id"] in tids
tid_claimed = claim["trial_id"]
aid_claimed = claim["attempt_id"]
print(f"ok: worker claimed trial {tid_claimed}")

# 6. Drop a fake canonical artifact so evaluate can score it
trial_dir = Path("$WORK/artifacts") / eid / "trials" / tid_claimed / "attempts" / aid_claimed / "canonical"
trial_dir.mkdir(parents=True, exist_ok=True)
atomic_write_text(trial_dir / "inst-1.json", json.dumps({"instance_id": "inst-1", "model_patch": "diff"}))
atomic_write_text(trial_dir / "inst-2.json", json.dumps({"instance_id": "inst-2"}))
print("ok: wrote canonical artifacts")

# 7. Pause / resume / cancel cycle on the OTHER trial — first claim it
#    so it transitions RUNNING, then drive the control surface.
other_tid = [t for t in tids if t != tid_claimed][0]
c.post(f"/api/experiments/{eid}/workers/fake-worker/claim")  # picks up the second trial
assert c.post(f"/api/experiments/{eid}/trials/{other_tid}/pause").get_json()["status"] == "paused"
assert c.post(f"/api/experiments/{eid}/trials/{other_tid}/resume").get_json()["status"] == "running"
assert c.post(f"/api/experiments/{eid}/trials/{other_tid}/cancel").get_json()["status"] == "aborted"
print("ok: pause/resume/cancel cycle")

# 8. Complete the claimed trial (fake runner succeeded)
assert c.post(f"/api/experiments/{eid}/trials/{tid_claimed}/complete", json={"status": "completed"}).get_json()["status"] == "completed"
print("ok: trial marked completed")

# 9. Select an attempt + evaluate + report
select = c.post(f"/api/experiments/{eid}/trials/{tid_claimed}/select-attempt", json={"attempt_id": aid_claimed}).get_json()
assert select["selected_attempt"] == aid_claimed
ev_resp = c.post(f"/api/experiments/{eid}/trials/{tid_claimed}/evaluate", json={"evaluator_version": "1.0.0", "image_digest": "sha256:dev"}).get_json()
assert ev_resp["summary"]["raw"]["resolved"] == 1
assert ev_resp["summary"]["denominator"] == 2
report = c.get(f"/api/experiments/{eid}/trials/{tid_claimed}/report").get_json()
assert report["summary"]["evaluator_image_digest"] == "sha256:dev"
print(f"ok: report -> resolved={report['summary']['raw']['resolved']}/{report['summary']['denominator']}")

# 10. Acceptance: refresh-equivalent reload restores state
app2 = create_app(store_root=Path("$WORK/store"), artifact_root=Path("$WORK/artifacts"))
c2 = app2.test_client()
status = c2.get(f"/api/experiments/{eid}").get_json()
assert len(status["trials"]) == 2
# completed trial is still completed
completed_trial = next(t for t in status["trials"] if t["trial_id"] == tid_claimed)
assert completed_trial["status"] == "completed"
events = c2.get(f"/api/experiments/{eid}/events").get_json()
kinds = {e["kind"] for e in events["events"]}
assert "experiment.created" in kinds
assert "trial.completed" in kinds
assert "trial.evaluated" in kinds
print(f"ok: reload preserved state ({len(events['events'])} events, kinds: {sorted(kinds)})")

# 11. Acceptance: completed trial not re-run on subsequent start
c2.post(f"/api/experiments/{eid}/start")
status2 = c2.get(f"/api/experiments/{eid}").get_json()
requeued = [t for t in status2["trials"] if t["status"] == "queued"]
assert tid_claimed not in [t["trial_id"] for t in requeued]
print("ok: completed trial not re-queued on subsequent start")

# 12. UI: index.html is served
rv = c.get("/")
assert b"Benchkit Control UI" in rv.data
print("ok: UI index.html served")

print("ALL CHECKS PASSED")
PY

echo "ALL CHECKS PASSED"