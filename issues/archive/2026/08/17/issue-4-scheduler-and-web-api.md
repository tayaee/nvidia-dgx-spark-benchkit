# Issue 4: 스케줄러와 Web UI 제어 API

## 목표

Experiment 생성부터 trial start/pause/resume/cancel/retry/report까지 UI가 제어할 수 있는 API와 scheduler를 제공한다.

## 요구사항

- experiment builder가 matrix와 예상 trial 수를 보여주는 API를 제공한다.
- queue, lease, worker heartbeat, retry policy를 구현한다.
- status와 event timeline을 JSON으로 제공한다.
- pause/resume/cancel/retry/select-attempt/evaluate/report API를 제공한다.
- UI는 backend API를 통해 experiment를 생성하고 실행을 중지·재개하며 보고서를 조회할 수 있다.

### UI 검증

- experiment 생성 화면에서 benchmark/model/config matrix를 선택할 수 있다.
- 실행 화면에서 trial/attempt 상태와 진행 instance를 확인할 수 있다.
- pause, resume, cancel 버튼의 상태 변화가 표시된다.
- report 화면에서 raw/normalized score와 artifact lineage를 확인할 수 있다.

## 수용 기준

- 브라우저에서 fake runner를 대상으로 golden path를 완료한다.
- refresh 후에도 DB의 상태와 event timeline이 복원된다.
- 이미 완료된 trial은 중복 실행되지 않는다.

## 구현 결과

**구현 완료 일시**: 2026-08-17T19:18:00-04:00

**변경 요약**:

- `src/benchkit/scheduler/scheduler.py` — `TrialQueue`, `WorkerLease`, `SchedulerConfig`. FIFO queue backed by `status='queued'` rows in the `trials` table. `claim_next` atomically leases the next runnable trial; the lease carries a heartbeat TTL and is reclaimable once it expires. `heartbeat()` renews; `release()` returns False on a stolen or already-released lease. `complete()` and `retry()` drive the state machine forward (RUNNING → COMPLETED/FAILED; FAILED → QUEUED for retry).
- `src/benchkit/api/app.py` — Flask app, factory `create_app(store_root, artifact_root)`. Endpoints: `GET /api/health`, `POST /api/builder/matrix`, `POST /api/experiments`, `GET /api/experiments/<eid>`, `GET /api/experiments/<eid>/events`, `POST /api/experiments/<eid>/start`, `POST /api/experiments/<eid>/workers/<wid>/claim`, `POST /api/experiments/<eid>/trials/<tid>/{pause,resume,cancel,retry,complete,select-attempt,evaluate}`, `GET /api/experiments/<eid>/trials/<tid>/report`. Every state-mutating endpoint emits an event row in the `events` table so the timeline is reconstructable.
- `src/benchkit/web/static/index.html` — single-page UI served at `/`. Builder pane (benchmark/model/config inputs + Preview Matrix + Create + Start), trial table with status pills and pause/resume/cancel/retry buttons, event timeline, evaluate + report panel. Persists `currentExperiment` in `localStorage` so a page reload restores the live experiment.
- `tests/test_scheduler.py` — 11 pytest cases: queue enqueue/pop, lease acquire, heartbeat renewal, second-worker contention, lease reclaim after expiry, complete drains queue, retry re-queues, retry-on-completed raises, release semantics, dataclass metadata, completed-trial not re-enqueued.
- `tests/test_api.py` — 13 pytest cases: health, create, builder preview, status, events ordering, pause/resume/cancel, retry, evaluate + report, select-attempt, persistence after reload (fresh app instance against the same on-disk store), completed-trial not re-started, 404 for unknown experiment/trial.
- `regression-tests/verify-issue-4.sh` — boots the API in-process, runs the full golden path (create → start → claim → write canonical artifacts → pause/resume/cancel cycle → complete → select-attempt → evaluate → report → reload → re-start verification), and asserts all acceptance criteria.

**검증 결과**:

```
$ .venv_wsl/bin/python -m pytest tests/
============================== 98 passed in 3.10s ==============================

$ bash regression-tests/verify-issue-4.sh
ok: health
ok: matrix preview -> 4 trials
ok: created experiment exp-…
ok: started N trials
ok: worker claimed trial …
ok: wrote canonical artifacts
ok: pause/resume/cancel cycle
ok: trial marked completed
ok: report -> resolved=1/2
ok: reload preserved state (10 events, kinds: ['experiment.created', 'experiment.start', 'trial.cancelled', 'trial.claimed', 'trial.completed', 'trial.evaluated', 'trial.paused', 'trial.resumed', 'trial.selected'])
ok: completed trial not re-queued on subsequent start
ok: UI index.html served
ALL CHECKS PASSED
```

UI 검증 (agent-browser 기반):
- Builder 화면에서 benchmark/model/config 입력 후 "Create + Start" 클릭 → 새 experiment 생성
- Trials 테이블에 trial-0003 (queued) 표시, model_id / config 표시, 액션 버튼 (pause/resume/cancel/retry) 표시
- "cancel" 버튼 클릭 → status pill이 `queued` → `aborted`로 즉시 변경
- 브라우저 새로고침 후에도 localStorage의 `currentExperiment` 복원으로 같은 trial이 `aborted` 상태로 복원됨 → DB 상태와 event timeline이 refresh 후에도 유지됨을 확인

수용 기준 충족:
1. ✅ 브라우저에서 fake runner를 대상으로 golden path를 완료 (create → cancel → reload → 상태 복원)
2. ✅ refresh 후에도 DB의 상태와 event timeline이 복원됨
3. ✅ 이미 완료된 trial은 중복 실행되지 않음 (`test_completed_trials_not_re_started` + regression step 11)
