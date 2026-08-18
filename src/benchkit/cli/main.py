"""benchkit CLI entry point.

Subcommands implemented for issue 1:
- create-experiment  produce an experiment_id + manifest from a spec YAML/JSON
- plan               expand the matrix into trials (dry-run, no DB writes)
- start-attempt      create a new attempt for a trial
- resume             resume a trial (skip already-completed instances)
- status             print experiment/trial/attempt state summary

Subcommands added by later issues: pause / cancel / retry / select-attempt
/ evaluate / report. Each subcommand accepts --json for machine output.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from benchkit.artifact import attempt_dir, ensure_attempt_layout
from benchkit.ids import (
    new_attempt_id,
    new_experiment_id,
    validate_experiment_id,
    validate_trial_id,
)
from benchkit.manifest import write_manifest
from benchkit.matrix import expand_matrix
from benchkit.store import Store


def _load_spec(path: str | None) -> dict:
    if path is None or path == "-":
        return json.load(sys.stdin)
    return json.loads(Path(path).read_text())


def _store_from_env() -> Store:
    root = Path(os.environ.get("BENCKKIT_ROOT", "."))
    return Store(root / "meta.db")


def cmd_create_experiment(args) -> int:
    spec = _load_spec(args.spec)
    spec["experiment_id"] = spec.get("experiment_id") or new_experiment_id()
    validate_experiment_id(spec["experiment_id"])

    store = _store_from_env()
    store.create_experiment(spec["experiment_id"], spec)

    root = Path(os.environ.get("BENCKKIT_ROOT", ".")) / "results" / spec["experiment_id"]
    root.mkdir(parents=True, exist_ok=True)
    write_manifest(root / "manifest.json", spec, level="experiment")

    if args.json:
        print(json.dumps({"experiment_id": spec["experiment_id"]}, indent=2))
    else:
        print(f"created experiment {spec['experiment_id']}")
        print(f"  manifest: {root / 'manifest.json'}")
    return 0


def cmd_plan(args) -> int:
    spec = _load_spec(args.spec)
    spec["experiment_id"] = spec.get("experiment_id") or new_experiment_id()
    validate_experiment_id(spec["experiment_id"])
    trials = expand_matrix(spec)
    if args.json:
        print(json.dumps({"experiment_id": spec["experiment_id"], "trial_count": len(trials), "trials": trials}, indent=2))
    else:
        print(f"experiment {spec['experiment_id']} → {len(trials)} trial(s):")
        for t in trials:
            print(f"  - {t['trial_id']:14s}  {t['model_id']}  cfg={t['config_bundle_id']}")
    return 0


def cmd_start_attempt(args) -> int:
    validate_trial_id(args.trial)
    root = Path(os.environ.get("BENCKKIT_ROOT", ".")) / "results" / args.experiment
    trial_dir = root / "trials" / args.trial / "attempts"
    aid = new_attempt_id(str(trial_dir) if trial_dir.exists() else None)
    a_path = Path(attempt_dir(str(root), args.trial, aid))
    ensure_attempt_layout(a_path)

    store = _store_from_env()
    store.create_trial(args.experiment, args.trial, {"state": "planned"})
    store.create_attempt(args.trial, aid, {"experiment_id": args.experiment})

    (a_path / "events.jsonl").write_text("")
    if args.json:
        print(json.dumps({"attempt_id": aid, "path": str(a_path)}, indent=2))
    else:
        print(f"started {aid} → {a_path}")
    return 0


def cmd_resume(args) -> int:
    validate_trial_id(args.trial)
    store = _store_from_env()
    pending = store.list_pending_instances(args.trial)
    if args.json:
        print(json.dumps({"trial_id": args.trial, "pending_instances": pending}, indent=2))
    else:
        print(f"trial {args.trial}: {len(pending)} pending instance(s)")
        for p in pending:
            print(f"  - {p}")
    return 0


def cmd_status(args) -> int:
    validate_experiment_id(args.experiment)
    store = _store_from_env()
    exp = store.get_experiment(args.experiment)
    trials = store.list_trials(args.experiment)
    if args.json:
        print(json.dumps({"experiment": exp, "trial_count": len(trials)}, indent=2))
    else:
        print(f"experiment {args.experiment}")
        print(f"  benchmark: {exp.get('benchmark_id')}@{exp.get('benchmark_version')}")
        print(f"  trials: {len(trials)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="benchkit", description="Multi-benchmark orchestration CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    def _add(name, handler):
        sp = sub.add_parser(name)
        sp.add_argument("--json", action="store_true")
        sp.set_defaults(func=handler)
        return sp

    p_ce = _add("create-experiment", cmd_create_experiment)
    p_ce.add_argument("--spec", required=True)

    p_plan = _add("plan", cmd_plan)
    p_plan.add_argument("--spec", required=True)

    p_sa = _add("start-attempt", cmd_start_attempt)
    p_sa.add_argument("--experiment", required=True)
    p_sa.add_argument("--trial", required=True)

    p_re = _add("resume", cmd_resume)
    p_re.add_argument("--trial", required=True)

    p_st = _add("status", cmd_status)
    p_st.add_argument("--experiment", required=True)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())