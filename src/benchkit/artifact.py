"""Atomic writes and attempt directory layout.

Every file in a benchmark run is written via temp-file + os.replace so
that a crash leaves either the previous complete file or a fully
written one — never a half-written file.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path


class ArtifactError(Exception):
    pass


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically (tmp + rename)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    atomic_write_bytes(path, text.encode(encoding))


def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def attempt_dir(experiment_root: str, trial_id: str, attempt_id: str) -> str:
    """Return the on-disk path of an attempt directory.

    Layout (see docs/spec/spec-benchmark-orchestration.md):
        <experiment_root>/trials/<trial_id>/attempts/<attempt_id>
    """
    return os.path.join(experiment_root, "trials", trial_id, "attempts", attempt_id)


def ensure_attempt_layout(attempt_path: Path) -> Path:
    """Create the canonical subdirectories and empty ledgers under an attempt.

    Idempotent — running it on an already-created attempt is a no-op.
    Returns the attempt path.
    """
    attempt_path = Path(attempt_path)
    for sub in ("raw", "canonical", "logs", "checkpoints"):
        (attempt_path / sub).mkdir(parents=True, exist_ok=True)
    # touch empty ledgers so readers can `open(..., "a")` immediately
    (attempt_path / "events.jsonl").touch(exist_ok=True)
    (attempt_path / "state.jsonl").touch(exist_ok=True)
    return attempt_path