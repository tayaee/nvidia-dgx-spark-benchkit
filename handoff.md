# WIP: large-scale benchmark run layout & runner

> **Status note (2026-08-19):** this is an early WIP draft that predates the current repo. Most of the "not yet implemented" items have since shipped. See the current `cheatsheet.md` for the live operator guide and `benchmark-lib.sh` for the actual layout.

Drafted: 2026-08-17

## User goal

Run SWE-bench (and other benchmarks) at scale from WSL2 using Docker, across
multiple model/config combinations. The Qwen server runs on `spark1.local` via
SGLang/Docker; the client and evaluator run on WSL2.

## Confirmed concepts

- `RUN_ID`: a single 500-task experiment bundle ID. Held stable while tuning.
  Example: `RUN_ID=1`.
- `SCRIPT_VER`: config version number. Increments when server or client config
  changes (even on only one side). Legacy alias `TUNE_NO` is still accepted.
- `--limit-new N`: don't run the full set — process up to N new tasks that have
  not yet completed cleanly, then exit. Smoke test uses `--limit-new 1`.
- Already-completed tasks are skipped, not overwritten.
- After interruption/failure, resume the remaining tasks.
- The same RUN directory accumulates; never deletes prior data.

## Confirmed script names

- `start-qwen-sglang.sh`: start the Qwen SGLang Docker server on the remote
  `spark1.local`.
- `start-swebench-verified.sh`: run the SWE-bench client locally (WSL2). Will
  start/wait for the server if not already up.
- `stop-qwen.sh`
- `stop-swebench-verified.sh`
- `eval.sh`
- `report.sh`
- Additional benchmark entry points:
  - `swebench-pro.sh`
  - `deepswe1.1.sh`
  - `terminal-bench2.1.sh`
  - `livecodebench.sh`
  - `frontiercode1.1.sh`

## Results layout (agreed)

```text
results/
└── run-$RUN_ID/
    ├── manifest.json
    ├── state.jsonl
    ├── predictions/
    │   ├── raw/          # raw trajectory/agent output — never overwritten
    │   └── canonical/    # normalised prediction format the evaluator requires
    ├── eval/
    │   ├── input/        # evaluator input transform outputs
    │   ├── raw/          # raw evaluator logs/output
    │   ├── breakdown.json
    │   └── summary.json
    ├── logs/
    └── archive/
        ├── v012-start-qwen-sglang.sh
        ├── v012-start-swebench-verified.sh
        └── v012-config.json
```

At the time this draft was written, the repo only had the research memo
`RESEARCH_BENCHMARK_RUN_LAYOUT.md` and a partial `benchmark-lib.sh`. The rest
of the implementation had not landed yet.

## Run interface target

```bash
RUN_ID=1 SCRIPT_VER=1 ./start-qwen-sglang.sh
RUN_ID=1 SCRIPT_VER=1 PARALLELISM=2 ./start-swebench-verified.sh --limit-new 1
RUN_ID=1 ./eval.sh
RUN_ID=1 ./report.sh
```

`RUN_ID` and `SCRIPT_VER` are required env vars on start scripts. The canonical
script is archived under `results/run-$RUN_ID/archive/` keyed by `SCRIPT_VER`.
If an archive already exists for that version, the script aborts and demands a
SCRIPT_VER bump (same version + changed content = config drift).

## Implementation stop point

Only `benchmark-lib.sh` was successfully added at the time this was written. It
provides:

- `--limit-new N` parsing
- RUN_ID / SCRIPT_VER positive-integer validation
- `results/run-$RUN_ID` structure creation
- Default `manifest.json` generation
- `state.jsonl` creation
- archive-script copy helper

A subsequent large patch was rejected because of `apply_patch` hunk syntax
errors, so the following files were not yet created or implemented:

- `start-qwen-sglang.sh`
- `start-swebench-verified.sh`
- `stop-qwen.sh`
- `stop-swebench-verified.sh`
- `eval.sh`
- `report.sh`
- `swebench-pro.sh`
- `deepswe1.1.sh`
- `terminal-bench2.1.sh`
- `livecodebench.sh`
- `frontiercode1.1.sh`

## Findings & best practice

- Official SWE-bench supports Docker-based evaluation, explicit
  dataset/predictions/run ID/worker config, and re-reporting from saved logs.
- Official leaderboard submissions are kept as structured experiments.
- A resumable harness separates per-instance ledger, verdict log, summary,
  and scored predictions.
- The manifest MUST record: model ID, dataset revision/fingerprint, git
  commit, Docker image digest, client/scaffold version, endpoint, worker
  count, seed, config hash.
- Separate raw trajectory, canonical prediction, and evaluation verdict.
- Retry artifacts are preserved per attempt; the canonical scoring attempt is
  explicitly named.
- `report.sh` reads the structured summary, not log scraping.
- Per the February 2026 OpenAI announcement, SWE-bench Verified is no longer
  suitable for measuring frontier capability due to contamination and test
  design issues — SWE-bench Pro is the recommended successor. Verified is still
  fine for local engineering experiments, but do not read its scores as
  current-leaderboard numbers.

## Next-step order

1. Review the current `benchmark-lib.sh`; strengthen JSON escaping / manifest
   structure if needed.
2. Write `start-qwen-sglang.sh`: `ssh user1@spark1.local`,
   `~/git/dgx-spark-qwen38/run.sh` based, Docker startup, health check, archive.
3. Write `start-swebench-verified.sh`: model `/v1/models` autodetection,
   server readiness check, `--limit-new`, raw trajectory + state ledger,
   archive.
4. Confirm mini-swe-agent's actual output format; write the raw → canonical
   SWE-bench `predictions.jsonl` adapter.
5. Wire `eval.sh` to the official SWE-bench evaluator and produce
   `eval/summary.json` / `breakdown.json` reliably.
6. In `report.sh`, print resolved/failed/missing and `resolved / 500`.
7. For the other 5 benchmarks: confirm each official CLI / I/O format first,
   then write the adapter. Do not hard-code a CLI that does not exist.
8. Mark every script executable and run a `--limit-new 1` smoke test.
9. Integrated Docker/SSH verification only after user approval and a confirmed
   `spark1.local` connection.

## Important caveats

- An earlier environment check showed local DNS did not resolve `spark1.local`,
  and an SSH attempt hit an SSH-config permission error. Real remote
  verification has not happened yet.
- `/home/user1/src/inference-engines/ds4-server` and `swebench-verified-mini`
  contain prior run scripts and conversion logic; reference them in future
  work but do not edit files outside this repo.
- Existing SWE-bench code already references conversion between
  mini-swe-agent's `preds.json`/trajectory and the official harness's
  `predictions.jsonl`. Check whether that conversion can be reused.
- The user wants to resume implementation the next day, so do not report
  "all scripts have been written" before implementation is actually complete.
