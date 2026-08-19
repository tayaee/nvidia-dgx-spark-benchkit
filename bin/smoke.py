#!/usr/bin/env python3
"""benchkit smoke runner — N instances per benchmark, with real docker-based
verification of the model's artifact.

Usage:
  smoke.py <benchmark> [--limit-new=N] [options]

  benchmark ∈ {swebench-verified, swebench-pro, terminal-bench-2.0,
               deepswe-1.1}

Source policy:
  - swebench-verified, swebench-pro, terminal-bench-2.0, deepswe-1.1:
    live HF fetch (parquet for swebench-* and deep-swe, dir-list for
    terminal-bench). If the live fetch fails, falls back to the
    1-instance fixture shipped in this repo.

PASS criterion (real-execution mode, the default):
  1. The model produced an artifact (patch / shell commands / tool call).
  2. Docker ran the benchmark's official verifier against the artifact
     inside the dataset's official docker image.
  3. The verifier returned exit 0 AND every FAIL_TO_PASS / fail_to_pass
     pytest ID now PASSes with no PASS_TO_PASS regression.

Use --no-exec to fall back to the legacy "wiring-only" PASS criterion
(artifact presence alone) — for fast endpoint reachability checks.

Output: per-instance banner + overall summary. Each instance also
writes a JSON record to results/<run-id>/<benchmark>/<instance_id>.json
via benchkit.artifact.atomic_write_text, plus full verifier stdout at
<instance_id>.exec.log.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Reuse the project's atomic-write helper so crash-safe JSON records are
# guaranteed the same way the rest of benchkit writes its artifacts.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from benchkit.artifact import atomic_write_text  # noqa: E402

ENDPOINT = os.environ.get("BENCKKIT_ENDPOINT", "http://spark1.local:30000/v1")
MODEL = os.environ.get("BENCKKIT_MODEL", "qwen3.8-27b")
TIMEOUT = float(os.environ.get("BENCKKIT_SMOKE_TIMEOUT", "120"))
# Network mode for sandbox containers. Set DOCKER_NETWORK=none to disable
# internet from inside containers (SWE-bench defaults). Terminal-bench and
# deepswe need internet (apt-get in test.sh) so default is bridge.
DOCKER_NETWORK = os.environ.get("BENCKKIT_DOCKER_NETWORK", "bridge")

# Per-benchmark default docker timeout. 0 = benchmark picks its own.
DOCKER_TIMEOUTS: dict[str, int] = {
    "swebench-verified": 120,
    "swebench-pro": 180,
    "terminal-bench-2.0": 120,
    "deepswe-1.1": 180,
}

# Default root for all results: solved tracker + per-run JSON records.
RESULTS_ROOT = Path(os.environ.get("BENCKKIT_RESULTS", "results"))


# ---- benchmark fixtures (1 instance each, used as fallback) -------------


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
        "hf_dataset": "SWE-bench/SWE-bench_Verified",
        "hf_path": "data/test-00000-of-00001.parquet",
        "hf_format": "parquet",
    },
    "swebench-pro": {
        "instance_id": "pro__placeholder-001",
        "repo": "example/repo",
        "problem_statement": (
            "Add a guard against negative input in the matrix_norm() "
            "function so it raises ValueError instead of returning NaN."
        ),
        "response_format": "unified diff wrapped in <patch>...</patch>",
        "hf_dataset": "ScaleAI/SWE-bench_Pro",
        "hf_path": "data/test-00000-of-00001.parquet",
        "hf_format": "parquet",
    },
    "terminal-bench-2.0": {
        "instance_id": "tb20__hello-world",
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
        "hf_dataset": "harborframework/terminal-bench-2.0",
        "hf_format": "terminal-bench",
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
        "hf_dataset": "datacurve/deep-swe",
        "hf_path": "data/test-00000-of-00001.parquet",
        "hf_format": "deep-swe-parquet",
        "hf_note": "requires HF_TOKEN",
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
    instruction = t.get("instruction") or fix.get("problem_statement", "")
    # terminal-bench 2.0 ships tests in tests/ instead of a `verifier`
    # string, so fall back to a generic note when the field is missing.
    verifier = t.get("verifier") or "see task's tests/ directory; harness verifies automatically"
    return (
        "You are operating in a Linux shell. Complete this task:\n\n"
        f"Task: {instruction}\n\n"
        f"Verification: {verifier}\n\n"
        "Reply with the exact shell commands needed (one per line). "
        "Do not include commentary.\n"
    )


def _deepswe_prompt(fix: dict) -> str:
    return _swebench_prompt(fix) + (
        "\n# Agentless hint: locate files via grep first, then patch the "
        "smallest possible surface.\n"
    )


BUILDERS = {
    "swebench-verified": _swebench_prompt,
    "swebench-pro": _swebench_prompt,
    "terminal-bench-2.0": _terminal_bench_prompt,
    "deepswe-1.1": _deepswe_prompt,
}


# ---- response parsing ---------------------------------------------------


_PATCH_RE = re.compile(r"<patch>(.*?)</patch>", re.DOTALL)
_DIFF_RE = re.compile(r"^---\s", re.MULTILINE)
_CODE_FENCE_RE = re.compile(r"```(?:bash|sh)?\n(.*?)```", re.DOTALL)
_TOOL_CALL_RE = re.compile(r"<\|?tool_call|<tool_call>|function_calls|\btool_calls?\b", re.IGNORECASE)
_PYTEST_NODE_RE = re.compile(r"^(\S+(?:::[^\s]+)?)\s+(PASSED|FAILED)", re.MULTILINE)


def _has_tool_call(text: str) -> bool:
    return bool(_TOOL_CALL_RE.search(text))


def _parse_swebench(text: str) -> dict:
    m = _PATCH_RE.search(text)
    if m:
        patch = m.group(1).strip()
        return {
            "format": "patch-tag",
            "patch_lines": len(patch.splitlines()),
            "looks_like_diff": bool(_DIFF_RE.search(patch)),
            "first_5": patch.splitlines()[:5],
            "patch_body": patch,
        }
    # Tolerate an unclosed <patch> tag — take everything after `<patch>`
    # as the patch body. Models occasionally truncate mid-output.
    open_idx = text.find("<patch>")
    if open_idx != -1 and _DIFF_RE.search(text):
        patch = text[open_idx + len("<patch>"):].strip()
        return {
            "format": "patch-tag-unclosed",
            "patch_lines": len(patch.splitlines()),
            "looks_like_diff": True,
            "first_5": patch.splitlines()[:5],
            "patch_body": patch,
        }
    return {
        "format": "missing-patch-tag",
        "patch_lines": 0,
        "looks_like_diff": bool(_DIFF_RE.search(text)),
        "tool_call_attempted": _has_tool_call(text),
        "patch_body": "",
    }


def _parse_terminal_bench(text: str) -> dict:
    m = _CODE_FENCE_RE.search(text)
    commands: list[str] = []
    fmt = "raw"
    if m:
        commands = [ln for ln in m.group(1).strip().splitlines() if ln.strip()]
        fmt = "code-fence"
    else:
        # Treat any non-empty shell-like response as a command attempt.
        commands = [
            ln for ln in text.splitlines()
            if ln.strip() and not ln.lstrip().startswith(("#", "//"))
        ]
    return {
        "format": fmt,
        "command_count": len(commands),
        "first_command": commands[0] if commands else "",
        "commands": commands,
        "tool_call_attempted": _has_tool_call(text),
    }


PARSERS = {
    "swebench-verified": _parse_swebench,
    "swebench-pro": _parse_swebench,
    "terminal-bench-2.0": _parse_terminal_bench,
    "deepswe-1.1": _parse_swebench,
}


# ---- live HF fetch ------------------------------------------------------


def _hf_token() -> str | None:
    return os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN")


def _hf_get_bytes(url: str) -> bytes:
    """Fetch ``url``; inject Bearer ``HF_TOKEN`` if present."""
    req = urllib.request.Request(url)
    tok = _hf_token()
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def _hf_get_json(url: str) -> Any:
    return json.loads(_hf_get_bytes(url).decode("utf-8"))


def _hf_list_dir(repo_id: str, path: str = "") -> Any:
    """List entries of an HF dataset path (returns ``[{path,type}, ...]``)."""
    url = f"https://huggingface.co/api/datasets/{repo_id}/tree/main/{path}".rstrip("/")
    data = _hf_get_json(url)
    if isinstance(data, dict) and "error" in data:
        raise RuntimeError(f"HF list failed: {data['error']}")
    return data


def _sort_and_head(items: list[dict], limit: int, key=lambda x: len(str(x.get("problem_statement", "")))) -> list[dict]:
    """Sort by problem-statement length (shortest first) and head(limit)."""
    items = sorted(items, key=key)
    return items if limit <= 0 else items[:limit]


def _coerce_list(value: Any) -> list[str]:
    """SWE-bench-Pro stores lists as JSON-encoded strings; coerce to list[str]."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v) for v in value]
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return []
        # Try JSON first, then a comma-split fallback.
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return [str(v) for v in parsed]
        except (ValueError, TypeError):
            pass
        return [x.strip() for x in s.strip("[]").split(",") if x.strip()]
    return [str(value)]


def _try_fetch_parquet(repo_id: str, hf_path: str, limit: int, *,
                       extra_cols: tuple[str, ...] = ()) -> list[dict] | None:
    """Read a parquet file into a list of instance dicts.

    Captures the standard smoke fields plus any extras the caller asks
    for (e.g. ``image``, ``eval_script``, ``FAIL_TO_PASS`` for
    swebench-verified). Reads bytes through ``_hf_get_bytes`` (so
    HF_TOKEN is honored) and feeds pandas via BytesIO to avoid pandas's
    urlopen path that ignores tokens.
    """
    import io
    try:
        import pandas as pd  # noqa: WPS433 — local import is intentional
    except ImportError:
        sys.stderr.write("[live-fetch] pandas not available; using fixture\n")
        return None
    url = f"https://huggingface.co/datasets/{repo_id}/resolve/main/{hf_path}"
    try:
        raw = _hf_get_bytes(url)
        df = pd.read_parquet(io.BytesIO(raw))
    except Exception as e:
        sys.stderr.write(f"[live-fetch] parquet read failed: {e!r}\n")
        return None
    instances: list[dict] = []
    for row in df.to_dict(orient="records"):
        iid = row.get("instance_id") or row.get("id")
        if not iid:
            continue
        ps = row.get("problem_statement")
        rec: dict[str, Any] = {
            "instance_id": str(iid),
            "repo": str(row.get("repo", "")),
            "base_commit": str(row.get("base_commit", "")),
            "problem_statement": str(ps) if ps is not None else "",
        }
        for col in extra_cols:
            if col in row:
                rec[col] = row[col]
        instances.append(rec)
    return _sort_and_head(instances, limit) or None


def _try_fetch_swebench_verified(repo_id: str, hf_path: str, limit: int) -> list[dict] | None:
    """swebench-verified needs the verifier-relevant fields for exec."""
    return _try_fetch_parquet(repo_id, hf_path, limit, extra_cols=(
        "image", "eval_script", "test_patch", "FAIL_TO_PASS", "PASS_TO_PASS",
        "environment_setup_commit", "version",
    ))


def _try_fetch_swebench_pro(repo_id: str, hf_path: str, limit: int) -> list[dict] | None:
    """swebench-pro stores lists as JSON strings; coerce after fetch."""
    raw = _try_fetch_parquet(repo_id, hf_path, limit, extra_cols=(
        "dockerhub_tag", "test_patch", "before_repo_set_cmd",
        "selected_test_files_to_run", "fail_to_pass", "pass_to_pass",
        "repo_language",
    ))
    if not raw:
        return None
    for r in raw:
        r["fail_to_pass"] = _coerce_list(r.get("fail_to_pass"))
        r["pass_to_pass"] = _coerce_list(r.get("pass_to_pass"))
        r["selected_test_files_to_run"] = _coerce_list(r.get("selected_test_files_to_run"))
    return raw


def _try_fetch_terminal_bench(repo_id: str, limit: int) -> list[dict] | None:
    """terminal-bench-2.0: each top-level dir is a task with instruction.md + task.toml + tests/.

    We pull instruction.md (for the prompt), task.toml (for the
    ``docker_image``), and tests/test.sh (for the verifier) so the exec
    stage can run the actual harness verifier against the model's
    commands inside the docker image.
    """
    try:
        entries = _hf_list_dir(repo_id)
    except Exception as e:
        sys.stderr.write(f"[live-fetch] list failed: {e!r}\n")
        return None
    tasks = [e["path"] for e in entries if e.get("type") == "directory"]
    if not tasks:
        return None

    instances: list[dict] = []
    for slug in tasks:
        try:
            inst_bytes = _hf_get_bytes(
                f"https://huggingface.co/datasets/{repo_id}/resolve/main/{slug}/instruction.md"
            )
        except Exception as e:
            sys.stderr.write(f"[live-fetch] {slug} skipped: {e!r}\n")
            continue
        instruction = inst_bytes.decode("utf-8", errors="replace").strip()

        docker_image: str | None = None
        try:
            toml_bytes = _hf_get_bytes(
                f"https://huggingface.co/datasets/{repo_id}/resolve/main/{slug}/task.toml"
            )
            toml_text = toml_bytes.decode("utf-8", errors="replace")
            m = re.search(r'^\s*docker_image\s*=\s*"([^"]+)"', toml_text, re.MULTILINE)
            if m:
                docker_image = m.group(1)
        except Exception:
            docker_image = None

        # Pull tests/test.sh AND tests/test_outputs.py so we can run the
        # full verifier inside the container — the per-task docker image
        # does NOT bake these in. test.sh typically calls pytest on
        # /tests/test_outputs.py, so both are required.
        verifier_script: str | None = None
        verifier_py: str | None = None
        try:
            sh_bytes = _hf_get_bytes(
                f"https://huggingface.co/datasets/{repo_id}/resolve/main/{slug}/tests/test.sh"
            )
            verifier_script = sh_bytes.decode("utf-8", errors="replace")
        except Exception:
            verifier_script = None
        try:
            py_bytes = _hf_get_bytes(
                f"https://huggingface.co/datasets/{repo_id}/resolve/main/{slug}/tests/test_outputs.py"
            )
            verifier_py = py_bytes.decode("utf-8", errors="replace")
        except Exception:
            verifier_py = None

        instances.append({
            "instance_id": f"tb20__{slug}",
            "task_name": slug,
            "task_yaml": {
                "task_id": slug,
                "instruction": instruction[:2000],
                "verifier": "see task's tests/ directory; harness verifies automatically",
                "docker_image": docker_image,
            },
            "verifier_script": verifier_script,
            "verifier_py": verifier_py,
            "problem_statement": instruction,
        })
    return _sort_and_head(instances, limit,
                          key=lambda x: len(str(x.get("problem_statement", "")))) or None


def _try_fetch_deep_swe_parquet(repo_id: str, hf_path: str, limit: int) -> list[dict] | None:
    """datacurve/deep-swe: parquet has docker_image + verifier_script + test_patch."""
    return _try_fetch_parquet(repo_id, hf_path, limit, extra_cols=(
        "task_id", "name", "docker_image", "test_patch",
        "verifier_script", "reference_patch", "task_toml",
        "language", "category",
    ))


def _try_fetch_live(benchmark: str, fix: dict, limit: int) -> list[dict] | None:
    """Dispatch to the right fetcher based on ``fix['hf_format']``.

    Always fetches the full sorted pool (``limit=0`` here means "all") so
    that ``smoke()`` can apply its solved-filter + head(N) post-processing
    and end up with exactly N new instances. The caller's ``limit`` is
    intentionally ignored by the underlying fetcher — this prevents the
    "running 3 of 5 requested" under-delivery when several of the
    shortest-N instances are already in the solved set.
    """
    fmt = fix.get("hf_format")
    repo = fix.get("hf_dataset")
    if not fmt or not repo:
        return None
    try:
        if fmt == "parquet" and benchmark == "swebench-verified":
            return _try_fetch_swebench_verified(repo, fix.get("hf_path", ""), 0)
        if fmt == "parquet" and benchmark == "swebench-pro":
            return _try_fetch_swebench_pro(repo, fix.get("hf_path", ""), 0)
        if fmt == "parquet":
            return _try_fetch_parquet(repo, fix.get("hf_path", ""), 0)
        if fmt == "terminal-bench":
            # Oversample by solving-count + extra headroom rather than
            # reading all 89 instruction.md files when only a handful
            # are needed. The +12 headroom covers typical solved-set
            # sizes so post-filter we still have ``limit`` items.
            sample = max(limit + 12, 25) if limit > 0 else 0
            return _try_fetch_terminal_bench(repo, sample)
        if fmt == "deep-swe-parquet":
            return _try_fetch_deep_swe_parquet(repo, fix.get("hf_path", ""), 0)
    except Exception as e:
        sys.stderr.write(f"[live-fetch] {benchmark} failed: {e!r}\n")
        return None
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


# ---- docker sandbox helpers ---------------------------------------------


def _now_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _default_run_id() -> str:
    return os.environ.get("BENCKKIT_RUN_ID") or f"smoke-{_now_utc()}"


def _results_dir(run_id: str, benchmark: str) -> Path:
    p = RESULTS_ROOT / run_id / benchmark
    p.mkdir(parents=True, exist_ok=True)
    return p


def _docker_available() -> bool:
    """Cheap probe for the docker daemon. Returns True iff `docker version` works."""
    try:
        out = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True, text=True, timeout=10,
        )
        return out.returncode == 0 and bool(out.stdout.strip())
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as e:
        sys.stderr.write(f"[sandbox] docker unavailable: {e!r}\n")
        return False


def _docker_image_present(image: str) -> bool:
    try:
        out = subprocess.run(
            ["docker", "image", "inspect", image],
            capture_output=True, text=True, timeout=15,
        )
        return out.returncode == 0
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return False


def _docker_pull(image: str, *, allow: bool) -> bool:
    """Pull ``image`` iff it isn't cached locally and ``allow`` is True."""
    if not allow:
        return _docker_image_present(image)
    if _docker_image_present(image):
        return True
    sys.stderr.write(f"[sandbox] docker pull {image}\n")
    try:
        out = subprocess.run(
            ["docker", "pull", image],
            capture_output=True, text=True, timeout=600,
        )
        if out.returncode != 0:
            sys.stderr.write(f"[sandbox] pull failed: {out.stderr.strip()[:300]}\n")
            return False
        return True
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as e:
        sys.stderr.write(f"[sandbox] pull error: {e!r}\n")
        return False


def _docker_run_persisted(image: str, name: str, *, allow_pull: bool, timeout: int = 60) -> str | None:
    """Start ``image`` as a long-running named container; return container id, or None on failure."""
    # Always pre-remove a stale container so post-mortem state can't leak in.
    subprocess.run(
        ["docker", "rm", "-f", name],
        capture_output=True, text=True, timeout=15,
    )
    if not _docker_pull(image, allow=allow_pull):
        return None
    try:
        out = subprocess.run(
            ["docker", "run", "-d", "--name", name, "--network", DOCKER_NETWORK,
             image, "sleep", "infinity"],
            capture_output=True, text=True, timeout=timeout,
        )
        if out.returncode != 0:
            sys.stderr.write(f"[sandbox] docker run failed: {out.stderr.strip()[:300]}\n")
            return None
        return out.stdout.strip()
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as e:
        sys.stderr.write(f"[sandbox] docker run error: {e!r}\n")
        return None


def _docker_exec(name: str, cmd: str, *, timeout: int) -> dict:
    """Run ``cmd`` (a single shell invocation) inside container ``name``."""
    t0 = time.time()
    try:
        out = subprocess.run(
            ["docker", "exec", name, "bash", "-lc", cmd],
            capture_output=True, text=True, timeout=timeout,
        )
        return {
            "ran": True,
            "exit": out.returncode,
            "duration_s": round(time.time() - t0, 2),
            "stdout_tail": _tail(out.stdout, 60),
            "stderr_tail": _tail(out.stderr, 30),
        }
    except subprocess.TimeoutExpired:
        return {
            "ran": True, "exit": 124,
            "duration_s": round(time.time() - t0, 2),
            "stdout_tail": "", "stderr_tail": f"timeout after {timeout}s",
        }
    except (subprocess.SubprocessError, FileNotFoundError, OSError) as e:
        return {
            "ran": False, "exit": 1,
            "duration_s": round(time.time() - t0, 2),
            "stdout_tail": "", "stderr_tail": f"docker exec error: {e!r}",
        }


def _docker_rm(name: str) -> None:
    try:
        subprocess.run(
            ["docker", "rm", "-f", name],
            capture_output=True, text=True, timeout=30,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        pass


def _detect_verifier_path(name: str) -> str:
    """Probe for a tests/test.sh-style verifier inside the container.

    Returns the first matching path, falling back to ``/tests/test.sh``
    if no probe succeeds (which will then fail in ``docker exec`` and
    surface as ``FAIL(no-verifier)``).
    """
    candidates = [
        "/tests/test.sh",
        "/test.sh",
        "/tests/test_outputs.py",
        "/app/tests/test.sh",
        "/opt/tests/test.sh",
    ]
    for path in candidates:
        check = _docker_exec(name, f"test -f {path} && echo OK", timeout=10)
        if check["exit"] == 0 and "OK" in check["stdout_tail"]:
            return path
    return "/tests/test.sh"


def _tail(text: str, n: int) -> str:
    if not text:
        return ""
    lines = text.splitlines()
    return "\n".join(lines[-n:]) if len(lines) > n else text


def _parse_pytest_pass_set(stdout: str) -> set[str]:
    """Extract the set of pytest node IDs that PASSED from a `-rA` log."""
    return {node for node, status in _PYTEST_NODE_RE.findall(stdout or "") if status == "PASSED"}


def _parse_pytest_fail_set(stdout: str) -> set[str]:
    return {node for node, status in _PYTEST_NODE_RE.findall(stdout or "") if status == "FAILED"}


# ---- per-benchmark exec functions ---------------------------------------


def _exec_swebench_verified(fix: dict, model_patch: str, *,
                            timeout: int, allow_pull: bool, keep_containers: bool) -> dict:
    """Apply ``model_patch`` and run the row's `eval_script` (which itself
    applies the harness's `test_patch` and runs pytest). Verifier gate:
    every FAIL_TO_PASS pytest ID ends up PASSED, and PASS_TO_PASS shows
    no regression.
    """
    image = fix.get("image")
    eval_script = fix.get("eval_script") or ""
    fail_to_pass = _coerce_list(fix.get("FAIL_TO_PASS"))
    pass_to_pass = _coerce_list(fix.get("PASS_TO_PASS"))

    if not image:
        return {"ran": False, "reason": "no-image"}
    if not eval_script:
        return {"ran": False, "reason": "no-verifier"}

    # The eval_script typically starts with `conda activate testbed && cd /testbed`.
    # We need to apply model_patch BEFORE the script applies test_patch.
    # Strategy: write model_patch to a tmp file, mount it via /workspace,
    # prepend `git apply -v - <<<"$model_patch"` to a copy of the script.
    with tempfile.TemporaryDirectory(prefix="smoke-swebv-") as tmp:
        tmp_path = Path(tmp)
        model_patch_file = tmp_path / "model.patch"
        model_patch_file.write_text(model_patch or "")

        # Compose: write a fresh script that applies model_patch first, then runs eval_script.
        composed = (
            f"set -uxo pipefail\n"
            f"if [ -s /workspace/model.patch ]; then\n"
            f"  echo '[smoke] applying model_patch'\n"
            f"  git apply -v /workspace/model.patch || "
            f"{{ echo 'APPLY_FAILED'; exit 1; }}\n"
            f"fi\n"
            f"{eval_script}\n"
        )
        script_file = tmp_path / "compose.sh"
        script_file.write_text(composed)

        if not _docker_pull(image, allow=allow_pull):
            return {"ran": False, "reason": "pull-failed", "image": image}

        # Mount tmp into /workspace; the harness expects /testbed to be cwd.
        # Without --keep-containers we use --rm; with it, the container is left
        # around for post-mortem inspection (under a stable name).
        rm_flag = [] if keep_containers else ["--rm"]
        try:
            out = subprocess.run(
                ["docker", "run", *rm_flag, "--network", DOCKER_NETWORK,
                 "-v", f"{tmp_path}:/workspace:ro",
                 image, "bash", "-lc", f"bash /workspace/compose.sh"],
                capture_output=True, text=True, timeout=timeout,
            )
            exit_code = out.returncode
            full_stdout = out.stdout
            stderr = out.stderr
            duration = 0.0  # approximate; not used downstream
        except subprocess.TimeoutExpired:
            return {"ran": True, "apply_exit": 1, "verifier_exit": 124,
                    "duration_s": float(timeout), "log_tail": f"timeout after {timeout}s",
                    "fail_to_pass_passed": 0, "fail_to_pass_total": len(fail_to_pass),
                    "pass_to_pass_regressed": len(pass_to_pass), "image": image}
        except (subprocess.SubprocessError, FileNotFoundError, OSError) as e:
            return {"ran": False, "reason": "exec-error", "image": image, "error": repr(e)}

        passed_set = _parse_pytest_pass_set(full_stdout)
        failed_set = _parse_pytest_fail_set(full_stdout)

        f2p_passed = sum(1 for t in fail_to_pass if t in passed_set)
        p2p_regressed = sum(1 for t in pass_to_pass if t in failed_set)

        apply_failed = "APPLY_FAILED" in full_stdout or exit_code != 0 and f2p_passed == 0 and not passed_set
        return {
            "ran": True,
            "image": image,
            "apply_exit": 1 if apply_failed else 0,
            "verifier_exit": exit_code,
            "fail_to_pass_passed": f2p_passed,
            "fail_to_pass_total": len(fail_to_pass),
            "pass_to_pass_regressed": p2p_regressed,
            "duration_s": duration,
            "log_tail": _tail(full_stdout, 60),
            "stderr_tail": _tail(stderr, 30),
        }


def _exec_swebench_pro(fix: dict, model_patch: str, *,
                       timeout: int, allow_pull: bool, keep_containers: bool) -> dict:
    """SWE-bench-Pro: run ``before_repo_set_cmd``, apply ``model_patch``,
    apply ``test_patch``, then pytest on ``selected_test_files_to_run``."""
    dockerhub_tag = fix.get("dockerhub_tag")
    if not dockerhub_tag:
        return {"ran": False, "reason": "no-image"}
    image = dockerhub_tag if ":" in dockerhub_tag else f"{dockerhub_tag}:latest"

    before = fix.get("before_repo_set_cmd") or ""
    test_patch = fix.get("test_patch") or ""
    files_to_run = _coerce_list(fix.get("selected_test_files_to_run"))
    fail_to_pass = _coerce_list(fix.get("fail_to_pass"))
    pass_to_pass = _coerce_list(fix.get("pass_to_pass"))

    if not files_to_run:
        return {"ran": False, "reason": "no-verifier"}

    with tempfile.TemporaryDirectory(prefix="smoke-swebp-") as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "model.patch").write_text(model_patch or "")
        (tmp_path / "test.patch").write_text(test_patch or "")

        composed = (
            "set -e\n"
            f"{before}\n"
            "if [ -s /workspace/model.patch ]; then\n"
            "  echo '[smoke] applying model_patch'\n"
            "  git apply -v /workspace/model.patch || { echo 'APPLY_FAILED'; exit 1; }\n"
            "fi\n"
            "if [ -s /workspace/test.patch ]; then\n"
            "  echo '[smoke] applying test_patch'\n"
            "  git apply -v /workspace/test.patch || { echo 'TEST_APPLY_FAILED'; exit 1; }\n"
            "fi\n"
            f"pytest -rA {' '.join(files_to_run)}\n"
        )
        (tmp_path / "compose.sh").write_text(composed)

        if not _docker_pull(image, allow=allow_pull):
            return {"ran": False, "reason": "pull-failed", "image": image}

        rm_flag = [] if keep_containers else ["--rm"]
        try:
            out = subprocess.run(
                ["docker", "run", *rm_flag, "--network", DOCKER_NETWORK,
                 "-v", f"{tmp_path}:/workspace:ro",
                 image, "bash", "-lc", "bash /workspace/compose.sh"],
                capture_output=True, text=True, timeout=timeout,
            )
            exit_code = out.returncode
            full_stdout = out.stdout
            stderr = out.stderr
        except subprocess.TimeoutExpired:
            return {"ran": True, "apply_exit": 1, "verifier_exit": 124,
                    "duration_s": float(timeout), "log_tail": f"timeout after {timeout}s",
                    "fail_to_pass_passed": 0, "fail_to_pass_total": len(fail_to_pass),
                    "pass_to_pass_regressed": len(pass_to_pass), "image": image}
        except (subprocess.SubprocessError, FileNotFoundError, OSError) as e:
            return {"ran": False, "reason": "exec-error", "image": image, "error": repr(e)}

        passed_set = _parse_pytest_pass_set(full_stdout)
        failed_set = _parse_pytest_fail_set(full_stdout)
        f2p_passed = sum(1 for t in fail_to_pass if t in passed_set)
        p2p_regressed = sum(1 for t in pass_to_pass if t in failed_set)
        apply_failed = "APPLY_FAILED" in full_stdout

        return {
            "ran": True, "image": image,
            "apply_exit": 1 if apply_failed else 0,
            "verifier_exit": exit_code,
            "fail_to_pass_passed": f2p_passed,
            "fail_to_pass_total": len(fail_to_pass),
            "pass_to_pass_regressed": p2p_regressed,
            "duration_s": 0.0,
            "log_tail": _tail(full_stdout, 60),
            "stderr_tail": _tail(stderr, 30),
        }


def _exec_terminal_bench(fix: dict, model_commands: list[str], *,
                         timeout: int, allow_pull: bool, keep_containers: bool) -> dict:
    """terminal-bench-2.0: run model commands inside a persisted container, then
    execute tests/test.sh. Verifier gate: tests/test.sh exit 0."""
    task_yaml = fix.get("task_yaml") or {}
    image = task_yaml.get("docker_image")
    if not image:
        return {"ran": False, "reason": "no-image"}

    slug = (fix.get("task_name") or fix.get("instance_id", "tb")).replace("/", "-")
    cname = f"smoke-tb-{slug}"

    cid = _docker_run_persisted(image, cname, allow_pull=allow_pull)
    if cid is None:
        return {"ran": False, "reason": "container-failed", "image": image}

    # The HF task ships tests/test.sh in the dataset; the per-task
    # docker image does NOT bake it in. Write it into the container
    # before running, so we can drive the canonical harness verifier.
    verifier_script = fix.get("verifier_script")
    if not verifier_script:
        return {"ran": False, "reason": "no-verifier", "image": image}

    try:
        _docker_exec(cname, "mkdir -p /tests /logs/verifier", timeout=15)
        heredoc = (
            "cat > /tests/test.sh <<'__SMOKE_VERIFIER_EOF__'\n"
            f"{verifier_script}\n"
            "__SMOKE_VERIFIER_EOF__\n"
            "chmod +x /tests/test.sh"
        )
        seed = _docker_exec(cname, heredoc, timeout=30)
        if seed["exit"] != 0:
            return {"ran": False, "reason": "verifier-seed-failed",
                    "image": image, "log_tail": seed["stderr_tail"]}
        # Also seed tests/test_outputs.py if we have it — test.sh
        # usually calls pytest on it.
        verifier_py = fix.get("verifier_py")
        if verifier_py:
            py_heredoc = (
                "cat > /tests/test_outputs.py <<'__SMOKE_PY_EOF__'\n"
                f"{verifier_py}\n"
                "__SMOKE_PY_EOF__"
            )
            _docker_exec(cname, py_heredoc, timeout=30)
        # Run the model's commands one at a time. Fail-fast heuristic:
        # if the first 5 lines contain `set -e`, abort on first non-zero exit.
        fail_fast = any(
            "set -e" in ln or "set -eu" in ln
            for ln in (model_commands or [])[:5]
        )
        applied = 0
        last_exit = 0
        last_stdout = ""
        for cmd in (model_commands or []):
            if not cmd.strip():
                continue
            r = _docker_exec(cname, cmd, timeout=timeout)
            applied += 1
            last_exit = r["exit"]
            last_stdout = r["stdout_tail"]
            if fail_fast and r["exit"] != 0:
                break

        # Run the verifier. Try common terminal-bench layout paths;
        # some tasks place tests elsewhere (e.g. /test.sh).
        verifier_path = _detect_verifier_path(cname)
        verifier = _docker_exec(cname, f"bash {verifier_path}", timeout=timeout)
        # The canonical terminal-bench verifier writes a reward file
        # rather than relying on exit code (test.sh exits 0 in both the
        # pass and fail branches because the trailing echo 0/1 always
        # succeeds). Read /logs/verifier/reward.txt if present.
        reward_check = _docker_exec(
            cname, "cat /logs/verifier/reward.txt 2>/dev/null || echo NONE",
            timeout=10,
        )
        reward_text = (reward_check["stdout_tail"] or "").strip().splitlines()
        reward = reward_text[-1] if reward_text else "NONE"
        # The reward file is the canonical pass/fail signal: "1" = pass,
        # "0" = fail. Fall back to exit code if no reward file exists.
        passed = (reward == "1") if reward in ("0", "1") else (verifier["exit"] == 0)
        verifier_exit_signal = verifier["exit"]
        if reward in ("0", "1"):
            verifier_exit_signal = 0 if passed else 1

        # Combine stdout tail for log.
        log_tail = _tail(
            f"[commands] applied={applied} fail_fast={fail_fast}\n"
            f"[last] exit={last_exit}\n{last_stdout}\n"
            f"[verifier] exit={verifier['exit']} reward={reward}\n"
            f"{verifier['stdout_tail']}\n"
            f"[verifier stderr]\n{verifier['stderr_tail']}",
            60,
        )

        return {
            "ran": True, "image": image, "container": cid,
            "apply_exit": last_exit,
            "verifier_exit": verifier_exit_signal,
            "commands_applied": applied,
            "reward": reward,
            "fail_to_pass_passed": 1 if passed else 0,
            "fail_to_pass_total": 1,
            "pass_to_pass_regressed": 0,
            "duration_s": 0.0,
            "log_tail": log_tail,
            "stderr_tail": verifier["stderr_tail"],
        }
    finally:
        if not keep_containers:
            _docker_rm(cname)


def _exec_deepswe(fix: dict, model_patch: str, *,
                  timeout: int, allow_pull: bool, keep_containers: bool) -> dict:
    """deepswe-1.1: run model commands in /app inside the task image,
    then drive the parquet-shipped verifier_script which captures the
    diff, applies /tests/test.patch, and runs /app/test.sh base+new."""
    image = fix.get("docker_image")
    test_patch = fix.get("test_patch") or ""
    verifier_script = fix.get("verifier_script") or ""
    if not image:
        return {"ran": False, "reason": "no-image"}
    if not verifier_script:
        return {"ran": False, "reason": "no-verifier", "image": image}

    slug = (fix.get("task_id") or fix.get("name") or fix.get("instance_id", "ds")).replace("/", "-")
    cname = f"smoke-ds-{slug}"

    cid = _docker_run_persisted(image, cname, allow_pull=allow_pull)
    if cid is None:
        return {"ran": False, "reason": "container-failed", "image": image}

    try:
        _docker_exec(cname, "mkdir -p /tests /logs/verifier /logs/artifacts",
                     timeout=15)
        # Seed /tests/test.patch from parquet
        if test_patch.strip():
            seed_test = (
                "cat > /tests/test.patch <<'__SMOKE_TP_EOF__'\n"
                f"{test_patch}\n"
                "__SMOKE_TP_EOF__"
            )
            _docker_exec(cname, seed_test, timeout=30)
        # Seed /tests/verifier.sh from parquet (the verifier captures
        # the model.diff at base_commit, resets, applies test.patch,
        # runs /app/test.sh base+new, writes reward.txt).
        seed_v = (
            "cat > /tests/verifier.sh <<'__SMOKE_V_EOF__'\n"
            f"{verifier_script}\n"
            "__SMOKE_V_EOF__\n"
            "chmod +x /tests/verifier.sh"
        )
        _docker_exec(cname, seed_v, timeout=30)
        # If the model emitted a patch, treat it as the model's edits
        # and apply it on top of /app BEFORE the verifier captures the
        # diff. If the model emitted commands instead, parse-them-out is
        # the dispatcher's job; here we just take a patch body.
        apply_exit = 0
        if model_patch.strip():
            apply_cmd = (
                "cat > /tmp/model.patch <<'__SMOKE_MP_EOF__'\n"
                f"{model_patch}\n"
                "__SMOKE_MP_EOF__\n"
                "git apply -v /tmp/model.patch"
            )
            apply_r = _docker_exec(cname, apply_cmd, timeout=60)
            apply_exit = apply_r["exit"]
            if apply_exit != 0:
                # Don't abort — let the verifier still capture whatever
                # state /app is in, since some "patches" are partial
                # or already-applied in spirit.
                apply_exit = 0
        # Run the verifier.
        verifier = _docker_exec(cname, "bash /tests/verifier.sh", timeout=timeout)
        # Read the reward file (canonical deepswe pass/fail signal).
        reward_check = _docker_exec(
            cname, "cat /logs/verifier/reward.txt 2>/dev/null || echo NONE",
            timeout=10,
        )
        reward_text = (reward_check["stdout_tail"] or "").strip().splitlines()
        reward = reward_text[-1] if reward_text else "NONE"
        passed = (reward == "1") if reward in ("0", "1") else (verifier["exit"] == 0)
        verifier_exit_signal = 0 if passed else 1

        log_tail = _tail(
            f"[verifier] exit={verifier['exit']} reward={reward}\n"
            f"{verifier['stdout_tail']}\n"
            f"[verifier stderr]\n{verifier['stderr_tail']}",
            60,
        )

        return {
            "ran": True, "image": image, "container": cid,
            "apply_exit": apply_exit,
            "verifier_exit": verifier_exit_signal,
            "fail_to_pass_passed": 1 if passed else 0,
            "fail_to_pass_total": 1,
            "pass_to_pass_regressed": 0,
            "reward": reward,
            "duration_s": 0.0,
            "log_tail": log_tail,
            "stderr_tail": verifier["stderr_tail"],
        }
    finally:
        if not keep_containers:
            _docker_rm(cname)


EXECUTORS = {
    "swebench-verified": _exec_swebench_verified,
    "swebench-pro": _exec_swebench_pro,
    "terminal-bench-2.0": _exec_terminal_bench,
    "deepswe-1.1": _exec_deepswe,
}


def _verdict_from_exec(exec_result: dict) -> str:
    """Turn a per-benchmark exec_result dict into a one-line verdict banner."""
    if not exec_result.get("ran"):
        reason = exec_result.get("reason", "unknown")
        return f"FAIL(no-{reason})"
    if exec_result.get("apply_exit") not in (0, None):
        return "FAIL(apply-failed)"
    if exec_result.get("verifier_exit") != 0:
        f2p = exec_result.get("fail_to_pass_passed", "?")
        f2t = exec_result.get("fail_to_pass_total", "?")
        return f"FAIL({f2p}/{f2t})"
    f2p = exec_result.get("fail_to_pass_passed", 0)
    f2t = exec_result.get("fail_to_pass_total", 0)
    if f2p != f2t:
        return f"FAIL({f2p}/{f2t})"
    return f"PASS({f2p}/{f2t})"


def _is_pass(verdict: str, exec_result: dict) -> bool:
    """Final ok = real-exec verifier passed AND no FAIL_TO_PASS regressions."""
    if not verdict.startswith("PASS("):
        return False
    return exec_result.get("fail_to_pass_passed", 0) == exec_result.get("fail_to_pass_total", 0)


# ---- per-instance runner ------------------------------------------------


def _persist_record(run_id: str, benchmark: str, fix: dict, record: dict) -> None:
    """Atomic-write the JSON record (always, regardless of verdict)."""
    out_dir = _results_dir(run_id, benchmark)
    out_path = out_dir / f"{fix['instance_id']}.json"
    try:
        atomic_write_text(out_path, json.dumps(record, indent=2, ensure_ascii=False))
    except Exception as e:
        sys.stderr.write(f"[record] write failed: {e!r}\n")
    # Persist the response text for diagnostics on FAIL/wiring.
    txt = record.get("parsed_text_for_log")
    if txt:
        try:
            atomic_write_text(out_dir / f"{fix['instance_id']}.response.txt", txt)
        except Exception:
            pass


def _extract_artifact(benchmark: str, parsed: dict) -> tuple[str, list[str]]:
    """Return (model_patch, model_commands) for the exec stage.

    For patch-style benchmarks, the patch body is everything between
    ``<patch>...</patch>``. For terminal-bench, the parsed commands are
    already split into a list.
    """
    if benchmark == "terminal-bench-2.0":
        return "", parsed.get("commands", [])
    return parsed.get("patch_body", ""), []


def _run_one(benchmark: str, idx: int, total: int, fix: dict, *,
             run_id: str, no_exec: bool, exec_timeout: int,
             no_pull: bool, keep_containers: bool) -> tuple[bool, dict]:
    """Run one instance; return (passed, record). Print a per-instance
    block to stdout with a single trailing verdict line.

    The ``record`` dict is what gets written to
    ``results/<run-id>/<benchmark>/<instance_id>.json`` via
    ``atomic_write_text``.
    """
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
        return False, {"verdict": "FAIL(inference)", "instance_id": fix["instance_id"]}
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
    print(f"parsed    : {json.dumps({k: v for k, v in parsed.items() if k not in ('patch_body','commands')}, ensure_ascii=False)}")
    print("--- response (first 800 chars) ---")
    print(text[:800] + ("…" if len(text) > 800 else ""))

    # --- wiring-only PASS (artifact presence) -------------------------
    extracted = False
    if tool_calls > 0:
        extracted = True
    elif parsed.get("tool_call_attempted"):
        extracted = True
    elif benchmark == "terminal-bench-2.0":
        extracted = parsed.get("command_count", 0) > 0
    else:
        extracted = parsed.get("format") not in (None, "missing-patch-tag")

    wiring_ok = bool(text or tool_calls) and extracted and latency < TIMEOUT

    record: dict[str, Any] = {
        "instance_id": fix["instance_id"],
        "benchmark": benchmark,
        "source": fix.get("source", "fixture"),
        "ts": _now_utc(),
        "model_latency_s": round(latency, 2),
        "tokens": {
            "prompt": usage.get("prompt_tokens"),
            "completion": usage.get("completion_tokens"),
            "reasoning": usage.get("completion_tokens_details", {}).get("reasoning_tokens"),
        },
        "parsed": {k: v for k, v in parsed.items() if k not in ("patch_body", "commands")},
        "wiring_ok": wiring_ok,
        "response_full_len": len(text),
        "parsed_text_for_log": text[:4000],
        "exec": None,
        "verdict": "FAIL(wiring)",
    }

    # --- exec stage ---------------------------------------------------
    if no_exec:
        print("exec      : --no-exec (wiring-only mode)")
        # Back-compat: treat wiring_ok as PASS for the per-instance banner,
        # but the caller (``smoke()``) skips the solved-mark in --no-exec
        # mode so the next --limit-new / --exec run retries the instance.
        verdict = "PASS" if wiring_ok else "FAIL(wiring)"
        record["verdict"] = verdict
        print()
        print(verdict)
        print()
        _persist_record(run_id, benchmark, fix, record)
        return wiring_ok, record

    if not _docker_available():
        record["verdict"] = "FAIL(no-exec)"
        print("exec      : docker daemon unreachable")
        print()
        print("FAIL(no-exec)")
        print()
        _persist_record(run_id, benchmark, fix, record)
        return False, record

    if not wiring_ok:
        print("exec      : skipped (wiring failed)")
        record["verdict"] = "FAIL(wiring)"
        print()
        print("FAIL(wiring)")
        print()
        _persist_record(run_id, benchmark, fix, record)
        return False, record

    model_patch, model_commands = _extract_artifact(benchmark, parsed)
    timeout = exec_timeout if exec_timeout > 0 else DOCKER_TIMEOUTS.get(benchmark, 120)

    print(f"exec      : docker (timeout={timeout}s, no_pull={no_pull}, "
          f"keep_containers={keep_containers})")
    executor = EXECUTORS[benchmark]
    exec_result = executor(fix, model_patch,  # patch body
                           timeout=timeout, allow_pull=not no_pull,
                           keep_containers=keep_containers)
    # terminal-bench passes commands instead of patch
    if benchmark == "terminal-bench-2.0":
        exec_result = executor(fix, model_commands,
                               timeout=timeout, allow_pull=not no_pull,
                               keep_containers=keep_containers)

    record["exec"] = exec_result
    verdict = _verdict_from_exec(exec_result)
    record["verdict"] = verdict
    passed = _is_pass(verdict, exec_result)

    # Print exec status lines.
    if exec_result.get("image"):
        print(f"image     : {exec_result['image']}")
    if exec_result.get("apply_exit") is not None:
        print(f"apply     : {'ok' if exec_result['apply_exit'] == 0 else 'FAIL'} "
              f"(exit={exec_result['apply_exit']})")
    f2p = exec_result.get("fail_to_pass_passed")
    f2t = exec_result.get("fail_to_pass_total")
    if f2p is not None:
        print(f"verifier  : FAIL_TO_PASS {f2p}/{f2t}  "
              f"verifier_exit={exec_result.get('verifier_exit')}  "
              f"({exec_result.get('duration_s', 0)}s)")
    if exec_result.get("log_tail"):
        print("log_tail  : " + exec_result["log_tail"].replace("\n", "\n            "))
    if exec_result.get("stderr_tail"):
        print("stderr    : " + exec_result["stderr_tail"].replace("\n", "\n            "))

    # Persist the JSON record (always, regardless of verdict).
    out_dir = _results_dir(run_id, benchmark)
    _persist_record(run_id, benchmark, fix, record)

    # Persist the full exec log (if we have any).
    if exec_result.get("log_tail"):
        try:
            atomic_write_text(out_dir / f"{fix['instance_id']}.exec.log",
                              exec_result["log_tail"])
        except Exception:
            pass

    print()
    print(verdict)
    print()
    return passed, record


# ---- already-solved tracking --------------------------------------------


SOLVED_DIR = RESULTS_ROOT / ".solved"


def _solved_file(benchmark: str) -> Path:
    SOLVED_DIR.mkdir(parents=True, exist_ok=True)
    return SOLVED_DIR / f"{benchmark}.solved"


def _load_solved(benchmark: str) -> set[str]:
    """Return the set of instance_ids previously solved for this benchmark."""
    p = _solved_file(benchmark)
    if not p.exists():
        return set()
    return {ln.strip() for ln in p.read_text().splitlines() if ln.strip()}


def _mark_solved(benchmark: str, instance_id: str) -> None:
    """Record ``instance_id`` as solved for this benchmark (append-only)."""
    p = _solved_file(benchmark)
    # Guard against accidental duplicate writes on repeated PASSes.
    if p.exists() and instance_id in p.read_text().splitlines():
        return
    SOLVED_DIR.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        f.write(instance_id + "\n")


# ---- main ---------------------------------------------------------------


def smoke(benchmark: str, limit: int, *,
          run_id: str = "", no_exec: bool = False,
          exec_timeout: int = 0, no_pull: bool = False,
          keep_containers: bool = False, emit_json: bool = False,
          skip_ids: str = "") -> int:
    if benchmark not in FIXTURES:
        print(f"unknown benchmark: {benchmark}", file=sys.stderr)
        return 2

    run_id = run_id or _default_run_id()
    fixture = FIXTURES[benchmark]

    # Decide the instance list.
    candidates: list[dict] = []
    source = "fixture"
    if fixture.get("hf_format") and fixture.get("hf_format") != "fixture-only":
        live = _try_fetch_live(benchmark, fixture, limit if limit > 0 else 0)
        if live:
            candidates = live
            source = f"live:{fixture['hf_dataset']}"
        else:
            candidates = [dict(fixture)]
            source = "fixture (live fetch failed)"
    else:
        candidates = [dict(fixture)]

    # Skip already-solved instances when a positive --limit-new was given.
    # With --limit-new=0 (default), keep the original "all candidates"
    # semantics so bulk pipelines still see every record.
    solved = _load_solved(benchmark)
    already_solved_in_pool = sum(1 for c in candidates if c["instance_id"] in solved)

    # Additionally skip explicitly listed instance ids (comma/space separated).
    skip_set: set[str] = set()
    if skip_ids.strip():
        skip_set = {s.strip() for s in skip_ids.replace(",", " ").split() if s.strip()}
    already_skipped = sum(1 for c in candidates if c["instance_id"] in skip_set)

    if limit > 0:
        pending = [
            c for c in candidates
            if c["instance_id"] not in solved and c["instance_id"] not in skip_set
        ][:limit]
        skipped = already_solved_in_pool + already_skipped
        if limit > 1 and len(candidates) <= 1 and skipped == 0:
            print(
                f"notice: {benchmark} has only 1 fixture instance; "
                f"--limit-new={limit} reduces to 1\n",
                file=sys.stderr,
            )
    else:
        pending = list(candidates)
        skipped = 0  # bulk mode intentionally re-runs solved instances

    if not pending:
        if already_solved_in_pool:
            print(
                f"notice: {benchmark} — {already_solved_in_pool} instance(s) "
                f"already solved; nothing new to run\n",
                file=sys.stderr,
            )
        else:
            print("no instances to run", file=sys.stderr)
        return 0

    print(f"benchmark : {benchmark}")
    print(f"source    : {source}")
    print(f"limit     : {limit if limit > 0 else 'all'} "
          f"(running {len(pending)}; {skipped} already solved and skipped)")
    print(f"endpoint  : {ENDPOINT}")
    print(f"model     : {MODEL}")
    print(f"run_id    : {run_id}")
    print()

    passed = 0
    failed = 0
    verified = 0
    exec_ok = 0
    exec_skipped = 0
    for idx, inst in enumerate(pending):
        inst.setdefault("source", source)
        ok, record = _run_one(
            benchmark, idx, len(pending), inst,
            run_id=run_id, no_exec=no_exec,
            exec_timeout=exec_timeout, no_pull=no_pull,
            keep_containers=keep_containers,
        )
        if emit_json:
            print(json.dumps({"run_id": run_id, "record": record}, ensure_ascii=False))
        # Mark solved only when a real docker exec passed. In --no-exec
        # mode the verdict "PASS" only means "wiring is intact" — we
        # deliberately skip the solved mark so the next --limit-new run
        # (or a real --exec run) will retry the instance.
        if ok and not no_exec:
            _mark_solved(benchmark, inst["instance_id"])
        if ok:
            passed += 1
        else:
            failed += 1
        v = record.get("verdict", "")
        if v.startswith("PASS("):
            verified += 1
            exec_ok += 1
        elif v in ("FAIL(no-exec)", "FAIL(inference)"):
            exec_skipped += 1
        elif v.startswith("FAIL("):
            exec_ok += 1

    print("=" * 67)
    print(f"SUMMARY  : {benchmark}  "
          f"PASS={passed}  FAIL={failed}  total={len(pending)}  "
          f"skipped={skipped}  verified={verified}  "
          f"exec_ok={exec_ok}  exec_skipped={exec_skipped}")
    print(f"           run_id={run_id}  "
          f"results={RESULTS_ROOT / run_id / benchmark}/")
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
    ap.add_argument(
        "--no-exec",
        action="store_true",
        help="Skip the docker sandbox; treat artifact presence as PASS. "
             "Use this with --limit-new=1 for fast endpoint reachability "
             "checks (equivalent to the legacy wiring-only behaviour).",
    )
    ap.add_argument(
        "--exec-timeout",
        type=int,
        default=0,
        metavar="N",
        help="Per-instance docker timeout in seconds. 0 = benchmark "
             "default (120 verified/terminal-bench, 180 pro/deepswe). "
             "Max 600.",
    )
    ap.add_argument(
        "--keep-containers",
        action="store_true",
        help="Leave named containers around after run (smoke-tb-<slug>, "
             "smoke-ds-<slug>) for post-mortem debugging.",
    )
    ap.add_argument(
        "--no-pull",
        action="store_true",
        help="Skip docker pull; assume image is cached. Faster iteration "
             "when images are known present.",
    )
    ap.add_argument(
        "--json",
        action="store_true",
        help="Emit NDJSON summary to stdout in addition to human banner.",
    )
    ap.add_argument(
        "--run-id",
        default=os.environ.get("BENCKKIT_RUN_ID", ""),
        metavar="ID",
        help="Tag results dir. Defaults to $BENCKKIT_RUN_ID or "
             "'smoke-<UTC-timestamp>'. Used as "
             "results/<run-id>/<benchmark>/<instance>.json.",
    )
    ap.add_argument(
        "--skip-ids",
        default="",
        metavar="IDS",
        help="Comma/space separated instance ids to skip even if not solved. "
             "Useful for --limit-new-ok retry loops that must move past failed "
             "instances.",
    )
    args = ap.parse_args()
    return smoke(
        args.benchmark, args.limit_new,
        run_id=args.run_id, no_exec=args.no_exec,
        exec_timeout=args.exec_timeout, no_pull=args.no_pull,
        keep_containers=args.keep_containers, emit_json=args.json,
        skip_ids=args.skip_ids,
    )


if __name__ == "__main__":
    sys.exit(main())