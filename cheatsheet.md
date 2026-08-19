# Cheatsheet — SWE-bench Verified 실행

Qwen SGLang 서버(`spark1.local:30000`, 모델 `qwen3.8-27b`)에 연결해
SWE-bench Verified 문제를 실제로 풀고, 공식 swebench harness로 평가한다.

## 1. 문제 풀기 (클라이언트)

```bash
# 첫 실행: 새 문제 2개
RUN_ID=1 TUNE_NO=1 PARALLELISM=2 ./start-swebench-verified.sh --limit-new 2

# 재실행: 같은 TUNE_NO 는 archive 충돌 → TUNE_NO 증가
# 완료된 인스턴스는 자동 스킵되고, 새 문제 N 개를 푼다
RUN_ID=1 TUNE_NO=2 PARALLELISM=2 ./start-swebench-verified.sh --limit-new 2
```

필수 환경변수:

| 변수 | 설명 | 기본값 |
|---|---|---|
| `RUN_ID` | 실험 묶음 ID (양의 정수) | — (필수) |
| `TUNE_NO` | 설정 변경 시 증가 (양의 정수) | — (필수) |
| `PARALLELISM` | 동시 worker 수 | `2` |
| `OPENAI_BASE_URL` | 서버 endpoint | `http://spark1.local:30000/v1` |
| `OPENAI_API_KEY` | 서버 키 | `none` |
| `MODEL_NAME` | 명시적 모델명 (없으면 `/v1/models` 자동 탐지) | 자동 |

동작:

- mini-swe-agent(litellm 기반)로 실제 인스턴스를 푼다 (Docker 컨테이너 사용).
- `results/run-$RUN_ID/predictions/raw/` 에 누적 (trajectory, `preds.json`).
- 완료 인스턴스는 스킵, `--limit-new N` 만큼 새 문제만 푼다.
- 실행 스크립트는 `results/run-$RUN_ID/archive/tuneNNN-start-swebench-verified.sh` 로 보존.
- 완료 레코드는 `results/run-$RUN_ID/state.jsonl` 에 append.

참고: 문제마다 소요 시간이 크게 다르다 (간단한 문제 5~10분, 어려운 문제 40~50분+).

## 2. 평가 (공식 swebench harness, 로컬 Docker)

```bash
RUN_ID=1 ./eval.sh
```

동작:

- `predictions/canonical/predictions.jsonl` 을 공식 `swebench.harness.run_evaluation` 에 전달.
- 이미 평가된 인스턴스는 건너뛰고 미평가분만 평가 (증분).
- 결과: `results/run-$RUN_ID/eval/raw/<model>.<run>.json` (harness 원본),
  `eval/summary.json`, `eval/breakdown.json`.

환경변수: `MAX_WORKERS`(기본 4), `TIMEOUT`(인스턴스당 테스트 타임아웃, 기본 1800s).

## 3. 리포트

```bash
RUN_ID=1 ./report.sh
```

`eval/summary.json` 을 읽어 resolved/unresolved/missing/not-evaluated 및
`resolved / total` 점수를 출력한다. 로그 스크래핑 없음.

## 4. 전체 파이프라인

```bash
RUN_ID=1 TUNE_NO=1 ./start-swebench-verified.sh --limit-new 2   # 풀기
RUN_ID=1 ./eval.sh                                               # 평가
RUN_ID=1 ./report.sh                                             # 리포트
```

## 결과 레이아웃

```text
results/run-$RUN_ID/
├── manifest.json
├── state.jsonl
├── predictions/
│   ├── raw/          # mini-swe-agent 원본 (traj, preds.json) — 절대 덮어쓰지 않음
│   └── canonical/    # swebench harness용 predictions.jsonl
├── eval/
│   ├── input/
│   ├── raw/          # harness 원본 리포트
│   ├── summary.json
│   └── breakdown.json
├── logs/
└── archive/          # 실행 스크립트 보존 (tuneNNN-*)
```

## 서버 상태 확인

```bash
# 모델 노출 확인
curl -s http://spark1.local:30000/v1/models

# 서버 로그 (spark1.local)
ssh user1@spark1.local 'docker logs --tail 20 qwen38-sglang-run'
```

## 주의사항

- `spark1.local` 의 Qwen 서버는 **30000 포트** (기존 스크립트의 8000 기본값 아님).
- mini-swe-agent 전역 설정 `~/.config/mini-swe-agent/.env` 가 8000 포트를
  가리킬 수 있으니, 실행 시 `OPENAI_BASE_URL` 로 반드시 30000 포트를 지정한다.
- eval/report 는 `TUNE_NO` 가 필요 없다.
- 같은 RUN 디렉터리의 기존 데이터는 삭제하지 않고 누적한다.
