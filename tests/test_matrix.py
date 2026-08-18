"""Tests for benchkit.matrix — experiment matrix expansion into trials."""

import pytest

from benchkit.ids import new_experiment_id
from benchkit.matrix import expand_matrix, MatrixError


def _model(i):
    return {"model_id": f"model-{i}", "model_revision": f"rev{i}"}


def _config(j):
    return {"server": {"tp": j}}


def _basic_spec():
    return {
        "experiment_id": new_experiment_id(),
        "benchmark_id": "swebench-verified",
        "benchmark_version": "1.0.0",
        "models": [_model(1), _model(2)],
        "configs": [_config(1), _config(2)],
        "dataset_fingerprint": "deadbeef",
        "endpoint": "http://localhost:1234",
        "seed": 0,
        "workers": 1,
    }


class TestExpandMatrix:
    def test_two_models_two_configs_yields_four_trials(self):
        spec = _basic_spec()
        trials = expand_matrix(spec)
        assert len(trials) == 4

    def test_each_trial_has_unique_id(self):
        spec = _basic_spec()
        trials = expand_matrix(spec)
        ids = [t["trial_id"] for t in trials]
        assert len(set(ids)) == 4

    def test_trial_carries_model_and_config(self):
        spec = _basic_spec()
        trials = expand_matrix(spec)
        # find a trial that combines model-1 with config 1
        matched = [t for t in trials if t["model_id"] == "model-1" and t["config_bundle"]["server"]["tp"] == 1]
        assert len(matched) == 1

    def test_does_not_share_state_between_trials(self):
        spec = _basic_spec()
        trials = expand_matrix(spec)
        # each trial must own its own state field
        for t in trials:
            assert "state" in t

    def test_empty_models_raises(self):
        spec = _basic_spec()
        spec["models"] = []
        with pytest.raises(MatrixError):
            expand_matrix(spec)

    def test_empty_configs_raises(self):
        spec = _basic_spec()
        spec["configs"] = []
        with pytest.raises(MatrixError):
            expand_matrix(spec)