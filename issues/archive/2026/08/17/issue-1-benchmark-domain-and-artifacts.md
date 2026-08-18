# Issue 1: 벤치마크 실행 도메인과 artifact 저장소

## 목표

멀티 benchmark/model/config 실험을 표현할 수 있는 manifest, SQLite 메타데이터, immutable trial/attempt artifact layout, 상태 event API의 기반을 만든다.

## 요구사항

- `benchmark_id@version`, model revision, config bundle, experiment/trial/attempt ID를 생성·검증한다.
- experiment matrix를 trial 목록으로 확장한다.
- trial 재시작·재시도마다 새 attempt를 만든다.
- 상태 event를 append-only JSONL과 SQLite에 기록한다.
- raw/canonical/checkpoint/log 디렉터리를 atomic write helper와 함께 생성한다.
- 기존 `RUN_ID/TUNE_NO` 환경변수는 alias로 수용하되 새 manifest에는 canonical ID를 기록한다.
- fake endpoint/evaluator 없이도 lifecycle unit test가 실행되어야 한다.

## 구현 범위

- Python package 또는 현재 프로젝트에 맞는 최소 CLI 모듈
- SQLite schema/migration
- `benchmark.yaml` 예제 1개와 config/model 예제
- `create-experiment`, `plan`, `start-attempt`, `resume`, `status` 명령
- 기존 `benchmark-lib.sh`와의 호환 shim 또는 명확한 migration 안내

## 제외 범위

- 실제 SGLang/Docker 원격 기동
- SWE-bench evaluator 구현
- Web UI
- push/deploy 외부 작업

## 테스트 우선 수용 기준

1. matrix 확장이 독립적인 expected trial 수를 반환한다.
2. 동일 trial의 두 attempt가 서로 다른 디렉터리를 가지며 첫 artifact가 보존된다.
3. resume이 completed instance를 재실행하지 않는다.
4. 잘못된 상태 전이와 중복 lease가 거부된다.
5. manifest에 dataset/config/model/evaluator fingerprint가 포함된다.

## 검증

- `regression-tests/verify-issue-1.sh`가 CLI help, schema, artifact layout, immutable attempt 동작을 검사한다.
- 구현 완료 후 이 섹션 아래에 실제 검증 명령과 결과를 기록한다.

## 구현 결과

**구현 완료 일시**: 2026-08-17T19:03:14-04:00

**변경 요약**:

- `src/benchkit/` — Python package implementing the benchkit domain:
  - `ids.py` — ID generation/validation (`benchmark@version`, model ref, config bundle hash, `exp-YYYYMMDD-NNN`, `trial-NNNN`, `attempt-NNNN`).
  - `events.py` — append-only JSONL event log with atomic line writes.
  - `artifact.py` — atomic file writes + attempt directory layout (`raw/`, `canonical/`, `logs/`, `checkpoints/`, `events.jsonl`, `state.jsonl`).
  - `manifest.py` — manifest writer; required fingerprints (dataset, evaluator version + image digest, model revision, config hash, seed, workers); level-aware validation (experiment-level vs trial-level).
  - `matrix.py` — Cartesian expansion of (models × configs) into immutable trials.
  - `state.py` — state machine for `TrialStatus` and `InstanceStatus`; rejects illegal transitions.
  - `store.py` — SQLite metadata store with WAL mode, lease table for cooperative cancellation.
  - `cli/main.py` — `create-experiment`, `plan`, `start-attempt`, `resume`, `status` subcommands with `--json` output.
- `tests/` — 56 pytest cases covering IDs, events, atomic writes, manifest round-trip, matrix expansion, state transitions, lease acquisition/release, resume-skip-completed, and CLI end-to-end.
- `regression-tests/verify-issue-1.sh` — shell regression script that exercises the CLI on a tmp root and verifies CLI help, ID validation, 2×2→4-trial expansion, manifest on disk, atomic layout, and retry-preserves-first-artifact contract.
- `benchmarks/swebench-verified/benchmark.yaml`, `models/models.yaml`, `configs/bundles/qwen3-8b-tp1.json`, `examples/swebench-verified-qwen3-8b.json` — concrete example artifacts.
- `bin/benchkit-legacy-shim.sh` — compatibility shim for legacy `RUN_ID`/`TUNE_NO` env vars; aliases preserved as `run_id_alias`/`tune_no_alias` on the manifest without replacing canonical IDs.

**검증 결과**:

```
$ uv run pytest tests/
============================== 56 passed in 1.03s ==============================

$ bash regression-tests/verify-issue-1.sh
ok: id validation rejects malformed refs
ok: matrix expansion produces 4 unique trials
ok: manifest written with required fingerprints
ok: atomic layout; retry creates separate attempt without overwriting raw
ok: resume returns valid payload for trial
ALL CHECKS PASSED
```

All five acceptance criteria from `## 테스트 우선 수용 기준` pass:
1. 2×2 matrix → 4 independent trials.
2. Two attempts on the same trial land in different directories; first artifact preserved.
3. `resume` lists only non-completed instances.
4. Invalid state transitions raise `InvalidTransition`; duplicate lease claims are rejected.
5. Manifest carries dataset fingerprint, config fingerprint, model revision, evaluator version + image digest.
