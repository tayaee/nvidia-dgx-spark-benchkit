"""Scheduler — queue + lease + heartbeat + retry on top of the Store.

This is the orchestrator the Web UI talks to. A worker process acquires
a lease on a trial by calling ``TrialQueue.claim_next`` and renews it
via ``WorkerLease.heartbeat``. If the worker dies without releasing the
lease, the next claim attempt after ``expires_at`` will reclaim it.

The scheduler is single-process by design — concurrency comes from
many worker processes each calling ``claim_next`` against the shared
SQLite store. The store's WAL mode keeps reads cheap while a single
writer serialises lease mutation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

from benchkit.state import TrialStatus, is_valid_transition


class HeartbeatTimeout(Exception):
    """Raised when a heartbeat is attempted after the lease expired."""


class LeaseLost(Exception):
    """Raised when the underlying lease row no longer belongs to us."""


@dataclass
class SchedulerConfig:
    heartbeat_ttl_seconds: int = 30
    max_heartbeat_skew_seconds: int = 5


@dataclass
class WorkerLease:
    """Handle returned by ``TrialQueue.claim_next``.

    Holds the trial/attempt ids + holder name so workers can call
    ``heartbeat()`` and ``release()`` without reaching into the store.
    """

    trial_id: str
    attempt_id: str
    holder: str
    store: object = field(repr=False)
    config: SchedulerConfig = field(default_factory=SchedulerConfig)
    expires_at: float = 0.0

    def heartbeat(self) -> None:
        """Renew the lease. Raises ``LeaseLost`` if the row is gone or
        held by another holder (e.g. someone stole our expired lease)."""
        now = time.time()
        if now >= self.expires_at + self.config.max_heartbeat_skew_seconds:
            raise HeartbeatTimeout("lease already expired; cannot heartbeat")
        new_expiry = now + self.config.heartbeat_ttl_seconds
        cur = self.store._conn.execute(
            "SELECT holder FROM leases WHERE attempt_id=? AND instance_id=?",
            (self.attempt_id, self.trial_id),
        ).fetchone()
        if cur is None or cur["holder"] != self.holder:
            raise LeaseLost(f"lease for {self.trial_id} no longer held by {self.holder}")
        self.store._conn.execute(
            "UPDATE leases SET acquired_at=?, expires_at=? WHERE attempt_id=? AND instance_id=? AND holder=?",
            (now, new_expiry, self.attempt_id, self.trial_id, self.holder),
        )
        self.expires_at = new_expiry

    def release(self) -> bool:
        return self.store.release_lease(self.attempt_id, self.trial_id, self.holder)


class TrialQueue:
    """FIFO queue over the trials table for one experiment.

    The queue is implemented as ``status='queued'`` rows in the ``trials``
    table — enqueue flips a ``planned``/``failed`` trial to ``queued``,
    ``pop`` returns the oldest one. Concurrent safety comes from the
    store's lease table: two workers cannot both claim the same trial
    while a lease is live.
    """

    def __init__(self, store):
        self.store = store

    def enqueue(self, experiment_id: str) -> int:
        """Flip all non-terminal trials for this experiment to QUEUED.

        Returns the number of trials queued. Already-completed trials
        are skipped — the contract: completed work is never re-run.
        """
        rows = self.store._conn.execute(
            "SELECT id, status FROM trials WHERE experiment_id=?",
            (experiment_id,),
        ).fetchall()
        queued = 0
        for r in rows:
            cur = TrialStatus(r["status"])
            if cur in (TrialStatus.COMPLETED, TrialStatus.ABORTED, TrialStatus.INVALID):
                continue
            if cur == TrialStatus.PLANNED:
                is_valid_transition(cur, TrialStatus.QUEUED)
                self.store.set_trial_status(r["id"], TrialStatus.QUEUED)
                queued += 1
            elif cur == TrialStatus.FAILED:
                # retry from failed: bring it back to queued
                is_valid_transition(cur, TrialStatus.QUEUED)
                self.store.set_trial_status(r["id"], TrialStatus.QUEUED)
                queued += 1
            elif cur == TrialStatus.PAUSED:
                # resume path: also accepted as a re-queue
                is_valid_transition(cur, TrialStatus.RUNNING)
                # keep it paused; runner will claim via worker -> running
                # but treat as "available" by leaving PAUSED alone.
                pass
            # already QUEUED/RUNNING — nothing to do
        return queued

    def peek(self, experiment_id: str) -> Optional[str]:
        row = self.store._conn.execute(
            "SELECT id FROM trials WHERE experiment_id=? AND status='queued' "
            "ORDER BY created_at, id LIMIT 1",
            (experiment_id,),
        ).fetchone()
        return row["id"] if row else None

    def pop(self, experiment_id: str) -> Optional[str]:
        """Pop the head trial — flips its status to RUNNING so it is no
        longer in the queue. Useful for diagnostics; for actual worker
        dispatch use ``claim_next`` which also acquires a lease.
        """
        tid = self.peek(experiment_id)
        if tid is None:
            return None
        try:
            self.store.set_trial_status(tid, TrialStatus.RUNNING)
        except Exception:
            return None
        return tid

    def claim_next(
        self,
        experiment_id: str,
        holder: str,
        config: Optional[SchedulerConfig] = None,
    ) -> Optional[WorkerLease]:
        """Try to acquire a lease on the next runnable trial.

        Returns ``None`` if no trial is enqueued, or if every candidate
        is currently held by another worker.
        """
        cfg = config or SchedulerConfig()
        rows = self.store._conn.execute(
            "SELECT id FROM trials WHERE experiment_id=? AND status='queued' "
            "ORDER BY created_at, id",
            (experiment_id,),
        ).fetchall()
        for r in rows:
            tid = r["id"]
            # find or create an attempt row for this trial
            attempt_row = self.store._conn.execute(
                "SELECT id FROM attempts WHERE trial_id=? ORDER BY created_at DESC LIMIT 1",
                (tid,),
            ).fetchone()
            if attempt_row is None:
                # no attempt exists yet — caller should have created one.
                continue
            aid = attempt_row["id"]
            ok = self.store.acquire_lease(aid, tid, holder, cfg.heartbeat_ttl_seconds)
            if not ok:
                # someone else holds a live lease on this trial's attempt
                continue
            # flip trial to RUNNING
            try:
                self.store.set_trial_status(tid, TrialStatus.RUNNING)
            except Exception:
                # Couldn't transition — release lease, try the next trial.
                self.store.release_lease(aid, tid, holder)
                continue
            return WorkerLease(
                trial_id=tid,
                attempt_id=aid,
                holder=holder,
                store=self.store,
                config=cfg,
                expires_at=time.time() + cfg.heartbeat_ttl_seconds,
            )
        return None

    def complete(
        self,
        lease: WorkerLease,
        status: TrialStatus = TrialStatus.COMPLETED,
    ) -> None:
        """Mark a leased trial as complete (or failed) and release the lease."""
        is_valid_transition(TrialStatus.RUNNING, status)
        self.store.set_trial_status(lease.trial_id, status)
        lease.release()

    def retry(self, trial_id: str) -> None:
        """Reset a terminal-failure trial back to QUEUED for re-run."""
        cur_row = self.store._conn.execute(
            "SELECT status FROM trials WHERE id=?", (trial_id,)
        ).fetchone()
        if cur_row is None:
            raise KeyError(trial_id)
        cur = TrialStatus(cur_row["status"])
        if cur != TrialStatus.FAILED:
            raise RuntimeError(f"cannot retry trial in status {cur.value!r}")
        is_valid_transition(cur, TrialStatus.QUEUED)
        self.store.set_trial_status(trial_id, TrialStatus.QUEUED)