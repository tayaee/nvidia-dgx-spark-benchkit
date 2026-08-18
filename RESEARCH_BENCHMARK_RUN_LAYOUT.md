# Benchmark run-layout research

## Bottom line

The proposed layout is a good small-team wrapper, but it is not yet a leaderboard-grade run system. Its strongest ideas are Docker isolation, resumability, immutable per-tune script archives, and separate inference/evaluation outputs. The main missing concepts are a run manifest, immutable attempt/config identities, a canonical prediction artifact, dataset/version metadata, and machine-readable per-instance state.

## What current first-party tooling does

- SWE-bench's official tooling is Docker-based for reproducibility and exposes dataset, prediction path, run ID, and worker count as explicit evaluation inputs. It also supports re-reporting saved evaluation logs without rerunning containers. Source: [SWE-bench repository](https://github.com/swe-bench/SWE-bench#readme).
- Official leaderboard submissions are maintained as structured experiments in the `SWE-bench/experiments` repository, rather than as an informal collection of console logs. Source: [SWE-bench submission guidance](https://www.swebench.com/submit.html).
- A representative resumable harness keeps a per-instance ledger, appends one verdict per instance, writes a summary report, and preserves the exact predictions that were scored. Source: [E2B SWE-bench harness outputs and layout](https://github.com/e2b-dev/swe-bench).
- The current official SWE-bench repository supports separate inference and evaluation workflows, explicit `--run-id`, and separate worker controls. Source: [SWE-bench README](https://github.com/swe-bench/SWE-bench#readme).
- As of February 2026, OpenAI advises that SWE-bench Verified is increasingly contaminated and recommends SWE-bench Pro for frontier capability reporting. Verified remains useful for this local engineering experiment, but its score should not be treated as a clean current leaderboard signal. Source: [OpenAI: Why SWE-bench Verified no longer measures frontier coding capabilities](https://openai.com/index/why-we-no-longer-evaluate-swe-bench-verified/).

## Comparison with the proposed design

### Already aligned

- Dockerized execution.
- A stable logical `RUN_ID` with resumable unfinished instances.
- A separate tune/config number for changed server or client settings.
- Archived executable scripts for reproducibility.
- Separate raw trajectories, evaluation inputs, evaluation outputs, and reports.
- Configurable parallelism.
- Scores reported against the fixed 500-instance denominator.

### Behind mature benchmark operators

- `RUN_ID` alone is too coarse. A leaderboard system distinguishes benchmark, dataset revision, model identity, scaffold/client version, server image, sampling parameters, hardware, and attempt/config hash.
- A directory of JSON files is useful for recovery, but a machine-readable ledger is needed as the resume source of truth. It should distinguish `queued`, `running`, `completed`, `failed`, `aborted`, and `invalid`, with timestamps and error metadata.
- Script copies are necessary but insufficient. The run must also record git commit, Docker image digest, model ID returned by `/v1/models`, dataset revision, prompt/scaffold version, worker count, endpoint, seed, and dependency lockfile hash.
- “Latest tune” is not a leaderboard result. Reports must name the exact immutable config and artifact hashes that were scored.
- Raw trajectories should never be overwritten. If an instance is retried, store a new attempt artifact and select a canonical attempt explicitly for scoring.
- Evaluation should consume a canonical adapter output (for SWE-bench, the official prediction schema), while raw agent trajectories remain separate and untouched.

## Recommended portable layout

```text
results/
  benchmark-swebench-verified/
    run-001/
      manifest.json
      dataset.json
      state.jsonl
      predictions/
        raw/
        canonical/
      eval/
        input/
        raw/
        breakdown.json
        summary.json
      logs/
      archive/
        tune012-start-qwen-sglang.sh
        tune012-start-swebench-verified.sh
        tune012-config.json
```

For multiple benchmarks and models, keep the benchmark and dataset outside the run ID. For example, a model/config combination gets a new immutable run, while a resumable run keeps one dataset revision and one intended experiment definition. Use `RUN_ID=1` as a human alias if desired, but also generate a globally unique `run_uuid` in `manifest.json`.

## Recommendation for this project

Keep the user-friendly scripts and `RUN_ID`/`TUNE_NO` interface, but add these safeguards before scaling:

1. `manifest.json` created at run start and never silently rewritten.
2. `state.jsonl` as append-only per-instance ledger.
3. Raw trajectory, canonical SWE-bench prediction, and evaluation verdict as three distinct artifact types.
4. Atomic writes plus explicit attempt IDs for retries.
5. A `config.json` snapshot alongside the archived scripts.
6. `report.sh` reading the evaluation summary, not scraping logs.
7. A dataset fingerprint and model/server fingerprint in every report.
8. Separate `inference_workers` and `eval_workers` settings.

