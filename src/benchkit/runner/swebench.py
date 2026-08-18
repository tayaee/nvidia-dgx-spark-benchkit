"""SWE-bench adapter — maps raw trajectory to the official prediction schema.

The official SWE-bench harness expects each line of ``predictions.jsonl``
to be ``{"instance_id": str, "model_patch": str, "model_name": str}``.

We accept either:
- the raw agent trajectory (a string with ``<patch>...</patch>`` markers), or
- a pre-extracted ``prediction_text`` field.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, asdict
from typing import Any

from benchkit.runner.plugin import BenchmarkPlugin, InstanceSpec, RunnerError, RuntimeContext


_PATCH_RE = re.compile(r"<patch>(.*?)</patch>", re.DOTALL)


@dataclass
class SwebenchPrediction:
    instance_id: str
    model_patch: str
    model_name_or_path: str = "unknown"

    def to_dict(self) -> dict:
        d = asdict(self)
        # Map model_name_or_path -> model_name (official field)
        d["model_name"] = d.pop("model_name_or_path")
        return d


class SwebenchAdapter(BenchmarkPlugin):
    name = "swebench"

    def enumerate(self, dataset_revision: str):
        # In real use, this would load the SWE-bench dataset for the
        # given revision. For tests, callers can inject fixtures; here
        # we return an empty list and expect the runner to be wired
        # with pre-populated instances.
        return []

    def prepare(self, instance: dict | InstanceSpec, config: dict) -> dict:
        if isinstance(instance, InstanceSpec):
            data = instance.input
            iid = instance.instance_id
        else:
            data = instance
            iid = instance.get("instance_id", "unknown")
        problem = data.get("problem_statement", "")
        prompt = (
            "You are an expert software engineer. Given the following issue, "
            "produce a patch that resolves it. Wrap the patch in <patch></patch> tags.\n\n"
            f"Issue:\n{problem}\n"
        )
        return {"prompt": prompt, "instance_id": iid}

    def run(self, endpoint, prepared: dict, runtime: RuntimeContext):
        resp = endpoint.chat([{"role": "user", "content": prepared["prompt"]}])
        return resp["choices"][0]["message"]["content"]

    def parse(self, raw_artifact: Any) -> SwebenchPrediction:
        """Convert raw trajectory to canonical SWE-bench prediction.

        ``raw_artifact`` may be:
        - a dict ``{"prediction_text": str}``
        - a plain string with ``<patch>...</patch>`` markers
        """
        if isinstance(raw_artifact, dict):
            text = raw_artifact.get("prediction_text", "")
            iid = raw_artifact.get("instance_id", "unknown")
        else:
            text = str(raw_artifact)
            iid = "unknown"
        m = _PATCH_RE.search(text)
        patch = m.group(1).strip() if m else text.strip()
        return SwebenchPrediction(
            instance_id=iid,
            model_patch=patch,
            model_name_or_path="benchkit",
        )

    def evaluate(self, canonical_set: list[dict]) -> dict:
        resolved = sum(1 for c in canonical_set if c.get("model_patch"))
        return {"resolved": resolved, "total": len(canonical_set)}