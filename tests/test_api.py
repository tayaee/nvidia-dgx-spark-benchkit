"""API tests — experiment builder, status, events, control endpoints, report."""

from __future__ import annotations

import json

import pytest


@pytest.fixture()
def client(tmp_path):
    from benchkit.api.app import create_app

    app = create_app(
        store_root=tmp_path / "store",
        artifact_root=tmp_path / "artifacts",
    )
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def _create_experiment_payload():
    return {
        "benchmark_id": "swebench@1.0.0",
        "benchmark_version": "1.0.0",
        "dataset_fingerprint": "ds-abc",
        "endpoint": {"kind": "openai-compatible", "base_url": "http://localhost:8000/v1"},
        "trials": [
            {
                "model_id": "m:org/x@rev1",
                "model_revision": "rev1",
                "precision": "fp8",
                "config_bundle": {"max_tokens": 256},
                "evaluator_version": "1.0.0",
                "evaluator_image_digest": "sha256:abc",
                "instance_ids": ["inst-1", "inst-2"],
            },
            {
                "model_id": "m:org/x@rev1",
                "model_revision": "rev1",
                "precision": "bf16",
                "config_bundle": {"max_tokens": 256},
                "evaluator_version": "1.0.0",
                "evaluator_image_digest": "sha256:abc",
                "instance_ids": ["inst-1", "inst-2"],
            },
        ],
    }


def test_health(client):
    rv = client.get("/api/health")
    assert rv.status_code == 200
    assert rv.get_json()["status"] == "ok"


def test_create_experiment_returns_id_and_trial_count(client):
    payload = _create_experiment_payload()
    rv = client.post("/api/experiments", json=payload)
    assert rv.status_code == 201
    body = rv.get_json()
    assert body["experiment_id"].startswith("exp-")
    assert body["trial_count"] == 2
    assert body["trial_ids"]  # at least one trial id


def test_builder_matrix_preview_returns_expected_trial_count(client):
    payload = _create_experiment_payload()
    rv = client.post("/api/builder/matrix", json=payload)
    assert rv.status_code == 200
    body = rv.get_json()
    assert body["expected_trial_count"] == 2
    assert "matrix" in body


def test_experiment_status_returns_trials(client):
    payload = _create_experiment_payload()
    create = client.post("/api/experiments", json=payload).get_json()
    rv = client.get(f"/api/experiments/{create['experiment_id']}")
    assert rv.status_code == 200
    body = rv.get_json()
    assert body["experiment_id"] == create["experiment_id"]
    assert len(body["trials"]) == 2


def test_event_timeline_endpoint_returns_chronological_events(client):
    payload = _create_experiment_payload()
    create = client.post("/api/experiments", json=payload).get_json()
    eid = create["experiment_id"]
    rv = client.get(f"/api/experiments/{eid}/events")
    assert rv.status_code == 200
    body = rv.get_json()
    assert "events" in body
    # events come back ordered (oldest first)
    ts = [e["ts"] for e in body["events"]]
    assert ts == sorted(ts)


def test_pause_resume_cancel_endpoints_flip_status(client):
    payload = _create_experiment_payload()
    create = client.post("/api/experiments", json=payload).get_json()
    eid = create["experiment_id"]
    tid = create["trial_ids"][0]

    # Queue the trial so pause/resume have something to act on.
    client.post(f"/api/experiments/{eid}/start")
    # Move the trial to RUNNING by running a single claim.
    claim = client.post(f"/api/experiments/{eid}/workers/w-1/claim").get_json()
    assert claim.get("trial_id") == tid

    rv = client.post(f"/api/experiments/{eid}/trials/{tid}/pause")
    assert rv.status_code == 200
    assert rv.get_json()["status"] == "paused"

    rv = client.post(f"/api/experiments/{eid}/trials/{tid}/resume")
    assert rv.status_code == 200
    assert rv.get_json()["status"] == "running"

    rv = client.post(f"/api/experiments/{eid}/trials/{tid}/cancel")
    assert rv.status_code == 200
    assert rv.get_json()["status"] == "aborted"


def test_retry_endpoint_requeues_trial(client):
    payload = _create_experiment_payload()
    create = client.post("/api/experiments", json=payload).get_json()
    eid = create["experiment_id"]
    tid = create["trial_ids"][0]

    # Drive trial to FAILED through the start/claim/complete path.
    client.post(f"/api/experiments/{eid}/start")
    claim = client.post(f"/api/experiments/{eid}/workers/w/claim").get_json()
    # Complete with failure
    client.post(
        f"/api/experiments/{eid}/trials/{tid}/complete",
        json={"status": "failed"},
    )
    rv = client.post(f"/api/experiments/{eid}/trials/{tid}/retry")
    assert rv.status_code == 200
    assert rv.get_json()["status"] == "queued"


def test_evaluate_and_report_endpoints(client):
    payload = _create_experiment_payload()
    create = client.post("/api/experiments", json=payload).get_json()
    eid = create["experiment_id"]
    tid = create["trial_ids"][0]
    aid = create["attempt_ids"][0]

    # Write a canonical artifact so evaluate can read it.
    from benchkit.artifact import atomic_write_text
    from pathlib import Path

    trial_dir = (
        Path(client.application.config["artifact_root"])
        / eid
        / "trials"
        / tid
    )
    (trial_dir / "attempts" / aid / "canonical").mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        trial_dir / "attempts" / aid / "canonical" / "inst-1.json",
        json.dumps({"instance_id": "inst-1", "model_patch": "diff"}),
    )
    atomic_write_text(
        trial_dir / "attempts" / aid / "canonical" / "inst-2.json",
        json.dumps({"instance_id": "inst-2"}),
    )

    rv = client.post(
        f"/api/experiments/{eid}/trials/{tid}/evaluate",
        json={
            "evaluator_version": "1.0.0",
            "image_digest": "sha256:deadbeef",
        },
    )
    assert rv.status_code == 201
    report = client.get(f"/api/experiments/{eid}/trials/{tid}/report")
    assert report.status_code == 200
    body = report.get_json()
    assert body["summary"]["raw"]["resolved"] == 1
    assert body["summary"]["denominator"] == 2


def test_select_attempt_endpoint_records_choice(client):
    payload = _create_experiment_payload()
    create = client.post("/api/experiments", json=payload).get_json()
    eid = create["experiment_id"]
    tid = create["trial_ids"][0]
    aid = create["attempt_ids"][0]
    rv = client.post(
        f"/api/experiments/{eid}/trials/{tid}/select-attempt",
        json={"attempt_id": aid},
    )
    assert rv.status_code == 200
    body = rv.get_json()
    assert body["selected_attempt"] == aid


def test_status_endpoint_persists_after_reload(tmp_path):
    """Acceptance: refresh-equivalent reload restores status + timeline."""
    from benchkit.api.app import create_app

    app = create_app(
        store_root=tmp_path / "store",
        artifact_root=tmp_path / "artifacts",
    )
    c = app.test_client()
    create = c.post("/api/experiments", json=_create_experiment_payload()).get_json()
    eid = create["experiment_id"]

    # "refresh": a fresh test client against the same on-disk store.
    app2 = create_app(
        store_root=tmp_path / "store",
        artifact_root=tmp_path / "artifacts",
    )
    c2 = app2.test_client()
    rv = c2.get(f"/api/experiments/{eid}")
    assert rv.status_code == 200
    body = rv.get_json()
    assert body["experiment_id"] == eid
    assert len(body["trials"]) == 2

    events = c2.get(f"/api/experiments/{eid}/events").get_json()
    assert len(events["events"]) >= 1


def test_completed_trial_not_re_started(client):
    """Acceptance: already-completed trials are not re-run."""
    payload = _create_experiment_payload()
    create = client.post("/api/experiments", json=payload).get_json()
    eid = create["experiment_id"]
    tid = create["trial_ids"][0]
    aid = create["attempt_ids"][0]

    # Drive trial to COMPLETED.
    client.post(f"/api/experiments/{eid}/start")
    client.post(f"/api/experiments/{eid}/workers/w/claim")
    client.post(
        f"/api/experiments/{eid}/trials/{tid}/complete", json={"status": "completed"}
    )
    # Restart attempt -> the completed trial should NOT appear in the queue.
    rv = client.post(f"/api/experiments/{eid}/start")
    body = rv.get_json()
    # No trial_ids are issued, or none of them is the completed one.
    assert tid not in (body.get("started_trial_ids") or [])


def test_unknown_experiment_returns_404(client):
    rv = client.get("/api/experiments/exp-does-not-exist")
    assert rv.status_code == 404


def test_unknown_trial_action_returns_404(client):
    payload = _create_experiment_payload()
    create = client.post("/api/experiments", json=payload).get_json()
    eid = create["experiment_id"]
    rv = client.post(f"/api/experiments/{eid}/trials/trial-bogus/pause")
    assert rv.status_code == 404