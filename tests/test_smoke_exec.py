"""Integration tests for bin/smoke.py docker sandbox helpers.

Gated on :func:`pytest.mark.docker` — CI skips when the docker daemon is
unreachable or the test image is not cached. The tests prove the
container lifecycle primitives (run-persisted, exec, rm) work end-to-end,
which is what the per-benchmark executors rely on.

Skipped automatically if:

- ``docker`` CLI not on PATH (or daemon not running) — detected via
  :func:`smoke._docker_available`.
- the chosen test image is not cached locally (and we're offline).

Run locally::

    pytest tests/test_smoke_exec.py -v -m docker

Force-run even without the marker (skip-aware)::

    pytest tests/test_smoke_exec.py -v -m "not docker"   # negative marker
    pytest tests/test_smoke_exec.py -v --docker           # custom flag
"""

from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(ROOT / "src"))

import smoke  # noqa: E402

docker_mark = pytest.mark.docker
skip_no_docker = pytest.mark.skipif(
    not smoke._docker_available(),
    reason="docker daemon unreachable",
)


# Tiny image used for the lifecycle tests. ``debian:stable-slim`` is the
# default because it's small (~75 MB), pulls in ~5s on a warm cache, and
# ships bash preinstalled — which the ``_docker_exec`` helper requires
# because it shells out via ``docker exec <name> bash -lc <cmd>``.
# Override with the ``BENCKKIT_SMOKE_TEST_IMAGE`` env var if you want a
# different image, but it MUST have bash on PATH.
TEST_IMAGE = os.environ.get(
    "BENCKKIT_SMOKE_TEST_IMAGE", "debian:stable-slim"
)

skip_no_image = pytest.mark.skipif(
    not smoke._docker_image_present(TEST_IMAGE),
    reason=(
        f"test image {TEST_IMAGE!r} not cached locally; "
        "pull first or set BENCKKIT_SMOKE_TEST_IMAGE"
    ),
)


def _unique_name(prefix: str = "benchkit-it") -> str:
    """Container names need to be globally unique on the docker host."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _start(name: str, *, timeout: int = 60) -> str | None:
    """Start a persisted container with bash on PATH.

    Returns the container id on success, ``None`` on failure. The image
    MUST have bash preinstalled because ``_docker_exec`` shells out via
    ``docker exec <name> bash -lc <cmd>``.
    """
    return smoke._docker_run_persisted(
        TEST_IMAGE, name, allow_pull=False, timeout=timeout
    )


@docker_mark
@skip_no_docker
@skip_no_image
def test_docker_run_persisted_returns_container_id():
    """``_docker_run_persisted`` should hand back a container id we can exec into."""
    name = _unique_name("it-run")
    try:
        cid = _start(name)
        assert cid, "expected a non-empty container id"
        # Sanity: the id should look like a hex string (>= 12 chars).
        assert len(cid) >= 12
    finally:
        smoke._docker_rm(name)


@docker_mark
@skip_no_docker
@skip_no_image
def test_docker_exec_runs_command_and_returns_exit_code():
    """``_docker_exec`` must run a shell command and surface the exit code."""
    name = _unique_name("it-exec")
    try:
        assert _start(name), "container failed to start"
        result = smoke._docker_exec(name, "exit 0", timeout=15)
        assert result["ran"] is True
        assert result["exit"] == 0
        assert result["duration_s"] >= 0
    finally:
        smoke._docker_rm(name)


@docker_mark
@skip_no_docker
@skip_no_image
def test_docker_exec_propagates_nonzero_exit():
    """Non-zero exit codes must come back as-is (no swallowing)."""
    name = _unique_name("it-exit")
    try:
        assert _start(name)
        result = smoke._docker_exec(name, "exit 7", timeout=15)
        assert result["ran"] is True
        assert result["exit"] == 7
    finally:
        smoke._docker_rm(name)


@docker_mark
@skip_no_docker
@skip_no_image
def test_docker_exec_captures_stdout_and_stderr():
    """``stdout_tail`` / ``stderr_tail`` must hold the streamed output."""
    name = _unique_name("it-out")
    try:
        assert _start(name)
        result = smoke._docker_exec(
            name,
            "echo hello-stdout; echo hello-stderr >&2",
            timeout=15,
        )
        assert "hello-stdout" in result["stdout_tail"]
        assert "hello-stderr" in result["stderr_tail"]
    finally:
        smoke._docker_rm(name)


@docker_mark
@skip_no_docker
@skip_no_image
def test_seed_and_run_test_sh_simulates_terminal_bench_happy_path():
    """End-to-end: seed /tests/test.sh via heredoc, then run it.

    Mirrors what ``_exec_terminal_bench`` and ``_exec_deepswe`` do for
    a passing instance — proves the lifecycle primitives chain together.
    """
    name = _unique_name("it-seed")
    try:
        assert _start(name)
        # Seed /tests/test.sh that writes the reward file exactly the way
        # terminal-bench / deepswe verifiers do.
        seed = (
            "mkdir -p /tests /logs/verifier && "
            "printf '#!/bin/bash\\necho 1 > /logs/verifier/reward.txt\\n' "
            "> /tests/test.sh && "
            "chmod +x /tests/test.sh"
        )
        seeded = smoke._docker_exec(name, seed, timeout=15)
        assert seeded["exit"] == 0, (
            f"heredoc seed failed: {seeded['stderr_tail']!r}"
        )

        ran = smoke._docker_exec(name, "bash /tests/test.sh", timeout=30)
        assert ran["exit"] == 0, (
            f"verifier exited {ran['exit']}: {ran['stderr_tail']!r}"
        )

        reward = smoke._docker_exec(
            name, "cat /logs/verifier/reward.txt", timeout=15
        )
        assert reward["exit"] == 0
        assert reward["stdout_tail"].strip() == "1"
    finally:
        smoke._docker_rm(name)


@docker_mark
@skip_no_docker
@skip_no_image
def test_docker_exec_respects_timeout():
    """Timeout exceeded must surface as ``exit=124`` + ``stderr_tail`` note."""
    name = _unique_name("it-tmo")
    try:
        assert _start(name)
        # sleep 10 inside the container, but with only 2s wall-clock budget.
        t0 = time.time()
        result = smoke._docker_exec(name, "sleep 10", timeout=2)
        elapsed = time.time() - t0
        assert result["exit"] == 124, f"expected 124 (timeout), got {result['exit']}"
        assert "timeout" in result["stderr_tail"].lower()
        # We should bail well before the 10s the container is asking for.
        assert elapsed < 6, f"timeout handler too slow: {elapsed:.2f}s"
    finally:
        smoke._docker_rm(name)


@docker_mark
@skip_no_docker
@skip_no_image
def test_docker_rm_is_idempotent_on_missing_container():
    """``_docker_rm`` of a never-created name must not raise."""
    # Should be a clean no-op — proves we don't depend on prior state.
    smoke._docker_rm(_unique_name("it-ghost"))


@docker_mark
@skip_no_docker
@skip_no_image
def test_reused_name_replaces_stale_container():
    """Pre-existing stale container with same name must be removed first.

    Mirrors the safety net inside ``_docker_run_persisted``: before every
    ``docker run``, it does ``docker rm -f`` on the target name. If a prior
    test crashed and left a container behind, the next run still succeeds.
    """
    name = _unique_name("it-stale")
    try:
        # First run — leave it behind by NOT calling _docker_rm here.
        first = smoke._docker_run_persisted(
            TEST_IMAGE, name, allow_pull=False, timeout=60
        )
        assert first, "first run failed"
        # Second run with the same name — _docker_run_persisted must
        # pre-rm the stale container and start fresh.
        second = smoke._docker_run_persisted(
            TEST_IMAGE, name, allow_pull=False, timeout=60
        )
        assert second, "second run with reused name failed"
        assert second != first, "expected a fresh container id"
    finally:
        smoke._docker_rm(name)
