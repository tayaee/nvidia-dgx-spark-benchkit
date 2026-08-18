"""Tests for benchkit.cli — CLI end-to-end with a tmp BENCKKIT_ROOT."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def _venv_python() -> str:
    """Path to the python interpreter inside the uv-managed .venv_wsl/."""
    repo_root = Path(__file__).resolve().parents[1]
    venv = repo_root / ".venv_wsl"
    # uv-managed venv: bin/python on linux
    py = venv / "bin" / "python"
    return str(py)


def _benchkit(*args, env):
    return subprocess.run(
        [_venv_python(), "-m", "benchkit.cli.main", *args],
        env=env,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def cli_env(tmp_path):
    e = os.environ.copy()
    e["BENCKKIT_ROOT"] = str(tmp_path)
    e["RESULTS_ROOT"] = str(tmp_path / "results")
    return e


def _spec(models=1, configs=1):
    return {
        "benchmark_id": "swebench-verified",
        "benchmark_version": "1.0.0",
        "models": [
            {"model_id": f"model-{i}", "model_revision": "rev1", "precision": "fp16"}
            for i in range(models)
        ],
        "configs": [{"server": {"tp": j + 1}} for j in range(configs)],
        "config_bundle": {"server": {"tp": 1}},  # outer manifest field (default)
        "dataset_fingerprint": "deadbeef",
        "evaluator_version": "1.2.3",
        "evaluator_image_digest": "sha256:cafe",
        "endpoint": "http://localhost:1234",
        "seed": 0,
        "workers": 1,
    }


def test_create_experiment_writes_manifest(cli_env, tmp_path):
    spec = _spec()
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec))

    r = _benchkit("create-experiment", "--spec", str(spec_path), "--json", env=cli_env)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["experiment_id"].startswith("exp-")

    # manifest file exists under results/<eid>/manifest.json
    manifest_path = tmp_path / "results" / out["experiment_id"] / "manifest.json"
    assert manifest_path.exists()
    m = json.loads(manifest_path.read_text())
    assert m["benchmark_id"] == "swebench-verified"


def test_plan_reports_trial_count(cli_env, tmp_path):
    spec = _spec(models=2, configs=2)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec))

    r = _benchkit("plan", "--spec", str(spec_path), "--json", env=cli_env)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["trial_count"] == 4
    assert len(out["trials"]) == 4


def test_start_attempt_creates_layout(cli_env, tmp_path):
    spec = _spec()
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec))

    # create
    r1 = _benchkit("create-experiment", "--spec", str(spec_path), "--json", env=cli_env)
    assert r1.returncode == 0, r1.stderr
    eid = json.loads(r1.stdout)["experiment_id"]

    # plan
    r2 = _benchkit("plan", "--spec", str(spec_path), "--json", env=cli_env)
    assert r2.returncode == 0, r2.stderr
    trials = json.loads(r2.stdout)["trials"]
    tid = trials[0]["trial_id"]

    # start-attempt
    r3 = _benchkit(
        "start-attempt", "--experiment", eid, "--trial", tid,
        env=cli_env,
    )
    assert r3.returncode == 0, r3.stderr

    # verify the attempt directory exists with the expected layout
    from benchkit.artifact import attempt_dir
    expected = Path(cli_env["BENCKKIT_ROOT"]) / "results" / eid / "trials" / tid / "attempts"
    assert expected.is_dir()
    attempts = list(expected.iterdir())
    assert len(attempts) == 1
    a = attempts[0]
    assert (a / "raw").is_dir()
    assert (a / "canonical").is_dir()
    assert (a / "logs").is_dir()
    assert (a / "checkpoints").is_dir()


def test_resume_lists_pending(cli_env, tmp_path):
    spec = _spec()
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(json.dumps(spec))
    r0 = _benchkit("create-experiment", "--spec", str(spec_path), env=cli_env)
    assert r0.returncode == 0, r0.stderr

    # populate the store directly so we have an instance_state row
    from benchkit.store import Store
    store = Store(tmp_path / "meta.db")
    eid = store.list_trials.__self__._conn  # just touch
    # the create-experiment above wrote the experiment id; find it
    row = store._conn.execute("SELECT id FROM experiments LIMIT 1").fetchone()
    eid = row["id"]
    store.create_trial(eid, "trial-0001", {})
    store.mark_instance_completed("trial-0001", "instance-1")
    store.mark_instance_queued("trial-0001", "instance-2")

    r = _benchkit("resume", "--trial", "trial-0001", "--json", env=cli_env)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["pending_instances"] == ["instance-2"]