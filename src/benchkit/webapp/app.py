"""FastAPI read-only viewer for results/ (benchmark run dashboard).

Layout: results/<target-key>/run-<N>/...  where target-key = model__bench__host-port.

Serves:
- GET  /api/targets             — targets (most recently updated first) with run list
- GET  /api/targets/<key>/runs/<run_id>          — run detail + per-instance list
- GET  /api/targets/<key>/runs/<run_id>/<instance_id> — instance detail
- POST /api/targets/<key>/runs/<run_id>/comment — set run comment
- GET  /                          — single-page frontend
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

STATIC_DIR = Path(__file__).resolve().parent / "static"
REPO_ROOT = Path(__file__).resolve().parents[3]
VIBE_CONFIG = REPO_ROOT / "vibe-coding.json"

# Internal layout dirs that must never be treated as runs/instances
_INTERNAL_DIRS = {"archive", "eval", "logs", "predictions", "raw", "canonical", "input"}


def create_app(results_root: str | os.PathLike | None = None) -> FastAPI:
    root = Path(results_root) if results_root else Path.cwd() / "results"
    root = root.resolve()

    app = FastAPI(title="benchkit results viewer")

    # ---- data loading helpers ----

    def _load_json(path: Path) -> dict | None:
        try:
            return json.loads(path.read_text())
        except Exception:
            return None

    def _target_dirs() -> list[tuple[str, Path]]:
        if not root.is_dir():
            return []
        out = []
        for p in sorted(root.iterdir()):
            if p.is_dir() and p.name not in _INTERNAL_DIRS and not p.name.startswith("."):
                # skip legacy run-* dirs at top level
                if p.name.startswith("run-"):
                    continue
                # only treat dirs with target.json as test targets
                if not (p / "target.json").exists():
                    continue
                out.append((p.name, p))
        return out

    def _run_dirs(target_dir: Path) -> list[tuple[str, Path]]:
        out = []
        for p in sorted(target_dir.iterdir()):
            if p.is_dir() and p.name.startswith("run-"):
                out.append((p.name, p))
        return out

    def _instance_id_from_dir(d: Path) -> str | None:
        for f in d.glob("*.traj.json"):
            return f.stem.removesuffix(".traj")
        return None

    def _bench_summary(run_dir: Path) -> tuple[dict, dict]:
        """Return (summary, breakdown). Prefers eval/summary.json; falls back to
        aggregating per-instance result JSONs (smoke.py format) when absent."""
        summary = _load_json(run_dir / "eval" / "summary.json") or {}
        breakdown = _load_json(run_dir / "eval" / "breakdown.json") or {}
        if summary:
            return summary, breakdown

        # Fallback: aggregate tb20__*/smoke result JSONs (verdict field)
        resolved = unresolved = missing = 0
        resolved_ids, unresolved_ids, missing_ids = [], [], []
        for f in sorted(run_dir.glob("*.json")):
            if f.name.startswith("."):
                continue
            rec = _load_json(f)
            if not isinstance(rec, dict) or not rec.get("instance_id"):
                continue
            iid = rec["instance_id"]
            verdict = str(rec.get("verdict", ""))
            if verdict.startswith("PASS("):
                resolved += 1
                resolved_ids.append(iid)
            elif verdict.startswith("FAIL("):
                unresolved += 1
                unresolved_ids.append(iid)
            else:
                missing += 1
                missing_ids.append(iid)
        summary = {
            "run_id": run_dir.name.removeprefix("run-"),
            "total_predicted": resolved + unresolved + missing,
            "resolved": resolved,
            "unresolved": unresolved,
            "missing": missing,
            "not_evaluated": 0,
            "resolved_ids": sorted(resolved_ids),
            "unresolved_ids": sorted(unresolved_ids),
            "missing_ids": sorted(missing_ids),
            "not_evaluated_ids": [],
        }
        return summary, breakdown

    def _last_tune_script(run_dir: Path) -> str:
        """Return the most recent archived start script name (highest tuneNNN)."""
        archive_dir = run_dir / "archive"
        if not archive_dir.is_dir():
            return ""
        scripts = sorted(archive_dir.glob("tune*-start-*.sh"))
        return scripts[-1].name if scripts else ""

    def _is_running(run_dir: Path) -> bool:
        """Heuristic: state.jsonl written within the last 10 minutes → running."""
        state = run_dir / "state.jsonl"
        if not state.exists():
            return False
        try:
            age = time.time() - state.stat().st_mtime
            return age < 600
        except Exception:
            return False

    def _run_meta(run_dir: Path, manifest: dict) -> dict:
        return {
            "model": manifest.get("model", ""),
            "model_url": manifest.get("model_url", ""),
            "server_script": manifest.get("server_script", ""),
            "server_host": manifest.get("server_host", ""),
            "bench_script": _last_tune_script(run_dir),
            "last_run_at": manifest.get("last_run_at", ""),
            "running": _is_running(run_dir),
        }

    def _comments(target_dir: Path) -> dict:
        c = _load_json(target_dir / "comments.json")
        return c if isinstance(c, dict) else {}

    def _run_summary_item(run_dir: Path) -> dict:
        manifest = _load_json(run_dir / "manifest.json") or {}
        summary, _b = _bench_summary(run_dir)
        state = run_dir / "state.jsonl"
        attempted = 0
        if state.exists():
            try:
                attempted = sum(1 for line in state.read_text().splitlines() if line.strip())
            except Exception:
                attempted = 0
        item = {
            "run_id": run_dir.name,
            "attempted": attempted,
            "resolved": summary.get("resolved", 0),
            "total": summary.get("total_predicted", 0),
            "score": summary.get("resolved", 0),
            "dataset": manifest.get("dataset", ""),
            "created_at": manifest.get("created_at", ""),
        }
        item.update(_run_meta(run_dir, manifest))
        return item

    # ---- API ----

    @app.get("/api/health")
    def health() -> dict:
        return {"status": "ok", "results_root": str(root)}

    def _targets_snapshot() -> dict:
        """Build the full targets snapshot (shared by REST and WebSocket)."""
        items = []
        for key, tdir in _target_dirs():
            tmeta = _load_json(tdir / "target.json") or {}
            runs = []
            for rname, rdir in _run_dirs(tdir):
                runs.append(_run_summary_item(rdir))
            runs.sort(key=lambda r: int(r["run_id"].removeprefix("run-")))
            url = tmeta.get("model_url", "")
            items.append({
                "key": key,
                "bench": tmeta.get("bench", ""),
                "model": tmeta.get("model", ""),
                "model_url": url,
                "server_up": _server_up(url),
                "active_run_id": tmeta.get("active_run_id", 1),
                "created_at": tmeta.get("created_at", ""),
                "last_run_at": tmeta.get("last_run_at", ""),
                "running": any(r["running"] for r in runs),
                "comments": _comments(tdir),
                "runs": runs,
            })
        # most recently updated first
        items.sort(key=lambda t: t["last_run_at"] or "", reverse=True)
        return {"targets": items}

    @app.get("/api/targets")
    def targets() -> dict:
        return _targets_snapshot()

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        """Push a fresh snapshot every 5s while the client is connected."""
        await websocket.accept()
        try:
            while True:
                await websocket.send_json(_targets_snapshot())
                await asyncio.sleep(5)
        except WebSocketDisconnect:
            pass
        except Exception:
            pass

    @app.get("/api/targets/{key}")
    def target_detail(key: str) -> dict:
        tdir = root / key
        if not tdir.is_dir():
            raise HTTPException(404, "target not found")
        tmeta = _load_json(tdir / "target.json") or {}
        runs = []
        for rname, rdir in _run_dirs(tdir):
            runs.append(_run_summary_item(rdir))
        runs.sort(key=lambda r: int(r["run_id"].removeprefix("run-")))
        return {
            "key": key,
            "bench": tmeta.get("bench", ""),
            "model": tmeta.get("model", ""),
            "model_url": tmeta.get("model_url", ""),
            "active_run_id": tmeta.get("active_run_id", 1),
            "created_at": tmeta.get("created_at", ""),
            "last_run_at": tmeta.get("last_run_at", ""),
            "running": any(r["running"] for r in runs),
            "comments": _comments(tdir),
            "runs": runs,
        }

    @app.post("/api/targets/{key}/runs/{run_id}/comment")
    async def set_comment(key: str, run_id: str, request: Request) -> dict:
        tdir = root / key
        if not tdir.is_dir():
            raise HTTPException(404, "target not found")
        body = await request.json()
        comment = str(body.get("comment", ""))
        cpath = tdir / "comments.json"
        comments = _comments(tdir)
        if comment:
            comments[run_id] = comment
        else:
            comments.pop(run_id, None)
        cpath.write_text(json.dumps(comments, ensure_ascii=False, indent=2))
        return {"key": key, "run_id": run_id, "comment": comment}

    def _fetch_models(url: str) -> dict:
        """GET <url>/models with a short timeout; returns parsed JSON or an error dict."""
        base = url.rstrip("/")
        if base.endswith("/models"):
            models_url = base
        elif base.endswith("/v1"):
            models_url = f"{base}/models"
        elif "/v1" in base:
            models_url = f"{base}/models"
        else:
            models_url = f"{base}/v1/models"
        try:
            req = urllib.request.Request(models_url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=5) as r:
                return json.loads(r.read().decode("utf-8", errors="replace"))
        except Exception as e:
            return {"error": f"{type(e).__name__}: {e}"}

    # url → server_up 캐시 (5초)
    _up_cache: dict[str, tuple[float, bool]] = {}

    def _server_up(url: str) -> bool:
        if not url:
            return False
        now = time.time()
        hit = _up_cache.get(url)
        if hit and now - hit[0] < 5:
            return hit[1]
        models = _fetch_models(url)
        ok = "error" not in models
        _up_cache[url] = (now, ok)
        return ok

    # ── vibe coding: Edit App ──

    _vibe_tasks: dict[str, dict] = {}
    _vibe_dir = REPO_ROOT / ".vibe"
    _vibe_dir.mkdir(exist_ok=True)

    def _vibe_config() -> dict:
        try:
            cfg = json.loads(VIBE_CONFIG.read_text())
        except Exception:
            cfg = {}
        return cfg

    def _vibe_agent_cmd(agent_id: str) -> str | None:
        cfg = _vibe_config()
        agent = (cfg.get("agents") or {}).get(agent_id)
        if not agent:
            return None
        cmd = agent.get("cmd", "")
        if "/" in cmd:
            return cmd
        for cand in (REPO_ROOT / "bin" / cmd, Path.home() / ".local" / "bin" / cmd):
            if cand.exists():
                return str(cand)
        return cmd

    def _git(args: list[str], cwd: Path = REPO_ROOT) -> tuple[int, str, str]:
        p = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=120,
        )
        return p.returncode, p.stdout, p.stderr

    def _git_remote_url() -> str:
        cfg = _vibe_config()
        url = (cfg.get("git") or {}).get("remote_url", "")
        if not url:
            _rc, out, _ = _git(["remote", "get-url", "origin"])
            url = out.strip() if _rc == 0 else ""
        return url

    def _commit_url(commit: str) -> str:
        url = _git_remote_url().removesuffix(".git")
        return f"{url}/commit/{commit}"

    def _log_vibe(task_id: str, msg: str) -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        with open(_vibe_dir / f"{task_id}.log", "a") as f:
            f.write(line + "\n")
        task = _vibe_tasks.get(task_id)
        if task is not None:
            task["log"].append(line)

    @app.get("/api/vibe/config")
    def vibe_config() -> dict:
        return {
            "config": _vibe_config(),
            "commit": _git(["rev-parse", "--short", "HEAD"])[1].strip(),
            "remote_url": _git_remote_url(),
            "running": any(t["status"] in ("running", "pending") for t in _vibe_tasks.values()),
        }

    def _run_vibe_agent(task_id: str, agent_id: str, prompt: str) -> None:
        task = _vibe_tasks[task_id]
        task["status"] = "running"
        cmd = _vibe_agent_cmd(agent_id)
        if not cmd:
            task["status"] = "error"
            task["error"] = f"unknown agent: {agent_id}"
            return
        try:
            _log_vibe(task_id, f"agent={agent_id} cmd={cmd}")
            p = subprocess.run(
                [cmd, "-p", prompt],
                cwd=REPO_ROOT, capture_output=True, text=True, timeout=1800,
            )
            _log_vibe(task_id, f"agent exit={p.returncode}")
            if p.stdout:
                _log_vibe(task_id, p.stdout[-4000:])
            if p.stderr:
                _log_vibe(task_id, "[stderr] " + p.stderr[-2000:])
            if p.returncode != 0:
                task["status"] = "error"
                task["error"] = f"agent exit {p.returncode}"
                return
            # 커밋 전 상태 확인
            rc, out, _ = _git(["status", "--porcelain"])
            if rc != 0 or not out.strip():
                task["status"] = "done"
                task["commit"] = ""
                task["note"] = "no changes to commit"
                return
            _log_vibe(task_id, "committing changes…")
            c1, _, _ = _git(["add", "-A"])
            if c1 != 0:
                raise RuntimeError("git add failed")
            c2, _, _ = _git(["commit", "-m", f"vibe: {agent_id} — {prompt[:60]}"])
            if c2 != 0:
                raise RuntimeError("git commit failed")
            commit = _git(["rev-parse", "--short", "HEAD"])[1].strip()
            _log_vibe(task_id, f"committed {commit}")
            task["commit"] = commit
            # push
            c3, out3, err3 = _git(["push", "origin", "HEAD"])
            if c3 != 0:
                _log_vibe(task_id, f"push failed: {err3}")
                task["status"] = "error"
                task["error"] = f"push failed: {err3}"
                return
            task["status"] = "done"
            task["commit_url"] = _commit_url(commit)
            _log_vibe(task_id, f"pushed {commit} → {task['commit_url']}")
        except Exception as e:
            task["status"] = "error"
            task["error"] = str(e)
            _log_vibe(task_id, f"error: {e}")

    @app.post("/api/vibe/run")
    async def vibe_run(body: dict) -> dict:
        agent = str(body.get("agent", ""))
        prompt = str(body.get("prompt", "")).strip()
        if not agent or not prompt:
            raise HTTPException(400, "agent and prompt are required")
        task_id = f"vibe-{int(time.time()*1000)}"
        _vibe_tasks[task_id] = {
            "id": task_id, "agent": agent, "prompt": prompt,
            "status": "pending", "log": [], "commit": "", "commit_url": "",
            "error": "", "note": "", "started": time.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        loop = asyncio.get_running_loop()
        loop.run_in_executor(None, _run_vibe_agent, task_id, agent, prompt)
        return {"task_id": task_id}

    @app.get("/api/vibe/tasks")
    def vibe_tasks() -> dict:
        items = sorted(_vibe_tasks.values(), key=lambda t: t["started"], reverse=True)
        return {"tasks": items}

    def _run_deploy(script: str) -> dict:
        path = REPO_ROOT / script
        if not path.exists():
            return {"ok": False, "error": f"missing script: {script}"}
        p = subprocess.run([str(path)], cwd=REPO_ROOT, capture_output=True, text=True, timeout=600)
        return {
            "ok": p.returncode == 0,
            "returncode": p.returncode,
            "output": (p.stdout or "") + (p.stderr or ""),
        }

    @app.post("/api/vibe/deploy")
    def vibe_deploy(body: dict) -> dict:
        target = str(body.get("target", "dev"))
        cfg = _vibe_config()
        script = (cfg.get("deploy") or {}).get(target)
        if not script:
            raise HTTPException(400, f"unknown deploy target: {target}")
        return _run_deploy(script)

    @app.post("/api/vibe/revert")
    def vibe_revert(body: dict) -> dict:
        commit = str(body.get("commit", "")).strip()
        if not commit:
            raise HTTPException(400, "commit is required")
        try:
            rc, out, err = _git(["revert", "--no-edit", commit])
            if rc != 0:
                return {"ok": False, "error": err or out}
            new_commit = _git(["rev-parse", "--short", "HEAD"])[1].strip()
            c3, _, err3 = _git(["push", "origin", "HEAD"])
            if c3 != 0:
                return {"ok": False, "error": f"push failed: {err3}"}
            return {"ok": True, "commit": new_commit, "commit_url": _commit_url(new_commit)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _read_server_script(host: str, script: str) -> dict:
        """Read the server script content. If host is a remote (ssh-able) host,
        read it over SSH; otherwise try local path. Returns {name, host, content}."""
        name = script.rsplit("/", 1)[-1]
        try:
            if host and host not in ("localhost", "127.0.0.1"):
                out = subprocess.run(
                    ["ssh", "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
                     host, f"cat {script} 2>/dev/null"],
                    capture_output=True, text=True, timeout=10,
                )
                if out.returncode == 0 and out.stdout.strip():
                    return {"name": name, "host": host, "content": out.stdout}
            local = Path(script.replace("~", str(Path.home())))
            if local.exists():
                return {"name": name, "host": host, "content": local.read_text()}
        except Exception as e:
            return {"name": name, "host": host, "content": "", "error": str(e)}
        return {"name": name, "host": host, "content": "", "error": "script not found"}

    @app.get("/api/targets/{key}/server-info")
    def server_info(key: str) -> dict:
        tdir = root / key
        if not tdir.is_dir():
            raise HTTPException(404, "target not found")
        tmeta = _load_json(tdir / "target.json") or {}
        url = tmeta.get("model_url", "")
        # server script from latest run manifest, fall back to target.json
        script = ""
        host = ""
        runs = _run_dirs(tdir)
        for _rname, rdir in reversed(runs):
            mf = _load_json(rdir / "manifest.json") or {}
            if mf.get("server_script"):
                script = mf["server_script"]
                host = mf.get("server_host", "")
                break
        if not script:
            script = tmeta.get("server_script", "")
            host = tmeta.get("server_host", "")
        script_info = _read_server_script(host, script) if script else {
            "name": "", "host": host, "content": "", "error": "no server script recorded"}
        return {
            "key": key,
            "model_url": url,
            "script": script_info,
            "models": _fetch_models(url) if url else {"error": "no model url"},
            "running": any(_is_running(rdir) for _rname, rdir in runs),
        }

    @app.get("/api/targets/{key}/runs/{run_id}/script")
    def run_script(key: str, run_id: str) -> dict:
        tdir = root / key
        run_dir = tdir / run_id
        if not run_dir.is_dir():
            raise HTTPException(404, "run not found")
        name = _last_tune_script(run_dir)
        if not name:
            raise HTTPException(404, "no archived start script")
        path = run_dir / "archive" / name
        return {
            "key": key,
            "run_id": run_id,
            "name": name,
            "content": path.read_text() if path.exists() else "",
        }

    def _run_detail(key: str, run_id: str) -> dict:
        tdir = root / key
        run_dir = tdir / run_id
        if not run_dir.is_dir():
            raise HTTPException(404, "run not found")
        manifest = _load_json(run_dir / "manifest.json") or {}
        summary, breakdown = _bench_summary(run_dir)

        instances = []
        raw_dir = run_dir / "predictions" / "raw"
        has_raw_instances = raw_dir.is_dir() and any(raw_dir.iterdir())
        if has_raw_instances:
            for d in sorted(raw_dir.iterdir()):
                if not d.is_dir():
                    continue
                iid = _instance_id_from_dir(d)
                if not iid:
                    continue
                traj = _load_json(d / f"{iid}.traj.json")
                info = traj.get("info", {}) if traj else {}
                status = "submitted" if info.get("exit_status") else "unknown"
                verdict = None
                if isinstance(breakdown, dict):
                    r = breakdown.get(iid)
                    if isinstance(r, dict):
                        verdict = "resolved" if r.get("resolved") is True else (
                            "unresolved" if r.get("resolved") is False else "missing"
                        )
                instances.append({
                    "instance_id": iid,
                    "status": status,
                    "exit_status": info.get("exit_status"),
                    "api_calls": (info.get("model_stats") or {}).get("api_calls"),
                    "verdict": verdict,
                    "msg_count": len(traj.get("messages", [])) if traj else 0,
                })
        else:
            # Fallback: build instance list from smoke.py result JSONs (tb20__*.json)
            seen = set()
            for f in sorted(run_dir.glob("*.json")):
                if f.name.startswith("."):
                    continue
                rec = _load_json(f)
                if not isinstance(rec, dict) or not rec.get("instance_id"):
                    continue
                iid = rec["instance_id"]
                if iid in seen:
                    continue
                seen.add(iid)
                verdict_str = str(rec.get("verdict", ""))
                verdict = (
                    "resolved" if verdict_str.startswith("PASS(") else
                    "unresolved" if verdict_str.startswith("FAIL(") else None
                )
                exec_info = rec.get("exec") or {}
                instances.append({
                    "instance_id": iid,
                    "status": verdict or "unknown",
                    "exit_status": verdict_str,
                    "api_calls": None,
                    "verdict": verdict,
                    "msg_count": 0,
                    "image": exec_info.get("image"),
                    "reward": exec_info.get("reward"),
                    "commands_applied": exec_info.get("commands_applied"),
                })

        meta = _run_meta(run_dir, manifest)
        return {
            "key": key,
            "run_id": run_id,
            "bench": manifest.get("benchmark", ""),
            "dataset": manifest.get("dataset", ""),
            "created_at": manifest.get("created_at", ""),
            "comment": _comments(tdir).get(run_id, ""),
            "summary": summary,
            "instances": instances,
            "experiment": meta,
            "server_up": _server_up(meta.get("model_url", "")),
        }

    @app.get("/api/targets/{key}/runs/{run_id}")
    def run_detail(key: str, run_id: str) -> dict:
        return _run_detail(key, run_id)

    @app.get("/api/targets/{key}/runs/{run_id}/{instance_id}")
    def instance_detail(key: str, run_id: str, instance_id: str) -> dict:
        tdir = root / key
        run_dir = tdir / run_id
        inst_dir = run_dir / "predictions" / "raw" / instance_id
        traj_path = inst_dir / f"{instance_id}.traj.json"

        # Fallback for smoke.py-style results: read tb20__<iid>.json result file
        if not traj_path.exists():
            rec = _load_json(run_dir / f"{instance_id}.json")
            if rec is None:
                raise HTTPException(404, "trajectory not found")
            exec_info = rec.get("exec") or {}
            verdict_str = str(rec.get("verdict", ""))
            problem = str(rec.get("parsed_text_for_log", ""))
            steps = [{
                "type": "verdict",
                "command": verdict_str,
                "output": (
                    f"image={exec_info.get('image')}\n"
                    f"reward={exec_info.get('reward')}\n"
                    f"commands_applied={exec_info.get('commands_applied')}\n"
                    f"verifier_exit={exec_info.get('verifier_exit')}\n"
                    f"apply_exit={exec_info.get('apply_exit')}\n"
                    f"log_tail:\n{exec_info.get('log_tail', '')}"
                ),
            }]
            return {
                "key": key,
                "run_id": run_id,
                "instance_id": instance_id,
                "exit_status": verdict_str,
                "api_calls": None,
                "model": rec.get("benchmark"),
                "problem": problem,
                "steps": steps,
            }

        traj = _load_json(traj_path)
        if traj is None:
            raise HTTPException(500, "trajectory unreadable")

        info = traj.get("info", {}) or {}
        msgs = traj.get("messages", []) or []

        # Problem statement: first user message
        problem = ""
        for m in msgs:
            if m.get("role") == "user":
                problem = str(m.get("content", ""))
                break

        # Trajectory: assistant tool_call + tool result pairs
        steps = []
        pending: dict[str, dict] = {}
        for m in msgs:
            role = m.get("role")
            if role == "assistant" and m.get("tool_calls"):
                for tc in m["tool_calls"]:
                    fn = (tc.get("function") or {}).get("name", "tool")
                    args = (tc.get("function") or {}).get("arguments", "")
                    call_id = tc.get("id", "")
                    pending[call_id] = {
                        "type": fn,
                        "command": args,
                        "reasoning": str(m.get("reasoning_content", "") or "")[:2000],
                    }
            elif role == "tool":
                cid = m.get("tool_call_id", "")
                if cid in pending:
                    pending[cid]["output"] = str(m.get("content", ""))[:8000]
                    steps.append(pending.pop(cid))

        # Record any leftover (unanswered) tool calls too
        for p in pending.values():
            p["output"] = ""
            steps.append(p)

        return {
            "key": key,
            "run_id": run_id,
            "instance_id": instance_id,
            "exit_status": info.get("exit_status"),
            "api_calls": (info.get("model_stats") or {}).get("api_calls"),
            "model": (info.get("config") or {}).get("model", {}).get("model_name"),
            "problem": problem,
            "steps": steps,
        }

    # ---- static frontend ----

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    return app


app = create_app()

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8001)
