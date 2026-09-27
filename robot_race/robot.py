"""SimRobot: the robot API from interfaces.API_DOC, implemented on the MuJoCo Panda.

Cartesian moves use damped-least-squares inverse kinematics on the 7 arm joints (with a
null-space pull toward the home posture) and feed the result to the Panda's position actuators.
"""
from __future__ import annotations

import math

import mujoco
import numpy as np

from . import scene
from .tasks import Env

CONTROL_DT = 0.02          # 50 Hz control loop
WORKSPACE_R = (0.25, 0.85)  # min/max horizontal-ish reach from the base (m)
Z_MIN = 0.02


def _yaw_R(yaw_deg: float) -> np.ndarray:
    """Target TCP orientation: gripper z-axis pointing down, fingers closing along world y at yaw 0."""
    c, s = math.cos(math.radians(yaw_deg)), math.sin(math.radians(yaw_deg))
    Rz = np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]])
    R0 = np.array([[-1, 0, 0], [0, 1, 0], [0, 0, -1]], float)  # columns: x, y, z of the hand frame
    return Rz @ R0


class SimRobot:
    def __init__(self, env: Env):
        self.env, self.m, self.d = env, env.model, env.data
        self._ik_data = mujoco.MjData(self.m)
        self._q_cmd = self.d.qpos[env.arm_qadr].copy()
        self._lo = self.m.jnt_range[[self.m.joint(f"joint{i}").id for i in range(1, 8)], 0]
        self._hi = self.m.jnt_range[[self.m.joint(f"joint{i}").id for i in range(1, 8)], 1]
        self.yaw_deg = self._current_yaw()
        self.call_log: list[str] = []
        # Start from an exact pose-matched command so the first move is smooth.
        self._q_cmd = self._ik(self.env.tcp_pos(), _yaw_R(self.yaw_deg), self._q_cmd)

    # ---------- internals ----------
    def _current_yaw(self) -> float:
        R = self.d.site_xmat[self.env.tcp_id].reshape(3, 3)
        return math.degrees(math.atan2(-R[0, 1], R[1, 1])) if abs(R[2, 2]) > 0.5 else 0.0

    def _ik(self, target_pos, target_R, q_init, iters=60, lam=0.05):
        m, di, env = self.m, self._ik_data, self.env
        di.qpos[:] = self.d.qpos
        q = np.array(q_init, float)
        jacp, jacr = np.zeros((3, m.nv)), np.zeros((3, m.nv))
        for _ in range(iters):
            di.qpos[env.arm_qadr] = q
            mujoco.mj_kinematics(m, di)
            mujoco.mj_comPos(m, di)
            pos = di.site_xpos[env.tcp_id]
            R = di.site_xmat[env.tcp_id].reshape(3, 3)
            e_pos = target_pos - pos
            e_rot = 0.5 * sum(np.cross(R[:, i], target_R[:, i]) for i in range(3))
            if np.linalg.norm(e_pos) < 5e-4 and np.linalg.norm(e_rot) < 5e-3:
                break
            mujoco.mj_jacSite(m, di, jacp, jacr, env.tcp_id)
            J = np.vstack([jacp[:, env.arm_dadr], jacr[:, env.arm_dadr]])
            err = np.concatenate([e_pos, e_rot])
            Jpinv = J.T @ np.linalg.inv(J @ J.T + lam ** 2 * np.eye(6))
            dq = Jpinv @ err + (np.eye(7) - Jpinv @ J) @ (0.05 * (scene.HOME_Q - q))
            q = np.clip(q + np.clip(dq, -0.2, 0.2), self._lo, self._hi)
        return q

    def _clamp(self, xyz):
        p = np.array(xyz, float)
        p[2] = max(p[2], Z_MIN)
        r = np.linalg.norm(p[:2])
        if r < WORKSPACE_R[0]:
            p[:2] = p[:2] / max(r, 1e-6) * WORKSPACE_R[0]
        dist = np.linalg.norm(p - [0, 0, 0.33])  # shoulder height
        if dist > WORKSPACE_R[1]:
            p = np.array([0, 0, 0.33]) + (p - [0, 0, 0.33]) / dist * WORKSPACE_R[1]
        return p

    def _steps(self, seconds):
        for _ in range(max(1, int(round(seconds / CONTROL_DT)))):
            self.d.ctrl[:7] = self._q_cmd
            self.env.step(int(CONTROL_DT / self.m.opt.timestep))

    # ---------- public API (see interfaces.API_DOC) ----------
    def get_state(self) -> dict:
        env, lay = self.env, self.env.layout
        width = float(self.d.qpos[self.m.joint("finger_joint1").qposadr[0]]
                      + self.d.qpos[self.m.joint("finger_joint2").qposadr[0]])
        item = dict(lay["item"], pos=[round(float(v), 4) for v in env.item_pos()], yaw_deg=round(env.item_yaw_deg(), 1))
        return {
            "time_s": round(env.sim_time, 2),
            "tcp": [round(float(v), 4) for v in env.tcp_pos()],
            "gripper_yaw_deg": round(self.yaw_deg, 1),
            "gripper_width": round(width, 4),
            "holding": env.holding(),
            "item": item,
            "bin": lay["bin"],
            "table": lay["table"],
        }

    def get_image(self, view: str = "front", width: int = 320, height: int = 240) -> np.ndarray:
        return self.env.render(view, width, height)

    def move_to(self, xyz, speed: float = 0.2) -> list:
        self.call_log.append(f"move_to({list(np.round(xyz, 3))}, speed={speed})")
        target = self._clamp(xyz)
        speed = float(np.clip(speed, 0.02, 1.0))
        start = self.env.tcp_pos()
        R = _yaw_R(self.yaw_deg)
        n = max(1, math.ceil(np.linalg.norm(target - start) / speed / CONTROL_DT))
        for k in range(1, n + 1):
            s = k / n
            s = s * s * (3 - 2 * s)  # smoothstep: gentle start and stop
            self._q_cmd = self._ik(start + s * (target - start), R, self._q_cmd)
            self._steps(CONTROL_DT)
        for _ in range(50):  # settle up to 1 s
            if np.linalg.norm(self.env.tcp_pos() - target) < 0.004:
                break
            self._steps(CONTROL_DT)
        return [round(float(v), 4) for v in self.env.tcp_pos()]

    def rotate_gripper(self, yaw_deg: float) -> None:
        self.call_log.append(f"rotate_gripper({yaw_deg})")
        yaw_deg = ((float(yaw_deg) + 90) % 180) - 90  # parallel gripper is symmetric: keep in [-90, 90)
        pos = self.env.tcp_pos()
        start, n = self.yaw_deg, 25
        for k in range(1, n + 1):
            self._q_cmd = self._ik(pos, _yaw_R(start + (yaw_deg - start) * k / n), self._q_cmd)
            self._steps(CONTROL_DT)
        self.yaw_deg = yaw_deg

    def open_gripper(self) -> None:
        self.call_log.append("open_gripper()")
        self.d.ctrl[7] = 255
        self._steps(0.4)

    def close_gripper(self) -> None:
        self.call_log.append("close_gripper()")
        self.d.ctrl[7] = 0
        self._steps(0.6)

    def wait(self, seconds: float) -> None:
        self.call_log.append(f"wait({seconds})")
        self._steps(max(0.0, float(seconds)))
