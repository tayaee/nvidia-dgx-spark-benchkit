"""Tests for benchkit.store — SQLite metadata + lease/state transitions."""

import pytest

from benchkit.ids import new_attempt_id, new_experiment_id, new_trial_id
from benchkit.state import (
    InstanceStatus,
    TrialStatus,
    attempt_terminal,
    is_valid_transition,
)
from benchkit.store import Store


@pytest.fixture
def store(tmp_path):
    db = tmp_path / "meta.db"
    return Store(db)


class TestStoreBasics:
    def test_create_and_get_experiment(self, store):
        eid = new_experiment_id()
        store.create_experiment(eid, {"benchmark": "swebench-verified"})
        assert store.get_experiment(eid)["benchmark"] == "swebench-verified"

    def test_create_trial_and_attempt(self, store):
        eid = new_experiment_id()
        tid = new_trial_id()
        aid = new_attempt_id()
        store.create_experiment(eid, {})
        store.create_trial(eid, tid, {"model_id": "m1"})
        store.create_attempt(tid, aid, {})
        assert store.get_trial(tid)["model_id"] == "m1"
        assert store.get_attempt(aid)["trial_id"] == tid

    def test_list_trials_for_experiment(self, store):
        eid = new_experiment_id()
        store.create_experiment(eid, {})
        for _ in range(3):
            store.create_trial(eid, new_trial_id(), {})
        assert len(store.list_trials(eid)) == 3


class TestStateTransitions:
    def test_valid_transition_queued_to_claimed(self):
        assert is_valid_transition(InstanceStatus.QUEUED, InstanceStatus.CLAIMED)

    def test_invalid_terminal_transition_rejected(self):
        with pytest.raises(Exception):
            is_valid_transition(InstanceStatus.COMPLETED, InstanceStatus.RUNNING)

    def test_terminal_statuses_are_terminal(self):
        assert attempt_terminal(TrialStatus.COMPLETED)
        assert attempt_terminal(TrialStatus.ABORTED)
        assert attempt_terminal(TrialStatus.FAILED)
        assert attempt_terminal(TrialStatus.INVALID)
        assert not attempt_terminal(TrialStatus.PLANNED)


class TestLeases:
    def test_claim_sets_holder_and_expiry(self, store):
        aid = new_attempt_id()
        eid = new_experiment_id()
        tid = new_trial_id()
        store.create_experiment(eid, {})
        store.create_trial(eid, tid, {})
        store.create_attempt(tid, aid, {})
        # lease for instance-1
        ok = store.acquire_lease(aid, "instance-1", "worker-a", ttl_seconds=60)
        assert ok
        lease = store.get_active_lease(aid, "instance-1")
        assert lease["holder"] == "worker-a"

    def test_second_claim_is_rejected_until_expiry(self, store):
        aid = new_attempt_id()
        eid = new_experiment_id()
        tid = new_trial_id()
        store.create_experiment(eid, {})
        store.create_trial(eid, tid, {})
        store.create_attempt(tid, aid, {})
        store.acquire_lease(aid, "instance-1", "worker-a", ttl_seconds=60)
        ok = store.acquire_lease(aid, "instance-1", "worker-b", ttl_seconds=60)
        assert not ok

    def test_release_allows_reclaim(self, store):
        aid = new_attempt_id()
        eid = new_experiment_id()
        tid = new_trial_id()
        store.create_experiment(eid, {})
        store.create_trial(eid, tid, {})
        store.create_attempt(tid, aid, {})
        store.acquire_lease(aid, "instance-1", "worker-a", ttl_seconds=60)
        store.release_lease(aid, "instance-1", "worker-a")
        ok = store.acquire_lease(aid, "instance-1", "worker-b", ttl_seconds=60)
        assert ok


class TestResume:
    def test_resume_skips_completed_instances(self, store):
        eid = new_experiment_id()
        tid = new_trial_id()
        store.create_experiment(eid, {})
        store.create_trial(eid, tid, {})
        # mark instance-1 completed, instance-2 not
        store.mark_instance_completed(tid, "instance-1")
        store.mark_instance_queued(tid, "instance-2")
        pending = store.list_pending_instances(tid)
        assert pending == ["instance-2"]


class TestAliasCompat:
    def test_run_id_alias_accepted(self, store):
        # legacy RUN_ID / SCRIPT_VER (TUNE_NO) alias resolution: store accepts them
        # as canonical IDs, not as replacements for the canonical experiment_id.
        store.create_experiment("exp-20260101-001", {"run_id_alias": "1"})
        loaded = store.get_experiment("exp-20260101-001")
        assert loaded["run_id_alias"] == "1"