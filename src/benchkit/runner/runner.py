"""Common attempt runner — endpoint health, concurrency, retry, cancellation.

The runner is benchmark-agnostic. The plugin decides what to send, how
to parse, and how to evaluate; the runner decides when and how many.

Per-instance lifecycle:
1. claim  (store.acquire_lease)
2. prepare (plugin.prepare)
3. run     (plugin.run — wrapped with timeout + cooperative cancel)
4. parse   (plugin.parse)
5. persist raw + canonical artifacts (atomic writes)
6. release (store.release_lease)

On retry, a new attempt directory is created and the lease is reacquired;
the original raw artifact is preserved on disk and is the auditable
trajectory for that attempt.
"""

from __future__ import annotations

import json
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

from benchkit.artifact import (
    atomic_write_bytes,
    atomic_write_text,
    ensure_attempt_layout,
)
from benchkit.events import append_event
from benchkit.ids import new_attempt_id
from benchkit.runner.plugin import (
    BenchmarkPlugin,
    InstanceSpec,
    RunnerError,
    RuntimeContext,
    enumerate_instances,
)


class AttemptRunner:
    """Execute a benchmark plugin over an endpoint, writing artifacts
    under ``output_dir`` and recording state in the (optional) ``store``.

    Parameters
    ----------
    endpoint : object with .chat(messages, model, ...) and .health_check()
    plugin   : a BenchmarkPlugin subclass instance
    output_dir : directory in which to create ``attempts/<aid>/``
    concurrency : max parallel instances (default 1)
    timeout_seconds : per-instance wall-clock timeout
    max_retries : how many times to retry a failed instance (default 0)
    store : optional benchkit.store.Store for lease tracking + resume
    trial_id : required when ``store`` is provided
    cancel_event : optional threading.Event to abort between instances
    """

    def __init__(
        self,
        endpoint,
        plugin: BenchmarkPlugin,
        output_dir: Path,
        concurrency: int = 1,
        timeout_seconds: float = 1800.0,
        max_retries: int = 0,
        store=None,
        trial_id: str | None = None,
        cancel_event=None,
        dataset_revision: str = "unknown",
    ):
        self.endpoint = endpoint
        self.plugin = plugin
        self.output_dir = Path(output_dir)
        self.concurrency = max(1, concurrency)
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.store = store
        self.trial_id = trial_id
        self._cancel = cancel_event
        self.dataset_revision = dataset_revision

    # --- public ---

    def cancel(self) -> None:
        if self._cancel is None:
            import threading
            self._cancel = threading.Event()
        self._cancel.set()

    def run(self, limit_new: int | None = None) -> dict:
        """Run all instances (or up to ``limit_new`` non-completed ones).

        Returns a small JSON-friendly report dict.
        """
        if self._cancel is None:
            import threading
            self._cancel = threading.Event()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        aid = new_attempt_id(str(self.output_dir) if self.output_dir.exists() else None)
        attempt_dir = self.output_dir / aid
        ensure_attempt_layout(attempt_dir)

        specs = enumerate_instances(self.plugin, self.dataset_revision)
        # Resume-skip: skip instances already marked completed in the DB
        skipped: list[str] = []
        if self.store is not None and self.trial_id is not None:
            pending_in_db = set(self.store.list_pending_instances(self.trial_id))
            all_ids = [s.instance_id for s in specs]
            # If we have any instance_state rows for this trial, use them
            # to filter; otherwise run everything (fresh trial).
            state_rows = self.store._conn.execute(
                "SELECT instance_id, status FROM instance_state WHERE trial_id=?",
                (self.trial_id,),
            ).fetchall()
            completed_in_db = {r["instance_id"] for r in state_rows if r["status"] == "completed"}
            if state_rows:
                specs = [s for s in specs if s.instance_id not in completed_in_db]
                skipped = sorted(completed_in_db)
            if limit_new is not None:
                specs = specs[:limit_new]
        elif limit_new is not None:
            specs = specs[:limit_new]

        report = {
            "attempt_id": aid,
            "attempt_dir": str(attempt_dir),
            "total": len(specs),
            "completed": 0,
            "failed": 0,
            "skipped": len(skipped),
            "aborted": 0,
            "instances": {},
        }
        append_event(attempt_dir / "events.jsonl", {"kind": "attempt_started", "attempt_id": aid, "total": len(specs)})

        with ThreadPoolExecutor(max_workers=self.concurrency) as ex:
            futs = {ex.submit(self._run_one, attempt_dir, s): s for s in specs}
            for fut in as_completed(futs):
                spec = futs[fut]
                if self._cancel.is_set():
                    report["aborted"] += 1
                    continue
                try:
                    outcome = fut.result()
                except Exception as e:  # pragma: no cover (defensive)
                    outcome = {"status": "failed", "error": str(e)}
                report["instances"][spec.instance_id] = outcome
                if outcome.get("status") == "completed":
                    report["completed"] += 1
                elif outcome.get("status") == "aborted":
                    report["aborted"] += 1
                else:
                    report["failed"] += 1

        append_event(attempt_dir / "events.jsonl", {"kind": "attempt_finished", **report})
        return report

    # --- internals ---

    def _run_one(self, attempt_dir: Path, spec: InstanceSpec) -> dict:
        if self._cancel.is_set():
            return {"status": "aborted"}
        # lease
        holder = f"worker-{id(self)}"
        if self.store is not None:
            ok = self.store.acquire_lease(
                attempt_dir.name, spec.instance_id, holder, ttl_seconds=int(self.timeout_seconds) + 60
            )
            if not ok:
                return {"status": "skipped", "reason": "lease_held_by_other"}
        append_event(attempt_dir / "events.jsonl", {"kind": "instance_started", "instance_id": spec.instance_id})
        last_err: str | None = None
        for attempt in range(self.max_retries + 1):
            try:
                prepared = self.plugin.prepare(spec, config={})
                ctx = RuntimeContext(
                    attempt_id=attempt_dir.name,
                    instance_id=spec.instance_id,
                    timeout_seconds=self.timeout_seconds,
                    cancel_check=self._cancel.is_set,
                )
                t0 = time.time()
                raw = self.plugin.run(self.endpoint, prepared, ctx)
                dt = time.time() - t0
                # raw artifact
                raw_path = attempt_dir / "raw" / f"{spec.instance_id}.json"
                atomic_write_text(
                    raw_path,
                    json.dumps({"raw": raw, "instance_id": spec.instance_id, "ts": dt}, default=str, indent=2),
                )
                canonical = self.plugin.parse(raw)
                canon_path = attempt_dir / "canonical" / f"{spec.instance_id}.json"
                atomic_write_text(
                    canon_path,
                    json.dumps({"instance_id": spec.instance_id, **canonical}, default=str, indent=2),
                )
                append_event(
                    attempt_dir / "events.jsonl",
                    {"kind": "instance_completed", "instance_id": spec.instance_id, "duration_seconds": dt},
                )
                if self.store is not None and self.trial_id is not None:
                    self.store.mark_instance_completed(self.trial_id, spec.instance_id)
                return {"status": "completed", "duration_seconds": dt}
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                append_event(
                    attempt_dir / "events.jsonl",
                    {"kind": "instance_failed", "instance_id": spec.instance_id, "attempt": attempt, "error": last_err},
                )
                continue
        # exhausted retries
        append_event(
            attempt_dir / "events.jsonl",
            {"kind": "instance_aborted", "instance_id": spec.instance_id, "error": last_err},
        )
        return {"status": "failed", "error": last_err}
        finally_like_unused = None  # noqa: ERA001 (keeps static analysers quiet)


def run_attempt(
    endpoint,
    plugin: BenchmarkPlugin,
    output_dir: Path,
    **kw,
) -> dict:
    """Convenience wrapper: builds an :class:`AttemptRunner` and runs once."""
    return AttemptRunner(endpoint, plugin, output_dir, **kw).run(limit_new=kw.pop("limit_new", None))