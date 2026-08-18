"""Reproducible evaluator.

The evaluator is deliberately pure-data: it consumes canonical artifacts
that the runner produced and emits a structured EvaluationResult. There
is no log-scraping, no heuristic parsing of free-form output — every
input hash and every evaluator fingerprint is recorded so that two
evaluator runs over the same inputs always produce identical reports.

Output:
- raw:         counts as produced (e.g. resolved=5)
- normalized:  random-baseline-corrected score
- breakdown:   per-bucket counts (resolved / failed / missing / invalid)
- denominator: total considered
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


class EvaluatorError(Exception):
    pass


@dataclass
class EvaluationResult:
    evaluator_version: str
    evaluator_image_digest: str
    input_artifact_hash: str
    raw: dict
    normalized: dict
    breakdown: dict
    denominator: int
    created_at: str = ""

    def __post_init__(self):
        if not self.evaluator_version:
            raise EvaluatorError("evaluator_version is required")
        if not self.evaluator_image_digest:
            raise EvaluatorError("evaluator_image_digest is required")
        if self.denominator < 0:
            raise EvaluatorError(f"denominator must be >= 0, got {self.denominator}")
        # mirror denominator into breakdown for self-contained reports
        self.breakdown = dict(self.breakdown)
        self.breakdown.setdefault("denominator", self.denominator)

    def to_dict(self) -> dict:
        return asdict(self)

    def fingerprint(self) -> str:
        """Stable hash of everything that affects the score.

        Used by tests to confirm reproducibility — two evaluation runs
        on the same input must produce the same fingerprint.
        """
        canonical = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def evaluate_canonical_set(canonical_set: list[dict]) -> dict:
    """Bucket canonical predictions into resolved / failed / invalid / missing.

    - resolved : has a non-empty model_patch (or "prediction" field)
    - failed   : present but no patch at all
    - missing  : not in canonical_set at all (caller handles)
    - invalid  : present but the patch is explicitly empty / malformed
    """
    resolved = failed = invalid = 0
    for c in canonical_set:
        if c.get("_invalid"):
            invalid += 1
            continue
        # explicit empty string is "invalid"; missing field is "failed"
        if "model_patch" in c or "prediction" in c:
            patch = c.get("model_patch", c.get("prediction"))
            if isinstance(patch, str) and patch.strip():
                resolved += 1
            elif isinstance(patch, str) and patch == "":
                invalid += 1
            else:
                failed += 1
        else:
            failed += 1
    return {
        "resolved": resolved,
        "failed": failed,
        "missing": 0,
        "invalid": invalid,
        "denominator": len(canonical_set),
    }


class RunEvaluator:
    """Stateless evaluator that scores a single trial's selected attempt.

    The evaluator version + image digest are recorded in every report
    so a future re-run can be told apart from the original.
    """

    def __init__(self, evaluator_version: str, image_digest: str, random_baseline: float = 0.0):
        if not evaluator_version:
            raise EvaluatorError("evaluator_version is required")
        if not image_digest:
            raise EvaluatorError("image_digest is required")
        self.evaluator_version = evaluator_version
        self.image_digest = image_digest
        self.random_baseline = random_baseline

    def evaluate(
        self,
        trial_path: str,
        canonical_set: list[dict] | None = None,
        selected_attempt: str | None = None,
    ) -> EvaluationResult:
        """Score one trial. If ``canonical_set`` is None, load it from disk.

        Reads only ``<trial>/attempts/<selected or 'selected.json'>/canonical/``
        — never touches the raw trajectory or any log file. The
        "canonical" set is the score contract; the raw set is just audit.
        """
        trial = Path(trial_path)
        if canonical_set is None:
            sel = selected_attempt
            if sel is None:
                sel_path = trial / "selected.json"
                if sel_path.exists():
                    sel = json.loads(sel_path.read_text()).get("attempt_id")
            if not sel:
                raise EvaluatorError(f"trial {trial} has no selected.json")
            canonical_dir = trial / "attempts" / sel / "canonical"
            if not canonical_dir.is_dir():
                raise EvaluatorError(f"canonical dir not found: {canonical_dir}")
            canonical_set = []
            for p in sorted(canonical_dir.glob("*.json")):
                canonical_set.append(json.loads(p.read_text()))

        breakdown = evaluate_canonical_set(canonical_set)
        raw = {"resolved": breakdown["resolved"]}
        # Normalize against random baseline if provided.
        denom = max(breakdown["denominator"], 1)
        norm = max((raw["resolved"] - self.random_baseline * denom) / denom, 0.0)
        normalized = {"resolved_normalized": norm}

        artifact_hash = _hash_canonical(canonical_set)
        return EvaluationResult(
            evaluator_version=self.evaluator_version,
            evaluator_image_digest=self.image_digest,
            input_artifact_hash=artifact_hash,
            raw=raw,
            normalized=normalized,
            breakdown=breakdown,
            denominator=breakdown["denominator"],
        )


def _hash_canonical(canonical_set: list[dict]) -> str:
    """Stable hash over the canonical artifact set."""
    canonical = json.dumps(canonical_set, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()