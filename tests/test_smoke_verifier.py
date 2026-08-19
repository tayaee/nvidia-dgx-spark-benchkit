"""Tests for bin/smoke.py verifier + exec helpers — no docker required.

Covers:
- _coerce_list (the JSON-encoded-list problem from swebench-pro)
- _parse_swebench / _parse_terminal_bench (response parsers)
- _verdict_from_exec (the gate for PASS / FAIL(k/n))
- _is_pass (final ok helper)
- _parse_pytest_pass_set / _parse_pytest_fail_set
- _extract_artifact (artifact routing per benchmark)
- _tail (log truncation)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Make bin/ importable.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "bin"))
sys.path.insert(0, str(ROOT / "src"))

import smoke  # noqa: E402


class TestCoerceList:
    def test_none(self):
        assert smoke._coerce_list(None) == []

    def test_native_list(self):
        assert smoke._coerce_list(["a", "b"]) == ["a", "b"]

    def test_json_string(self):
        assert smoke._coerce_list('["a", "b"]') == ["a", "b"]

    def test_empty_string(self):
        assert smoke._coerce_list("") == []

    def test_comma_separated(self):
        # fallback when JSON parsing fails
        assert smoke._coerce_list("a, b, c") == ["a", "b", "c"]

    def test_other_types(self):
        assert smoke._coerce_list(42) == ["42"]


class TestParseSwebench:
    def test_patch_tag_extracted(self):
        text = "Some preamble\n<patch>\n--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-foo\n+bar\n</patch>\n"
        out = smoke._parse_swebench(text)
        assert out["format"] == "patch-tag"
        assert out["patch_lines"] >= 4
        assert out["looks_like_diff"] is True
        assert out["patch_body"].startswith("--- a/x.py")

    def test_missing_patch_tag(self):
        out = smoke._parse_swebench("I don't know how to fix this.")
        assert out["format"] == "missing-patch-tag"
        assert out["patch_lines"] == 0
        assert out["patch_body"] == ""

    def test_tool_call_signature(self):
        out = smoke._parse_swebench("let me try a tool_call now")
        assert out["tool_call_attempted"] is True


class TestParseTerminalBench:
    def test_fenced_block(self):
        text = "Here's what to do:\n```bash\necho hello > /tmp/x.txt\ncat /tmp/x.txt\n```\nDone."
        out = smoke._parse_terminal_bench(text)
        assert out["format"] == "code-fence"
        assert out["command_count"] == 2
        assert "echo" in out["first_command"]
        assert "echo hello" in out["commands"][0]

    def test_raw_lines_fallback(self):
        text = "# my header\nmkdir -p /tmp/work\nls -la\n"
        out = smoke._parse_terminal_bench(text)
        assert out["format"] == "raw"
        assert out["command_count"] == 2  # comments stripped

    def test_empty_response(self):
        out = smoke._parse_terminal_bench("")
        assert out["command_count"] == 0
        assert out["commands"] == []


class TestVerdictFromExec:
    def test_pass(self):
        er = {"ran": True, "apply_exit": 0, "verifier_exit": 0,
              "fail_to_pass_passed": 7, "fail_to_pass_total": 7}
        assert smoke._verdict_from_exec(er) == "PASS(7/7)"

    def test_fail_partial(self):
        er = {"ran": True, "apply_exit": 0, "verifier_exit": 1,
              "fail_to_pass_passed": 3, "fail_to_pass_total": 7}
        assert smoke._verdict_from_exec(er) == "FAIL(3/7)"

    def test_fail_apply(self):
        er = {"ran": True, "apply_exit": 1, "verifier_exit": 0,
              "fail_to_pass_passed": 0, "fail_to_pass_total": 7}
        assert smoke._verdict_from_exec(er) == "FAIL(apply-failed)"

    def test_fail_no_exec_with_reason(self):
        er = {"ran": False, "reason": "no-image"}
        assert smoke._verdict_from_exec(er) == "FAIL(no-no-image)"

    def test_fail_no_exec_default_reason(self):
        er = {"ran": False}
        assert smoke._verdict_from_exec(er) == "FAIL(no-unknown)"


class TestIsPass:
    def test_passes_when_verdict_passes(self):
        er = {"ran": True, "apply_exit": 0, "verifier_exit": 0,
              "fail_to_pass_passed": 5, "fail_to_pass_total": 5}
        verdict = smoke._verdict_from_exec(er)
        assert smoke._is_pass(verdict, er) is True

    def test_fails_when_partial(self):
        er = {"ran": True, "apply_exit": 0, "verifier_exit": 1,
              "fail_to_pass_passed": 3, "fail_to_pass_total": 5}
        verdict = smoke._verdict_from_exec(er)
        assert smoke._is_pass(verdict, er) is False

    def test_fails_when_no_exec(self):
        er = {"ran": False, "reason": "no-exec"}
        verdict = smoke._verdict_from_exec(er)
        assert smoke._is_pass(verdict, er) is False


class TestParsePytest:
    def test_passed_set(self):
        stdout = """
test_a.py::test_one PASSED
test_a.py::test_two PASSED
test_b.py::test_three FAILED
"""
        passed = smoke._parse_pytest_pass_set(stdout)
        assert passed == {"test_a.py::test_one", "test_a.py::test_two"}

    def test_failed_set(self):
        stdout = """
test_a.py::test_one PASSED
test_b.py::test_three FAILED
test_c.py::test_x FAILED
"""
        failed = smoke._parse_pytest_fail_set(stdout)
        assert failed == {"test_b.py::test_three", "test_c.py::test_x"}

    def test_empty(self):
        assert smoke._parse_pytest_pass_set("") == set()
        assert smoke._parse_pytest_fail_set("") == set()


class TestExtractArtifact:
    def test_swebench_returns_patch_body(self):
        parsed = {"format": "patch-tag", "patch_body": "diff --git a/x b/x\n"}
        patch, commands = smoke._extract_artifact("swebench-verified", parsed)
        assert patch.startswith("diff --git")
        assert commands == []

    def test_terminal_bench_returns_commands(self):
        parsed = {"format": "code-fence", "commands": ["echo hi", "ls"]}
        patch, commands = smoke._extract_artifact("terminal-bench-2.0", parsed)
        assert patch == ""
        assert commands == ["echo hi", "ls"]

    def test_missing_patch_tag_yields_empty_patch(self):
        parsed = {"format": "missing-patch-tag", "patch_body": ""}
        patch, commands = smoke._extract_artifact("deepswe-1.1", parsed)
        assert patch == ""
        assert commands == []


class TestTail:
    def test_short_text_unchanged(self):
        assert smoke._tail("hello\nworld", 10) == "hello\nworld"

    def test_long_text_truncated(self):
        text = "\n".join(f"line {i}" for i in range(100))
        out = smoke._tail(text, 5)
        assert out == "\n".join(f"line {i}" for i in range(95, 100))

    def test_empty(self):
        assert smoke._tail("", 10) == ""


class TestPytestRegexes:
    @pytest.mark.parametrize("text,expected_passed,expected_failed", [
        ("test_x.py::test_one PASSED",
         {"test_x.py::test_one"}, set()),
        ("test_z.py::test_three FAILED",
         set(), {"test_z.py::test_three"}),
        # leading newline should NOT be captured as part of the node id
        ("\ntest_a.py::test_one PASSED",
         {"test_a.py::test_one"}, set()),
        # mixed on multiple lines
        ("\ntest_a.py::test_one PASSED\ntest_b.py::test_two FAILED\n",
         {"test_a.py::test_one"}, {"test_b.py::test_two"}),
    ])
    def test_pytest_pass_and_fail(self, text, expected_passed, expected_failed):
        assert smoke._parse_pytest_pass_set(text) == expected_passed
        assert smoke._parse_pytest_fail_set(text) == expected_failed