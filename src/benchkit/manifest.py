"""Manifest creation, fingerprinting, and validation.

A manifest captures the full identity of a benchmark run: dataset
fingerprint, evaluator version + image digest, model revision, config
bundle hash, seed, and worker count. Anything missing from the manifest
is treated as a deployment failure — re-running without those fields
would make the result non-reproducible.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Iterable


class ManifestError(Exception):
    pass


REQUIRED_EXPERIMENT_FIELDS = (
    "benchmark_id",
    "benchmark_version",
    "dataset_fingerprint",
    "endpoint",
)

REQUIRED_TRIAL_FIELDS = (
    "benchmark_id",
    "benchmark_version",
    "model_id",
    "model_revision",
    "precision",
    "config_bundle",
    "dataset_fingerprint",
    "evaluator_version",
    "evaluator_image_digest",
    "endpoint",
    "seed",
    "workers",
)


def compute_config_fingerprint(bundle: dict) -> str:
    """SHA-256 of a canonicalised JSON bundle, returned as hex.

    Key-order insensitive: ``{"a":1,"b":2}`` and ``{"b":2,"a":1}`` hash
    to the same value. Whitespace is irrelevant.
    """
    canonical = json.dumps(bundle, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def manifest_has_required_fingerprints(payload: dict, level: str = "trial") -> bool:
    fields = REQUIRED_TRIAL_FIELDS if level == "trial" else REQUIRED_EXPERIMENT_FIELDS
    for f in fields:
        if f not in payload:
            return False
        if payload[f] in (None, "", []):
            return False
    return True


def write_manifest(path: Path, payload: dict, level: str = "trial") -> None:
    """Validate then write a manifest atomically.

    Raises ManifestError if any required fingerprint is missing.
    Level is 'trial' (default, full fingerprints) or 'experiment'
    (benchmark + dataset + endpoint only — model-specific fields
    belong in the per-trial manifest).
    """
    if not manifest_has_required_fingerprints(payload, level=level):
        fields = REQUIRED_TRIAL_FIELDS if level == "trial" else REQUIRED_EXPERIMENT_FIELDS
        missing = [f for f in fields if f not in payload]
        raise ManifestError(f"missing required manifest fields: {missing}")
    payload = dict(payload)
    if level == "trial":
        payload.setdefault(
            "config_fingerprint",
            compute_config_fingerprint(payload["config_bundle"]),
        )
    from benchkit.artifact import atomic_write_text

    atomic_write_text(path, json.dumps(payload, indent=2, sort_keys=True))


def load_manifest(path: Path) -> dict:
    return json.loads(Path(path).read_text())


def merge_legacy_env(
    payload: dict,
    run_id: str | None,
    script_ver: str | None,
    tune_no: str | None = None,
) -> dict:
    """Accept legacy RUN_ID / SCRIPT_VER (TUNE_NO) env vars as canonical aliases.

    The new manifest carries its own canonical IDs; legacy environment
    variables are preserved as aliases only — they never replace the
    canonical experiment_id / config_bundle_id.

    SCRIPT_VER is the canonical config (server/client settings) version
    number. TUNE_NO is kept as a documented legacy alias for backwards
    compatibility; callers should prefer SCRIPT_VER.
    """
    if run_id:
        payload.setdefault("run_id_alias", run_id)
    if script_ver:
        payload.setdefault("script_ver_alias", script_ver)
    if tune_no:
        payload.setdefault("tune_no_alias", tune_no)
    return payload