# Spec: 멀티 벤치마크 실행 오케스트레이션

## 목표

CLI와 향후 Web UI가 동일한 API/상태 모델로 benchmark × model × config matrix를 계획·실행·중지·재개·재시도·평가·보고한다.

## ID와 불변성

- `benchmark_id@version`, `model_id@revision`, `config_bundle_id`, `experiment_id`, `trial_id`, `attempt_id`는 생성 후 변경하지 않는다.
- 모든 resolved manifest에는 dataset fingerprint, evaluator version/image digest, client/server git commit, endpoint, seed, worker 수, config hash를 기록한다.
- config 변경은 기존 trial을 수정하지 않고 새 trial을 만든다.

## 상태

Experiment/trial/attempt 상태: `planned`, `queued`, `running`, `paused`, `completed`, `failed`, `aborted`, `invalid`.

Instance 상태: `queued`, `claimed`, `running`, `checkpointed`, `completed`, `failed`, `aborted`, `invalid`, `superseded`.

상태 전이는 append-only event와 UTC timestamp, actor, reason, error metadata를 가진다. 중복 worker claim은 lease/token으로 방지한다.

## Benchmark plugin contract

```text
validate_definition()
enumerate_instances(dataset_revision)
prepare_instance(instance, config)
run_instance(endpoint, prepared_input, runtime)
parse_output(raw_artifact) -> canonical_artifact
evaluate(canonical_artifact_set) -> summary, breakdown
```

plugin manifest는 protocol, timeout, retry policy, artifact schema, evaluator command/image, metric aggregation을 선언한다. evaluator 결과에는 raw metric, normalized metric, denominator, missing/invalid count를 포함한다.

## Artifact layout

```text
results/<experiment-id>/
  manifest.json
  trials/<trial-id>/
    manifest.json
    attempts/<attempt-id>/
      events.jsonl
      state.jsonl
      raw/
      canonical/
      logs/
      checkpoints/
    selected.json
    scores/<report-id>/{summary,breakdown}.json
```

파일은 temporary path에 쓴 뒤 atomic rename한다. retry artifact는 덮어쓰지 않는다. `selected.json`은 scoring에 사용할 attempt와 canonical artifact hash를 명시한다.

## CLI/API 최소 명령

`create-experiment`, `plan`, `start`, `pause`, `resume`, `cancel`, `retry`, `select-attempt`, `evaluate`, `report`, `status`.

모든 명령은 사람이 읽는 출력과 JSON 출력(`--json`)을 제공한다. UI는 shell을 직접 제어하지 않고 이 서비스 경계를 호출한다.

## 수용 기준

- 두 benchmark, 두 model, 두 config를 계획하면 8개 trial이 생성된다.
- trial 재시작은 새 attempt를 만들며 기존 raw artifact는 동일 hash로 남는다.
- 중단 후 resume은 completed instance를 건너뛰고 미완료 instance만 claim한다.
- report는 선택된 attempt와 evaluator fingerprint를 표시한다.
- 서로 다른 precision/commit/config 결과가 자동 병합되지 않는다.
- fake OpenAI-compatible endpoint와 fake evaluator로 네트워크 없이 전체 lifecycle을 검증한다.
