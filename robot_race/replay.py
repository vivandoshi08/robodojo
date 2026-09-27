"""Re-render a recorded attempt (trajectory.npz) from any camera, at any resolution, as frames or MP4.

The scene is deterministic given (task, seed), so replaying qpos reproduces the attempt exactly.
CLI: python -m robot_race.replay runs/<id>/attempt_<k> --width 1920 --height 1080 --fps 60 --shadows
"""
from __future__ import annotations

import argparse
import os
import time
from typing import Iterable, Iterator

import mujoco
import numpy as np

from .tasks import Env


def _nearest(t: np.ndarray, times) -> np.ndarray:
    """Index of the trajectory sample closest to each requested time."""
    i = np.clip(np.searchsorted(t, times), 1, len(t) - 1)
    return np.where(np.abs(t[i - 1] - times) <= np.abs(t[i] - times), i - 1, i)


def iter_frames(task: str, seed: int, t, qpos, times, width: int, height: int,
                camera: str = "front", shadows: bool = False) -> Iterator[np.ndarray]:
    """Yields one HxWx3 uint8 frame per requested time (streams: no big buffers for HD video)."""
    t, qpos = np.asarray(t), np.asarray(qpos)
    env = Env(task, seed, record=False)
    m, d = env.model, env.data
    r = mujoco.Renderer(m, height, width)
    try:
        idx = [0] * len(times) if len(t) == 1 else _nearest(t, np.asarray(times, float))
        for i in idx:
            d.qpos[:] = qpos[i]
            mujoco.mj_forward(m, d)
            r.update_scene(d, camera=camera)
            r.scene.flags[mujoco.mjtRndFlag.mjRND_SHADOW] = shadows
            yield r.render()  # fresh array per call
    finally:
        r.close()


def render_frames(task: str, seed: int, t, qpos, times, width: int, height: int,
                  camera: str = "front", shadows: bool = False) -> list[np.ndarray]:
    return list(iter_frames(task, seed, t, qpos, times, width, height, camera, shadows))


def write_mp4(frames: Iterable[np.ndarray], path: str, fps: float) -> str:
    """H.264 yuv420p +faststart (browser-playable). Written to a tmp name, then renamed. Returns path."""
    import imageio_ffmpeg
    tmp, writer = path + ".tmp", None
    try:
        for f in frames:
            if writer is None:
                h, w = f.shape[:2]
                if w % 2 or h % 2:
                    raise ValueError(f"width/height must be even for yuv420p, got {w}x{h}")
                writer = imageio_ffmpeg.write_frames(
                    tmp, (w, h), fps=fps, codec="libx264", pix_fmt_out="yuv420p", quality=None,
                    macro_block_size=1, ffmpeg_log_level="error",
                    output_params=["-crf", "20", "-preset", "veryfast", "-movflags", "+faststart", "-f", "mp4"])
                writer.send(None)
            writer.send(np.ascontiguousarray(f))
        if writer is None:
            raise ValueError("no frames")
        writer.close()
        writer = None
        os.replace(tmp, path)
    finally:
        if writer is not None:
            writer.close()
        if os.path.exists(tmp):
            os.remove(tmp)
    return path


def video_times(t: np.ndarray, fps: float) -> np.ndarray:
    """Frame times from 0 to the end of the trajectory, always including the last sample."""
    end = float(t[-1])
    ts = np.arange(0.0, end, 1.0 / fps)
    return np.append(ts, end)


def load_trajectory(attempt_dir: str):
    z = np.load(os.path.join(attempt_dir, "trajectory.npz"))
    return str(z["task"]), int(z["seed"]), z["t"], z["qpos"]


def render_attempt(attempt_dir: str, width: int = 640, height: int = 480, fps: float = 30,
                   camera: str = "front", shadows: bool = False, out_name: str = "attempt.mp4") -> str:
    """Renders attempt_dir/trajectory.npz to attempt_dir/out_name. Returns the mp4 path."""
    task, seed, t, qpos = load_trajectory(attempt_dir)
    frames = iter_frames(task, seed, t, qpos, video_times(t, fps), width, height, camera, shadows)
    return write_mp4(frames, os.path.join(attempt_dir, out_name), fps)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("attempt_dir")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
    ap.add_argument("--fps", type=float, default=60)
    ap.add_argument("--camera", default="front")
    ap.add_argument("--shadows", action="store_true")
    ap.add_argument("--out", default=None, help="output file name inside attempt_dir")
    a = ap.parse_args()
    out = a.out or ("attempt.mp4" if (a.width, a.height) == (640, 480) else "attempt_hd.mp4")
    t0 = time.time()
    path = render_attempt(a.attempt_dir, a.width, a.height, a.fps, a.camera, a.shadows, out)
    print(f"{path}  {a.width}x{a.height} @ {a.fps:g} fps  ({time.time() - t0:.1f}s)")


if __name__ == "__main__":
    main()
