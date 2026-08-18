"""Experiment matrix expansion into trials.

A spec lists benchmark, model list, config list, endpoint, etc. The
expansion is the Cartesian product (model x config), each producing
one immutable trial. Pre-existing trials are never modified — re-running
expansion always produces the same trial_ids for the same inputs.
"""

from __future__ import annotations

import copy
from typing import Iterable

from benchkit.ids import (
    new_config_bundle_id,
    new_trial_id,
    validate_benchmark_ref,
)


class MatrixError(Exception):
    pass


def _validate_spec(spec: dict) -> None:
    for key in ("experiment_id", "benchmark_id", "benchmark_version",
                "dataset_fingerprint", "endpoint", "seed", "workers"):
        if key not in spec:
            raise MatrixError(f"missing field: {key}")
    if not spec.get("models"):
        raise MatrixError("spec has no models")
    if not spec.get("configs"):
        raise MatrixError("spec has no configs")
    for m in spec["models"]:
        if "model_id" not in m or "model_revision" not in m:
            raise MatrixError(f"model missing id/revision: {m}")
    for c in spec["configs"]:
        if not isinstance(c, dict):
            raise MatrixError(f"config must be a dict: {c!r}")


def expand_matrix(spec: dict) -> list[dict]:
    """Return the list of trial dicts implied by ``spec``.

    Each trial carries the resolved benchmark ref, model, config bundle
    (with its computed fingerprint), state="planned", and a unique
    trial_id. Order is deterministic: sorted by model_id then config
    fingerprint, so two runs with identical inputs produce identical
    trial_id sequences (assuming the in-process counter resets).
    """
    _validate_spec(spec)

    trials: list[dict] = []
    models = sorted(spec["models"], key=lambda m: m["model_id"])
    configs = sorted(spec["configs"], key=lambda c: new_config_bundle_id(c))

    for m in models:
        for c in configs:
            cb_id = new_config_bundle_id(c)
            trials.append({
                "trial_id": new_trial_id(),
                "experiment_id": spec["experiment_id"],
                "benchmark_id": spec["benchmark_id"],
                "benchmark_version": spec["benchmark_version"],
                "model_id": m["model_id"],
                "model_revision": m["model_revision"],
                "precision": m.get("precision", "fp16"),
                "config_bundle_id": cb_id,
                "config_bundle": copy.deepcopy(c),
                "endpoint": spec["endpoint"],
                "dataset_fingerprint": spec["dataset_fingerprint"],
                "seed": spec["seed"],
                "workers": spec["workers"],
                "state": "planned",
            })
    return trials