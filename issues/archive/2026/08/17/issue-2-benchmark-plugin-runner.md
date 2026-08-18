# Issue 2: 공통 benchmark plugin runner와 SWE-bench adapter

## 목표

공통 runner가 endpoint health, instance checkpoint, retry, canonical output을 처리하고, plugin이 benchmark별 차이를 담당하도록 한다.

## 요구사항

- OpenAI-compatible fake endpoint를 지원한다.
- plugin contract(`enumerate`, `prepare`, `run`, `parse`, `evaluate`)를 정의한다.
- `--limit-new N`, timeout, concurrency, cooperative cancel을 지원한다.
- raw trajectory와 canonical prediction을 분리 저장한다.
- SWE-bench adapter는 official prediction schema로 변환한다.

## 수용 기준

- fake endpoint에서 실패 후 retry가 새 attempt artifact를 생성한다.
- `--limit-new 1`이 이미 완료된 instance를 건너뛴다.
- canonical output은 schema validation을 통과한다.
- 네트워크 없이 fake endpoint/evaluator 통합 테스트가 통과한다.

## 구현 결과

**구현 완료 일시**: 2026-08-17T19:04:50-04:00

**변경 요약**:

- `src/benchkit/runner/plugin.py` — plugin contract (`BenchmarkPlugin`, `InstanceSpec`, `RuntimeContext`) and `parse_plugin_manifest` (YAML subset parser with required-field validation).
- `src/benchkit/runner/runner.py` — `AttemptRunner`: orchestrates the per-instance lifecycle (claim → prepare → run → parse → persist raw + canonical → release), with concurrency (`ThreadPoolExecutor`), per-instance timeout, retry-with-new-attempt, and cooperative cancellation via a `threading.Event`. Honours `--limit-new N` and skip-already-completed by querying the store's instance_state table.
- `src/benchkit/runner/fake_endpoint.py` — `FakeOpenAIEndpoint`: in-process OpenAI-compatible endpoint used by tests (chat completions + model discovery + health check). No HTTP plumbing — the runner takes a callable, not a URL.
- `src/benchkit/runner/swebench.py` — `SwebenchAdapter`: implements the contract for SWE-bench; `parse()` extracts `<patch>...</patch>` blocks and emits the official prediction schema `{instance_id, model_patch, model_name}`.
- `tests/test_runner.py` — 9 pytest cases covering plugin manifest parsing, instance enumeration, full-attempt lifecycle, `--limit-new` skip, retry-creates-new-attempt, canonical-artifact on disk, cooperative cancel, and SWE-bench schema validation.
- `regression-tests/verify-issue-2.sh` — shell regression that runs the runner against the fake endpoint in five independent python scripts.
- `schemas/swebench-summary.json` — JSON Schema for the canonical SWE-bench prediction shape.
- `examples/swebench-mini-fixture.jsonl` — three-instance fixture for offline tests.

**검증 결과**:

```
$ uv run pytest tests/
============================== 65 passed in 1.11s ==============================

$ bash regression-tests/verify-issue-2.sh
ok: full attempt writes raw + canonical + ledgers
ok: --limit-new skips already-completed instances
ok: retry produced 2 attempt dir(s): ['attempt-0001', 'attempt-0002']
ok: swebench adapter emits {instance_id, model_patch, model_name}
ok: canonical passes schema validation
ok: plugin manifest parsed
ALL CHECKS PASSED
```

All four acceptance criteria pass:
1. Flaky plugin + retry → fresh `attempt-0002/` directory; original raw preserved.
2. `--limit-new 10` with 2 already-completed → only 1 instance processed, 2 skipped.
3. Canonical output `{instance_id, model_patch, model_name}` passes schema validation against `schemas/swebench-summary.json`.
4. End-to-end run completes against `FakeOpenAIEndpoint` with no network — concurrency 2 + 3 instances.
