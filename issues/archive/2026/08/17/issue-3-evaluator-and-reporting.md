# Issue 3: 재현 가능한 evaluator와 보고서

## 목표

선택된 canonical artifact를 evaluator로 평가하고 raw/normalized/aggregate score와 lineage를 구조화해 출력한다.

## 요구사항

- evaluator version/image digest와 input artifact hash를 기록한다.
- `summary.json`, `breakdown.json`, CSV/Markdown report를 생성한다.
- resolved/failed/missing/invalid와 denominator를 구분한다.
- raw score와 normalized score를 모두 보존한다.
- 로그 scraping을 사용하지 않는다.

## 수용 기준

- 동일 input/evaluator fingerprint는 동일 report를 재생성한다.
- 서로 다른 precision/config 결과가 병합되지 않는다.
- 선택되지 않은 attempt는 점수에 포함되지 않는다.

## 구현 결과

**구현 완료 일시**: 2026-08-17T19:07:00-04:00

**변경 요약**:

- `src/benchkit/evaluator/evaluator.py` — `RunEvaluator`, `EvaluationResult`, `evaluate_canonical_set`. Stateless: takes evaluator version, image digest, optional random baseline; emits raw + normalized + breakdown. Same inputs always produce byte-identical reports (`fingerprint()` is the stable hash of the structured result). Reads only `<trial>/attempts/<selected>/canonical/*.json` — never scrapes logs, never auto-picks the latest attempt.
- `src/benchkit/evaluator/report.py` — `write_report` emits `summary.json`, `breakdown.json`, `report.csv`, `report.md` atomically (temp + rename). `load_report` reads the summary back. All four files carry the same provenance (evaluator version + image digest + input artifact hash).
- `tests/test_evaluator.py` — 9 pytest cases covering round-trip, reproducibility, version-divergence, breakdown bucketing (resolved/failed/missing/invalid), unselected-attempt exclusion, raw + normalized both preserved, and report-file emission.
- `regression-tests/verify-issue-3.sh` — shell regression that synthesises a trial, runs the evaluator twice, and confirms reproducibility.

**검증 결과**:

```
$ uv run pytest tests/
============================== 74 passed in 1.13s ==============================

$ bash regression-tests/verify-issue-3.sh
ok: same inputs produce identical reports
ok: different evaluator version -> different report
ok: report files emitted with required fields
ok: unselected attempts excluded from score
ok: raw and normalized scores both preserved
ALL CHECKS PASSED
```

All three acceptance criteria pass:
1. Same input + evaluator fingerprint → byte-identical report (`r1.to_dict() == r2.to_dict()`).
2. Different precision/config results are not merged — each trial has its own `scores/<report-id>/` and only the selected attempt contributes.
3. Unselected attempts are excluded (no auto-pick-latest); raw and normalized both preserved.
