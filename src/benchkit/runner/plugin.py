"""Benchmark plugin contract.

A plugin implements the five lifecycle stages. The common runner calls
each stage in order; plugins are expected to be deterministic for a
given (input, config) pair so reruns reproduce.

Stages:
- enumerate()   -> list of InstanceSpec, given the dataset revision
- prepare()     -> runtime-agnostic input (prompt, request body)
- run()         -> execute against an endpoint, returning raw output
- parse()       -> convert raw output to canonical prediction
- evaluate()    -> score a list of canonical predictions
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


class RunnerError(Exception):
    """Raised by a plugin or runner when a single instance fails."""


@dataclass
class InstanceSpec:
    instance_id: str
    input: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)


@dataclass
class RuntimeContext:
    attempt_id: str
    instance_id: str
    timeout_seconds: float = 1800.0
    cancel_check: callable = None  # type: ignore[type-arg]


class BenchmarkPlugin:
    """Base class every benchmark plugin inherits from."""

    name: str = "unnamed"

    def enumerate(self, dataset_revision: str) -> list[InstanceSpec]:
        raise NotImplementedError

    def prepare(self, instance: InstanceSpec, config: dict) -> Any:
        raise NotImplementedError

    def run(self, endpoint, prepared: Any, runtime: RuntimeContext) -> Any:
        raise NotImplementedError

    def parse(self, raw_artifact: Any) -> dict:
        raise NotImplementedError

    def evaluate(self, canonical_set: list[dict]) -> dict:
        raise NotImplementedError


def enumerate_instances(plugin: BenchmarkPlugin, dataset_revision: str) -> list[InstanceSpec]:
    """Convenience wrapper — calls ``plugin.enumerate`` and tags each spec."""
    specs = plugin.enumerate(dataset_revision)
    return [s if isinstance(s, InstanceSpec) else InstanceSpec(**s) for s in specs]


def parse_plugin_manifest(path: Path) -> dict:
    """Read a YAML manifest and validate the required fields.

    Minimal YAML support — only the keys/scalars we actually use. We
    avoid pulling in a full YAML library for this small surface; if
    the manifest grows, switch to PyYAML.
    """
    p = Path(path)
    text = p.read_text()
    out: dict = {}
    stack = [out]
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        key_val = line.lstrip()
        if ":" not in key_val:
            raise RunnerError(f"malformed manifest line: {raw_line!r}")
        key, _, val = key_val.partition(":")
        key = key.strip()
        val = val.strip()
        # trim stack
        while len(stack) > 1 and indent <= _stack_indent(stack):
            stack.pop()
        if val == "":
            new = {}
            stack[-1][key] = new
            stack.append(new)
        else:
            stack[-1][key] = _coerce(val)
    # required fields
    for f in ("id", "version", "adapter"):
        if f not in out:
            raise RunnerError(f"manifest missing required field: {f}")
    return out


def _stack_indent(stack: list[dict]) -> int:
    # helper: track approximate indent by counting how deep we are
    return len(stack) - 1


def _coerce(v: str) -> Any:
    if v.lower() in ("true", "yes"):
        return True
    if v.lower() in ("false", "no"):
        return False
    try:
        return int(v)
    except ValueError:
        try:
            return float(v)
        except ValueError:
            return v.strip('"').strip("'")