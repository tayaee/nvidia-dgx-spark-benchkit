"""Flask app — benchkit control API + UI serving.

Endpoints:
- GET  /api/health
- POST /api/experiments
- GET  /api/experiments/<eid>
- GET  /api/experiments/<eid>/events
- POST /api/experiments/<eid>/start
- POST /api/experiments/<eid>/workers/<wid>/claim
- POST /api/experiments/<eid>/trials/<tid>/pause
- POST /api/experiments/<eid>/trials/<tid>/resume
- POST /api/experiments/<eid>/trials/<tid>/cancel
- POST /api/experiments/<eid>/trials/<tid>/retry
- POST /api/experiments/<eid>/trials/<tid>/complete
- POST /api/experiments/<eid>/trials/<tid>/select-attempt
- POST /api/experiments/<eid>/trials/<tid>/evaluate
- GET  /api/experiments/<eid>/trials/<tid>/report
- POST /api/builder/matrix

The UI is a single static HTML page in benchkit/web/static/.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

from benchkit.ids import (
    new_attempt_id,
    new_experiment_id,
    new_trial_id,
)
from benchkit.matrix import MatrixError, expand_matrix
from benchkit.scheduler.scheduler import SchedulerConfig, TrialQueue
from benchkit.state import TrialStatus
from benchkit.store import Store


# ---- artifact path helpers ------------------------------------------------


def _trial_root(artifact_root: Path, eid: str, tid: str) -> Path:
    return artifact_root / eid / "trials" / tid


# ---- app factory ----------------------------------------------------------


def create_app(store_root: Path | str, artifact_root: Path | str) -> Flask:
    """Build a Flask app bound to its own on-disk store + artifact roots."""
    store_root = Path(store_root)
    artifact_root = Path(artifact_root)
    store_root.mkdir(parents=True, exist_ok=True)
    artifact_root.mkdir(parents=True, exist_ok=True)
    store = Store(store_root / "benchkit.db")

    app = Flask(
        __name__,
        static_folder=str(Path(__file__).resolve().parent.parent / "web" / "static"),
        static_url_path="/static",
    )
    app.config["store"] = store
    app.config["artifact_root"] = artifact_root
    app.config["store_root"] = store_root
    app.config["DEFAULT_HEARTBEAT_TTL"] = 30

    # ---- helpers ----

    def _emit(event_kind: str, **fields) -> None:
        """Append an event row to the events table."""
        store._conn.execute(
            "INSERT INTO events(attempt_id, trial_id, payload_json, created_at) VALUES (?, ?, ?, ?)",
            (
                fields.get("attempt_id"),
                fields.get("trial_id"),
                json.dumps({"kind": event_kind, **fields}, sort_keys=True),
                time.time(),
            ),
        )

    def _get_trial_or_404(eid: str, tid: str) -> dict:
        try:
            payload = store.get_trial(tid)
        except KeyError:
            return None
        if payload.get("experiment_id") != eid:
            return None
        return payload

    def _status_view(eid: str) -> dict:
        rows = store._conn.execute(
            "SELECT id, payload_json, status FROM trials WHERE experiment_id=? ORDER BY id",
            (eid,),
        ).fetchall()
        trials = []
        for r in rows:
            payload = json.loads(r["payload_json"])
            trials.append({
                "trial_id": r["id"],
                "status": r["status"],
                "model_id": payload.get("model_id"),
                "config_bundle": payload.get("config_bundle"),
                "attempt_ids": [
                    row["id"] for row in store._conn.execute(
                        "SELECT id FROM attempts WHERE trial_id=? ORDER BY id",
                        (r["id"],),
                    ).fetchall()
                ],
                "selected_attempt": payload.get("selected_attempt"),
            })
        return {"experiment_id": eid, "trials": trials}

    # ---- routes ----

    @app.route("/api/health")
    def health():
        return jsonify({"status": "ok"})

    @app.route("/api/builder/matrix", methods=["POST"])
    def builder_matrix():
        body = request.get_json(force=True) or {}
        try:
            preview = _preview_matrix(body)
        except MatrixError as e:
            return jsonify({"error": str(e)}), 400
        return jsonify(preview)

    @app.route("/api/experiments", methods=["POST"])
    def create_experiment():
        body = request.get_json(force=True) or {}
        try:
            trials = _build_trials_from_payload(body)
        except (MatrixError, KeyError, ValueError) as e:
            return jsonify({"error": str(e)}), 400

        # Honour caller-supplied experiment_id; otherwise reuse the one
        # already embedded in each trial dict.
        eid = body.get("experiment_id") or trials[0]["experiment_id"]
        for t in trials:
            t["experiment_id"] = eid

        experiment_payload = {
            "benchmark_id": body["benchmark_id"],
            "benchmark_version": body["benchmark_version"],
            "dataset_fingerprint": body["dataset_fingerprint"],
            "endpoint": body["endpoint"],
            "created_at": time.time(),
        }
        store.create_experiment(eid, experiment_payload)
        trial_ids = []
        attempt_ids = []
        for t in trials:
            tid = t["trial_id"]
            trial_ids.append(tid)
            store.create_trial(eid, tid, t)
            aid = new_attempt_id()
            attempt_ids.append(aid)
            store.create_attempt(tid, aid, {
                "trial_id": tid,
                "experiment_id": eid,
                "created_at": time.time(),
            })
            # also create the artifact root for the trial
            (_trial_root(artifact_root, eid, tid)).mkdir(parents=True, exist_ok=True)
        _emit("experiment.created", experiment_id=eid, trial_count=len(trial_ids))

        return jsonify({
            "experiment_id": eid,
            "trial_count": len(trial_ids),
            "trial_ids": trial_ids,
            "attempt_ids": attempt_ids,
        }), 201

    @app.route("/api/experiments/<eid>")
    def experiment_status(eid):
        try:
            store.get_experiment(eid)
        except KeyError:
            return jsonify({"error": "not found"}), 404
        view = _status_view(eid)
        return jsonify(view)

    @app.route("/api/experiments/<eid>/events")
    def experiment_events(eid):
        try:
            store.get_experiment(eid)
        except KeyError:
            return jsonify({"error": "not found"}), 404
        rows = store._conn.execute(
            "SELECT id, attempt_id, trial_id, payload_json, created_at FROM events "
            "WHERE trial_id IN (SELECT id FROM trials WHERE experiment_id=?) "
            "   OR attempt_id IN (SELECT id FROM attempts WHERE trial_id IN "
            "                       (SELECT id FROM trials WHERE experiment_id=?)) "
            "   OR (trial_id IS NULL AND attempt_id IS NULL) "
            "ORDER BY id",
            (eid, eid),
        ).fetchall()
        out = []
        for r in rows:
            payload = json.loads(r["payload_json"])
            out.append({
                "id": r["id"],
                "ts": r["created_at"],
                "trial_id": r["trial_id"],
                "attempt_id": r["attempt_id"],
                "kind": payload.get("kind", "unknown"),
                "data": payload,
            })
        return jsonify({"events": out})

    @app.route("/api/experiments/<eid>/start", methods=["POST"])
    def experiment_start(eid):
        try:
            store.get_experiment(eid)
        except KeyError:
            return jsonify({"error": "not found"}), 404
        q = TrialQueue(store)
        n = q.enqueue(eid)
        _emit("experiment.start", experiment_id=eid, queued=n)
        # gather started trial ids
        rows = store._conn.execute(
            "SELECT id FROM trials WHERE experiment_id=? AND status=? ORDER BY id",
            (eid, TrialStatus.QUEUED.value),
        ).fetchall()
        return jsonify({
            "experiment_id": eid,
            "queued": n,
            "started_trial_ids": [r["id"] for r in rows],
        })

    @app.route("/api/experiments/<eid>/workers/<wid>/claim", methods=["POST"])
    def worker_claim(eid, wid):
        try:
            store.get_experiment(eid)
        except KeyError:
            return jsonify({"error": "not found"}), 404
        q = TrialQueue(store)
        cfg = SchedulerConfig(heartbeat_ttl_seconds=app.config["DEFAULT_HEARTBEAT_TTL"])
        claim = q.claim_next(eid, holder=wid, config=cfg)
        if claim is None:
            return jsonify({"trial_id": None, "attempt_id": None})
        _emit(
            "trial.claimed",
            experiment_id=eid,
            trial_id=claim.trial_id,
            attempt_id=claim.attempt_id,
            holder=wid,
        )
        return jsonify({
            "trial_id": claim.trial_id,
            "attempt_id": claim.attempt_id,
            "holder": claim.holder,
            "expires_at": claim.expires_at,
        })

    def _trial_action(eid, tid, action, target_status, event_kind):
        t = _get_trial_or_404(eid, tid)
        if t is None:
            return jsonify({"error": "trial not found"}), 404
        if target_status == "queued":
            # retry path: must currently be failed
            try:
                store._conn.execute(
                    "UPDATE trials SET status=? WHERE id=?",
                    (TrialStatus.QUEUED.value, tid),
                )
            except Exception as e:
                return jsonify({"error": str(e)}), 400
        else:
            try:
                store.set_trial_status(tid, TrialStatus(target_status))
            except Exception as e:
                return jsonify({"error": str(e)}), 400
        _emit(event_kind, experiment_id=eid, trial_id=tid)
        return jsonify({"trial_id": tid, "status": target_status})

    @app.route("/api/experiments/<eid>/trials/<tid>/pause", methods=["POST"])
    def trial_pause(eid, tid):
        return _trial_action(eid, tid, "pause", TrialStatus.PAUSED.value, "trial.paused")

    @app.route("/api/experiments/<eid>/trials/<tid>/resume", methods=["POST"])
    def trial_resume(eid, tid):
        return _trial_action(eid, tid, "resume", TrialStatus.RUNNING.value, "trial.resumed")

    @app.route("/api/experiments/<eid>/trials/<tid>/cancel", methods=["POST"])
    def trial_cancel(eid, tid):
        return _trial_action(eid, tid, "cancel", TrialStatus.ABORTED.value, "trial.cancelled")

    @app.route("/api/experiments/<eid>/trials/<tid>/retry", methods=["POST"])
    def trial_retry(eid, tid):
        t = _get_trial_or_404(eid, tid)
        if t is None:
            return jsonify({"error": "trial not found"}), 404
        q = TrialQueue(store)
        try:
            q.retry(tid)
        except Exception as e:
            return jsonify({"error": str(e)}), 400
        _emit("trial.retried", experiment_id=eid, trial_id=tid)
        return jsonify({"trial_id": tid, "status": "queued"})

    @app.route("/api/experiments/<eid>/trials/<tid>/complete", methods=["POST"])
    def trial_complete(eid, tid):
        t = _get_trial_or_404(eid, tid)
        if t is None:
            return jsonify({"error": "trial not found"}), 404
        body = request.get_json(force=True) or {}
        target = body.get("status", "completed")
        try:
            store.set_trial_status(tid, TrialStatus(target))
        except Exception as e:
            return jsonify({"error": str(e)}), 400
        _emit("trial.completed", experiment_id=eid, trial_id=tid, status=target)
        return jsonify({"trial_id": tid, "status": target})

    @app.route("/api/experiments/<eid>/trials/<tid>/select-attempt", methods=["POST"])
    def trial_select_attempt(eid, tid):
        t = _get_trial_or_404(eid, tid)
        if t is None:
            return jsonify({"error": "trial not found"}), 404
        body = request.get_json(force=True) or {}
        aid = body.get("attempt_id")
        if not aid:
            return jsonify({"error": "attempt_id required"}), 400
        try:
            store.get_attempt(aid)
        except KeyError:
            return jsonify({"error": "attempt not found"}), 404
        payload = store.get_trial(tid)
        payload["selected_attempt"] = aid
        store._conn.execute(
            "UPDATE trials SET payload_json=? WHERE id=?",
            (json.dumps(payload, sort_keys=True), tid),
        )
        # also persist selected.json next to the trial artifacts so the
        # evaluator can find it.
        sel = _trial_root(artifact_root, eid, tid) / "selected.json"
        from benchkit.artifact import atomic_write_text

        atomic_write_text(sel, json.dumps({"attempt_id": aid}, sort_keys=True))
        _emit("trial.selected", experiment_id=eid, trial_id=tid, attempt_id=aid)
        return jsonify({"trial_id": tid, "selected_attempt": aid})

    @app.route("/api/experiments/<eid>/trials/<tid>/evaluate", methods=["POST"])
    def trial_evaluate(eid, tid):
        t = _get_trial_or_404(eid, tid)
        if t is None:
            return jsonify({"error": "trial not found"}), 404
        body = request.get_json(force=True) or {}
        evaluator_version = body.get("evaluator_version")
        image_digest = body.get("image_digest")
        if not (evaluator_version and image_digest):
            return jsonify({"error": "evaluator_version and image_digest required"}), 400
        from benchkit.evaluator.evaluator import RunEvaluator
        from benchkit.evaluator.report import write_report

        ev = RunEvaluator(evaluator_version=evaluator_version, image_digest=image_digest)
        trial_path = _trial_root(artifact_root, eid, tid)
        # If no explicit selection, default to the first attempt — the
        # API stays ergonomic for the common case where the user wants
        # to score whatever the runner produced.
        sel_path = trial_path / "selected.json"
        if not sel_path.exists():
            from benchkit.artifact import atomic_write_text
            rows = store._conn.execute(
                "SELECT id FROM attempts WHERE trial_id=? ORDER BY id LIMIT 1",
                (tid,),
            ).fetchall()
            if not rows:
                return jsonify({"error": "no attempts to evaluate"}), 400
            atomic_write_text(
                sel_path,
                json.dumps({"attempt_id": rows[0]["id"]}, sort_keys=True),
            )
        try:
            result = ev.evaluate(str(trial_path))
        except Exception as e:
            return jsonify({"error": str(e)}), 400
        report_id = f"report-{int(time.time())}"
        out_dir = trial_path / "scores" / report_id
        write_report(str(out_dir), result)
        _emit("trial.evaluated", experiment_id=eid, trial_id=tid, report_id=report_id)
        return jsonify({
            "trial_id": tid,
            "report_id": report_id,
            "summary": result.to_dict(),
        }), 201

    @app.route("/api/experiments/<eid>/trials/<tid>/report")
    def trial_report(eid, tid):
        t = _get_trial_or_404(eid, tid)
        if t is None:
            return jsonify({"error": "trial not found"}), 404
        from benchkit.evaluator.report import load_report

        scores_dir = _trial_root(artifact_root, eid, tid) / "scores"
        if not scores_dir.exists():
            return jsonify({"error": "no report yet"}), 404
        # pick latest
        report_dirs = sorted(p for p in scores_dir.iterdir() if p.is_dir())
        if not report_dirs:
            return jsonify({"error": "no report yet"}), 404
        summary = load_report(str(report_dirs[-1]))
        return jsonify({
            "trial_id": tid,
            "report_id": report_dirs[-1].name,
            "summary": summary,
        })

    @app.route("/")
    def index():
        return send_from_directory(app.static_folder, "index.html")

    return app


# ---- helpers --------------------------------------------------------------


def _build_trials_from_payload(body: dict) -> list[dict]:
    """Translate the API payload into a list of trial dicts."""
    benchmark_id = body.get("benchmark_id")
    benchmark_version = body.get("benchmark_version")
    dataset_fingerprint = body.get("dataset_fingerprint")
    endpoint = body.get("endpoint")
    if not all([benchmark_id, benchmark_version, dataset_fingerprint, endpoint]):
        raise ValueError("benchmark_id, benchmark_version, dataset_fingerprint, endpoint required")

    eid = body.get("experiment_id") or new_experiment_id()

    raw_trials = body.get("trials")
    if not raw_trials:
        raise ValueError("trials required")

    out = []
    for t in raw_trials:
        for required in (
            "model_id",
            "model_revision",
            "precision",
            "config_bundle",
            "evaluator_version",
            "evaluator_image_digest",
        ):
            if required not in t:
                raise ValueError(f"trial missing field: {required}")

        tid = t.get("trial_id") or new_trial_id()
        out.append({
            "trial_id": tid,
            "experiment_id": eid,
            "benchmark_id": benchmark_id,
            "benchmark_version": benchmark_version,
            "model_id": t["model_id"],
            "model_revision": t["model_revision"],
            "precision": t["precision"],
            "config_bundle": t["config_bundle"],
            "config_bundle_id": t.get("config_bundle_id"),
            "evaluator_version": t["evaluator_version"],
            "evaluator_image_digest": t["evaluator_image_digest"],
            "endpoint": endpoint,
            "dataset_fingerprint": dataset_fingerprint,
            "instance_ids": t.get("instance_ids", []),
        })
    return out


def _preview_matrix(body: dict) -> dict:
    """Show the trial count + per-trial preview the builder UI needs.

    Accepts either the rich ``models``/``configs`` shape (which is what
    the matrix expansion consumes) or the flat ``trials`` shape (which
    the create endpoint accepts). For the flat shape we count rows
    directly and surface them as the matrix entries.
    """
    benchmark_id = body.get("benchmark_id", "x@1.0.0")
    benchmark_version = body.get("benchmark_version", "1.0.0")
    dataset_fingerprint = body.get("dataset_fingerprint", "preview")
    endpoint = body.get("endpoint", {"kind": "openai-compatible"})

    models = body.get("models") or []
    configs = body.get("configs") or []
    flat_trials = body.get("trials") or []
    if not models and flat_trials:
        # Treat the flat trials list as a one-trial-per-row matrix.
        rows = []
        for t in flat_trials:
            rows.append({
                "trial_id": t.get("trial_id"),
                "model_id": t.get("model_id"),
                "config_bundle_id": t.get("config_bundle_id"),
            })
        return {
            "expected_trial_count": len(rows),
            "matrix": {
                "models": sorted({r["model_id"] for r in rows if r["model_id"]}),
                "configs": sorted({r["config_bundle_id"] for r in rows if r["config_bundle_id"]}),
            },
            "trials": rows,
        }
    if not models or not configs:
        raise MatrixError("models and configs are required for matrix preview")

    eid = body.get("experiment_id") or new_experiment_id()
    spec = {
        "experiment_id": eid,
        "benchmark_id": benchmark_id,
        "benchmark_version": benchmark_version,
        "dataset_fingerprint": dataset_fingerprint,
        "endpoint": endpoint,
        "seed": body.get("seed", 42),
        "workers": body.get("workers", 4),
        "models": models,
        "configs": configs,
    }
    trials = expand_matrix(spec)
    return {
        "expected_trial_count": len(trials),
        "matrix": {
            "models": [m["model_id"] for m in models],
            "configs": [c.get("name") or c.get("id") or str(i) for i, c in enumerate(configs)],
        },
        "trials": [
            {
                "trial_id": t["trial_id"],
                "model_id": t["model_id"],
                "config_bundle_id": t["config_bundle_id"],
            }
            for t in trials
        ],
    }