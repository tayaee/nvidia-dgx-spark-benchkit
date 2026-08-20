# Cheatsheet — running SWE-bench Verified

Connects to the Qwen SGLang server (`spark1.local:30000`, model `qwen3.8-27b`),
solves SWE-bench Verified tasks, and evaluates with the official swebench harness.

## 1. Solving tasks (client)

```bash
# First run: solve 2 new tasks
RUN_ID=1 SCRIPT_VER=1 PARALLELISM=2 ./start-swebench-verified.sh --limit-new 2

# Plain rerun (no config change): keep SCRIPT_VER
# Completed instances are auto-skipped; solve up to N new ones.
RUN_ID=1 SCRIPT_VER=1 PARALLELISM=2 ./start-swebench-verified.sh --limit-new 2

# Increment SCRIPT_VER only when the config changes (server options, script edits).
RUN_ID=1 SCRIPT_VER=2 PARALLELISM=2 ./start-swebench-verified.sh --limit-new 2

# After the first run, you can omit both vars: the script picks them up from
# .cache/start-swebench-verified.sh.env (auto-written after each successful run).
PARALLELISM=2 ./start-swebench-verified.sh --limit-new 2
```

Environment variables:

| Variable          | Description                                                                                                | Default                                                |
|-------------------|------------------------------------------------------------------------------------------------------------|--------------------------------------------------------|
| `RUN_ID`          | Experiment bundle ID (non-negative integer)                                                                | last-used `.cache/<script>.env`, or 1                  |
| `SCRIPT_VER`      | Config (server/client settings) version number. Increment **only** when the config changes (non-negative) | last-used `.cache/<script>.env`, or 1                  |
| `PARALLELISM`     | Concurrent worker count                                                                                    | `2`                                                    |
| `OPENAI_BASE_URL` | Server endpoint                                                                                            | `http://spark1.local:30000/v1`                          |
| `OPENAI_API_KEY`  | Server key                                                                                                 | `none`                                                 |
| `MODEL_NAME`      | Explicit model name (auto-detected via `/v1/models` if unset)                                              | auto                                                   |

Resolution order for `RUN_ID` / `SCRIPT_VER`: (1) command-line env var, (2)
`.cache/<script-basename>.env`, (3) literal `1`. The cache is written
atomically after every successful run; delete the file to reset both values
to 1.

Behavior:

- mini-swe-agent (litellm-based) solves real instances (Docker containers).
- Accumulates in `results/run-$RUN_ID/predictions/raw/` (trajectory, `preds.json`).
- Completed instances are skipped; only `--limit-new N` new instances are run.
- The launch script is archived as
  `results/run-$RUN_ID/archive/vNNN-start-swebench-verified.sh`.
  If the same `SCRIPT_VER` is re-run with identical content, it passes through
  (plain rerun); if the content changed, the script aborts and demands a bump.
- Completion records append to `results/run-$RUN_ID/state.jsonl`.

Note: per-instance runtime varies widely (5–10 min for easy, 40–50 min+ for hard).

## 2. Evaluation (official swebench harness, local Docker)

```bash
RUN_ID=1 ./eval.sh
```

Behavior:

- Passes `predictions/canonical/predictions.jsonl` to the official
  `swebench.harness.run_evaluation`.
- Skips already-evaluated instances (incremental).
- Outputs: `results/run-$RUN_ID/eval/raw/<model>.<run>.json` (raw harness),
  `eval/summary.json`, `eval/breakdown.json`.

Environment variables: `MAX_WORKERS` (default 4), `TIMEOUT` (per-instance test
timeout, default 1800s).

## 3. Reporting

```bash
RUN_ID=1 ./report.sh
```

Reads `eval/summary.json` and prints resolved / unresolved / missing /
not-evaluated counts and `resolved / total` score. No log scraping.

## 4. Full pipeline

```bash
RUN_ID=1 SCRIPT_VER=1 ./start-swebench-verified.sh --limit-new 2   # solve
RUN_ID=1 ./eval.sh                                                  # evaluate
RUN_ID=1 ./report.sh                                                # report
```

## Results layout

```text
results/run-$RUN_ID/
├── manifest.json
├── state.jsonl
├── predictions/
│   ├── raw/          # mini-swe-agent raw output (traj, preds.json) — never overwritten
│   └── canonical/    # predictions.jsonl for swebench harness
├── eval/
│   ├── input/
│   ├── raw/          # raw harness reports
│   ├── summary.json
│   └── breakdown.json
├── logs/
└── archive/          # archived launch scripts (vNNN-*)
```

## Server status

```bash
# Check exposed models
curl -s http://spark1.local:30000/v1/models

# Server logs (spark1.local)
ssh user1@spark1.local 'docker logs --tail 20 qwen38-sglang-run'
```

## Notes

- The Qwen server on `spark1.local` listens on **port 30000** (not the legacy
  8000 default).
- mini-swe-agent's global config (`~/.config/mini-swe-agent/.env`) may point at
  port 8000, so always set `OPENAI_BASE_URL` to the 30000 endpoint at runtime.
- `eval.sh` / `report.sh` do not need `SCRIPT_VER`.
- The same RUN directory accumulates, never deletes.
