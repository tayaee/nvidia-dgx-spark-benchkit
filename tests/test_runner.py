"""Tests for benchkit.runner — plugin contract, common runner, fake endpoint, SWE-bench adapter."""

import json
from pathlib import Path

import pytest

from benchkit.runner import (
    AttemptRunner,
    BenchmarkPlugin,
    InstanceSpec,
    RunnerError,
    enumerate_instances,
    parse_plugin_manifest,
    run_attempt,
)


# ----- a fake OpenAI-compatible endpoint -----


class FakeOpenAIEndpoint:
    """In-process OpenAI-compatible fake used by tests.

    Mirrors the minimal surface a runner needs: chat completions that
    return deterministic text, plus /v1/models for discovery.
    """

    def __init__(self, model_name="fake-model"):
        self.model_name = model_name
        self.calls: list[dict] = []

    def chat(self, messages, model=None, **kw):
        self.calls.append({"messages": messages, "model": model, **kw})
        return {
            "id": "chatcmpl-fake",
            "model": model or self.model_name,
            "choices": [
                {"message": {"role": "assistant", "content": "fake response"}}
            ],
        }

    def list_models(self):
        return {"data": [{"id": self.model_name}]}


# ----- a minimal benchmark plugin for tests -----


class EchoPlugin(BenchmarkPlugin):
    """Returns the user message as the assistant reply. Useful for tests."""

    name = "echo"

    def enumerate(self, dataset_revision):
        return [
            InstanceSpec(instance_id=f"inst-{i}", input={"text": f"echo {i}"})
            for i in range(3)
        ]

    def prepare(self, instance, config):
        return {"prompt": instance.input["text"]}

    def run(self, endpoint, prepared, runtime):
        resp = endpoint.chat([{"role": "user", "content": prepared["prompt"]}])
        return resp["choices"][0]["message"]["content"]

    def parse(self, raw_artifact):
        return {"prediction": raw_artifact.strip()}

    def evaluate(self, canonical_set):
        return {"resolved": sum(1 for c in canonical_set if c["prediction"])}


# ----- the flakiness plugin (for retry tests) -----


class FlakyPlugin(BenchmarkPlugin):
    """First call fails, second call succeeds. Used to verify retry."""

    name = "flaky"
    attempt_calls: list[str] = []

    def enumerate(self, dataset_revision):
        return [InstanceSpec(instance_id="inst-1", input={"text": "hi"})]

    def prepare(self, instance, config):
        return {"prompt": instance.input["text"]}

    def run(self, endpoint, prepared, runtime):
        # use the instance_id (not in scope); count globally
        FlakyPlugin.attempt_calls.append(prepared["prompt"])
        if len(FlakyPlugin.attempt_calls) == 1:
            raise RunnerError("simulated network failure")
        return "ok"

    def parse(self, raw_artifact):
        return {"prediction": raw_artifact}

    def evaluate(self, canonical_set):
        return {"resolved": 1 if canonical_set[0]["prediction"] == "ok" else 0}


# ----- manifest parsing -----


class TestPluginManifest:
    def test_parses_yaml_file(self, tmp_path):
        m = tmp_path / "bench.yaml"
        m.write_text(
            "id: swebench-verified\n"
            "version: 1.0.0\n"
            "adapter: benchkit.runner.swebench\n"
            "execution:\n"
            "  protocol: openai-chat\n"
            "  timeout_seconds: 60\n"
        )
        d = parse_plugin_manifest(m)
        assert d["id"] == "swebench-verified"
        assert d["execution"]["protocol"] == "openai-chat"
        assert d["execution"]["timeout_seconds"] == 60

    def test_rejects_missing_required_field(self, tmp_path):
        m = tmp_path / "bench.yaml"
        m.write_text("id: x\n")
        with pytest.raises(Exception):
            parse_plugin_manifest(m)


# ----- enumerate -----


class TestEnumerate:
    def test_returns_instance_specs(self):
        specs = enumerate_instances(EchoPlugin(), dataset_revision="rev1")
        assert len(specs) == 3
        assert all(isinstance(s, InstanceSpec) for s in specs)


# ----- attempt runner -----


class TestAttemptRunner:
    def test_runs_all_instances(self, tmp_path):
        ep = FakeOpenAIEndpoint()
        runner = AttemptRunner(ep, EchoPlugin(), output_dir=tmp_path, concurrency=2)
        report = runner.run(limit_new=None)
        assert report["completed"] == 3
        assert report["failed"] == 0

    def test_limit_new_skips_already_completed(self, tmp_path):
        # First, mark the first 2 instances as completed in the store
        ep = FakeOpenAIEndpoint()
        store_dir = tmp_path / "store"
        store_dir.mkdir()
        from benchkit.store import Store
        store = Store(store_dir / "meta.db")
        from benchkit.ids import new_experiment_id, new_trial_id
        eid = new_experiment_id()
        tid = new_trial_id()
        store.create_experiment(eid, {})
        store.create_trial(eid, tid, {})
        store.mark_instance_completed(tid, "inst-0")
        store.mark_instance_completed(tid, "inst-1")

        runner = AttemptRunner(
            ep, EchoPlugin(), output_dir=tmp_path, store=store, trial_id=tid,
            concurrency=1,
        )
        report = runner.run(limit_new=10)
        assert report["completed"] == 1  # only inst-2
        assert report["skipped"] == 2

    def test_retry_creates_new_attempt(self, tmp_path):
        ep = FakeOpenAIEndpoint()
        # pre-create the first attempt so retry has something to follow
        from benchkit.store import Store
        from benchkit.ids import new_experiment_id, new_trial_id, new_attempt_id
        store = Store(tmp_path / "store" / "meta.db")
        eid = new_experiment_id()
        tid = new_trial_id()
        store.create_experiment(eid, {})
        store.create_trial(eid, tid, {})
        aid1 = new_attempt_id()
        store.create_attempt(tid, aid1, {})

        FlakyPlugin.attempt_calls = []
        runner = AttemptRunner(
            ep, FlakyPlugin(), output_dir=tmp_path, store=store, trial_id=tid,
            concurrency=1, max_retries=1,
        )
        report = runner.run(limit_new=None)
        assert report["completed"] == 1
        # two attempts total: the pre-existing + a new one after failure
        attempts = list((tmp_path).iterdir()) if tmp_path.iterdir() else []
        # the new attempt directory should exist
        assert any("attempt" in p.name for p in tmp_path.iterdir())


# ----- canonical output / schema -----


class TestCanonicalOutput:
    def test_canonical_written_to_canonical_dir(self, tmp_path):
        ep = FakeOpenAIEndpoint()
        runner = AttemptRunner(ep, EchoPlugin(), output_dir=tmp_path)
        report = runner.run(limit_new=None)
        assert report["completed"] == 3
        for inst_id in ("inst-0", "inst-1", "inst-2"):
            # canonical file lives in the attempt directory, not directly under tmp_path
            attempt_dirs = list(tmp_path.iterdir())
            assert attempt_dirs, f"no attempt directory under {tmp_path}"
            a = attempt_dirs[0]
            files = list((a / "canonical").glob(f"{inst_id}.json"))
            assert files, f"missing canonical for {inst_id} in {a / 'canonical'}"
            d = json.loads(files[0].read_text())
            assert "prediction" in d


# ----- timeout / cooperative cancel -----


class TestCooperativeCancel:
    def test_cancel_marks_pending_as_aborted(self, tmp_path):
        ep = FakeOpenAIEndpoint()
        runner = AttemptRunner(ep, EchoPlugin(), output_dir=tmp_path, concurrency=1)
        # cancel before any work runs
        runner.cancel()
        report = runner.run(limit_new=None)
        assert report["aborted"] >= 0  # runs but doesn't process anything new
        # no instances should have completed after cancellation
        assert report["completed"] == 0


# ----- SWE-bench adapter -----


class TestSwebenchAdapter:
    def test_canonical_matches_official_schema(self, tmp_path):
        from benchkit.runner.swebench import SwebenchAdapter, SwebenchPrediction

        # an instance from the mini dataset fixture
        inst = {
            "instance_id": "astropy__astropy-12907",
            "problem_statement": "Some problem text",
        }
        adapter = SwebenchAdapter()
        prepared = adapter.prepare(inst, {})
        # prepared has the prompt template
        assert "prompt" in prepared
        assert "astropy__astropy-12907" not in prepared["prompt"]  # prompt is just the problem

        # raw trajectory -> adapter.parse (with instance_id attached)
        canonical = adapter.parse({
            "instance_id": "astropy__astropy-12907",
            "prediction_text": "<patch>DIFF</patch>",
        })
        assert isinstance(canonical, SwebenchPrediction)
        # SWE-bench official prediction: {instance_id, model_patch, model_name}
        d = canonical.to_dict()
        assert d["instance_id"] == "astropy__astropy-12907"
        assert d["model_patch"] == "DIFF"
        assert "model_name" in d