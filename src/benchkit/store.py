"""SQLite-backed metadata store for experiments, trials, attempts, and leases.

Designed to be the source of truth for the scheduler (issue 4): pause,
resume, cancel, retry, status all read/write through this single object.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from benchkit.state import InstanceStatus, TrialStatus, is_valid_transition


SCHEMA = """
CREATE TABLE IF NOT EXISTS experiments (
    id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS trials (
    id TEXT PRIMARY KEY,
    experiment_id TEXT NOT NULL REFERENCES experiments(id),
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'planned',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS attempts (
    id TEXT PRIMARY KEY,
    trial_id TEXT NOT NULL REFERENCES trials(id),
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    created_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS instance_state (
    trial_id TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    updated_at REAL NOT NULL,
    PRIMARY KEY (trial_id, instance_id)
);

CREATE TABLE IF NOT EXISTS leases (
    attempt_id TEXT NOT NULL,
    instance_id TEXT NOT NULL,
    holder TEXT NOT NULL,
    acquired_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    PRIMARY KEY (attempt_id, instance_id)
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    attempt_id TEXT,
    trial_id TEXT,
    payload_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
"""


class Store:
    """Thin wrapper over sqlite3 with helper methods for the benchkit domain.

    Connections are opened in WAL mode so concurrent readers (e.g. the
    web API) don't block writers (the scheduler / worker).
    """

    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False so the runner's worker threads can use
        # the same store. sqlite3's connection lock serialises access,
        # so this is safe across threads.
        self._conn = sqlite3.connect(str(self.db_path), isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        for stmt in SCHEMA.strip().split(";"):
            stmt = stmt.strip()
            if stmt:
                self._conn.execute(stmt)

    # ----- experiments -----

    def create_experiment(self, eid: str, payload: dict) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO experiments(id, payload_json, created_at) VALUES (?, ?, ?)",
            (eid, json.dumps(payload, sort_keys=True), time.time()),
        )

    def get_experiment(self, eid: str) -> dict:
        row = self._conn.execute(
            "SELECT payload_json FROM experiments WHERE id=?", (eid,)
        ).fetchone()
        if row is None:
            raise KeyError(eid)
        return json.loads(row["payload_json"])

    # ----- trials -----

    def create_trial(self, eid: str, tid: str, payload: dict) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO trials(id, experiment_id, payload_json, status, created_at) VALUES (?, ?, ?, ?, ?)",
            (tid, eid, json.dumps(payload, sort_keys=True), TrialStatus.PLANNED.value, time.time()),
        )

    def get_trial(self, tid: str) -> dict:
        row = self._conn.execute(
            "SELECT payload_json FROM trials WHERE id=?", (tid,)
        ).fetchone()
        if row is None:
            raise KeyError(tid)
        return json.loads(row["payload_json"])

    def list_trials(self, eid: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT payload_json FROM trials WHERE experiment_id=?", (eid,)
        ).fetchall()
        return [json.loads(r["payload_json"]) for r in rows]

    def set_trial_status(self, tid: str, new: TrialStatus) -> None:
        cur = self._conn.execute(
            "SELECT status FROM trials WHERE id=?", (tid,)
        ).fetchone()
        if cur is None:
            raise KeyError(tid)
        is_valid_transition(TrialStatus(cur["status"]), new)
        self._conn.execute(
            "UPDATE trials SET status=? WHERE id=?", (new.value, tid)
        )

    # ----- attempts -----

    def create_attempt(self, tid: str, aid: str, payload: dict) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO attempts(id, trial_id, payload_json, status, created_at) VALUES (?, ?, ?, ?, ?)",
            (aid, tid, json.dumps(payload, sort_keys=True), TrialStatus.QUEUED.value, time.time()),
        )

    def get_attempt(self, aid: str) -> dict:
        row = self._conn.execute(
            "SELECT payload_json, trial_id FROM attempts WHERE id=?", (aid,)
        ).fetchone()
        if row is None:
            raise KeyError(aid)
        d = json.loads(row["payload_json"])
        d["trial_id"] = row["trial_id"]
        return d

    # ----- instance state -----

    def mark_instance_queued(self, tid: str, iid: str) -> None:
        self._upsert_instance(tid, iid, InstanceStatus.QUEUED)

    def mark_instance_completed(self, tid: str, iid: str) -> None:
        self._upsert_instance(tid, iid, InstanceStatus.COMPLETED)

    def list_pending_instances(self, tid: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT instance_id FROM instance_state WHERE trial_id=? AND status IN ('queued','claimed','running','checkpointed') ORDER BY instance_id",
            (tid,),
        ).fetchall()
        return [r["instance_id"] for r in rows]

    def _upsert_instance(self, tid: str, iid: str, status: InstanceStatus) -> None:
        cur = self._conn.execute(
            "SELECT status FROM instance_state WHERE trial_id=? AND instance_id=?",
            (tid, iid),
        ).fetchone()
        if cur is not None:
            is_valid_transition(InstanceStatus(cur["status"]), status)
        self._conn.execute(
            "INSERT INTO instance_state(trial_id, instance_id, status, updated_at) VALUES (?,?,?,?)"
            " ON CONFLICT(trial_id, instance_id) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at",
            (tid, iid, status.value, time.time()),
        )

    # ----- leases -----

    def acquire_lease(self, aid: str, iid: str, holder: str, ttl_seconds: int) -> bool:
        now = time.time()
        expires = now + ttl_seconds
        cur = self._conn.execute(
            "SELECT holder, expires_at FROM leases WHERE attempt_id=? AND instance_id=?",
            (aid, iid),
        ).fetchone()
        if cur is not None:
            # expired lease can be re-claimed
            if cur["expires_at"] > now and cur["holder"] != holder:
                return False
            self._conn.execute(
                "UPDATE leases SET holder=?, acquired_at=?, expires_at=? WHERE attempt_id=? AND instance_id=?",
                (holder, now, expires, aid, iid),
            )
            return True
        self._conn.execute(
            "INSERT INTO leases(attempt_id, instance_id, holder, acquired_at, expires_at) VALUES (?,?,?,?,?)",
            (aid, iid, holder, now, expires),
        )
        return True

    def release_lease(self, aid: str, iid: str, holder: str) -> bool:
        cur = self._conn.execute(
            "SELECT holder FROM leases WHERE attempt_id=? AND instance_id=?",
            (aid, iid),
        ).fetchone()
        if cur is None or cur["holder"] != holder:
            return False
        self._conn.execute(
            "DELETE FROM leases WHERE attempt_id=? AND instance_id=?",
            (aid, iid),
        )
        return True

    def get_active_lease(self, aid: str, iid: str) -> dict | None:
        row = self._conn.execute(
            "SELECT holder, acquired_at, expires_at FROM leases WHERE attempt_id=? AND instance_id=?",
            (aid, iid),
        ).fetchone()
        if row is None:
            return None
        return {"holder": row["holder"], "acquired_at": row["acquired_at"], "expires_at": row["expires_at"]}

    def close(self) -> None:
        self._conn.close()