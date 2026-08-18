"""Scheduler tests — queue, lease, heartbeat, retry, cooperative cancel."""

from __future__ import annotations

import time

import pytest

from benchkit.scheduler.scheduler import (
    HeartbeatTimeout,
    LeaseLost,
    SchedulerConfig,
    TrialQueue,
    WorkerLease,
)
from benchkit.state import TrialStatus


@pytest.fixture()
def store(tmp_path):
    from benchkit.store import Store

    return Store(tmp_path / "benchkit.db")


@pytest.fixture()
def seeded(store):
    """Seed an experiment with two trials in PLANNED state."""
    from benchkit.ids import new_attempt_id, new_experiment_id, new_trial_id

    eid = new_experiment_id()
    store.create_experiment(eid, {"benchmark_id": "x@1.0.0"})
    tids = []
    for _ in range(2):
        tid = new_trial_id()
        store.create_trial(eid, tid, {"model_id": "m:org/name@rev"})
        aid = new_attempt_id()
        store.create_attempt(tid, aid, {})
        tids.append((tid, aid))
    return store, eid, tids


def _first_trial(seeded):
    store, eid, tids = seeded
    return store, eid, tids[0]


def test_queue_enqueues_and_pops_in_order(seeded):
    store, eid, tids = seeded
    q = TrialQueue(store)
    q.enqueue(eid)
    assert q.peek(eid) == tids[0][0]
    nxt = q.pop(eid)
    assert nxt == tids[0][0]
    assert q.peek(eid) == tids[1][0]


def test_acquire_lease_returns_worker_lease_and_persists(seeded):
    store, eid, (tid, aid) = _first_trial(seeded)
    sched_cfg = SchedulerConfig(heartbeat_ttl_seconds=10)
    q = TrialQueue(store)
    q.enqueue(eid)

    claim = q.claim_next(eid, holder="worker-1", config=sched_cfg)
    assert claim is not None
    assert claim.trial_id == tid
    assert claim.attempt_id == aid
    assert claim.holder == "worker-1"
    assert claim.expires_at > time.time()

    active = store.get_active_lease(aid, tid)
    assert active is not None
    assert active["holder"] == "worker-1"


def test_lease_renewal_extends_heartbeat(seeded):
    store, eid, (tid, aid) = _first_trial(seeded)
    sched_cfg = SchedulerConfig(heartbeat_ttl_seconds=1)
    q = TrialQueue(store)
    q.enqueue(eid)
    claim = q.claim_next(eid, holder="worker-1", config=sched_cfg)
    initial_expiry = claim.expires_at
    time.sleep(0.05)
    claim.heartbeat()
    assert claim.expires_at > initial_expiry


def test_second_worker_cannot_steal_active_lease(seeded):
    store, eid, (tid, aid) = _first_trial(seeded)
    sched_cfg = SchedulerConfig(heartbeat_ttl_seconds=10)
    q = TrialQueue(store)
    q.enqueue(eid)
    first = q.claim_next(eid, holder="worker-1", config=sched_cfg)
    assert first is not None
    assert first.trial_id == tid
    # Force the trial back to QUEUED so a second claim could target it.
    store._conn.execute(
        "UPDATE trials SET status=? WHERE id=?", (TrialStatus.QUEUED.value, tid)
    )
    second = q.claim_next(eid, holder="worker-2", config=sched_cfg)
    # claim_next skips trials with active leases — the second worker
    # moves on to a different trial; the original lease is preserved.
    assert second is not None
    assert second.trial_id != tid
    assert store.get_active_lease(aid, tid)["holder"] == "worker-1"


def test_lease_can_be_reclaimed_after_heartbeat_timeout(seeded):
    store, eid, (tid, aid) = _first_trial(seeded)
    sched_cfg = SchedulerConfig(heartbeat_ttl_seconds=1)
    q = TrialQueue(store)
    q.enqueue(eid)
    first = q.claim_next(eid, holder="worker-1", config=sched_cfg)
    assert first is not None
    time.sleep(1.2)  # let lease expire
    store._conn.execute(
        "UPDATE trials SET status=? WHERE id=?", (TrialStatus.QUEUED.value, tid)
    )
    second = q.claim_next(eid, holder="worker-2", config=sched_cfg)
    assert second is not None
    assert second.holder == "worker-2"


def test_complete_trial_persists_status_and_drains_queue(seeded):
    store, eid, tids = seeded
    q = TrialQueue(store)
    q.enqueue(eid)
    sched_cfg = SchedulerConfig(heartbeat_ttl_seconds=10)
    claim = q.claim_next(eid, holder="w", config=sched_cfg)
    assert claim is not None
    q.complete(claim, status=TrialStatus.COMPLETED)
    cur = store._conn.execute(
        "SELECT status FROM trials WHERE id=?", (claim.trial_id,)
    ).fetchone()
    assert cur["status"] == "completed"
    assert q.peek(eid) == tids[1][0]


def test_retry_resets_trial_and_re_enqueues(seeded):
    store, eid, (tid, aid) = _first_trial(seeded)
    q = TrialQueue(store)
    q.enqueue(eid)
    sched_cfg = SchedulerConfig(heartbeat_ttl_seconds=10)
    claim = q.claim_next(eid, holder="w", config=sched_cfg)
    q.complete(claim, status=TrialStatus.FAILED)
    q.retry(tid)
    cur = store._conn.execute(
        "SELECT status FROM trials WHERE id=?", (tid,)
    ).fetchone()
    assert cur["status"] == "queued"
    assert q.peek(eid) == tid


def test_retry_on_completed_trial_raises(seeded):
    store, eid, (tid, aid) = _first_trial(seeded)
    q = TrialQueue(store)
    q.enqueue(eid)
    sched_cfg = SchedulerConfig(heartbeat_ttl_seconds=10)
    claim = q.claim_next(eid, holder="w", config=sched_cfg)
    q.complete(claim, status=TrialStatus.COMPLETED)
    with pytest.raises(Exception):
        q.retry(tid)


def test_release_lease_returns_bool(seeded):
    store, eid, (tid, aid) = _first_trial(seeded)
    q = TrialQueue(store)
    q.enqueue(eid)
    sched_cfg = SchedulerConfig(heartbeat_ttl_seconds=10)
    claim = q.claim_next(eid, holder="w", config=sched_cfg)
    assert claim.release() is True
    assert store.get_active_lease(aid, tid) is None
    assert claim.release() is False


def test_worker_lease_dataclass_carries_metadata(seeded):
    store, eid, (tid, aid) = _first_trial(seeded)
    sched_cfg = SchedulerConfig(heartbeat_ttl_seconds=10)
    q = TrialQueue(store)
    q.enqueue(eid)
    claim = q.claim_next(eid, holder="worker-x", config=sched_cfg)
    assert claim.holder == "worker-x"
    assert claim.attempt_id == aid
    assert claim.trial_id == tid
    assert isinstance(claim.expires_at, float)


def test_completed_trials_not_reenqueued_by_enqueue(seeded):
    """Acceptance: already-completed trials are not re-run on resume."""
    store, eid, (tid, aid) = _first_trial(seeded)
    q = TrialQueue(store)
    q.enqueue(eid)
    sched_cfg = SchedulerConfig(heartbeat_ttl_seconds=10)
    claim = q.claim_next(eid, holder="w", config=sched_cfg)
    q.complete(claim, status=TrialStatus.COMPLETED)
    q.enqueue(eid)
    queued_rows = store._conn.execute(
        "SELECT id FROM trials WHERE experiment_id=? AND status=?",
        (eid, TrialStatus.QUEUED.value),
    ).fetchall()
    assert len(queued_rows) == 1
    assert queued_rows[0]["id"] != tid