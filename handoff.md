# WIP: 대규모 벤치마크 실행 레이아웃 및 러너

작성 시점: 2026-08-17

## 사용자 목표

WSL2에서 Docker를 사용해 여러 모델/설정으로 SWE-bench 및 기타 벤치마크를 대규모 실행한다. Qwen 서버는 `spark1.local`에서 SGLang/Docker로 실행하고, 클라이언트와 평가는 현재 WSL2에서 실행한다.

## 확정된 개념

- `RUN_ID`: 하나의 500문제 실험 묶음 ID. 튜닝 중에는 유지한다. 예: `RUN_ID=1`.
- `TUNE_NO`: 서버 또는 클라이언트 설정이 바뀔 때마다 증가한다. 한쪽만 바뀌어도 증가시킨다.
- `--limit-new N`: 전체 문제를 돌리지 않고, 아직 정상 완료되지 않은 새 문제를 최대 N개 처리한 뒤 종료한다. smoke test는 `--limit-new 1`.
- 이미 정상 완료된 문제는 덮어쓰지 않고 건너뛴다.
- 중단/실패 뒤에는 남은 문제를 재개한다.
- 같은 RUN 디렉터리의 기존 데이터는 삭제하지 않고 누적한다.

## 확정된 스크립트 이름

- `start-qwen-sglang.sh`: 원격 `spark1.local`에서 Qwen SGLang Docker 서버를 시작.
- `start-swebench-verified.sh`: 로컬 WSL2에서 SWE-bench 클라이언트를 실행. 서버가 준비되지 않았으면 서버를 시작/대기하는 흐름을 넣는 방향.
- `stop-qwen.sh`
- `stop-swebench-verified.sh`
- `eval.sh`
- `report.sh`
- 추가 benchmark 진입점:
  - `swebench-pro.sh`
  - `deepswe1.1.sh`
  - `terminal-bench2.1.sh`
  - `livecodebench.sh`
  - `frontiercode1.1.sh`

## 결과 레이아웃 합의안

```text
results/
└── run-$RUN_ID/
    ├── manifest.json
    ├── state.jsonl
    ├── predictions/
    │   ├── raw/          # 원본 trajectory/agent 출력, 절대 덮어쓰지 않음
    │   └── canonical/    # 평가기가 요구하는 정규 prediction 포맷
    ├── eval/
    │   ├── input/        # 평가 입력 변환 산출물
    │   ├── raw/          # 평가기 원본 로그/출력
    │   ├── breakdown.json
    │   └── summary.json
    ├── logs/
    └── archive/
        ├── tune012-start-qwen-sglang.sh
        ├── tune012-start-swebench-verified.sh
        └── tune012-config.json
```

실제 현재 저장소에는 연구 메모 `RESEARCH_BENCHMARK_RUN_LAYOUT.md`와 불완전한 `benchmark-lib.sh`만 생성되어 있다. 나머지 구현은 아직 완료되지 않았다.

## 실행 인터페이스 목표

```bash
RUN_ID=1 TUNE_NO=1 ./start-qwen-sglang.sh
RUN_ID=1 TUNE_NO=1 PARALLELISM=2 ./start-swebench-verified.sh --limit-new 1
RUN_ID=1 ./eval.sh
RUN_ID=1 ./report.sh
```

`RUN_ID`와 `TUNE_NO`는 시작 스크립트에서 필수 환경변수로 받는다. 실행 시 canonical script를 `results/run-$RUN_ID/archive/`에 같은 `TUNE_NO`로 보존하며, 동일 archive가 이미 있으면 TUNE_NO 증가를 요구한다.

## 구현 중단 지점

현재 `benchmark-lib.sh`만 성공적으로 추가되었다. 내용은 다음을 제공한다.

- `--limit-new N` 파싱
- RUN_ID/TUNE_NO 양의 정수 검증
- `results/run-$RUN_ID` 구조 생성
- 기본 `manifest.json` 생성
- `state.jsonl` 생성
- archive script 복사 helper

이후 대규모 patch를 시도했으나 `apply_patch` hunk 문법 오류로 전체 patch가 적용되지 않았다. 따라서 다음 파일들은 아직 생성되지 않았거나 구현되지 않았다.

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

## 조사 결과 및 best practice

- 공식 SWE-bench는 Docker 기반 평가, 명시적 dataset/predictions/run ID/worker 설정, 저장된 로그 재보고를 지원한다.
- 공식 리더보드 제출은 구조화된 experiments 형태로 유지한다.
- resumable harness는 per-instance ledger, verdict log, summary, scored predictions를 분리한다.
- 반드시 모델 ID, dataset revision/fingerprint, git commit, Docker image digest, client/scaffold 버전, endpoint, worker 수, seed, config hash를 manifest에 기록해야 한다.
- raw trajectory, canonical prediction, evaluation verdict를 분리한다.
- retry는 attempt artifact로 보존하고 canonical scoring attempt를 명시한다.
- `report.sh`는 로그 scraping 대신 구조화된 summary를 읽어야 한다.
- 2026-02 OpenAI 발표에 따르면 SWE-bench Verified는 contamination 및 테스트 설계 문제 때문에 frontier capability 측정에는 부적절해졌고 SWE-bench Pro를 권고한다. Verified를 로컬 엔지니어링 실험에 사용하는 것은 가능하지만 최신 leaderboard 점수처럼 해석하지 않는다.

## 다음 작업 순서

1. 현재 `benchmark-lib.sh` 검토 및 필요하면 JSON escaping/manifest 구조 보강.
2. `start-qwen-sglang.sh` 작성: `ssh user1@spark1.local`, `~/git/dgx-spark-qwen38/run.sh` 기반, Docker 기동, health check, archive.
3. `start-swebench-verified.sh` 작성: 모델 `/v1/models` 검출, 서버 readiness 확인, `--limit-new`, raw trajectory 및 state ledger, archive.
4. 실제 mini-swe-agent 출력 포맷을 확인해 raw → canonical SWE-bench `predictions.jsonl` adapter 작성.
5. `eval.sh`를 공식 SWE-bench evaluator에 연결하고 `eval/summary.json`, `breakdown.json`을 안정적으로 생성.
6. `report.sh`에서 resolved/failed/missing 및 `resolved / 500` 표시.
7. 기타 benchmark 5개는 실제 공식 CLI/입출력 포맷을 확인한 뒤 각 adapter를 구현. 단순히 존재하지 않는 CLI를 하드코딩하지 않는다.
8. 모든 스크립트에 executable bit 설정 및 `--limit-new 1` smoke test.
9. Docker/SSH가 필요한 통합 검증은 사용자 승인 및 실제 `spark1.local` 연결 가능 여부 확인 후 실행.

## 중요한 주의사항

- 이전 환경 확인에서 로컬 DNS가 `spark1.local`을 해석하지 못했고 SSH 시도는 SSH config 권한 오류가 있었다. 실제 원격 검증은 아직 못 했다.
- `/home/user1/src/inference-engines/ds4-server`와 `swebench-verified-mini`에 기존 실행 스크립트와 변환 로직이 있으므로 다음 작업에서 참고하되, 현재 저장소 밖 파일은 함부로 수정하지 않는다.
- 기존 SWE-bench 관련 코드에는 mini-swe-agent의 `preds.json`/trajectory와 official harness의 `predictions.jsonl` 사이 변환이 이미 언급되어 있다. 이 변환을 재사용할 수 있는지 확인한다.
- 사용자가 내일 이어서 구현을 원하므로, 구현 완료 전까지는 “모든 스크립트가 작성되었다”고 보고하지 않는다.
