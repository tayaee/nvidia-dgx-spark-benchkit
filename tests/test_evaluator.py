"""Tests for benchkit.evaluator — reproducible evaluator + report generation."""

import json
from pathlib import Path

import pytest

from benchkit.evaluator import (
    EvaluationResult,
    EvaluatorError,
    RunEvaluator,
    write_report,
    load_report,
    evaluate_canonical_set,
)


def _make_run(tmp_path, instance_states):
    """Build an attempt-like directory tree with selected canonicals."""
    run = tmp_path / "trial-0001"
    attempt = run / "attempts" / "attempt-0001"
    attempt.mkdir(parents=True)
    (attempt / "canonical").mkdir()
    canonical = []
    for iid, status in instance_states:
        c = {"instance_id": iid, "model_patch": f"patch-for-{iid}"}
        (attempt / "canonical" / f"{iid}.json").write_text(json.dumps(c))
        canonical.append(c)
    (attempt / "events.jsonl").write_text("")
    (attempt / "state.jsonl").write_text("")
    # selected.json points at this attempt
    (run / "selected.json").write_text(json.dumps({"attempt_id": "attempt-0001", "canonical_count": len(canonical)}))
    return run, canonical


class TestEvaluationResult:
    def test_round_trip(self):
        r = EvaluationResult(
            evaluator_version="1.0.0",
            evaluator_image_digest="sha256:abc",
            input_artifact_hash="h1",
            raw={"resolved": 5},
            normalized={"resolved_normalized": 0.5},
            breakdown={"resolved": 5, "failed": 1, "missing": 0, "invalid": 0, "total": 6},
            denominator=6,
        )
        d = r.to_dict()
        assert d["raw"]["resolved"] == 5
        assert d["breakdown"]["denominator"] == 6

    def test_rejects_missing_evaluator_digest(self):
        with pytest.raises(EvaluatorError):
            EvaluationResult(
                evaluator_version="1.0.0",
                evaluator_image_digest="",
                input_artifact_hash="h",
                raw={}, normalized={}, breakdown={}, denominator=0,
            )


class TestEvaluatorReproducibility:
    def test_same_inputs_produce_same_report(self, tmp_path):
        run, canonical = _make_run(tmp_path, [("inst-1", "ok"), ("inst-2", "ok"), ("inst-3", "fail")])
        ev1 = RunEvaluator(evaluator_version="1.0.0", image_digest="sha256:abc")
        r1 = ev1.evaluate(str(run), canonical_set=canonical)
        # same evaluator on same inputs -> same report hash
        ev2 = RunEvaluator(evaluator_version="1.0.0", image_digest="sha256:abc")
        r2 = ev2.evaluate(str(run), canonical_set=canonical)
        assert r1.to_dict() == r2.to_dict()

    def test_different_evaluator_version_yields_different_report(self, tmp_path):
        run, canonical = _make_run(tmp_path, [("inst-1", "ok")])
        a = RunEvaluator("1.0.0", "sha256:abc").evaluate(str(run), canonical_set=canonical)
        b = RunEvaluator("1.1.0", "sha256:abc").evaluate(str(run), canonical_set=canonical)
        assert a.evaluator_version != b.evaluator_version
        # reports carry the version so consumers can detect divergence
        assert a.to_dict()["evaluator_version"] != b.to_dict()["evaluator_version"]


class TestBreakdownCounts:
    def test_counts_resolved_failed_missing_invalid(self, tmp_path):
        canonical = [
            {"instance_id": "i1", "model_patch": "p"},  # resolved
            {"instance_id": "i2"},  # missing patch -> failed
            {"instance_id": "i3", "model_patch": ""},  # empty -> invalid
        ]
        ev = RunEvaluator("1.0.0", "sha256:abc")
        run = tmp_path / "trial"
        (run / "attempts" / "attempt-0001" / "canonical").mkdir(parents=True)
        result = ev.evaluate(str(run), canonical_set=canonical)
        assert result.breakdown["resolved"] == 1
        assert result.breakdown["failed"] == 1
        assert result.breakdown["invalid"] == 1
        assert result.breakdown["denominator"] == 3

    def test_excludes_unselected_attempts(self, tmp_path):
        run = tmp_path / "trial"
        a1 = run / "attempts" / "attempt-0001"
        a2 = run / "attempts" / "attempt-0002"
        for a in (a1, a2):
            (a / "canonical").mkdir(parents=True)
            (a / "canonical" / "i1.json").write_text(json.dumps({"instance_id": "i1", "model_patch": "p1"}))
            (a / "canonical" / "i2.json").write_text(json.dumps({"instance_id": "i2", "model_patch": ""}))
        (run / "selected.json").write_text(json.dumps({"attempt_id": "attempt-0001"}))
        ev = RunEvaluator("1.0.0", "sha256:abc")
        result = ev.evaluate(str(run), canonical_set=None)
        # only attempt-0001 is selected -> counts should reflect i1 (resolved) + i2 (invalid)
        assert result.breakdown["resolved"] == 1
        assert result.breakdown["invalid"] == 1


class TestReportFiles:
    def test_writes_summary_and_breakdown(self, tmp_path):
        run = tmp_path / "trial"
        (run / "attempts" / "attempt-0001" / "canonical").mkdir(parents=True)
        ev = RunEvaluator("1.0.0", "sha256:abc")
        canonical = [{"instance_id": "i1", "model_patch": "p1"}, {"instance_id": "i2"}]
        result = ev.evaluate(str(run), canonical_set=canonical)
        write_report(str(run / "scores" / "report-0001"), result)
        s = load_report(str(run / "scores" / "report-0001"))
        assert s["raw"]["resolved"] == result.raw["resolved"]
        assert s["breakdown"]["denominator"] == 2


class TestEvaluateCanonicalSet:
    def test_returns_aggregated_counts(self):
        canonical = [
            {"instance_id": "i1", "model_patch": "x"},
            {"instance_id": "i2"},
            {"instance_id": "i3", "model_patch": ""},
            {"instance_id": "i4", "model_patch": "y"},
        ]
        r = evaluate_canonical_set(canonical)
        assert r["resolved"] == 2
        assert r["failed"] == 1
        assert r["invalid"] == 1
        assert r["denominator"] == 4


class TestNormalization:
    def test_raw_score_preserved(self, tmp_path):
        run = tmp_path / "trial"
        (run / "attempts" / "attempt-0001" / "canonical").mkdir(parents=True)
        canonical = [{"instance_id": f"i{i}", "model_patch": "x"} for i in range(10)]
        canonical.extend([{"instance_id": f"j{i}"} for i in range(10)])  # 10 failed
        ev = RunEvaluator("1.0.0", "sha256:abc")
        result = ev.evaluate(str(run), canonical_set=canonical)
        assert result.raw["resolved"] == 10
        assert result.normalized["resolved_normalized"] == 0.5
        assert result.raw["resolved"] != result.normalized["resolved_normalized"]