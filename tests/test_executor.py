"""Executor: one attempt in a subprocess -> result.json + media (CLAUDE.md §8)."""
from __future__ import annotations

import json
import os

import numpy as np
import pytest

from robot_race.executor import ROOT, run_policy
from robot_race.interfaces import RESULT_KEYS

pytestmark = pytest.mark.skipif(
    not os.path.exists(os.path.join(ROOT, "assets", "menagerie", "franka_emika_panda", "panda.xml")),
    reason="Panda assets missing (bash scripts/fetch_assets.sh)")

REFERENCE = open(os.path.join(ROOT, "policies", "reference_pick_and_drop.py")).read()
EXTRA = ["item_final_pos", "calls", "wall_s", "media"]


def _check_keys(res, out):
    assert set(RESULT_KEYS + EXTRA) <= set(res)
    with open(os.path.join(out, "result.json")) as f:
        assert set(RESULT_KEYS + EXTRA) <= set(json.load(f))
    assert set(res["media"]) == {"video", "poster", "keyframes", "trajectory", "live"}


def test_reference_policy_success_with_video(tmp_path):
    out = str(tmp_path / "attempt_1")
    res = run_policy(REFERENCE, "can_to_bin", 0, out)
    _check_keys(res, out)
    assert res["success"] is True and res["error"] is None
    assert res["calls"] and res["calls"][0] == "open_gripper()"
    mp4 = os.path.join(out, "attempt.mp4")
    assert os.path.getsize(mp4) > 0 and res["video"] == "attempt.mp4" and res["media"]["video"] == "attempt.mp4"
    for name in ["poster.jpg", "trajectory.npz", "policy.py", "live.jpg"] + [f"key_{i}.png" for i in range(4)]:
        assert os.path.exists(os.path.join(out, name)), name
    assert res["media"]["keyframes"] == [f"key_{i}.png" for i in range(4)] and len(res["frames"]) == 4
    z = np.load(os.path.join(out, "trajectory.npz"))
    assert z["qpos"].shape[0] == z["t"].shape[0] > 100 and str(z["task"]) == "can_to_bin"


def test_policy_exception(tmp_path):
    code = "def run(robot):\n    robot.open_gripper()\n    raise ValueError('boom from policy')\n"
    res = run_policy(code, "can_to_bin", 0, str(tmp_path), fast=True, live=False)
    _check_keys(res, str(tmp_path))
    assert res["success"] is False and "boom from policy" in res["error"]
    assert res["video"] is None and res["media"]["poster"] == "poster.jpg" and res["media"]["live"] is None


def test_time_limit_not_hang(tmp_path):
    code = "def run(robot):\n    while True:\n        robot.wait(1)\n"
    res = run_policy(code, "can_to_bin", 0, str(tmp_path), timeout_s=120, fast=True, live=False)
    assert res["success"] is False and "TimeLimit" in res["error"]


def test_syntax_error(tmp_path):
    res = run_policy("def run(robot)\n    pass\n", "can_to_bin", 0, str(tmp_path), fast=True, live=False)
    _check_keys(res, str(tmp_path))
    assert res["success"] is False and "SyntaxError" in res["error"]


def test_missing_run(tmp_path):
    res = run_policy("x = 1\n", "can_to_bin", 0, str(tmp_path), fast=True, live=False)
    assert res["success"] is False and "run(robot)" in res["error"]


def test_wall_timeout(tmp_path):
    code = "def run(robot):\n    while True:\n        pass\n"
    res = run_policy(code, "can_to_bin", 0, str(tmp_path), timeout_s=3, fast=True, live=False)
    _check_keys(res, str(tmp_path))
    assert res["success"] is False and res["error"].startswith("timeout after 3")
    assert res["time_s"] is None and res["media"]["trajectory"] is None
