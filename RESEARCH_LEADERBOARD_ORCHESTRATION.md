# 멀티 벤치마크 리더보드 실행 시스템 재설계 연구

작성 시점: 2026-08-17

## 결론

현재의 `results/run-$RUN_ID` 구조는 단일 SWE-bench 실험의 임시 실행 폴더로는 적절하지만, 여러 벤치마크·모델·구성·재시작·재개를 관리하는 시스템의 기본 모델로는 부족하다. 핵심 단위는 `run` 하나가 아니라 다음 네 가지를 분리해야 한다.

1. **Benchmark Definition**: 무엇을, 어떤 데이터와 채점기로 평가하는가.
2. **Experiment**: 어떤 모델·서버·클라이언트 구성 조합을 비교하는가.
3. **Execution Attempt**: 중단·재시작·재시도 때마다 생성되는 불변 실행 단위.
4. **Score Report**: 특정 artifact와 evaluator 버전에 대해 생성된 재현 가능한 결과.

웹 UI는 이 계층을 직접 다루되, 사용자는 `benchmark + model matrix + config matrix + policy`를 제출하면 된다. 실행기는 각 조합을 불변 `trial`로 확장하고, 실패나 재시작은 새 `attempt`로 기록한다.

## 참고한 1차 자료와 관찰

- [Hugging Face Open LLM Leaderboard 문서](https://huggingface.co/docs/leaderboards/open_llm_leaderboard/about)는 여러 작업을 하나의 통일된 평가 프레임워크로 실행하고, 모델별 request/status, 전체 결과, 상세 결과를 분리한다. 이는 실행 큐와 점수 저장소를 분리해야 한다는 근거다.
- [Hugging Face FAQ](https://huggingface.co/docs/leaderboards/open_llm_leaderboard/faq)는 동일 모델도 commit 또는 precision이 다르면 별도 행으로 취급한다고 설명한다. 모델 표시명만으로 결과를 합치지 말고 immutable model revision과 precision을 identity에 포함해야 한다.
- [Hugging Face 점수 정규화 문서](https://huggingface.co/docs/leaderboards/en/open_llm_leaderboard/normalization)는 raw score와 normalized score를 구분하고 random baseline을 반영한다. 보고서는 raw, normalized, aggregate를 모두 보존해야 한다.
- [LMSYS FastChat Arena 문서](https://github.com/lm-sys/FastChat/blob/main/docs/arena.md)는 OpenAI-compatible endpoint를 선호하고, 외부 endpoint를 등록해 모델 목록에 포함하는 방식을 설명한다. 서버별 adapter보다 공통 endpoint contract를 우선할 수 있다.
- [FastChat Arena 구현](https://github.com/lm-sys/FastChat/blob/main/fastchat/serve/gradio_web_server.py)은 모델 선택, 생성 파라미터, regenerate, 사용자 vote를 별도 상태로 다룬다. 상호작용형 벤치는 deterministic batch 벤치와 다른 evaluator/결과 모델이 필요하다.
- [SWE-bench 공식 저장소](https://github.com/swe-bench/SWE-bench#readme)와 [제출 안내](https://www.swebench.com/submit.html)는 Docker, dataset/prediction/run ID/worker, 구조화된 experiments를 명시한다. 실행 로그를 긁어 점수를 만들지 말고 canonical prediction과 evaluator output을 보존해야 한다.
- [OpenAI의 SWE-bench Verified 평가 안내](https://openai.com/index/why-we-no-longer-evaluate-swe-bench-verified/)는 벤치마크 이름만으로 frontier 성능을 주장하지 말고 데이터 오염 및 테스트 설계 한계를 보고서에 표시해야 함을 보여준다.

## 권장 도메인 모델

```text
BenchmarkDefinition  1 ── * BenchmarkVersion
Experiment           1 ── * Trial
Trial                1 ── * Attempt
Attempt              1 ── * InstanceExecution
Attempt              1 ── * Artifact
ScoreReport          * ── 1 Attempt/ArtifactSet
```

### BenchmarkDefinition

벤치마다 다음 contract를 선언한다.

- `benchmark_id`, `version`, dataset URI/revision/fingerprint
- 입력 생성기와 실행 adapter
- endpoint contract: OpenAI-compatible chat/completions, shell agent, 또는 custom
- instance key와 expected output schema
- evaluator command/container/image digest
- metric definitions, aggregation, normalization, pass/fail/invalid 규칙
- retry 및 resume 가능 여부
- secret/credential 요구사항과 비용·timeout 제한

벤치 정의는 코드와 함께 버전 관리하고, 실행 시점에 resolved manifest를 snapshot한다.

### Model, Endpoint, Config

`model`은 표시명과 별개로 `provider`, `model_id`, `revision`, `precision`, `weights_uri`를 가진다. `endpoint`는 URL 자체보다 `endpoint_id`, health/model discovery 방식, protocol, auth reference, server image digest를 기록한다.

구성은 하나의 `TUNE_NO` 숫자가 아니라 immutable `config_id`와 canonical JSON hash로 만든다. server config, client/agent config, evaluator config를 별도 객체로 두고 `config_bundle_id`로 묶는다. 숫자 tune 번호는 UI 표시용 alias로만 유지한다.

### Trial과 Attempt

`trial`은 `(benchmark_version, model_revision, endpoint, config_bundle, seed, sample_policy)`의 한 조합이다. 같은 trial을 재개해도 trial ID는 유지한다. 프로세스 재시작, worker 재시작, retry는 각각 새 `attempt_id`를 만든다.

각 instance에는 append-only event를 남긴다: `queued`, `claimed`, `started`, `checkpointed`, `completed`, `failed`, `aborted`, `invalid`, `superseded`. canonical scoring attempt는 명시적으로 선택하며 “가장 최근 파일”을 자동 채택하지 않는다.

## 권장 디렉터리 및 저장소

```text
benchmarks/
  swebench-verified/benchmark.yaml
  swebench-pro/benchmark.yaml
  terminal-bench-2.0/benchmark.yaml
models/
  models.yaml
configs/
  bundles/*.json
experiments/
  exp-20260817-001/experiment.yaml
results/
  exp-20260817-001/
    manifest.json
    trials/
      trial-0001/
        manifest.json
        attempts/attempt-0001/
          state.jsonl
          events.jsonl
          raw/
          canonical/
          logs/
          checkpoints/
        attempts/attempt-0002/
        selected.json
        scores/
          report-0001/summary.json
          report-0001/breakdown.json
    reports/
      leaderboard.json
      leaderboard.csv
      README.md
    archive/
      definitions/
      configs/
      scripts/
```

작은 SQLite/PostgreSQL 메타데이터 DB를 UI의 조회·스케줄·상태 source of truth로 사용하고, 파일 artifact는 재현성과 복구를 위한 immutable 저장소로 둔다. `state.jsonl`은 DB가 없어도 복구 가능한 실행 ledger로 유지한다.

## “URL에 대고 실행해서 아래에 저장” wrapper의 가능 범위

가능하다. 다만 URL만으로는 부족하고, 최소한 `BenchmarkDefinition`이 다음을 제공해야 한다.

```yaml
id: swebench-verified
version: 1.0.0
input:
  source: dataset
  revision: <fingerprint>
adapter:
  protocol: openai-chat
  request_template: prompts/swebench.jinja2
  output_parser: adapters/swebench.py
execution:
  command: python -m runner --endpoint ${ENDPOINT} --input ${INPUT}
  checkpoint: per-instance
evaluation:
  command: docker run ... evaluator ${CANONICAL}
  output_schema: schemas/swebench-summary.json
metrics:
  - name: resolved
    direction: maximize
```

공통 runner는 endpoint discovery/health check, concurrency, timeout, retry, checkpoint, artifact naming, event ledger, cancellation, upload을 담당한다. 벤치별 plugin은 input adapter, prompt/request mapping, output parser, evaluator, metric schema만 담당한다.

따라서 “모든 벤치를 URL만 바꾸어 실행”은 **OpenAI-compatible batch generation 벤치**에서는 상당 부분 가능하지만, SWE-bench처럼 repository checkout·patch·Docker test가 필요한 agentic 벤치, Terminal-bench처럼 터미널 sandbox가 필요한 벤치, Arena처럼 human preference가 필요한 벤치에는 공통 wrapper와 별도 execution backend를 조합해야 한다. evaluator를 URL 응답으로 대체할 수 있다고 가정해서는 안 된다.

## 웹 UI 제안

1. **Catalog**: benchmark version, dataset fingerprint, evaluator image, metric/주의사항을 보여준다.
2. **Experiment Builder**: benchmark 선택 → model/endpoint 선택 → config matrix → seed/sample policy → retry/resource policy를 입력한다. 조합 수와 예상 비용을 미리 계산한다.
3. **Queue/Run Control**: experiment/trial/attempt 계층, 상태, worker, 현재 instance, pause/resume/cancel/retry를 표시한다. cancel은 cooperative cancellation 후 `aborted`로 기록한다.
4. **Trial Detail**: config diff, tune history, attempt timeline, instance별 상태와 raw/canonical/eval artifact 링크를 제공한다.
5. **Reports**: benchmark별 raw/normalized score, resolved/failed/missing/invalid, confidence interval, latency/cost/token, hardware, exact revisions를 필터·비교한다.
6. **Leaderboard View**: 기본 정렬은 aggregate지만, benchmark version·precision·commit·config·provider 필터 없이는 행을 합치지 않는다. “공식 비교 가능”과 “내부 실험”을 구분한다.

## 구현 순서

1. `benchmark-lib.sh`를 단일 run 폴더 helper에서 `experiment/trial/attempt` ID와 atomic artifact/event helper로 확장한다.
2. 먼저 SQLite schema와 CLI 명령(`create`, `plan`, `start`, `pause`, `resume`, `cancel`, `retry`, `report`)을 만든다. UI는 이 CLI/API의 얇은 클라이언트로 시작한다.
3. 공통 OpenAI-compatible runner와 SWE-bench plugin 하나를 완성한다.
4. evaluator는 반드시 canonical schema와 versioned output schema를 사용하게 한다.
5. 이후 terminal/interactive/human-preference용 backend를 추가한다. 각 벤치를 같은 shell script로 위장하지 않는다.
6. 마지막으로 scheduler와 웹 UI를 붙이고, smoke test는 실제 원격 서버 없이 fake endpoint/evaluator로 먼저 수행한다.

## 설계상 금지할 것

- `RUN_ID` 하나에 여러 benchmark/model/config를 섞기
- `TUNE_NO`를 결과 identity로 사용하기
- 재시도 결과를 raw 파일에 덮어쓰기
- 최신 attempt를 암묵적으로 scoring하기
- 로그 scraping으로 score 계산하기
- precision, commit, dataset revision이 다른 모델을 같은 leaderboard 행으로 합치기
- evaluator가 없는 벤치를 URL wrapper만으로 지원한다고 선언하기
