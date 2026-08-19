#!/usr/bin/env bash
# Regression test for issue 5 — smoke now solves real problems, not
# "wiring-only" artifact detection. This test exercises the back-compat
# escape hatch (``--no-exec``) and proves the wiring-only mode still
# produces a well-formed JSON record + per-instance response.txt.
#
# Acceptance criteria (all must hold):
#
#   1. ``bin/swebench-verified-smoke.sh --limit-new=1 --no-exec`` exits 0.
#   2. ``results/<run-id>/swebench-verified/<instance>.json`` exists.
#   3. That JSON record has the expected schema (benchmark, instance_id,
#      verdict in {PASS, FAIL(no-...)}, source=live:*).
#   4. A sibling ``<instance>.response.txt`` is also written.
#   5. ``results/.solved/swebench-verified.solved`` is unchanged
#      (--no-exec must not mark instance solved; future runs should
#      retry).
#
# Run manually::
#
#     ./regression-tests/verify-issue-5.sh
#
# Exits 0 on success, non-zero on any check failure.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PY="${REPO_ROOT}/.venv/bin/python"
[[ -x "$PY" ]] || PY="$(command -v python3)"
[[ -x "$PY" ]] || PY="$(command -v python)"

cd "$REPO_ROOT"

# Use a unique run id so we don't collide with prior runs and so we can
# find the results directory without scanning.
RUN_ID="regress-issue-5-$(date -u +%Y%m%dT%H%M%S)-$$"
export BENCKKIT_RUN_ID="$RUN_ID"

# Use a results dir we can clean up on exit.
RESULTS_DIR="${TMPDIR:-/tmp}/benchkit-issue-5-$$/results"
mkdir -p "$RESULTS_DIR"
trap 'rm -rf "$(dirname "$RESULTS_DIR")"' EXIT

# Snapshot the solved file BEFORE the run so we can prove --no-exec
# doesn't add to it (per the new contract).
SOLVED="${REPO_ROOT}/results/.solved/swebench-verified.solved"
SOLVED_BEFORE="$(cat "$SOLVED" 2>/dev/null || true)"

echo "==> running bin/swebench-verified-smoke.sh --limit-new=1 --no-exec (run_id=$RUN_ID)"
BENCKKIT_RESULTS="$RESULTS_DIR" \
  bin/swebench-verified-smoke.sh --limit-new=1 --no-exec --no-pull --run-id "$RUN_ID"

# 1. Exit code already checked (set -e). Now verify artifacts.
BENCH_DIR="$RESULTS_DIR/$RUN_ID/swebench-verified"
if [[ ! -d "$BENCH_DIR" ]]; then
  echo "FAIL: expected results dir $BENCH_DIR to exist"
  ls -la "$RESULTS_DIR/$RUN_ID" 2>/dev/null || true
  exit 1
fi

# Find the one instance file we wrote.
JSON_FILE="$(find "$BENCH_DIR" -maxdepth 1 -name '*.json' -print -quit)"
if [[ -z "$JSON_FILE" ]]; then
  echo "FAIL: no JSON record written under $BENCH_DIR"
  ls -la "$BENCH_DIR"
  exit 1
fi
INSTANCE_ID="$(basename "$JSON_FILE" .json)"
echo "==> instance_id=$INSTANCE_ID"

# 3. JSON schema check.
#
# In --no-exec mode the verdict can be either:
#   * "PASS"  — when the model emitted a parseable artifact (legacy
#               artifact-detection behaviour, kept for back-compat)
#   * "FAIL(no-...)" — when the model produced nothing usable
#
# Both are acceptable here. What matters is that no docker exec actually
# ran (the exec field should be absent OR marked as wiring-only).
"$PY" - "$JSON_FILE" <<'PY'
import json, sys
path = sys.argv[1]
with open(path) as f:
    rec = json.load(f)

required_keys = {"instance_id", "benchmark", "source", "ts", "parsed", "verdict"}
missing = required_keys - rec.keys()
assert not missing, f"missing keys {missing} in {path}"

assert rec["benchmark"] == "swebench-verified", f"bad benchmark: {rec['benchmark']}"
assert rec["instance_id"], "empty instance_id"
assert rec["source"].startswith("live:"), f"bad source: {rec['source']}"
verdict = rec["verdict"]
assert verdict == "PASS" or verdict.startswith("FAIL(no-"), (
    f"unexpected verdict in --no-exec mode: {verdict!r} "
    "(expected 'PASS' or 'FAIL(no-...)')"
)
parsed = rec["parsed"]
assert parsed.get("format") in {"patch-tag", "missing-patch-tag"}, (
    f"unexpected parsed.format: {parsed.get('format')!r}"
)
# The exec stage should reflect that docker was bypassed. Either the
# ``exec`` field is absent (model produced nothing to verify) or it
# carries a no-op marker like ``--no-exec`` / ``ran: False``.
exec_field = rec.get("exec")
if exec_field is not None:
    ran = exec_field.get("ran", True)
    if ran:
        # If exec ran, --no-exec mode would have produced ran=False;
        # surface anything suspicious.
        assert "skip" in str(exec_field).lower() or "no-exec" in str(exec_field).lower(), (
            f"unexpected exec stage in --no-exec mode: {exec_field!r}"
        )
print(f"==> ok: schema valid (verdict={verdict!r}, parsed.format={parsed['format']!r})")
PY

# 4. response.txt sibling.
RESP_FILE="$BENCH_DIR/$INSTANCE_ID.response.txt"
if [[ ! -s "$RESP_FILE" ]]; then
  echo "FAIL: expected non-empty $RESP_FILE"
  ls -la "$BENCH_DIR"
  exit 1
fi
echo "==> ok: response.txt present ($(wc -c <"$RESP_FILE") bytes)"

# 5. Solved file must not grow during a --no-exec run. The new contract
# is: --no-exec never marks instances as solved (so the next --limit-new
# / --exec run will retry them).
SOLVED_AFTER="$(cat "$SOLVED" 2>/dev/null || true)"
if [[ "$SOLVED_BEFORE" != "$SOLVED_AFTER" ]]; then
  echo "FAIL: solved file changed during --no-exec run"
  diff <(echo "$SOLVED_BEFORE") <(echo "$SOLVED_AFTER") || true
  exit 1
fi
echo "==> ok: solved file unchanged by --no-exec run (back-compat contract)"

echo
echo "ALL CHECKS PASSED (issue-5: --no-exec mode still produces JSON + response.txt)"
