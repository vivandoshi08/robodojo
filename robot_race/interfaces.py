"""The contract every track codes against. Change only with the whole team's agreement.

API_DOC is the single source of truth for the robot API: the agent's prompt is built from it.
"""
from __future__ import annotations

from typing import Protocol

import numpy as np


class Robot(Protocol):
    def get_state(self) -> dict: ...
    def get_image(self, view: str = "front", width: int = 320, height: int = 240) -> np.ndarray: ...
    def move_to(self, xyz, speed: float = 0.2) -> list: ...
    def rotate_gripper(self, yaw_deg: float) -> None: ...
    def open_gripper(self) -> None: ...
    def close_gripper(self) -> None: ...
    def wait(self, seconds: float) -> None: ...


# Keys written to result.json for every attempt.
RESULT_KEYS = ["task", "seed", "success", "time_s", "collisions", "energy_j", "dropped",
               "lifted", "error", "frames", "video", "code_path"]

API_DOC = """
ROBOT API (the only way to act; `robot` is passed to your run(robot) function)

World frame: meters. +x points away from the robot base, +y to the robot's left, +z up.
The robot is a Franka Panda arm on the floor at the origin, with a parallel-jaw gripper pointing down.
"TCP" = the point midway between the two fingertips.

robot.get_state() -> dict
    {"time_s": float,                      # sim time so far
     "tcp": [x, y, z],                     # gripper TCP position
     "gripper_yaw_deg": float,             # 0 = fingers close along world y; at yaw a they close along a+90 deg
     "gripper_width": float,               # 0.0 (closed) .. 0.08 (fully open), meters
     "holding": bool,                      # both fingers touching the item
     "item": {"name", "shape", "size", "mass", "lying", "pos": [x,y,z], "yaw_deg": float},
     "bin":  {"center": [x,y,0], "inner_half_size": float, "rim_height": float},
     "table": {"center": [x,y], "half_size": [hx,hy], "height": float}}
    item "size" uses MuJoCo conventions: cylinder [radius, half_height], capsule [radius, half_length],
    sphere [radius], box [half_x, half_y, half_z]. "pos" is the item's center.
    "yaw_deg" = direction of the item's long axis in the xy-plane, degrees from +x
    (box: its local x axis; lying capsule: its length axis; 0 for round items).

robot.get_image(view="front"|"side"|"top", width=320, height=240) -> HxWx3 uint8 RGB array

robot.move_to([x, y, z], speed=0.2) -> [x, y, z]
    Moves the TCP in a straight line at `speed` m/s (clamped to 0.02..1.0), gripper pointing down,
    then returns the TCP position actually reached. Targets are clamped to the reachable workspace.

robot.rotate_gripper(yaw_deg)   # sets gripper yaw about the vertical axis, in place (wrapped to [-90, 90))
robot.open_gripper()            # opens fully (0.08 m), takes ~0.4 s
robot.close_gripper()           # closes until it hits something, takes ~0.6 s
robot.wait(seconds)             # lets physics run

Episode limit: 40 s of sim time. Exceptions end the attempt and are reported back to you.
""".strip()
