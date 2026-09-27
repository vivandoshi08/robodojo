"""Tiny dev server over runs/ + the Robo Dojo UI (stdlib only).

python -m robot_race.serve --port 8080 --runs runs      (then open http://127.0.0.1:8080/)

Port 8080 by default: the racetrack tracker owns 8000 (RACETRACK_URL, default http://localhost:8000).

Static:  GET /                                      -> ui/index.html (the dojo; ?demo=1 = offline simulation)
         GET /<run_id>/attempt_<k>/attempt.mp4 ...  (HTTP Range -> 206, so <video> can seek)
         anything not found under runs/ falls back to ui/ (e.g. /sponsors/mujoco.png)
API:     GET /api/runs                              -> manifest (same as runs/index.json, built live)
         GET /api/runs/<run_id>                     -> summary.json (synthesized for run_policy dirs)
         GET /api/runs/<run_id>/events?since=<n>    -> {"events": [...lines n..], "next": <n'>}
         GET /api/runs/<run_id>/transcript          -> {"turns": [transcript.jsonl lines]} (one per model call)
         GET /api/tasks                             -> {"tasks": [{id, text}]}
         POST /api/race {task, agents, seeds, tries, label, memory, backend: "local"|"qm"}
                                                    -> local: spawns run_race.py detached; qm: calls
                                                       qm.launch.launch_qm_race(...) in a thread (503 if
                                                       qm/launch.py is missing); {"launch_id", "backend", ...}
         GET /api/launch/<launch_id>                -> {running, exit_code, race_id (once planned), log_tail}
         GET /api/races                             -> tracker races (fail-soft) merged with local races/<id>/
         GET /api/races/<race_id>                   -> composite: plan, race, close, context, memory (races/<id>/
                                                       files), runs (summary.json with race.race_id == id,
                                                       slimmed), tracker race/leaderboard/attempts (or null)
         GET /api/races/<race_id>/qm                -> qm.launch.qm_status(race_id) (fail-soft: {"available": false})
         GET /api/races/<race_id>/<rest>            -> proxied to the tracker GET /races/<race_id>/<rest>
         GET  /api/runs/<run_id>/attempt_<k>/render -> {state: done|pending|failed|none|unavailable, video}
         POST /api/runs/<run_id>/attempt_<k>/render -> renders attempt.mp4 (640x480 @ 30) from trajectory.npz
                                                       via robot_race.replay in a subprocess; idempotent, one
                                                       job per attempt, at most MAX_RENDERS at once (429)
         GET /api/brain                             -> GBrain skill (gbrain/skills/trash-to-bin/SKILL.md)
         GET /api/memory?task=&limit=               -> recent Memorable episodes (local EpisodeStore; fail-open)
All GET responses: Access-Control-Allow-Origin: *. POST refuses cross-origin requests (it spends API credit).
JSON / events / live.jpg: Cache-Control: no-store.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from functools import partial
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from . import viewer

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TRACKER_URL = os.environ.get("RACETRACK_URL", "http://localhost:8000").rstrip("/")
SEEDS = re.compile(r"^\d{1,3}(-\d{1,3})?(,\d{1,3}(-\d{1,3})?)*$")
LABEL = re.compile(r"^[A-Za-z0-9 _.-]{0,40}$")
MAX_JOBS = 12          # agents x seeds per launch: every one is a paid agent loop
RACE_FILES = {"plan": "plan.json", "race": "race.json", "close": "close.json", "memory": "memory.json",
              "launch": "launch.json"}
RUN_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
MIME = {".mp4": "video/mp4", ".jsonl": "application/x-ndjson", ".npz": "application/octet-stream",
        ".json": "application/json", ".md": "text/markdown; charset=utf-8", ".py": "text/plain; charset=utf-8",
        ".jpg": "image/jpeg", ".png": "image/png", ".gif": "image/gif", ".html": "text/html; charset=utf-8"}
NO_STORE = (".json", ".jsonl", "live.jpg", ".html")
ATTEMPT = re.compile(r"^attempt_(\d{1,3})$")
MAX_RENDERS = 2        # replay renders are CPU-bound; more at once only slows every one down


class Renderer:
    """On-demand attempt.mp4 for --fast runs: `python -m robot_race.replay <attempt_dir>` at 640x480 @ 30,
    the executor's own format (replay writes to a tmp name and renames, so a half file is never served)."""

    def __init__(self, max_jobs: int = MAX_RENDERS):
        self.max_jobs = max_jobs
        self.jobs: dict[str, subprocess.Popen] = {}
        self.errors: dict[str, str] = {}
        self.lock = threading.Lock()

    def _reap(self):
        for d, p in list(self.jobs.items()):
            rc = p.poll()
            if rc is None:
                continue
            del self.jobs[d]
            if rc != 0 or not os.path.exists(os.path.join(d, "attempt.mp4")):
                tail = ""
                try:
                    with open(os.path.join(d, ".render.log")) as f:
                        tail = f.read()[-400:]
                except OSError:
                    pass
                self.errors[d] = f"replay exited {rc}: {tail.strip()}"
            else:
                _set_video(d)

    def status(self, attempt_dir: str) -> dict:
        with self.lock:
            self._reap()
            if attempt_dir in self.jobs:  # the mp4 can appear a moment before replay exits
                return {"state": "pending", "video": None}
            if os.path.exists(os.path.join(attempt_dir, "attempt.mp4")):
                _set_video(attempt_dir)  # also covers renders started elsewhere (run_race winner render)
                return {"state": "done", "video": "attempt.mp4"}
            if attempt_dir in self.errors:
                return {"state": "failed", "video": None, "error": self.errors[attempt_dir]}
            if not os.path.exists(os.path.join(attempt_dir, "trajectory.npz")):
                return {"state": "unavailable", "video": None, "error": "no trajectory.npz (attempt never ran)"}
            return {"state": "none", "video": None}

    def start(self, attempt_dir: str) -> dict:
        st = self.status(attempt_dir)
        if st["state"] in ("done", "pending", "unavailable"):
            return st
        with self.lock:
            if attempt_dir in self.jobs:
                return {"state": "pending", "video": None}
            if len(self.jobs) >= self.max_jobs:
                raise RuntimeError(f"{len(self.jobs)} renders already running, try again shortly")
            self.errors.pop(attempt_dir, None)
            log = open(os.path.join(attempt_dir, ".render.log"), "w")
            self.jobs[attempt_dir] = subprocess.Popen(
                [sys.executable, "-m", "robot_race.replay", attempt_dir, "--width", "640", "--height", "480",
                 "--fps", "30"], cwd=REPO, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
            log.close()
        return {"state": "pending", "video": None}


def _set_video(attempt_dir: str) -> None:
    """Point result.json media.video / video at the fresh attempt.mp4 (atomic rewrite; best effort)."""
    p = os.path.join(attempt_dir, "result.json")
    r = _read_json(p)
    if not isinstance(r, dict) or (r.get("video") == "attempt.mp4"
                                   and (r.get("media") or {}).get("video") in (None, "attempt.mp4")
                                   and (not isinstance(r.get("media"), dict) or r["media"].get("video"))):
        return
    r["video"] = "attempt.mp4"
    if isinstance(r.get("media"), dict):
        r["media"]["video"] = "attempt.mp4"
    try:
        with open(p + ".tmp", "w") as f:
            json.dump(r, f, indent=2)
        os.replace(p + ".tmp", p)
    except OSError:
        pass


def parse_range(header: str, size: int) -> tuple[int, int] | None:
    """First range of a 'bytes=a-b' header -> (start, end) inclusive. None = unsatisfiable/invalid.
    Raises ValueError for headers we ignore (non-bytes units)."""
    unit, _, spec = header.partition("=")
    if unit.strip().lower() != "bytes" or not spec:
        raise ValueError(header)
    first = spec.split(",")[0].strip()
    a, sep, b = first.partition("-")
    if not sep:
        return None
    a, b = a.strip(), b.strip()
    try:
        if a == "":  # suffix: last b bytes
            n = int(b)
            if n <= 0 or size == 0:
                return None
            return max(0, size - n), size - 1
        start = int(a)
        end = int(b) if b else size - 1
    except ValueError:
        return None
    if start < 0 or start >= size or end < start:
        return None
    return start, min(end, size - 1)


def expand_seeds(text: str) -> list[int]:
    out: list[int] = []
    for part in text.split(","):
        lo, _, hi = part.partition("-")
        lo_i, hi_i = int(lo), int(hi or lo)
        if hi_i < lo_i:
            raise ValueError("seed range goes backwards")
        out += range(lo_i, hi_i + 1)
    return out


def task_ids() -> dict[str, str]:
    from .tasks import TASKS  # lazy: pulls in MuJoCo
    return {k: v.get("text", "") for k, v in TASKS.items()}


def validate_race(body) -> dict:
    """POST /api/race body -> clean launch params. Raises ValueError with a user-facing message."""
    if not isinstance(body, dict):
        raise ValueError("body must be a JSON object")
    task = body.get("task", "can_to_bin")
    if not isinstance(task, str) or task not in task_ids():
        raise ValueError(f"unknown task {task!r}")

    def bounded(key, default, lo, hi):
        v = body.get(key, default)
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise ValueError(f"{key} must be an integer in [{lo}, {hi}]")
        return v

    agents = bounded("agents", 4, 1, 6)
    tries = bounded("tries", 5, 1, 5)
    seeds = str(body.get("seeds", "0")).replace(" ", "")
    if not SEEDS.match(seeds):
        raise ValueError("seeds must look like 0, 0-2 or 0,3,5")
    n_seeds = len(set(expand_seeds(seeds)))
    if agents * n_seeds > MAX_JOBS:
        raise ValueError(f"agents x seeds = {agents * n_seeds} > {MAX_JOBS}")
    label = body.get("label", "")
    if not isinstance(label, str) or not LABEL.match(label):
        raise ValueError("label: up to 40 letters, digits, space, _ . -")
    memory = body.get("memory", "tracker")
    if memory not in ("tracker", "none"):
        raise ValueError("memory must be 'tracker' or 'none'")
    backend = body.get("backend", "local")
    if backend not in ("local", "qm"):
        raise ValueError("backend must be 'local' or 'qm'")
    return {"task": task, "agents": agents, "seeds": seeds, "tries": tries, "label": label, "memory": memory,
            "backend": backend}


class Unavailable(RuntimeError):
    """An optional backend (QM) is not installed / not reachable."""


def qm_module():
    """qm/launch.py, imported lazily (QM is optional). Raises Unavailable."""
    if REPO not in sys.path:
        sys.path.insert(0, REPO)
    try:
        import importlib
        return importlib.import_module("qm.launch")
    except Exception as e:  # noqa: BLE001
        raise Unavailable(f"QM backend unavailable: {type(e).__name__}: {e}") from e


def qm_status(race_id: str) -> dict:
    try:
        st = qm_module().qm_status(race_id)
    except Exception as e:  # noqa: BLE001
        return {"available": False, "race_id": race_id, "error": str(e)}
    return {"available": True, "race_id": race_id, **(st if isinstance(st, dict) else {"status": st})}


def race_cmd(p: dict, runs_root: str, races_root: str) -> list[str]:
    """Argument list for run_race.py (never a shell string)."""
    cmd = [sys.executable, os.path.join(REPO, "run_race.py"), "--task", p["task"], "--agents", str(p["agents"]),
           "--seeds", p["seeds"], "--tries", str(p["tries"]), "--fast", "--memory", p["memory"],
           "--runs-dir", runs_root, "--races-dir", races_root]
    if p["label"]:
        cmd += ["--label", p["label"]]
    return cmd


def tracker_get(path: str, timeout: float = 1.5):
    """GET TRACKER_URL + path -> (status, content_type, bytes); (None, None, None) when unreachable."""
    try:
        with urllib.request.urlopen(TRACKER_URL + path, timeout=timeout) as r:
            return r.status, r.headers.get("Content-Type", "application/json"), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", "application/json"), e.read()
    except (OSError, ValueError):
        return None, None, None


def tracker_json(path: str):
    status, _, data = tracker_get(path)
    if status != 200:
        return None
    try:
        return json.loads(data)
    except ValueError:
        return None


def _read_json(path: str):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def slim_run(run_dir: str, s: dict) -> dict:
    """What the dojo needs from one summary.json: no code, no transcripts."""
    attempts = []
    for a in s.get("attempts") or []:
        r = a.get("result") or {}
        attempts.append({"k": a.get("k"), **{k: r.get(k) for k in (
            "success", "time_s", "collisions", "energy_j", "dropped", "lifted", "error", "item_final_pos",
            "media", "frames", "video")}})
        if a.get("k") and os.path.exists(os.path.join(run_dir, f"attempt_{a['k']}", "attempt.mp4")):
            m = dict(attempts[-1].get("media") or {})
            m["video"] = "attempt.mp4"  # rendered after the run (serve.py Renderer / run_race winner render)
            attempts[-1]["media"] = m
    k_now = len(attempts) + 1 if s.get("status") == "running" else None
    live = None
    if k_now and os.path.exists(os.path.join(run_dir, f"attempt_{k_now}", "live.jpg")):
        live = f"attempt_{k_now}/live.jpg"
    return {k: s.get(k) for k in ("run_id", "task", "seed", "model", "strategy", "status", "solved_at", "tries",
                                  "created_at", "updated_at", "race", "hints", "hinted", "scripted", "error")} | {
        "run_id": s.get("run_id") or os.path.basename(run_dir), "attempts": attempts,
        "current_attempt": k_now, "live": live}


def race_runs(runs_root: str, race_id: str) -> list[dict]:
    out = []
    try:
        names = os.listdir(runs_root)
    except OSError:
        return out
    for name in names:
        d = os.path.join(runs_root, name)
        s = _read_json(os.path.join(d, "summary.json"))
        if isinstance(s, dict) and (s.get("race") or {}).get("race_id") == race_id:
            out.append(slim_run(d, s))
    return sorted(out, key=lambda r: ((r.get("race") or {}).get("agent_id") or "", r.get("seed") or 0))


def memory_episodes(task: str | None, limit: int) -> dict:
    """Recent episodes from memorable_layer's local EpisodeStore. Never raises: memory is optional."""
    try:
        from memorable_layer.config import Settings
        from memorable_layer.store import EpisodeStore
        root = Settings.from_env().store_root
        if not (root / "episodes.jsonl").exists():
            return {"root": str(root), "episodes": []}
        eps = [e.to_dict() for e in EpisodeStore(root)]
    except Exception as e:  # noqa: BLE001
        return {"root": None, "episodes": [], "error": f"{type(e).__name__}: {e}"}
    if task:
        eps = [e for e in eps if e.get("task") in (None, task)]
    return {"root": str(root), "episodes": eps[-limit:][::-1]}


class Launcher:
    """run_race.py processes started from the UI. One at a time: each launch spends API credit."""

    def __init__(self, runs_root: str, races_root: str):
        self.runs_root, self.races_root = runs_root, races_root
        self.log_dir = os.path.join(races_root, "_launch")
        self.procs: dict[str, subprocess.Popen] = {}
        self.qm: dict[str, dict] = {}  # launch_id -> {thread, result, error}
        self.lock = threading.Lock()

    def running(self) -> list[str]:
        return ([k for k, p in self.procs.items() if p.poll() is None]
                + [k for k, q in self.qm.items() if q["thread"].is_alive()])

    def start(self, params: dict) -> dict:
        with self.lock:
            if self.running():
                raise RuntimeError(f"a race is already running (launch {self.running()[0]})")
            os.makedirs(self.log_dir, exist_ok=True)
            launch_id = time.strftime("%Y%m%d-%H%M%S")
            while (launch_id in self.procs or launch_id in self.qm
                   or os.path.exists(os.path.join(self.log_dir, launch_id + ".log"))):
                launch_id += "x"
            log_path = os.path.join(self.log_dir, launch_id + ".log")
            if params.get("backend") == "qm":
                return self._start_qm(launch_id, log_path, params)
            with open(log_path, "w") as log:
                log.write(json.dumps({"launch": launch_id, "params": params}) + "\n")
                log.flush()
                self.procs[launch_id] = subprocess.Popen(
                    race_cmd(params, self.runs_root, self.races_root), cwd=REPO, stdout=log,
                    stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, start_new_session=True,
                    env={**os.environ, "PYTHONUNBUFFERED": "1"})
            return {"launch_id": launch_id, "backend": "local", "log": os.path.relpath(log_path, REPO),
                    "params": params}

    def _start_qm(self, launch_id: str, log_path: str, params: dict) -> dict:
        launch = getattr(qm_module(), "launch_qm_race", None)  # Unavailable -> 503
        if launch is None:
            raise Unavailable("QM backend unavailable: qm/launch.py has no launch_qm_race")
        state: dict = {"result": None, "error": None}

        def run():
            try:
                state["result"] = launch(params["task"], params["agents"], expand_seeds(params["seeds"]),
                                         params["tries"], params["label"] or "qm")
                line = {"result": state["result"]}
            except Exception as e:  # noqa: BLE001
                state["error"] = f"{type(e).__name__}: {e}"
                line = {"error": state["error"]}
            with open(log_path, "a") as f:
                f.write(json.dumps(line, default=str) + "\n")

        with open(log_path, "w") as f:
            f.write(json.dumps({"launch": launch_id, "params": params}) + "\n")
        state["thread"] = threading.Thread(target=run, name=f"qm-launch-{launch_id}", daemon=True)
        self.qm[launch_id] = state
        state["thread"].start()
        return {"launch_id": launch_id, "backend": "qm", "log": os.path.relpath(log_path, REPO), "params": params}

    def status(self, launch_id: str) -> dict | None:
        log_path = os.path.join(self.log_dir, launch_id + ".log")
        if not os.path.exists(log_path):
            return None
        with open(log_path, errors="replace") as f:
            text = f.read()
        tail = text.splitlines()[-25:]
        q = self.qm.get(launch_id)
        if q is not None:
            res = q["result"]
            rid = res.get("race_id") if isinstance(res, dict) else res if isinstance(res, str) else None
            alive = q["thread"].is_alive()
            return {"launch_id": launch_id, "backend": "qm", "running": alive,
                    "exit_code": None if alive else (1 if q["error"] else 0), "known": True,
                    "race_id": rid, "error": q["error"], "result": res, "log_tail": tail}
        p = self.procs.get(launch_id)
        rc = p.poll() if p else None
        m = re.search(r"^race (\S+?): ", text, re.M)
        return {"launch_id": launch_id, "backend": "local", "running": bool(p and rc is None), "exit_code": rc,
                "known": p is not None, "race_id": m.group(1) if m else None, "log_tail": tail}


class Handler(SimpleHTTPRequestHandler):
    server_version = "RobotRace/1"

    def __init__(self, *args, runs_root: str, ui_root: str | None = None, races_root: str | None = None,
                 launcher: Launcher | None = None, renderer: Renderer | None = None, **kw):
        self.runs_root = os.path.realpath(runs_root)
        self.ui_root = os.path.realpath(ui_root or os.path.join(REPO, "ui"))
        self.races_root = os.path.realpath(races_root or os.path.join(REPO, "races"))
        self.launcher = launcher
        self.renderer = renderer
        super().__init__(*args, directory=self.runs_root, **kw)

    def log_request(self, code="-", size="-"):  # quiet: the UI polls a lot; log errors only
        if str(getattr(code, "value", code)).startswith(("4", "5")):
            super().log_request(code, size)

    # -------------------------------------------------------- plumbing
    def end_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Expose-Headers", "Content-Range, Content-Length, Accept-Ranges")
        path = urlsplit(self.path).path
        if path.startswith("/api/") or path.endswith(NO_STORE) or path.endswith("/"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(HTTPStatus.NO_CONTENT)
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Range, Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, obj, status=HTTPStatus.OK, head=False):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if not head:
            self.wfile.write(data)

    def _error(self, status, msg, head=False):
        self._json({"error": msg}, status, head)

    def do_GET(self):
        self._handle(head=False)

    def do_HEAD(self):
        self._handle(head=True)

    def do_POST(self):
        path = unquote(urlsplit(self.path).path)
        origin = self.headers.get("Origin")
        if origin and urlsplit(origin).netloc != self.headers.get("Host"):
            return self._error(HTTPStatus.FORBIDDEN, "cross-origin request refused")
        adir = self._render_target(path)
        if adir is not False:
            if adir is None:
                return self._error(HTTPStatus.NOT_FOUND, "no such attempt")
            if self.renderer is None:
                return self._error(HTTPStatus.SERVICE_UNAVAILABLE, "rendering is disabled on this server")
            try:
                st = self.renderer.start(adir)
            except RuntimeError as e:
                return self._error(HTTPStatus.TOO_MANY_REQUESTS, str(e))
            return self._json(st, HTTPStatus.ACCEPTED if st["state"] == "pending" else HTTPStatus.OK)
        if path != "/api/race":
            return self._error(HTTPStatus.NOT_FOUND, "not found")
        if self.launcher is None:
            return self._error(HTTPStatus.SERVICE_UNAVAILABLE, "launching is disabled on this server")
        try:
            n = int(self.headers.get("Content-Length") or 0)
            if n > 4096:
                raise ValueError("body too large")
            params = validate_race(json.loads(self.rfile.read(n) or b"{}"))
        except (ValueError, TypeError) as e:  # json.JSONDecodeError is a ValueError
            return self._error(HTTPStatus.BAD_REQUEST, str(e))
        try:
            return self._json(self.launcher.start(params), HTTPStatus.ACCEPTED)
        except Unavailable as e:
            return self._error(HTTPStatus.SERVICE_UNAVAILABLE, str(e))
        except RuntimeError as e:
            return self._error(HTTPStatus.CONFLICT, str(e))

    def _handle(self, head: bool):
        u = urlsplit(self.path)
        raw = unquote(u.path)
        if "\x00" in raw or any(p == ".." for p in re.split(r"[/\\]", raw)):
            return self._error(HTTPStatus.FORBIDDEN, "forbidden", head)
        q = parse_qs(u.query)
        if raw == "/api/runs" or raw.startswith("/api/runs/"):
            return self._api(raw, q, head)
        if raw == "/api/races" or raw.startswith("/api/races/"):
            return self._races(raw, u.query, head)
        if raw.startswith("/api/launch/"):
            lid = raw[len("/api/launch/"):]
            st = self.launcher.status(lid) if self.launcher and RUN_ID.match(lid) else None
            return self._json(st, head=head) if st else self._error(HTTPStatus.NOT_FOUND, "no such launch", head)
        if raw == "/api/tasks":
            return self._json({"tasks": [{"id": k, "text": v} for k, v in task_ids().items()]}, head=head)
        if raw == "/api/brain":
            p = os.path.join(REPO, "gbrain", "skills", "trash-to-bin", "SKILL.md")
            try:
                with open(p) as f:
                    md = f.read()
                return self._json({"path": os.path.relpath(p, REPO), "markdown": md,
                                   "mtime": os.path.getmtime(p)}, head=head)
            except OSError:
                return self._json({"path": None, "markdown": "", "mtime": None}, head=head)
        if raw == "/api/memory":
            try:
                limit = max(1, min(500, int(q.get("limit", ["100"])[0])))
            except ValueError:
                return self._error(HTTPStatus.BAD_REQUEST, "limit must be an int", head)
            return self._json(memory_episodes(q.get("task", [None])[0], limit), head=head)
        if raw.startswith("/api"):
            return self._error(HTTPStatus.NOT_FOUND, "not found", head)
        if raw in ("/", "/dojo", "/dojo/") and os.path.isfile(os.path.join(self.ui_root, "index.html")):
            return self._send_file(os.path.join(self.ui_root, "index.html"), head)
        self._static(raw, head)

    def _render_target(self, path: str):
        """/api/runs/<run_id>/attempt_<k>/render -> attempt dir (None if missing); False if not that route."""
        parts = [p for p in path.split("/") if p]
        if len(parts) != 5 or parts[:2] != ["api", "runs"] or parts[4] != "render":
            return False
        if not RUN_ID.match(parts[2]) or not ATTEMPT.match(parts[3]) or "\x00" in path:
            return None
        d = self._safe(f"{parts[2]}/{parts[3]}")
        return d if d and os.path.isdir(d) else None

    def _safe(self, rel: str) -> str | None:
        p = os.path.realpath(os.path.join(self.runs_root, rel.lstrip("/")))
        if p != self.runs_root and not p.startswith(self.runs_root + os.sep):
            return None
        return p

    # -------------------------------------------------------- API
    def _api(self, raw: str, q: dict, head: bool):
        parts = [p for p in raw.split("/") if p][2:]  # drop "api", "runs"
        if not parts:
            return self._json(viewer.build_manifest(self.runs_root), head=head)
        run_id = parts[0]
        run_dir = self._safe(run_id) if RUN_ID.match(run_id) else None
        if not run_dir or not os.path.isdir(run_dir):
            return self._error(HTTPStatus.NOT_FOUND, "no such run", head)
        if len(parts) == 1:
            spath = os.path.join(run_dir, "summary.json")
            if os.path.exists(spath):
                s = viewer._load_json(spath)
                if s is None:  # mid-write; client should retry
                    return self._error(HTTPStatus.SERVICE_UNAVAILABLE, "summary not readable yet", head)
                return self._json(s, head=head)
            info = viewer.run_info(run_dir)
            if not info:
                return self._error(HTTPStatus.NOT_FOUND, "no such run", head)
            attempts = [dict(dir=n, **(info["results"][n] or {})) for n in info["done"]]
            return self._json({k: info[k] for k in ("task", "seed", "model", "strategy", "status", "solved_at",
                                                    "kind", "seeds")} | {"attempts": attempts}, head=head)
        if len(parts) == 2 and parts[1] == "events":
            try:
                since = max(0, int(q.get("since", ["0"])[0]))
            except ValueError:
                return self._error(HTTPStatus.BAD_REQUEST, "since must be an int", head)
            return self._json(read_events(os.path.join(run_dir, "events.jsonl"), since), head=head)
        if len(parts) == 3 and parts[2] == "render":
            adir = self._render_target(raw)
            if not adir:
                return self._error(HTTPStatus.NOT_FOUND, "no such attempt", head)
            if self.renderer is None:
                has = os.path.exists(os.path.join(adir, "attempt.mp4"))
                return self._json({"state": "done" if has else "none", "video": "attempt.mp4" if has else None},
                                  head=head)
            return self._json(self.renderer.status(adir), head=head)
        if len(parts) == 2 and parts[1] == "transcript":
            turns = read_events(os.path.join(run_dir, "transcript.jsonl"))["events"]
            return self._json({"turns": turns}, head=head)
        return self._error(HTTPStatus.NOT_FOUND, "not found", head)

    # -------------------------------------------------------- races (racetrack + races/<id>/)
    def _local_races(self) -> list[dict]:
        try:
            names = os.listdir(self.races_root)
        except OSError:
            return []
        out = []
        for n in names:
            d = os.path.join(self.races_root, n)
            if n.startswith(("_", ".")) or not os.path.isdir(d) or not RUN_ID.match(n):
                continue
            plan = _read_json(os.path.join(d, "plan.json")) or {}
            race = _read_json(os.path.join(d, "race.json")) or {}
            launch = _read_json(os.path.join(d, "launch.json")) or {}
            # the dir mtime moves whenever post-race files land, so date a race by its first file instead
            born = [os.path.getmtime(os.path.join(d, f)) for f in ("plan.json", "launch.json", "context.md")
                    if os.path.exists(os.path.join(d, f))]
            out.append({"race_id": n, "task": race.get("task") or plan.get("task_id") or launch.get("task"),
                        "label": race.get("label") or launch.get("label"),
                        "backend": launch.get("backend") or "local",
                        "created": launch.get("started") or (min(born) if born else os.path.getmtime(d)),
                        "done": os.path.exists(os.path.join(d, "race.json")),
                        "closed": os.path.exists(os.path.join(d, "close.json")),
                        "agents": len(plan.get("strategies") or [])})
        return out

    def _races(self, raw: str, query: str, head: bool):
        parts = [p for p in raw.split("/") if p][2:]  # drop "api", "races"
        if not parts:
            tracked = tracker_json("/races")
            by = {r["race_id"]: {"race_id": r["race_id"], "local": r} for r in self._local_races()}
            for r in tracked or []:
                by.setdefault(r["race_id"], {"race_id": r["race_id"], "local": None})["tracker"] = r
            rows = sorted(by.values(), key=lambda r: (r.get("tracker") or {}).get("created_at")
                          or (r.get("local") or {}).get("created") or 0, reverse=True)
            return self._json({"tracker": TRACKER_URL if tracked is not None else None, "races": rows}, head=head)
        race_id = parts[0]
        if not RUN_ID.match(race_id):
            return self._error(HTTPStatus.NOT_FOUND, "no such race", head)
        if parts[1:] == ["qm"]:
            return self._json(qm_status(race_id), head=head)
        if len(parts) > 1:  # proxy to the tracker
            if not all(RUN_ID.match(p) for p in parts[1:]):
                return self._error(HTTPStatus.NOT_FOUND, "not found", head)
            status, ctype, data = tracker_get("/races/" + "/".join(parts) + (f"?{query}" if query else ""), 5)
            if status is None:
                return self._error(HTTPStatus.BAD_GATEWAY, f"tracker not reachable at {TRACKER_URL}", head)
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if not head:
                self.wfile.write(data)
            return
        d = os.path.join(self.races_root, race_id)
        out: dict = {"race_id": race_id, "local": os.path.isdir(d)}
        for key, fname in RACE_FILES.items():
            out[key] = _read_json(os.path.join(d, fname))
        try:
            with open(os.path.join(d, "context.md")) as f:
                out["context"] = f.read()
        except OSError:
            out["context"] = None
        out["runs"] = race_runs(self.runs_root, race_id)
        out["tracker"] = tracker_json(f"/races/{race_id}")
        out["leaderboard"] = tracker_json(f"/races/{race_id}/leaderboard") if out["tracker"] else None
        out["attempts"] = tracker_json(f"/races/{race_id}/attempts") if out["tracker"] else None
        if not out["local"] and not out["tracker"] and not out["runs"]:
            return self._error(HTTPStatus.NOT_FOUND, "no such race", head)
        return self._json(out, head=head)

    # -------------------------------------------------------- static files with Range
    def _static(self, raw: str, head: bool):
        path = self._safe(raw)
        if path is None:
            return self._error(HTTPStatus.FORBIDDEN, "forbidden", head)
        if not os.path.exists(path):  # UI assets (sponsors/...) live next to ui/index.html
            alt = os.path.realpath(os.path.join(self.ui_root, raw.lstrip("/")))
            if alt.startswith(self.ui_root + os.sep) and os.path.isfile(alt):
                path = alt
        if os.path.isdir(path):
            if not raw.endswith("/"):
                self.send_response(HTTPStatus.MOVED_PERMANENTLY)
                self.send_header("Location", raw + "/")
                self.send_header("Content-Length", "0")
                return self.end_headers()
            idx = os.path.join(path, "index.html")
            if not os.path.isfile(idx):
                return self._error(HTTPStatus.NOT_FOUND, "not found", head)
            path = idx
        self._send_file(path, head)

    def _send_file(self, path: str, head: bool):
        try:
            f = open(path, "rb")
        except OSError:
            return self._error(HTTPStatus.NOT_FOUND, "not found", head)
        with f:
            size = os.fstat(f.fileno()).st_size
            ctype = MIME.get(os.path.splitext(path)[1].lower()) or mimetypes.guess_type(path)[0] \
                or "application/octet-stream"
            start, end, status = 0, size - 1, HTTPStatus.OK
            rng = self.headers.get("Range")
            if rng:
                try:
                    r = parse_range(rng, size)
                except ValueError:  # not a bytes range: ignore it, send the whole file
                    r, rng = None, None
                if rng and r is None:
                    self.send_response(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE)
                    self.send_header("Content-Range", f"bytes */{size}")
                    self.send_header("Content-Length", "0")
                    return self.end_headers()
                if rng:
                    start, end, status = r[0], r[1], HTTPStatus.PARTIAL_CONTENT
            length = max(0, end - start + 1)
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(length))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Last-Modified", self.date_time_string(int(os.fstat(f.fileno()).st_mtime)))
            if status == HTTPStatus.PARTIAL_CONTENT:
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.end_headers()
            if head or length == 0:
                return
            f.seek(start)
            left = length
            try:
                while left > 0:
                    chunk = f.read(min(64 * 1024, left))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):  # browsers abort video requests all the time
                pass


def read_events(path: str, since: int = 0) -> dict:
    """Complete lines of events.jsonl from line `since` (0-based). A trailing line without a newline is
    still being written: it's left for the next poll. Unparseable complete lines are skipped (but counted)."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return {"events": [], "next": since}
    lines = data.split(b"\n")[:-1]  # last element is "" or a partial line
    events = []
    for ln in lines[since:]:
        try:
            if ln.strip():
                events.append(json.loads(ln))
        except ValueError:
            pass
    return {"events": events, "next": max(since, len(lines))}


def make_server(runs_root: str = "runs", host: str = "127.0.0.1", port: int = 8080, ui_root: str | None = None,
                races_root: str | None = None, launch: bool = True) -> ThreadingHTTPServer:
    os.makedirs(runs_root, exist_ok=True)
    races_root = os.path.abspath(races_root or os.path.join(REPO, "races"))
    launcher = Launcher(os.path.abspath(runs_root), races_root) if launch else None
    renderer = Renderer() if launch else None  # --no-launch = read-only viewer: no subprocesses at all
    srv = ThreadingHTTPServer((host, port), partial(Handler, runs_root=runs_root, ui_root=ui_root,
                                                    races_root=races_root, launcher=launcher, renderer=renderer))
    srv.daemon_threads = True
    return srv


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Serve runs/ with Range, CORS and a small JSON API.")
    ap.add_argument("--runs", default="runs")
    ap.add_argument("--races", default=os.path.join(REPO, "races"))
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-launch", action="store_true", help="disable POST /api/race and renders (read-only viewer)")
    a = ap.parse_args(argv)
    srv = make_server(a.runs, a.host, a.port, races_root=a.races, launch=not a.no_launch)
    print(f"dojo at http://{a.host}:{srv.server_address[1]}/  (runs: {os.path.abspath(a.runs)}, "
          f"tracker: {TRACKER_URL}, launch: {'off' if a.no_launch else 'on'})")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
