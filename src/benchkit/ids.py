"""ID generation and validation for benchkit.

Every ID is immutable once issued. Validation is strict: malformed IDs
raise ValueError so callers can't accidentally confuse identifiers.

ID schemes:
- benchmark_id@version:  lowercase letters, digits, dashes — e.g. "swebench-verified"
- model: provider:model_id@revision — e.g. "huggingface:Qwen/Qwen3-8B@abc1234"
- config_bundle_id: cfg-<8 hex>
- experiment_id: exp-YYYYMMDD-NNN (date-prefixed, sequential within day)
- trial_id: trial-NNNN (zero-padded sequence per experiment)
- attempt_id: attempt-NNNN
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import datetime

# ---------- regex patterns ----------

_BENCHMARK_RE = re.compile(r"^[a-z][a-z0-9-]*@[0-9]+\.[0-9]+\.[0-9]+(-[a-zA-Z0-9.]+)?$")
_MODEL_RE = re.compile(r"^[a-z][a-z0-9_]*:[^@]+@[^@]+$")
_CONFIG_BUNDLE_RE = re.compile(r"^cfg-[0-9a-f]{6,32}$")
_EXPERIMENT_RE = re.compile(r"^exp-\d{8}-\d{3,}$")
_TRIAL_RE = re.compile(r"^trial-\d{4,}$")
_ATTEMPT_RE = re.compile(r"^attempt-\d{4,}$")


# ---------- counters (file-locked) ----------

_counter_lock = threading.Lock()
_counters: dict[str, int] = {}


def _next_counter(scope: str) -> int:
    """Return the next sequence number for a given scope, in-memory.

    Counter state is intentionally not persisted — IDs are unique only
    within a single process lifetime. For shared/external uniqueness,
    combine with another stable namespace or use UUIDv4.
    """
    with _counter_lock:
        _counters[scope] = _counters.get(scope, 0) + 1
        return _counters[scope]


# ---------- benchmark / model references ----------


def validate_benchmark_ref(ref: str) -> None:
    if not isinstance(ref, str) or not _BENCHMARK_RE.match(ref):
        raise ValueError(
            f"invalid benchmark ref: {ref!r}; expected 'name@x.y.z' with lowercase name"
        )


def validate_model_ref(ref: str) -> None:
    if not isinstance(ref, str) or not _MODEL_RE.match(ref):
        raise ValueError(
            f"invalid model ref: {ref!r}; expected 'provider:model_id@revision'"
        )


# ---------- config bundle ----------


def new_config_bundle_id(payload: dict, prefix: str = "cfg") -> str:
    """Hash a JSON-serialisable config bundle into a short, immutable ID.

    The payload is canonicalised (sorted keys, no whitespace) before
    hashing so semantically-equal bundles produce the same ID regardless
    of key ordering or formatting.
    """
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    h = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}-{h}"


def validate_config_bundle_id(cid: str) -> None:
    if not isinstance(cid, str) or not _CONFIG_BUNDLE_RE.match(cid):
        raise ValueError(f"invalid config bundle id: {cid!r}")


# ---------- experiment ----------


def new_experiment_id(now: datetime | None = None) -> str:
    """Generate an experiment ID of the form exp-YYYYMMDD-NNN.

    Sequence number resets daily.
    """
    if now is None:
        now = datetime.now()
    date_part = now.strftime("%Y%m%d")
    scope = f"exp-{date_part}"
    seq = _next_counter(scope)
    return f"exp-{date_part}-{seq:03d}"


def validate_experiment_id(eid: str) -> None:
    if not isinstance(eid, str) or not _EXPERIMENT_RE.match(eid):
        raise ValueError(f"invalid experiment id: {eid!r}")


def parse_experiment_id(eid: str) -> str:
    validate_experiment_id(eid)
    return eid


# ---------- trial ----------


def new_trial_id() -> str:
    """Generate a trial id of the form trial-NNNN."""
    seq = _next_counter("trial")
    return f"trial-{seq:04d}"


def validate_trial_id(tid: str) -> None:
    if not isinstance(tid, str) or not _TRIAL_RE.match(tid):
        raise ValueError(f"invalid trial id: {tid!r}")


def parse_trial_id(tid: str) -> str:
    validate_trial_id(tid)
    return tid


# ---------- attempt ----------


def new_attempt_id(trial_dir: str | None = None) -> str:
    """Generate an attempt id of the form attempt-NNNN.

    If ``trial_dir`` is given and exists, the next sequence number is
    chosen to be one higher than any existing attempt in that directory,
    so IDs remain unique across separate process invocations.
    """
    seq = _next_counter("attempt")
    if trial_dir:
        try:
            for child in os.listdir(trial_dir):  # type: ignore[name-defined]
                m = _ATTEMPT_RE.match(child)
                if m:
                    n = int(child.split("-")[1])
                    if n >= seq:
                        seq = n + 1
        except OSError:
            pass
    return f"attempt-{seq:04d}"


def validate_attempt_id(aid: str) -> None:
    if not isinstance(aid, str) or not _ATTEMPT_RE.match(aid):
        raise ValueError(f"invalid attempt id: {aid!r}")


def parse_attempt_id(aid: str) -> str:
    validate_attempt_id(aid)
    return aid