"""Report serialisation — summary.json, breakdown.json, CSV, Markdown.

Reports carry the full provenance chain: evaluator version, image
digest, input artifact hash, raw + normalized scores, denominator.
Anything that consumes a report can verify reproducibility without
re-running the evaluator.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from benchkit.artifact import atomic_write_text
from benchkit.evaluator.evaluator import EvaluationResult


def write_report(report_dir: str, result: EvaluationResult) -> None:
    """Write ``summary.json`` + ``breakdown.json`` + ``report.csv`` + ``report.md``.

    All four are written via atomic temp-file + rename so a crash never
    produces a partial report. The directory is created if missing.
    """
    d = Path(report_dir)
    d.mkdir(parents=True, exist_ok=True)

    summary = {
        "evaluator_version": result.evaluator_version,
        "evaluator_image_digest": result.evaluator_image_digest,
        "input_artifact_hash": result.input_artifact_hash,
        "raw": result.raw,
        "normalized": result.normalized,
        "breakdown": result.breakdown,
        "denominator": result.denominator,
        "fingerprint": result.fingerprint(),
    }
    atomic_write_text(d / "summary.json", json.dumps(summary, indent=2, sort_keys=True))
    atomic_write_text(d / "breakdown.json", json.dumps(result.breakdown, indent=2, sort_keys=True))

    # CSV: one row per metric
    csv_path = d / "report.csv"
    with open(csv_path.with_suffix(".tmp"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["metric", "kind", "value"])
        for k, v in result.raw.items():
            w.writerow([k, "raw", v])
        for k, v in result.normalized.items():
            w.writerow([k, "normalized", v])
        for k, v in result.breakdown.items():
            w.writerow([k, "breakdown", v])
    csv_path.with_suffix(".tmp").replace(csv_path)

    md = [
        f"# Evaluation Report",
        f"",
        f"- Evaluator: `{result.evaluator_version}` (`{result.evaluator_image_digest}`)",
        f"- Input artifact hash: `{result.input_artifact_hash}`",
        f"- Denominator: **{result.denominator}**",
        f"",
        f"## Raw scores",
        f"",
    ]
    for k, v in result.raw.items():
        md.append(f"- **{k}**: {v}")
    md += ["", "## Normalized scores", ""]
    for k, v in result.normalized.items():
        md.append(f"- **{k}**: {v}")
    md += ["", "## Breakdown", ""]
    md.append("| bucket | count |")
    md.append("|---|---|")
    for k, v in result.breakdown.items():
        md.append(f"| {k} | {v} |")
    atomic_write_text(d / "report.md", "\n".join(md) + "\n")


def load_report(report_dir: str) -> dict:
    """Load a previously-written report's summary.

    Returns the parsed summary dict, or raises FileNotFoundError if
    ``summary.json`` is absent.
    """
    p = Path(report_dir) / "summary.json"
    return json.loads(p.read_text())