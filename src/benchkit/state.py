"""State machine for trials, attempts, and individual instances.

Single source of truth for which transitions are legal. Anything that
mutates lifecycle state must call ``is_valid_transition`` first or
assert it through the Store API.
"""

from __future__ import annotations

from enum import Enum


class TrialStatus(str, Enum):
    PLANNED = "planned"
    QUEUED = "queued"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"
    INVALID = "invalid"


class InstanceStatus(str, Enum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    RUNNING = "running"
    CHECKPOINTED = "checkpointed"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"
    INVALID = "invalid"
    SUPERSEDED = "superseded"


# Trial status transitions.
_VALID_TRIAL: dict[TrialStatus, set[TrialStatus]] = {
    TrialStatus.PLANNED: {TrialStatus.QUEUED, TrialStatus.ABORTED},
    TrialStatus.QUEUED: {TrialStatus.RUNNING, TrialStatus.ABORTED, TrialStatus.INVALID},
    TrialStatus.RUNNING: {
        TrialStatus.PAUSED,
        TrialStatus.COMPLETED,
        TrialStatus.FAILED,
        TrialStatus.ABORTED,
    },
    TrialStatus.PAUSED: {TrialStatus.RUNNING, TrialStatus.ABORTED},
    TrialStatus.COMPLETED: set(),
    TrialStatus.FAILED: {TrialStatus.QUEUED},  # retry creates a new attempt, but trial can be re-queued
    TrialStatus.ABORTED: set(),
    TrialStatus.INVALID: set(),
}

# Instance-level transitions are a superset of the trial rules plus a
# supersede transition when a new attempt supersedes an unfinished one.
_VALID_INSTANCE: dict[InstanceStatus, set[InstanceStatus]] = {
    InstanceStatus.QUEUED: {
        InstanceStatus.CLAIMED,
        InstanceStatus.ABORTED,
        InstanceStatus.INVALID,
        InstanceStatus.SUPERSEDED,
    },
    InstanceStatus.CLAIMED: {
        InstanceStatus.RUNNING,
        InstanceStatus.ABORTED,
        InstanceStatus.FAILED,
    },
    InstanceStatus.RUNNING: {
        InstanceStatus.CHECKPOINTED,
        InstanceStatus.COMPLETED,
        InstanceStatus.FAILED,
        InstanceStatus.ABORTED,
    },
    InstanceStatus.CHECKPOINTED: {
        InstanceStatus.RUNNING,
        InstanceStatus.COMPLETED,
        InstanceStatus.FAILED,
        InstanceStatus.ABORTED,
    },
    InstanceStatus.COMPLETED: set(),
    InstanceStatus.FAILED: {InstanceStatus.QUEUED, InstanceStatus.SUPERSEDED},
    InstanceStatus.ABORTED: set(),
    InstanceStatus.INVALID: set(),
    InstanceStatus.SUPERSEDED: set(),
}

_TERMINAL_TRIAL = {TrialStatus.COMPLETED, TrialStatus.ABORTED, TrialStatus.FAILED, TrialStatus.INVALID}
_TERMINAL_INSTANCE = {
    InstanceStatus.COMPLETED,
    InstanceStatus.ABORTED,
    InstanceStatus.FAILED,
    InstanceStatus.INVALID,
    InstanceStatus.SUPERSEDED,
}


class InvalidTransition(Exception):
    pass


def is_valid_transition(src, dst) -> bool:
    """Return True iff moving from ``src`` to ``dst`` is legal.

    Raises InvalidTransition if not — call this when you want to fail
    fast, or check the bool when you want to recover gracefully.
    """
    table: dict
    if isinstance(src, TrialStatus):
        table = _VALID_TRIAL
    elif isinstance(src, InstanceStatus):
        table = _VALID_INSTANCE
    else:
        raise TypeError(f"unknown status enum: {type(src).__name__}")
    if dst not in table[src]:
        raise InvalidTransition(f"{src.value!r} -> {dst.value!r} is not allowed")
    return True


def attempt_terminal(status: TrialStatus) -> bool:
    return status in _TERMINAL_TRIAL


def instance_terminal(status: InstanceStatus) -> bool:
    return status in _TERMINAL_INSTANCE