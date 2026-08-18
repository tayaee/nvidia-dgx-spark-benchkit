"""benchkit.runner — common plugin runner + benchmark plugin contract.

The runner handles everything benchmark-agnostic: endpoint discovery
and health, concurrency, timeout, retry, artifact naming, event
ledger, cooperative cancellation. Benchmark-specific behaviour lives
in plugins implementing :class:`BenchmarkPlugin`.
"""

from __future__ import annotations

from benchkit.runner.plugin import (
    BenchmarkPlugin,
    InstanceSpec,
    RunnerError,
    enumerate_instances,
    parse_plugin_manifest,
)
from benchkit.runner.runner import AttemptRunner, run_attempt
from benchkit.runner.fake_endpoint import FakeOpenAIEndpoint

__all__ = [
    "AttemptRunner",
    "BenchmarkPlugin",
    "FakeOpenAIEndpoint",
    "InstanceSpec",
    "RunnerError",
    "enumerate_instances",
    "parse_plugin_manifest",
    "run_attempt",
]