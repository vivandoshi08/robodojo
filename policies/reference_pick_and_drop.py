"""Hand-written reference policy: top-down grasp, lift, carry over the bin, release.

Used for Gate 1 (does the sim + robot API work?) and optionally as a worked example in the agent prompt.
"""
import numpy as np


def run(robot):
    s = robot.get_state()
    item, b = s["item"], s["bin"]
    p = np.array(item["pos"])

    robot.open_gripper()
    if item["shape"] in ("box", "capsule"):
        # At gripper yaw = item yaw, the fingers close across the item's long axis (its narrow side).
        robot.rotate_gripper(item["yaw_deg"])

    robot.move_to([p[0], p[1], p[2] + 0.15], speed=0.4)   # hover above
    robot.move_to([p[0], p[1], p[2] + 0.005], speed=0.1)  # descend so fingertips straddle the item center
    robot.close_gripper()
    robot.move_to([p[0], p[1], p[2] + 0.25], speed=0.15)  # lift straight up

    cx, cy = b["center"][0], b["center"][1]
    robot.move_to([cx, cy, b["rim_height"] + 0.15], speed=0.2)  # carry over the bin
    robot.open_gripper()
    robot.wait(0.5)
