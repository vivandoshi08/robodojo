"""FakeRobot: the interfaces.Robot API without physics, for building/testing agent code offline.

Same public methods as SimRobot. State starts from a real SimRobot.get_state() captured at seed 0
(CANNED_STATES, one per task). move_to() teleports the TCP to the clamped target (same workspace clamp
as SimRobot), sim time advances by plausible durations and TimeLimit is raised past the episode limit,
so agent code sees the same failure mode as in the sim. Grasping is faked: closing near the item
"holds" it and the item then follows the TCP. Images are black.
"""
from __future__ import annotations

import copy
import math

import numpy as np

from .robot import SimRobot
from .tasks import EPISODE_LIMIT_S, TASKS, TimeLimit

_BIN = {"center": [0.45, 0.35, 0.0], "inner_half_size": 0.11, "rim_height": 0.22}
_TABLE = {"center": [0.55, -0.15], "half_size": [0.2, 0.25], "height": 0.2}


def _state(item: dict, bin_: dict = _BIN) -> dict:
    return {"time_s": 0.0, "tcp": [0.5546, -0.0001, 0.5144], "gripper_yaw_deg": -90.0, "gripper_width": 0.0794,
            "holding": False, "item": item, "bin": bin_, "table": _TABLE}


# Captured from SimRobot(Env(task, 0, record=False)).get_state() for every task.
CANNED_STATES = {
    "can_to_bin": _state({"name": "can", "shape": "cylinder", "size": [0.03, 0.05], "mass": 0.06, "lying": False,
                          "pos": [0.5774, -0.2161, 0.2498], "yaw_deg": 0.0}),
    "bottle_to_bin": _state({"name": "bottle", "shape": "capsule", "size": [0.028, 0.07], "mass": 0.08, "lying": True,
                             "pos": [0.5774, -0.216, 0.2276], "yaw_deg": -82.6}),
    "paper_to_bin": _state({"name": "paper", "shape": "sphere", "size": [0.034], "mass": 0.03, "lying": False,
                            "pos": [0.5774, -0.216, 0.2334], "yaw_deg": -0.0}),
    "box_to_bin": _state({"name": "box", "shape": "box", "size": [0.06, 0.03, 0.03], "mass": 0.05, "lying": False,
                          "pos": [0.5774, -0.216, 0.2298], "yaw_deg": -82.6}),
    "can_to_far_bin": _state({"name": "can", "shape": "cylinder", "size": [0.03, 0.05], "mass": 0.06, "lying": False,
                              "pos": [0.5774, -0.2161, 0.2498], "yaw_deg": 0.0},
                             {"center": [0.2, 0.55, 0.0], "inner_half_size": 0.11, "rim_height": 0.22}),
}
assert set(CANNED_STATES) == set(TASKS)

GRASP_RADIUS = 0.03  # TCP within this of the item center when closing -> holding


class FakeRobot:
    def __init__(self, task: str = "can_to_bin", state: dict | None = None):
        if task not in CANNED_STATES:
            raise KeyError(f"unknown task {task!r}; choose from {list(CANNED_STATES)}")
        self.task = task
        self._s = copy.deepcopy(state if state is not None else CANNED_STATES[task])
        self.yaw_deg = float(self._s["gripper_yaw_deg"])
        self.call_log: list[str] = []

    # ---------- internals ----------
    def _advance(self, seconds: float):
        self._s["time_s"] = round(self._s["time_s"] + max(0.0, float(seconds)), 2)
        if self._s["time_s"] > EPISODE_LIMIT_S:
            raise TimeLimit(f"episode exceeded {EPISODE_LIMIT_S:.0f} s of sim time")

    def _item_width(self) -> float:
        it = self._s["item"]
        return 2 * min(it["size"][:3] if it["shape"] == "box" else it["size"][:1])

    # ---------- public API (see interfaces.API_DOC) ----------
    def get_state(self) -> dict:
        return copy.deepcopy(self._s)

    def get_image(self, view: str = "front", width: int = 320, height: int = 240, depth: bool = False) -> np.ndarray:
        if depth:
            return np.full((int(height), int(width)), 1.0, np.float32)
        return np.zeros((int(height), int(width), 3), np.uint8)

    def get_camera(self, view: str = "front", width: int = 320, height: int = 240) -> dict:
        f = 0.5 * height / math.tan(math.radians(25))
        return {"view": view, "width": width, "height": height,
                "K": [[f, 0.0, width / 2], [0.0, f, height / 2], [0.0, 0.0, 1.0]],
                "cam_to_world": [[1.0, 0, 0, 0], [0, 1.0, 0, 0], [0, 0, 1.0, 0], [0, 0, 0, 1.0]]}

    def move_to(self, xyz, speed: float = 0.2) -> list:
        self.call_log.append(f"move_to({list(np.round(xyz, 3))}, speed={speed})")
        target = SimRobot._clamp(self, xyz)  # SimRobot's workspace clamp (doesn't touch self)
        speed = float(np.clip(speed, 0.02, 1.0))
        start = np.array(self._s["tcp"], float)
        self._advance(np.linalg.norm(target - start) / speed + 0.1)
        self._s["tcp"] = [round(float(v), 4) for v in target]
        if self._s["holding"]:
            self._s["item"]["pos"] = list(self._s["tcp"])
        return list(self._s["tcp"])

    def rotate_gripper(self, yaw_deg: float) -> None:
        self.call_log.append(f"rotate_gripper({yaw_deg})")
        self.yaw_deg = ((float(yaw_deg) + 90) % 180) - 90
        self._s["gripper_yaw_deg"] = round(self.yaw_deg, 1)
        self._advance(0.5)

    def open_gripper(self) -> None:
        self.call_log.append("open_gripper()")
        self._s["gripper_width"], self._s["holding"] = 0.0794, False
        self._advance(0.4)

    def close_gripper(self) -> None:
        self.call_log.append("close_gripper()")
        self._s["holding"] = self._s["holding"] or math.dist(self._s["tcp"], self._s["item"]["pos"]) < GRASP_RADIUS
        self._s["gripper_width"] = round(self._item_width(), 4) if self._s["holding"] else 0.0
        self._advance(0.6)

    def wait(self, seconds: float) -> None:
        self.call_log.append(f"wait({seconds})")
        self._advance(max(0.02, float(seconds)))
