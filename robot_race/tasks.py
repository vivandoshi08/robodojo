"""Tasks, the sim episode (Env), success check and metrics.

Env owns the MuJoCo model/data, steps physics, records video frames and accumulates metrics.
SimRobot (robot.py) drives it.
"""
from __future__ import annotations

import os

import mujoco
import numpy as np

from . import scene

TASKS = {
    "can_to_bin":      dict(item="can",    text="Put the red soda can into the green trash bin."),
    "bottle_to_bin":   dict(item="bottle", text="Put the blue bottle (lying on its side) into the green trash bin."),
    "paper_to_bin":    dict(item="paper",  text="Put the crumpled paper ball into the green trash bin."),
    "box_to_bin":      dict(item="box",    text="Put the takeout box into the green trash bin."),
    # Variant for the memory demo: same skill, bin moved further away.
    "can_to_far_bin":  dict(item="can",    text="Put the red soda can into the green trash bin.", bin_center=(0.2, 0.55)),
}

EPISODE_LIMIT_S = 40.0
VIDEO_SIZE = (320, 240)
# Rendering is the slow part on CPU-only machines (~80 ms/frame without shadows, ~290 ms with).
# Laptops with a GPU can afford RR_SHADOWS=1 for nicer demo videos.
DEFAULT_FPS = float(os.environ.get("RR_VIDEO_FPS", "12"))
SHADOWS = os.environ.get("RR_SHADOWS", "0") == "1"


class TimeLimit(Exception):
    pass


class Env:
    def __init__(self, task: str, seed: int = 0, record: bool = True, video_view: str = "front",
                 video_fps: float | None = None, live_path: str | None = None, live_hz: float = 2.0):
        """record=False: no frames at all (fastest, for scoring only).
        video_fps: frames per sim-second; use ~0.5 for key frames only (fast races).
        live_path: if set, a 320x240 front-camera JPEG is written there atomically at ~live_hz (sim time)."""
        if task not in TASKS:
            raise KeyError(f"unknown task {task!r}; choose from {list(TASKS)}")
        self.task, self.seed, self.cfg = task, seed, TASKS[task]
        rng = np.random.default_rng(seed)
        item = self.cfg["item"]
        tc, th, tz = scene.TABLE["center"], scene.TABLE["half"], scene.TABLE["height"]
        xy = [tc[0] + rng.uniform(-0.1, 0.1), tc[1] + rng.uniform(-0.12, 0.08)]
        yaw = rng.uniform(-np.pi / 2, np.pi / 2) if item in ("box", "bottle") else 0.0
        z = tz + scene.item_half_height(item) + 0.002
        self.model, self.layout = scene.build_model(item, [xy[0], xy[1], z], yaw, self.cfg.get("bin_center"))
        self.data = mujoco.MjData(self.model)
        m = self.model

        self.arm_qadr = np.array([m.joint(f"joint{i}").qposadr[0] for i in range(1, 8)])
        self.arm_dadr = np.array([m.joint(f"joint{i}").dofadr[0] for i in range(1, 8)])
        self.tcp_id = m.site("tcp").id
        self.item_body = m.body("item").id
        self.item_geom = m.geom("item").id
        robot_root = m.body("link0").id
        self.robot_geoms = {g for g in range(m.ngeom) if self._in_subtree(m.geom_bodyid[g], robot_root)}
        self.finger_geoms = {g for g in range(m.ngeom)
                             if m.geom_bodyid[g] in (m.body("left_finger").id, m.body("right_finger").id)}
        env_names = ["floor", "table", "bin_floor"] + [f"bin_wall{i}" for i in range(4)]
        self.env_geoms = {m.geom(n).id for n in env_names}

        # Home pose, gripper open, let the item settle (not recorded, not scored).
        self.data.qpos[self.arm_qadr] = scene.HOME_Q
        self.data.ctrl[:7] = scene.HOME_Q
        self.data.ctrl[7] = 255
        mujoco.mj_forward(m, self.data)
        for _ in range(250):
            mujoco.mj_step(m, self.data)
        self.t0 = self.data.time

        self.record, self.video_view = record, video_view
        self.video_fps = DEFAULT_FPS if video_fps is None else video_fps
        self.renderer = mujoco.Renderer(m, VIDEO_SIZE[1], VIDEO_SIZE[0]) if record else None
        self.frames: list[np.ndarray] = []
        self._next_frame_t = 0.0
        self.collisions, self.energy_j, self.max_item_z = 0, 0.0, self.item_pos()[2]
        self._touching: set = set()
        self._traj_t: list[float] = []
        self._traj_q: list[np.ndarray] = []
        self.live_path, self.live_hz = live_path, live_hz
        self._live_renderer = mujoco.Renderer(m, VIDEO_SIZE[1], VIDEO_SIZE[0]) if live_path else None
        self._next_live_t = 0.0
        self._log()
        self._frame()

    # ---------- helpers ----------
    def _in_subtree(self, body, root):
        while body != 0:
            if body == root:
                return True
            body = self.model.body_parentid[body]
        return root == 0

    @property
    def sim_time(self):
        return self.data.time - self.t0

    def item_pos(self):
        return self.data.xpos[self.item_body].copy()

    def item_yaw_deg(self):
        R = self.data.xmat[self.item_body].reshape(3, 3)
        axis = R[:, 2] if self.layout["item"]["lying"] else R[:, 0]  # long axis for lying items, x for box
        return float(np.degrees(np.arctan2(axis[1], axis[0])))

    def tcp_pos(self):
        return self.data.site_xpos[self.tcp_id].copy()

    def holding(self) -> bool:
        touching = set()
        for c in self.data.contact[: self.data.ncon]:
            pair = {c.geom1, c.geom2}
            if self.item_geom in pair:
                other = (pair - {self.item_geom}) or {self.item_geom}
                g = next(iter(other))
                if g in self.finger_geoms:
                    touching.add(self.model.geom_bodyid[g])
        return len(touching) == 2

    def _frame(self, force=False):
        if not self.record:
            return
        if force or self.sim_time >= self._next_frame_t:
            self.renderer.update_scene(self.data, camera=self.video_view)
            self.renderer.scene.flags[mujoco.mjtRndFlag.mjRND_SHADOW] = SHADOWS
            self.frames.append(self.renderer.render().copy())
            self._next_frame_t = self.sim_time + 1.0 / max(self.video_fps, 1e-3)

    def _log(self):
        """Trajectory sample (called every control step, ~50 Hz) + optional live frame."""
        self._traj_t.append(self.sim_time)
        self._traj_q.append(self.data.qpos.copy())
        if self.live_path and self.sim_time >= self._next_live_t:
            self._next_live_t = self.sim_time + 1.0 / max(self.live_hz, 1e-3)
            self._write_live()

    def _write_live(self):
        from PIL import Image
        r = self._live_renderer
        r.update_scene(self.data, camera="front")
        r.scene.flags[mujoco.mjtRndFlag.mjRND_SHADOW] = SHADOWS
        tmp = self.live_path + ".tmp"
        Image.fromarray(r.render()).save(tmp, format="JPEG", quality=80)
        os.replace(tmp, self.live_path)

    def trajectory(self) -> tuple[np.ndarray, np.ndarray]:
        """(t [T], qpos [T, nq]) sampled every step()/settle() chunk."""
        return np.array(self._traj_t), np.array(self._traj_q)

    def render(self, view="front", width=320, height=240):
        r = mujoco.Renderer(self.model, height, width)
        r.update_scene(self.data, camera=view)
        r.scene.flags[mujoco.mjtRndFlag.mjRND_SHADOW] = SHADOWS
        img = r.render().copy()
        r.close()
        return img

    def close(self):
        if self.renderer is not None:
            self.renderer.close()
            self.renderer = None
        if self._live_renderer is not None:
            self._live_renderer.close()
            self._live_renderer = None

    # ---------- stepping + metrics ----------
    def step(self, n: int = 10):
        m, d = self.model, self.data
        for _ in range(n):
            mujoco.mj_step(m, d)
        dt = n * m.opt.timestep
        self.energy_j += float(np.sum(np.abs(d.actuator_force[:7] * d.actuator_velocity[:7]))) * dt
        self.max_item_z = max(self.max_item_z, self.item_pos()[2])
        now = set()
        for c in d.contact[: d.ncon]:
            a, b = c.geom1, c.geom2
            if (a in self.robot_geoms and b in self.env_geoms) or (b in self.robot_geoms and a in self.env_geoms):
                now.add((min(a, b), max(a, b)))
        self.collisions += len(now - self._touching)  # count new contacts only
        self._touching = now
        self._log()
        self._frame()
        if self.sim_time > EPISODE_LIMIT_S:
            raise TimeLimit(f"episode exceeded {EPISODE_LIMIT_S:.0f} s of sim time")

    # ---------- scoring ----------
    def item_in_bin(self) -> bool:
        p, b = self.item_pos(), self.layout["bin"]
        h = b["inner_half_size"]
        return abs(p[0] - b["center"][0]) < h and abs(p[1] - b["center"][1]) < h and p[2] < b["rim_height"]

    def settle(self, seconds=1.0):
        """Let physics run after the policy ends, ignoring the time limit."""
        n = int(seconds / (10 * self.model.opt.timestep))
        for _ in range(n):
            for _ in range(10):
                mujoco.mj_step(self.model, self.data)
            self._log()
            self._frame()

    def result(self) -> dict:
        in_bin = self.item_in_bin()
        speed = float(np.linalg.norm(self.data.cvel[self.item_body][3:]))
        lifted = self.max_item_z > self.layout["table"]["height"] + scene.item_half_height(self.cfg["item"]) + 0.05
        on_floor = self.item_pos()[2] < self.layout["table"]["height"] - 0.05 and not in_bin
        return dict(
            task=self.task, seed=self.seed,
            success=bool(in_bin and speed < 0.1),
            time_s=round(self.sim_time, 2),
            collisions=int(self.collisions),
            energy_j=round(self.energy_j, 2),
            dropped=bool(on_floor),
            lifted=bool(lifted),
            item_final_pos=[round(float(v), 3) for v in self.item_pos()],
        )
