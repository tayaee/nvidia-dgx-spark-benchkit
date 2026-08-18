# ADR: 멀티 벤치마크 실행 오케스트레이션 모델

- 상태: accepted
- 일자: 2026-08-17

## 문맥

여러 benchmark, model, endpoint, configuration 조합을 실행하고, configuration 변경·재시작·재개·재시도를 추적하며, 동일 artifact를 재평가하고 웹 UI에서 제어해야 한다. 기존 `RUN_ID/TUNE_NO`와 단일 결과 디렉터리는 이 요구사항을 표현하기 어렵다.

## 결정

실행 모델을 `BenchmarkDefinition → Experiment → Trial → Attempt → InstanceExecution`으로 분리한다.

- `BenchmarkDefinition`: 입력, adapter, evaluator, metric을 버전 관리한다.
- `Experiment`: 사용자가 만든 실행 계획과 matrix를 불변 snapshot으로 저장한다.
- `Trial`: benchmark version × model revision × config bundle × seed의 한 조합이다.
- `Attempt`: trial의 실행 프로세스 단위다. 재시작/재시도마다 새 attempt를 만든다.
- `InstanceExecution`: 개별 문제의 상태와 artifact를 append-only event로 기록한다.

메타데이터와 scheduler 상태는 SQLite부터 시작하되 저장소 경계를 분리하고, 모든 raw/canonical/evaluation artifact는 파일 기반 immutable 저장소로 보존한다. `RUN_ID`와 `TUNE_NO`는 호환성·표시용 alias로만 유지한다.

공통 runner는 endpoint health, concurrency, retry, checkpoint, cancellation, artifact lifecycle을 담당한다. benchmark plugin은 input adapter, request adapter, output parser, evaluator, metric을 담당한다.

## 결과

OpenAI-compatible batch 벤치는 공통 runner로 단순화할 수 있다. repository/sandbox/human-preference가 필요한 벤치는 별도 backend가 필요하다. 점수는 선택된 canonical artifact와 evaluator 버전에 대해 생성하며 로그 scraping을 사용하지 않는다.

## 대안

- 단일 `run-$id` 폴더: 구현은 쉽지만 matrix와 재시도 lineage를 잃는다.
- benchmark별 독립 shell script: 초기에는 빠르지만 UI·재개·보고서가 중복된다.
- 모든 벤치를 URL wrapper로 통일: endpoint protocol이 다른 agentic/interactive 벤치를 잘못 단순화한다.
