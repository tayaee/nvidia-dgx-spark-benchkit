"""benchkit.evaluator — reproducible evaluator + report generation."""

from __future__ import annotations

from benchkit.evaluator.evaluator import (
    EvaluationResult,
    EvaluatorError,
    RunEvaluator,
    evaluate_canonical_set,
)
from benchkit.evaluator.report import load_report, write_report

__all__ = [
    "EvaluationResult",
    "EvaluatorError",
    "RunEvaluator",
    "evaluate_canonical_set",
    "load_report",
    "write_report",
]