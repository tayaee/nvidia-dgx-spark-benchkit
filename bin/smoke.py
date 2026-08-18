#!/usr/bin/env python3
"""benchkit smoke runner — N instances per benchmark, against a real
OpenAI-compatible inference endpoint.

Usage:
  smoke.py <benchmark> [--limit-new=N]
    benchmark ∈ {swebench-verified, swebench-pro, terminal-bench-2.1,
                 deepswe-1.1, frontiercode-1.1}

Flags:
  --limit-new=N    Process only the first N instances. Default (0)
                   means "process all instances available for this
                   benchmark". With --limit-new=1 you get exactly the
                   original one-shot smoke.

Source policy:
  - swebench-verified: live HF parquet (public dataset). If the live
    fetch fails (network/gated), falls back to a 1-instance fixture.
  - swebench-pro, terminal-bench-2.1, deepswe-1.1, frontiercode-1.1:
    only 1-instance fixtures ship in this repo (those datasets are
    gated). Asking for --limit-new > 1 prints a notice and runs the
    single fixture.

Output: one PASS/FAIL line per instance plus an overall summary —
short enough to read in a terminal, long enough to spot regressions.

A single instance PASSes when the model produced *something* the
runner can act on: a <patch> block, a fenced shell command, a
Search/Replace block, or an OpenAI-style tool_calls entry.
Correctness is the official benchmark's job; this is a smoke test of
the model + endpoint wiring.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any

ENDPOINT = os.environ.get("BENCKKIT_ENDPOINT", "http://spark1.local:30000/v1")
MODEL = os.environ.get("BENCKKIT_MODEL", "qwen3.8-27b")
TIMEOUT = float(os.environ.get("BENCKKIT_SMOKE_TIMEOUT", "120"))


# ---- benchmark fixtures (1 instance each) ------------------------------


FIXTURES: dict[str, dict[str, Any]] = {
    "swebench-verified": {
        "instance_id": "sympy__sympy-20916",
        "repo": "sympy/sympy",
        "problem_statement": (
            "pprint unicode does not format subscripts on Greek letters. "
            "Symbols like \\u03b9 should display their sub/superscripts "
            "in unicode output the same way Latin letters do."
        ),
        "response_format": "unified diff wrapped in <patch>...</patch>",
        "hf_dataset": "princeton-nlp/SWE-bench_Verified",
    },
    "swebench-pro": {
        "instance_id": "pro__placeholder-001",
        "repo": "example/repo",
        "problem_statement": (
            "Add a guard against negative input in the matrix_norm() "
            "function so it raises ValueError instead of returning NaN."
        ),
        "response_format": "unified diff wrapped in <patch>...</patch>",
        "hf_dataset": "scaleapi/SWE-bench_Pro",
        "hf_note": "gated — fixture is used in lieu of the live data",
    },
    "terminal-bench-2.1": {
        "instance_id": "tb21__hello-world",
        "task_name": "hello-world",
        "task_yaml": {
            "task_id": "hello-world",
            "instruction": (
                "Create /tmp/hello.txt that contains exactly the text "
                "'hello world' (one newline at end of file)."
            ),
            "verifier": "test_file /tmp/hello.txt contains 'hello world\\n'",
        },
        "response_format": "natural language describing the shell command(s) to run",
        "hf_dataset": "laude-institute/terminal-bench",
        "hf_note": "gated — fixture is used in lieu of the live data",
    },
    "deepswe-1.1": {
        "instance_id": "deepswe__placeholder-001",
        "repo": "example/repo",
        "problem_statement": (
            "Fix the off-by-one in the bucket sort partition: when the "
            "input contains negative values, items should land in the "
            "right bucket regardless of sign."
        ),
        "response_format": "unified diff wrapped in <patch>...</patch> (Agentless format)",
        "hf_dataset": "allenai/DeepSWE",
        "hf_note": "gated — fixture is used in lieu of the live data",
    },
    "frontiercode-1.1": {
        "instance_id": "fc11__placeholder-001",
        "repo": "example/production-code",
        "problem_statement": (
            "The billing webhook is double-charging on retry. Trace the "
            "idempotency key path through retry_queue.py and apply the "
            "minimal change so a duplicate POST with the same key is "
            "treated as a no-op."
        ),
        "response_format": "Search/Replace diff (SEARCH/REPLACE blocks) or unified diff",
        "hf_dataset": "anthropic/FrontierCode (or successor)",
        "hf_note": "gated — fixture is used in lieu of the live data",
    },
}


# ---- benchmark-specific prompt builders ---------------------------------


def _swebench_prompt(fix: dict) -> str:
    return (
        "You are an expert software engineer. Solve the following GitHub issue.\n\n"
        f"Repository: {fix['repo']}\n"
        f"Instance: {fix['instance_id']}\n\n"
        f"Issue:\n{fix['problem_statement']}\n\n"
        "Produce a unified diff that fixes the issue. Wrap the patch in "
        "<patch>...</patch> tags. Be minimal — change only what's necessary.\n\n"
        "<patch>\n--- a/path/to/file.py\n+++ b/path/to/file.py\n@@ ...\n"
        "</patch>\n"
    )


def _terminal_bench_prompt(fix: dict) -> str:
    t = fix["task_yaml"]
    return (
        "You are operating in a Linux shell. Complete this task:\n\n"
        f"Task: {t['instruction']}\n\n"
        "Verification: " + t["verifier"] + "\n\n"
        "Reply with the exact shell commands needed (one per line). "
        "Do not include commentary.\n"
    )


def _deepswe_prompt(fix: dict) -> str:
    return _swebench_prompt(fix) + (
        "\n# Agentless hint: locate files via grep first, then patch the "
        "smallest possible surface.\n"
    )


def _frontiercode_prompt(fix: dict) -> str:
    return (
        "You are working in a large production repo. Apply the minimal "
        "fix to the issue described below.\n\n"
        f"Issue:\n{fix['problem_statement']}\n\n"
        "Use Search/Replace blocks:\n\n"
        "```\nSEARCH:\n<replace me>\nREPLACE:\n<with this>\n```\n\n"
        "or, if you prefer, a unified diff wrapped in <patch>...</patch>.\n"
    )


BUILDERS = {
    "swebench-verified": _swebench_prompt,
    "swebench-pro": _swebench_prompt,
    "terminal-bench-2.1": _terminal_bench_prompt,
    "deepswe-1.1": _deepswe_prompt,
    "frontiercode-1.1": _frontiercode_prompt,
}


# ---- response parsing ---------------------------------------------------


_PATCH_RE = re.compile(r"<patch>(.*?)</patch>", re.DOTALL)
_DIFF_RE = re.compile(r"^---\s", re.MULTILINE)
_SEARCH_RE = re.compile(
    r"(?:^|\n)SEARCH:\s*\n(.*?)\n(?:^|\n)REPLACE:\s*\n(.*?)(?:\n```|$)",
    re.DOTALL,
)
_CODE_FENCE_RE = re.compile(r"```(?:bash|sh)?\n(.*?)```", re.DOTALL)
_TOOL_CALL_RE = re.compile(r"<\|?tool_call|<tool_call>|function_calls|tool_calls", re.IGNORECASE)


def _has_tool_call(text: str) -> bool:
    return bool(_TOOL_CALL_RE.search(text))


def _parse_swebench(text: str) -> dict:
    m = _PATCH_RE.search(text)
    if not m:
        return {
            "format": "missing-patch-tag",
            "patch_lines": 0,
            "looks_like_diff": bool(_DIFF_RE.search(text)),
            "tool_call_attempted": _has_tool_call(text),
        }
    patch = m.group(1).strip()
    return {
        "format": "patch-tag",
        "patch_lines": len(patch.splitlines()),
        "looks_like_diff": bool(_DIFF_RE.search(patch)),
        "first_5": patch.splitlines()[:5],
    }


def _parse_terminal_bench(text: str) -> dict:
    m = _CODE_FENCE_RE.search(text)
    if m:
        cmd = m.group(1).strip().splitlines()
        return {"format": "code-fence", "command_count": len(cmd), "first_command": cmd[0] if cmd else ""}
    # Treat any non-empty shell-like response as a command attempt.
    cmd = [ln for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith(("#", "//"))]
    return {
        "format": "raw",
        "command_count": len(cmd),
        "first_command": cmd[0] if cmd else "",
        "tool_call_attempted": _has_tool_call(text),
    }


def _parse_frontiercode(text: str) -> dict:
    sr = _SEARCH_RE.search(text)
    if sr:
        return {
            "format": "search-replace",
            "search_lines": len(sr.group(1).splitlines()),
            "replace_lines": len(sr.group(2).splitlines()),
        }
    base = _parse_swebench(text)
    base["fallback"] = "swebench-style"
    return base


PARSERS = {
    "swebench-verified": _parse_swebench,
    "swebench-pro": _parse_swebench,
    "terminal-bench-2.1": _parse_terminal_bench,
    "deepswe-1.1": _parse_swebench,
    "frontiercode-1.1": _parse_frontiercode,
}


# ---- live HF fetch (only for swebench-verified today) -------------------


def _try_fetch_swebench_verified(fix: dict, limit: int) -> list[dict] | None:
    """Best-effort fetch of N (or all) instances from the live HF
    parquet. Returns ``None`` on any failure so the caller falls back
    to fixtures. Uses ``fix['hf_dataset']`` to compute the URL.
    """
    dataset = fix.get("hf_dataset")
    if not dataset:
        return None
    try:
        url = (
            f"https://huggingface.co/datasets/{dataset}/"
            "resolve/main/data/test-00000-of-00001.parquet"
        )
        # limit=0 means "all"; otherwise head(limit).
        head_expr = f"head({limit})" if limit > 0 else ""
        proc = subprocess.run(
            [sys.executable, "-c", f"""
import pandas as pd, json, sys
df = pd.read_parquet('{url}')
df['ps_len'] = df['problem_statement'].astype(str).str.len()
df = df.sort_values('ps_len')
out = []
for i in df.{head_expr}.itertuples():
    out.append({{
        'instance_id': i.instance_id,
        'repo': i.repo,
        'base_commit': str(i.base_commit),
        'problem_statement': str(i.problem_statement),
    }})
sys.stdout.write(json.dumps(out))
"""],
            capture_output=True, text=True, timeout=60,
        )
        if proc.returncode != 0:
            sys.stderr.write(f"[live-fetch] rc={proc.returncode} stderr={proc.stderr!r}\n")
            return None
        out = proc.stdout.strip()
        if not out:
            sys.stderr.write(f"[live-fetch] empty stdout, stderr={proc.stderr!r}\n")
            return None
        result = json.loads(out)
        return result if isinstance(result, list) else [result]
    except Exception as e:
        sys.stderr.write(f"[live-fetch] exception: {e!r}\n")
        return None


# ---- HTTP call ----------------------------------------------------------


def _chat(prompt: str) -> dict:
    body = {
        "model": MODEL,
        "messages": [
            {"role": "system", "content": (
                "You are a concise engineer. Produce ONLY the requested "
                "artifact: a <patch>...</patch> block for code fixes, a "
                "fenced shell command for terminal tasks, or a "
                "SEARCH:/REPLACE: block for production edits. "
                "No preamble, no 'I will investigate', no commentary."
            )},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 8192,
        "temperature": 0.0,
        "stream": False,
        # SGLang/Qwen reasoning control: turn off the "thinking" trace
        # so the model commits to an answer instead of rambling.
        "chat_template_kwargs": {"enable_thinking": False},
    }
    req = urllib.request.Request(
        f"{ENDPOINT}/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        return json.loads(r.read())


def _model_text_and_tools(resp: dict) -> tuple[str, int]:
    """Return (visible text, tool_call_count) from the chat response."""
    msg = resp["choices"][0]["message"]
    text = (msg.get("content") or msg.get("reasoning_content") or "").strip()
    tools = msg.get("tool_calls") or []
    return text, len(tools)


# ---- per-instance runner ------------------------------------------------


def _run_one(benchmark: str, idx: int, total: int, fix: dict) -> bool:
    """Run one instance; return True on PASS. Print a per-instance
    block to stdout with a single trailing PASS/FAIL line."""
    prompt = BUILDERS[benchmark](fix)
    print(f"--- [{idx + 1}/{total}] {benchmark} / {fix['instance_id']} ---")
    print(f"source    : {fix.get('source', 'fixture')}")
    print(f"repo      : {fix.get('repo', '-')}")
    print(f"endpoint  : {ENDPOINT}")
    print(f"model     : {MODEL}")

    t0 = time.time()
    try:
        resp = _chat(prompt)
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        print(f"FAIL: inference call failed: {e}")
        return False
    latency = time.time() - t0

    text, tool_calls = _model_text_and_tools(resp)
    parsed = PARSERS[benchmark](text)
    parsed["tool_calls"] = tool_calls
    parsed["text_len"] = len(text)

    usage = resp.get("usage", {})
    print(f"latency   : {latency:.2f}s")
    print(f"tokens    : {usage.get('total_tokens')} "
          f"(prompt={usage.get('prompt_tokens')}, "
          f"completion={usage.get('completion_tokens')}, "
          f"reasoning={usage.get('completion_tokens_details', {}).get('reasoning_tokens', '?')})")
    print(f"parsed    : {json.dumps(parsed, ensure_ascii=False)}")
    print("--- response (first 800 chars) ---")
    print(text[:800] + ("…" if len(text) > 800 else ""))

    # PASS criterion: the model produced *something* the runner can act
    # on. Accept (a) a parsed artifact, (b) an OpenAI-style tool_calls
    # entry, or (c) a tool-call signature inside the visible text.
    extracted = False
    if tool_calls > 0:
        extracted = True
    elif parsed.get("tool_call_attempted"):
        extracted = True
    elif benchmark == "terminal-bench-2.1":
        extracted = parsed.get("command_count", 0) > 0
    elif benchmark == "frontiercode-1.1":
        extracted = parsed.get("format") in ("search-replace", "patch-tag")
    else:
        extracted = parsed.get("format") not in (None, "missing-patch-tag")

    ok = bool(text or tool_calls) and extracted and latency < TIMEOUT
    print()
    print("PASS" if ok else "FAIL")
    print()
    return ok


# ---- main ---------------------------------------------------------------


def smoke(benchmark: str, limit: int) -> int:
    if benchmark not in FIXTURES:
        print(f"unknown benchmark: {benchmark}", file=sys.stderr)
        return 2

    fixture = FIXTURES[benchmark]

    # Decide the instance list.
    instances: list[dict] = []
    source = "fixture"
    if benchmark == "swebench-verified":
        live = _try_fetch_swebench_verified(fixture, limit)
        if live:
            instances = live
            source = f"live:{fixture['hf_dataset']}"
        else:
            instances = [dict(fixture)]
            source = "fixture (live fetch failed)"
    else:
        instances = [dict(fixture)]
        if limit > 1:
            print(
                f"notice: {benchmark} has only 1 fixture instance; "
                f"--limit-new={limit} reduces to 1\n",
                file=sys.stderr,
            )

    # Apply limit (caller's --limit-new).
    if limit > 0:
        instances = instances[:limit]

    if not instances:
        print("no instances to run", file=sys.stderr)
        return 1

    print(f"benchmark : {benchmark}")
    print(f"source    : {source}")
    print(f"limit     : {limit if limit > 0 else 'all'} "
          f"(running {len(instances)} instance(s))")
    print(f"endpoint  : {ENDPOINT}")
    print(f"model     : {MODEL}")
    print()

    passed = 0
    failed = 0
    for idx, inst in enumerate(instances):
        inst.setdefault("source", source)
        if _run_one(benchmark, idx, len(instances), inst):
            passed += 1
        else:
            failed += 1

    print("=" * 67)
    print(f"SUMMARY  : {benchmark}  "
          f"PASS={passed}  FAIL={failed}  total={len(instances)}")
    print("=" * 67)
    return 0 if failed == 0 else 1


def main() -> int:
    ap = argparse.ArgumentParser(
        description="benchkit smoke runner (one or more instances per benchmark).",
    )
    ap.add_argument(
        "benchmark",
        choices=sorted(FIXTURES.keys()),
        help="Which benchmark to run.",
    )
    ap.add_argument(
        "--limit-new",
        type=int,
        default=0,
        metavar="N",
        help="Process only the first N instances. Default 0 means "
             "process all instances available for this benchmark. "
             "Use --limit-new=1 for the original one-shot smoke.",
    )
    args = ap.parse_args()
    return smoke(args.benchmark, args.limit_new)


if __name__ == "__main__":
    sys.exit(main())