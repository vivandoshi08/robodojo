"""The contract every track codes against. Change only with the whole team's agreement.

API_DOC is the single source of truth for the robot API: the agent's prompt is built from it.
"""
from __future__ import annotations

from typing import Protocol

import numpy as np


class Robot(Protocol):
    def get_state(self) -> dict: ...
    def get_image(self, view: str = "front", width: int = 320, height: int = 240, depth: bool = False) -> np.ndarray: ...
    def get_camera(self, view: str = "front", width: int = 320, height: int = 240) -> dict: ...
    def move_to(self, xyz, speed: float = 0.2) -> list: ...
    def rotate_gripper(self, yaw_deg: float) -> None: ...
    def open_gripper(self) -> None: ...
    def close_gripper(self) -> None: ...
    def wait(self, seconds: float) -> None: ...


# Keys written to result.json for every attempt.
RESULT_KEYS = ["task", "seed", "success", "time_s", "collisions", "energy_j", "dropped",
               "lifted", "error", "frames", "video", "code_path"]

_API_HEAD = """
ROBOT API (the only way to act; `robot` is passed to your run(robot) function)

World frame: meters. +x points away from the robot base, +y to the robot's left, +z up.
The robot is a Franka Panda arm on the floor at the origin, with a parallel-jaw gripper pointing down.
"TCP" = the point midway between the two fingertips.
""".strip()

_STATE_TELEMETRY = """
robot.get_state() -> dict   (robot telemetry: what the arm's own sensors and the calibrated workcell provide)
    {"time_s": float,                      # time since the episode started
     "joint_pos": [7 floats], "joint_vel": [7 floats],   # arm joint encoders (rad, rad/s)
     "tcp": [x, y, z],                     # TCP position from forward kinematics
     "gripper_yaw_deg": float,             # 0 = fingers close along world y; at yaw a they close along a+90 deg
     "gripper_width": float,               # finger opening, 0.0 (closed) .. 0.08 (fully open), meters
     "gripper_command": "open"|"close",    # last gripper command sent
     "grasp_detected": bool,               # gripper commanded closed but stopped before fully closing
     "workcell": {"bin":   {"center": [x,y,0], "inner_half_size": float, "rim_height": float},
                  "table": {"center": [x,y], "half_size": [hx,hy], "height": float}},  # fixed, surveyed
     "cameras": ["front", "side", "top"]}  # fixed, calibrated cameras
    Objects on the table are NOT in the state: locate them with the cameras.
""".strip()

_STATE_ORACLE = """
robot.get_state() -> dict   (oracle mode: telemetry + simulator ground truth for the item)
    telemetry keys (time_s, joint_pos, joint_vel, tcp, gripper_yaw_deg, gripper_width, gripper_command,
    grasp_detected, workcell, cameras), plus:
     "holding": bool,                      # both fingers touching the item
     "item": {"name", "shape", "size", "mass", "lying", "pos": [x,y,z], "yaw_deg": float},
     "bin": ..., "table": ...              # same as workcell
    item "size" uses MuJoCo conventions: cylinder [radius, half_height], capsule [radius, half_length],
    sphere [radius], box [half_x, half_y, half_z]. "pos" is the item's center.
    "yaw_deg" = direction of the item's long axis in the xy-plane, degrees from +x.
""".strip()

_API_TAIL = """
robot.get_image(view="front"|"side"|"top", width=320, height=240, depth=False) -> array
    depth=False: HxWx3 uint8 RGB.  depth=True: HxW float32 depth in meters along the camera's optical axis.
robot.get_camera(view="front"|"side"|"top", width=320, height=240) -> dict
    {"K": 3x3 intrinsics, "cam_to_world": 4x4, "width", "height"}  (OpenCV convention: x right, y down,
    z forward). Pixel (u, v) with depth z maps to world as  cam_to_world @ [z * inv(K) @ [u, v, 1], 1].

robot.move_to([x, y, z], speed=0.2) -> [x, y, z]
    Moves the TCP in a straight line at `speed` m/s (clamped to 0.02..1.0), gripper pointing down,
    then returns the TCP position actually reached. Targets are clamped to the reachable workspace.

robot.rotate_gripper(yaw_deg)   # sets gripper yaw about the vertical axis, in place (wrapped to [-90, 90))
robot.open_gripper()            # opens fully (0.08 m), takes ~0.4 s
robot.close_gripper()           # closes until it hits something, takes ~0.6 s
robot.wait(seconds)             # lets physics run

Episode limit: 40 s of sim time. Exceptions end the attempt and are reported back to you.
""".strip()


def api_doc(observation: str = "telemetry") -> str:
    """The robot API text given to the model, for an observation mode ("telemetry" | "oracle")."""
    state = {"telemetry": _STATE_TELEMETRY, "oracle": _STATE_ORACLE}[observation]
    return "\n\n".join([_API_HEAD, state, _API_TAIL])


API_DOC = api_doc("telemetry")
