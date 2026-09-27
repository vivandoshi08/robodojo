"""FakeRobot matches SimRobot's API and runs real policy code without physics."""
from __future__ import annotations

import inspect
import math
import os
import sys

import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from robot_race.fake_robot import CANNED_STATES, FakeRobot  # noqa: E402
from robot_race.interfaces import Robot  # noqa: E402
from robot_race.robot import SimRobot  # noqa: E402
from robot_race.tasks import TASKS, TimeLimit  # noqa: E402

API = [n for n in vars(Robot) if not n.startswith("_")]


def _run(code: str, robot):
    ns = {"robot": robot, "np": np, "math": math}
    exec(compile(code, "policy.py", "exec"), ns)
    ns["run"](robot)


def test_signatures_match_simrobot_and_protocol():
    assert set(API) == {"get_state", "get_image", "move_to", "rotate_gripper", "open_gripper", "close_gripper", "wait"}
    for name in API:
        fake, sim, proto = (inspect.signature(getattr(c, name)) for c in (FakeRobot, SimRobot, Robot))
        assert fake == sim, name
        assert list(fake.parameters) == list(proto.parameters), name
        assert [p.default for p in fake.parameters.values()] == [p.default for p in proto.parameters.values()], name
    assert {n for n in vars(FakeRobot) if not n.startswith("_")} == {n for n in vars(SimRobot) if not n.startswith("_")}


@pytest.mark.parametrize("task", list(TASKS))
def test_canned_state_shape(task):
    s = FakeRobot(task).get_state()
    assert set(s) == {"time_s", "tcp", "gripper_yaw_deg", "gripper_width", "holding", "item", "bin", "table"}
    assert set(s["item"]) == {"name", "shape", "size", "mass", "lying", "pos", "yaw_deg"}
    assert s["item"]["name"] == TASKS[task]["item"]
    if "bin_center" in TASKS[task]:
        assert s["bin"]["center"][:2] == list(TASKS[task]["bin_center"])
    s["tcp"][0] = 99  # get_state returns a copy
    assert FakeRobot(task).get_state()["tcp"][0] != 99 and CANNED_STATES[task]["tcp"][0] != 99


@pytest.mark.parametrize("task", list(TASKS))
def test_reference_policy_runs(task):
    r = FakeRobot(task)
    _run(open(os.path.join(ROOT, "policies", "reference_pick_and_drop.py")).read(), r)
    s = r.get_state()
    assert r.call_log[0] == "open_gripper()" and r.call_log[-1] == "wait(0.5)"
    assert any(c.startswith("move_to(") for c in r.call_log) and "close_gripper()" in r.call_log
    assert 3 < s["time_s"] < 40
    assert not s["holding"] and s["gripper_width"] > 0.07
    b = s["bin"]  # faked grasp carried the item over the bin
    assert abs(s["item"]["pos"][0] - b["center"][0]) < 1e-3 and abs(s["item"]["pos"][1] - b["center"][1]) < 1e-3


def test_move_clamp_gripper_and_image():
    r = FakeRobot()
    assert r.move_to([0.5, 0.0, -1.0]) == [0.5, 0.0, 0.02]  # Z_MIN clamp from robot.py
    far = r.move_to([3.0, 0.0, 0.33])
    assert abs(far[0] - 0.85) < 1e-3  # WORKSPACE_R reach clamp
    r.close_gripper()
    assert r.get_state()["gripper_width"] == 0.0 and not r.get_state()["holding"]
    r.rotate_gripper(135)
    assert r.get_state()["gripper_yaw_deg"] == -45.0
    img = r.get_image("top", 64, 48)
    assert img.shape == (48, 64, 3) and img.dtype == np.uint8 and not img.any()


def test_time_limit():
    r = FakeRobot()
    with pytest.raises(TimeLimit):
        _run("def run(robot):\n    while True:\n        robot.wait(1)\n", r)
    assert 40 < r.get_state()["time_s"] <= 41
    assert len(r.call_log) == 41


def test_unknown_task():
    with pytest.raises(KeyError):
        FakeRobot("nope")
