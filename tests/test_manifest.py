"""Tests for benchkit.manifest — manifest creation, parsing, fingerprinting."""

import json

import pytest

from benchkit.ids import new_config_bundle_id, new_experiment_id, new_trial_id
from benchkit.manifest import (
    ManifestError,
    compute_config_fingerprint,
    write_manifest,
    load_manifest,
    manifest_has_required_fingerprints,
)


def _basic_payload():
    return {
        "benchmark_id": "swebench-verified",
        "benchmark_version": "1.0.0",
        "model_id": "Qwen/Qwen3-8B",
        "model_revision": "abc123",
        "precision": "nvfp4",
        "config_bundle": {"server": {"tp": 4}, "client": {"max_tokens": 2048}},
        "dataset_fingerprint": "deadbeef",
        "evaluator_version": "1.2.3",
        "evaluator_image_digest": "sha256:cafe",
        "endpoint": "http://spark1.local:8000",
        "seed": 42,
        "workers": 2,
    }


class TestConfigFingerprint:
    def test_same_payload_yields_same_fingerprint(self):
        p = _basic_payload()
        a = compute_config_fingerprint(p["config_bundle"])
        b = compute_config_fingerprint(p["config_bundle"])
        assert a == b

    def test_different_payload_yields_different_fingerprint(self):
        a = compute_config_fingerprint({"server": {"tp": 4}})
        b = compute_config_fingerprint({"server": {"tp": 8}})
        assert a != b

    def test_key_order_irrelevant(self):
        a = compute_config_fingerprint({"server": {"tp": 4}, "client": {"x": 1}})
        b = compute_config_fingerprint({"client": {"x": 1}, "server": {"tp": 4}})
        assert a == b


class TestManifestRoundTrip:
    def test_round_trip_via_file(self, tmp_path):
        path = tmp_path / "manifest.json"
        payload = _basic_payload()
        write_manifest(path, payload)
        loaded = load_manifest(path)
        assert loaded["benchmark_id"] == "swebench-verified"
        assert loaded["model_revision"] == "abc123"

    def test_rejects_missing_required_field(self, tmp_path):
        path = tmp_path / "manifest.json"
        bad = _basic_payload()
        del bad["model_revision"]
        with pytest.raises(ManifestError):
            write_manifest(path, bad)


class TestRequiredFingerprints:
    def test_passes_when_all_present(self):
        assert manifest_has_required_fingerprints(_basic_payload())

    def test_fails_when_dataset_fingerprint_missing(self):
        bad = _basic_payload()
        del bad["dataset_fingerprint"]
        assert not manifest_has_required_fingerprints(bad)

    def test_fails_when_evaluator_digest_missing(self):
        bad = _basic_payload()
        del bad["evaluator_image_digest"]
        assert not manifest_has_required_fingerprints(bad)


class TestConfigBundleId:
    def test_includes_fingerprint(self):
        cid = new_config_bundle_id({"server": {"tp": 4}})
        assert cid.startswith("cfg-")
        assert len(cid) > 10