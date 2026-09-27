"""Runs one attempt (policy code string) in a subprocess -> out_dir/result.json + media (CLAUDE.md §8).

Parent: run_policy() writes policy.py, spawns the worker with a wall-clock timeout, reads result.json.
Worker (python -m robot_race.executor): sim without video, then renders key frames / poster / mp4
from the recorded trajectory (replay.py).
"""
from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
import time
import traceback

import numpy as np

from .interfaces import RESULT_KEYS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY_SIZE, POSTER_SIZE, VIDEO_SIZE, VIDEO_FPS = (320, 240), (640, 480), (640, 480), 30


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
                trajectory=has("trajectory.npz"), live=has("live.jpg"))


def _fill(res: dict, task: str, seed: int, out_dir: str, code_path: str) -> dict:
    """Ensures every contract key is present (unknown physics = None)."""
    base = {k: None for k in RESULT_KEYS}
    base.update(task=task, seed=seed, success=False, frames=[], code_path=code_path,
                item_final_pos=None, calls=[], wall_s=None)
    base.update(res)
    base["media"] = res.get("media") or _media(out_dir)
    if not base["frames"]:
        base["frames"] = [os.path.join(out_dir, k) for k in base["media"]["keyframes"]]
    if base["video"] is None and base["media"]["video"]:
        base["video"] = os.path.join(out_dir, base["media"]["video"])
    return base


def run_policy(code: str, task: str, seed: int, out_dir: str, timeout_s: float = 180,
               fast: bool = False, live: bool = True) -> dict:
    """Runs `code` (must define run(robot)) on (task, seed) in a subprocess. Always returns a full result dict."""
    out_dir = os.path.abspath(out_dir)
    os.makedirs(out_dir, exist_ok=True)
    code_path = os.path.join(out_dir, "policy.py")
    with open(code_path, "w") as f:
        f.write(code)
    res_path = os.path.join(out_dir, "result.json")
    if os.path.exists(res_path):
        os.remove(res_path)
    cmd = [sys.executable, "-m", "robot_race.executor", "--task", task, "--seed", str(seed),
           "--code", code_path, "--out", out_dir] + (["--fast"] if fast else []) + ([] if live else ["--no-live"])
    t0, err = time.time(), None
    try:
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=timeout_s)
        if p.returncode != 0 or not os.path.exists(res_path):
            tail = "\n".join((p.stderr or p.stdout or "").strip().splitlines()[-6:])
            err = f"worker exited with code {p.returncode}: {tail}"
    except subprocess.TimeoutExpired:
        err = f"timeout after {timeout_s:g}s"
    if err is None:
        try:
            with open(res_path) as f:
                return _fill(json.load(f), task, seed, out_dir, code_path)
        except (OSError, ValueError) as e:
            err = f"unreadable result.json: {e!r}"
    res = _fill(dict(error=err, wall_s=round(time.time() - t0, 2)), task, seed, out_dir, code_path)
    _write_json(res_path, res)
    return res


# ---------------- worker ----------------
def _render_media(task, seed, t, qpos, out_dir, fast) -> None:
    from PIL import Image
    from . import replay
    end = float(t[-1])
    keys = replay.render_frames(task, seed, t, qpos, [0, end / 3, 2 * end / 3, end], *KEY_SIZE)
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


def worker(task: str, seed: int, code_path: str, out_dir: str, fast: bool = False, live: bool = True) -> dict:
    from .robot import SimRobot
    from .tasks import Env
    t0 = time.time()
    env = Env(task, seed, record=False, live_path=os.path.join(out_dir, "live.jpg") if live else None)
    robot, error = SimRobot(env), None
    try:
        with open(code_path) as f:
            code = f.read()
        ns = {"robot": robot, "np": np, "math": math, "__name__": "policy"}
        exec(compile(code, "policy.py", "exec"), ns)
        if not callable(ns.get("run")):
            raise NameError("policy code must define run(robot)")
        ns["run"](robot)
    except BaseException as e:  # noqa: BLE001  (policy code may raise anything, incl. SystemExit)
        if isinstance(e, KeyboardInterrupt):
            raise
        error = "\n".join(traceback.format_exc().strip().splitlines()[-6:])
    env.settle(1.0)
    res = env.result()
    env.close()
    t, qpos = env.trajectory()
    tmp = os.path.join(out_dir, "trajectory.tmp.npz")
    np.savez_compressed(tmp, t=t, qpos=qpos, task=task, seed=seed)
    os.replace(tmp, os.path.join(out_dir, "trajectory.npz"))
    try:
        _render_media(task, seed, t, qpos, out_dir, fast)
    except Exception:
        traceback.print_exc()  # media stays null; the physics result is still valid
    media = _media(out_dir)
    res.update(error=error, calls=robot.call_log, code_path=code_path, media=media,
               frames=[os.path.join(out_dir, k) for k in media["keyframes"]],
               video=os.path.join(out_dir, media["video"]) if media["video"] else None,
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
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    res = worker(a.task, a.seed, a.code, a.out, a.fast, not a.no_live)
    print(json.dumps({k: res[k] for k in ("task", "seed", "success", "time_s", "error", "wall_s")}))


if __name__ == "__main__":
    main()
