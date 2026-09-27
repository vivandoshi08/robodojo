"""Runs one attempt (policy code string) in a subprocess -> out_dir/result.json + media (CLAUDE.md §8).

Parent: run_policy() writes policy.py, spawns the worker with a wall-clock timeout, reads result.json.
Worker (python -m robot_race.executor): sim without video, then renders key frames / poster / mp4
from the recorded trajectory (replay.py). Provenance: calls.json (every robot call with sim times) and
provenance.json (sha256 of the code that ran vs. the code in the model reply, versions, timing).
Video time == sim time: frame i of attempt.mp4 shows sim time i / VIDEO_FPS (see replay.video_times).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import re
import subprocess
import sys
import time
import traceback

import numpy as np

from .interfaces import RESULT_KEYS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MISMATCH_ERROR = "code does not match model reply"  # policy.py sha256 != response_code_sha256: nothing ran
KEY_SIZE, POSTER_SIZE, VIDEO_SIZE, VIDEO_FPS = (320, 240), (640, 480), (640, 480), 30


def sha256_text(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def scrub_paths(text):
    """Remove local absolute paths from tracebacks/stderr (result.json etc. are public)."""
    if not text:
        return text
    text = text.replace(ROOT + os.sep, "")
    text = re.sub(r"""(?:[A-Za-z]:)?[/\\][^\s"'<>]*?[/\\]site-packages[/\\]""", "<site-packages>/", text)
    home = os.path.expanduser("~")
    return text.replace(home, "~") if home not in ("", "/", "~") else text


def _write_json(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2)
    os.replace(tmp, path)


def _media(out_dir: str) -> dict:
    """Relative paths of the media that exist in out_dir (missing = None)."""
    has = lambda n: n if os.path.exists(os.path.join(out_dir, n)) else None
    keys = [n for n in (f"key_{i}.png" for i in range(4)) if has(n)]
    return dict(video=has("attempt.mp4"), poster=has("poster.jpg"), keyframes=keys,
                trajectory=has("trajectory.npz"), live=has("live.jpg"), calls_json=has("calls.json"),
                provenance=has("provenance.json"))


def _fill(res: dict, task: str, seed: int, out_dir: str, code_path: str) -> dict:
    """Ensures every contract key is present (unknown physics = None)."""
    base = {k: None for k in RESULT_KEYS}
    base.update(task=task, seed=seed, success=False, frames=[], code_path=os.path.basename(code_path),
                item_final_pos=None, calls=[], wall_s=None)
    base.update(res)
    base["media"] = res.get("media") or _media(out_dir)
    if not base["frames"]:
        base["frames"] = list(base["media"]["keyframes"])
    if base["video"] is None and base["media"]["video"]:
        base["video"] = base["media"]["video"]
    return base


def run_policy(code: str, task: str, seed: int, out_dir: str, timeout_s: float = 180,
               fast: bool = False, live: bool = True, response_code_sha256: str | None = None) -> dict:
    """Runs `code` (must define run(robot)) on (task, seed) in a subprocess. Always returns a full result dict.
    response_code_sha256: sha256 of the code block extracted from the model reply. provenance.json records
    whether the code that ran matches it, and the worker REFUSES to run policy.py if it doesn't
    (result error = MISMATCH_ERROR)."""
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    code_path = os.path.join(out_dir, "policy.py")
    with open(code_path, "wb") as f:  # bytes, no newline translation: sha256(policy.py) == sha256(code)
        f.write(code.encode("utf-8"))
    res_path = os.path.join(out_dir, "result.json")
    for stale in ("result.json", "calls.json", "provenance.json"):
        if os.path.exists(os.path.join(out_dir, stale)):
            os.remove(os.path.join(out_dir, stale))
    cmd = [sys.executable, "-m", "robot_race.executor", "--task", task, "--seed", str(seed),
           "--code", code_path, "--out", out_dir] + (["--fast"] if fast else []) + ([] if live else ["--no-live"]) \
        + (["--response-sha256", response_code_sha256] if response_code_sha256 else [])
    t0, err = time.time(), None
    try:
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout_s)
        if p.returncode != 0 or not os.path.exists(res_path):
            tail = "\n".join((p.stderr or p.stdout or "").strip().splitlines()[-6:])
            err = scrub_paths(f"worker exited with code {p.returncode}: {tail}")
    except subprocess.TimeoutExpired:
        err = f"timeout after {timeout_s:g}s"
    t1 = time.time()
    if err is None:
        try:
            with open(res_path) as f:
                return _fill(json.load(f), task, seed, out_dir, code_path)
        except (OSError, ValueError) as e:
            err = f"unreadable result.json: {e!r}"
    if not os.path.exists(os.path.join(out_dir, "provenance.json")):  # worker died before writing it
        with open(code_path, "rb") as f:
            ran = hashlib.sha256(f.read()).hexdigest()
        _write_json(os.path.join(out_dir, "provenance.json"), _provenance(
            task, seed, ran, response_code_sha256, t0, t1, error=err, completed=False))
    res = _fill(dict(error=err, wall_s=round(t1 - t0, 2)), task, seed, out_dir, code_path)
    _write_json(res_path, res)
    return res


def _provenance(task, seed, policy_sha, response_sha, t0, t1, completed=True, **extra) -> dict:
    """Everything needed to audit an attempt: which exact code ran, on what, with which versions."""
    import mujoco
    from .tasks import EPISODE_LIMIT_S
    from .robot import CONTROL_DT
    return dict(task=task, seed=seed, policy_file="policy.py", policy_sha256=policy_sha,
                response_code_sha256=response_sha,
                code_matches_response=(policy_sha == response_sha) if response_sha else None,
                completed=completed, mujoco_version=mujoco.__version__, python_version=platform.python_version(),
                numpy_version=np.__version__, control_dt=CONTROL_DT, episode_limit_s=EPISODE_LIMIT_S,
                wall_started=round(t0, 3), wall_finished=round(t1, 3), wall_s=round(t1 - t0, 3), **extra)


# ---------------- worker ----------------
def keyframe_times(t) -> list[float]:
    end = float(t[-1])
    return [0.0, end / 3, 2 * end / 3, end]


def _render_media(task, seed, t, qpos, out_dir, fast) -> None:
    from PIL import Image
    from . import replay
    end = float(t[-1])
    keys = replay.render_frames(task, seed, t, qpos, keyframe_times(t), *KEY_SIZE)
    for i, img in enumerate(keys):
        tmp = os.path.join(out_dir, f"key_{i}.tmp.png")
        Image.fromarray(img).save(tmp)
        os.replace(tmp, os.path.join(out_dir, f"key_{i}.png"))
    poster = replay.render_frames(task, seed, t, qpos, [end], *POSTER_SIZE)[0]
    tmp = os.path.join(out_dir, "poster.tmp.jpg")
    Image.fromarray(poster).save(tmp, quality=90)
    os.replace(tmp, os.path.join(out_dir, "poster.jpg"))
    if not fast:
        frames = replay.iter_frames(task, seed, t, qpos, replay.video_times(t, VIDEO_FPS), *VIDEO_SIZE)
        replay.write_mp4(frames, os.path.join(out_dir, "attempt.mp4"), VIDEO_FPS)


def worker(task: str, seed: int, code_path: str, out_dir: str, fast: bool = False, live: bool = True,
           response_sha256: str | None = None) -> dict:
    from . import replay
    from .robot import SimRobot
    from .tasks import Env
    t0 = time.time()
    with open(code_path, "rb") as f:
        code_bytes = f.read()
    policy_sha = hashlib.sha256(code_bytes).hexdigest()
    if response_sha256 and policy_sha != response_sha256:
        # Provenance guard: only the exact code block from the model reply may drive the robot.
        # Checked before the scene is even built: a mismatched file never touches the sim.
        err = f"{MISMATCH_ERROR} (policy.py sha256 {policy_sha[:12]} != reply {response_sha256[:12]}): refused to run"
        _write_json(os.path.join(out_dir, "provenance.json"), _provenance(
            task, seed, policy_sha, response_sha256, t0, time.time(), completed=False, refused=True, error=err))
        res = _fill(dict(error=err, wall_s=round(time.time() - t0, 2)), task, seed, out_dir, code_path)
        _write_json(os.path.join(out_dir, "result.json"), res)
        return res
    env = Env(task, seed, record=False, live_path=os.path.join(out_dir, "live.jpg") if live else None)
    robot, error, exc = SimRobot(env), None, None
    try:
        code = code_bytes.decode("utf-8")  # exactly these bytes are compiled and hashed
        ns = {"robot": robot, "np": np, "math": math, "__name__": "policy"}
        exec(compile(code, "policy.py", "exec"), ns)
        if not callable(ns.get("run")):
            raise NameError("policy code must define run(robot)")
        ns["run"](robot)
    except BaseException as e:  # noqa: BLE001  (policy code may raise anything, incl. SystemExit)
        if isinstance(e, KeyboardInterrupt):
            raise
        error = scrub_paths("\n".join(traceback.format_exc().strip().splitlines()[-6:]))
        exc = dict(type=type(e).__name__, message=scrub_paths(str(e)), sim_time=round(env.sim_time, 4),
                   in_call=next((c["i"] for c in reversed(robot.call_trace) if c.get("error")), None),
                   traceback=error)
    t_policy_end = round(env.sim_time, 4)
    env.settle(1.0)
    res = env.result()
    env.close()
    t, qpos = env.trajectory()
    tmp = os.path.join(out_dir, "trajectory.tmp.npz")
    np.savez_compressed(tmp, t=t, qpos=qpos, task=task, seed=seed)
    os.replace(tmp, os.path.join(out_dir, "trajectory.npz"))
    _write_json(os.path.join(out_dir, "calls.json"), dict(
        time_unit="sim seconds since episode start (== attempt.mp4 time)", calls=robot.call_trace,
        exception=exc, policy_t_end=t_policy_end, episode_t_end=round(float(t[-1]), 4)))
    try:
        _render_media(task, seed, t, qpos, out_dir, fast)
    except Exception:
        traceback.print_exc()  # media stays null; the physics result is still valid
    vt = replay.video_times(t, VIDEO_FPS)
    has_mp4 = os.path.exists(os.path.join(out_dir, "attempt.mp4"))
    _write_json(os.path.join(out_dir, "provenance.json"), _provenance(
        task, seed, policy_sha, response_sha256, t0, time.time(), refused=False,
        sim_timestep=float(env.model.opt.timestep), trajectory_samples=int(len(t)),
        trajectory_t_range=[round(float(t[0]), 4), round(float(t[-1]), 4)],
        keyframe_times=[round(x, 4) for x in keyframe_times(t)],
        video=dict(file="attempt.mp4", fps=VIDEO_FPS, n_frames=int(len(vt)),
                   frame_time="frame i shows sim time i/fps (nearest trajectory sample); last frame = end")
        if has_mp4 else None, error=error))
    media = _media(out_dir)
    # Paths are relative to out_dir: result.json is served to a public site, so no local absolute paths.
    res.update(error=error, calls=robot.call_log, code_path=os.path.basename(code_path), media=media,
               frames=list(media["keyframes"]), video=media["video"],
               wall_s=round(time.time() - t0, 2))
    res = _fill(res, task, seed, out_dir, code_path)
    _write_json(os.path.join(out_dir, "result.json"), res)
    return res


def main():
    ap = argparse.ArgumentParser(description="Run one policy attempt (worker).")
    ap.add_argument("--task", required=True)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--code", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--fast", action="store_true", help="no mp4: trajectory + key frames + poster only")
    ap.add_argument("--no-live", action="store_true")
    ap.add_argument("--response-sha256", default=None, help="sha256 of the code block in the model reply")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    res = worker(a.task, a.seed, a.code, a.out, a.fast, not a.no_live, a.response_sha256)
    print(json.dumps({k: res[k] for k in ("task", "seed", "success", "time_s", "error", "wall_s")}))


if __name__ == "__main__":
    main()
