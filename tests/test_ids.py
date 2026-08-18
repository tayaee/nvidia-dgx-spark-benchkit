"""Tests for benchkit.ids — ID generation and validation."""

import pytest

from benchkit.ids import (
    new_attempt_id,
    new_config_bundle_id,
    new_experiment_id,
    new_trial_id,
    parse_attempt_id,
    parse_experiment_id,
    parse_trial_id,
    validate_attempt_id,
    validate_benchmark_ref,
    validate_config_bundle_id,
    validate_experiment_id,
    validate_model_ref,
    validate_trial_id,
)


class TestBenchmarkRef:
    def test_accepts_id_at_version(self):
        validate_benchmark_ref("swebench-verified@1.0.0")

    def test_accepts_id_at_semver_with_prerelease(self):
        validate_benchmark_ref("swebench-pro@2.1.0-rc.1")

    def test_rejects_missing_version(self):
        with pytest.raises(ValueError):
            validate_benchmark_ref("swebench-verified")

    def test_rejects_empty(self):
        with pytest.raises(ValueError):
            validate_benchmark_ref("")

    def test_rejects_id_with_uppercase(self):
        with pytest.raises(ValueError):
            validate_benchmark_ref("SWEbench@1.0.0")


class TestModelRef:
    def test_accepts_provider_model_revision(self):
        validate_model_ref("huggingface:Qwen/Qwen3-8B@abc123")

    def test_rejects_revision(self):
        with pytest.raises(ValueError):
            validate_model_ref("Qwen/Qwen3-8B")


class TestConfigBundleId:
    def test_round_trip(self):
        cid = new_config_bundle_id({"server": {"tp": 4}}, prefix="cfg")
        validate_config_bundle_id(cid)
        assert cid.startswith("cfg-")

    def test_rejects_malformed(self):
        with pytest.raises(ValueError):
            validate_config_bundle_id("not-a-cfg-id")


class TestExperimentId:
    def test_round_trip(self):
        eid = new_experiment_id()
        validate_experiment_id(eid)
        parts = parse_experiment_id(eid)
        assert parts.startswith("exp-")

    def test_format_is_iso_date(self):
        eid = new_experiment_id()
        # exp-YYYYMMDD-NNN
        import re
        assert re.match(r"^exp-\d{8}-\d{3,}$", eid)


class TestTrialId:
    def test_round_trip(self):
        tid = new_trial_id()
        validate_trial_id(tid)
        parts = parse_trial_id(tid)
        assert parts.startswith("trial-")


class TestAttemptId:
    def test_round_trip(self):
        aid = new_attempt_id()
        validate_attempt_id(aid)
        # attempt-NNNN
        assert aid.startswith("attempt-")