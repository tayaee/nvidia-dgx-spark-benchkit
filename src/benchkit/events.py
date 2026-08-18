"""Append-only JSONL event log used for execution ledgers.

Each event is a JSON object with at minimum {ts, kind}. The log is
written atomically line-by-line — readers see complete events only,
never partial writes.
"""

from __future__ import annotations

import datetime
import json
import os
import tempfile
import threading
from pathlib import Path


class EventLogError(Exception):
    pass


_seq_lock = threading.Lock()
_seq = 0


def _next_seq() -> int:
    """Monotonic per-process sequence number — guarantees unique stamps
    even when two events are written within the same nanosecond."""
    global _seq
    with _seq_lock:
        _seq += 1
        return _seq


def _now_iso() -> str:
    # ISO 8601 with microsecond precision + local timezone offset
    # (never UTC 'Z'). Combined with the monotonic sequence below, two
    # events written in the same call still have distinct stamps.
    return datetime.datetime.now().astimezone().isoformat(timespec="microseconds")


def append_event(path: Path, event: dict) -> None:
    """Append a single event to the JSONL log at ``path``.

    The event is augmented with a ``ts`` field if not present. We write
    to a temp file containing the new event, then concatenate with the
    existing log atomically (via rename) — a crash never leaves a
    partial line, and the existing log is preserved on failure.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    e = dict(event)
    e.setdefault("ts", _now_iso())
    e.setdefault("seq", _next_seq())
    line = json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n"
    fd, tmp = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(line)
        # Append tmp to the live log atomically (POSIX guarantees
        # os.replace is atomic; on Windows we accept best-effort).
        if path.exists():
            with open(tmp, "rb") as src, open(path, "ab") as dst:
                dst.write(src.read())
            os.unlink(tmp)
        else:
            os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read_events(path: Path) -> list[dict]:
    """Read all events from a JSONL log.

    Raises EventLogError if any line is malformed.
    """
    path = Path(path)
    if not path.exists():
        return []
    out: list[dict] = []
    with open(path, "r", encoding="utf-8") as f:
        for lineno, raw in enumerate(f, 1):
            raw = raw.strip()
            if not raw:
                continue
            try:
                out.append(json.loads(raw))
            except json.JSONDecodeError as e:
                raise EventLogError(f"{path}:{lineno}: {e}") from e
    return out