"""Gate 1 check: reference policy on every task x 10 seeds, no video. Run from the repo root."""
import os, sys, time, runpy
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from robot_race.tasks import Env, TASKS
from robot_race.robot import SimRobot
ns = runpy.run_path("policies/reference_pick_and_drop.py")
for task in TASKS:
    ok = 0; fails = []; t = time.time()
    for seed in range(10):
        env = Env(task, seed, record=False); r = SimRobot(env, observation="oracle")
        err = None
        try: ns["run"](r)
        except Exception as e: err = repr(e)
        env.settle(1.0); res = env.result()
        ok += res["success"]
        if not res["success"]: fails.append((seed, res["item_final_pos"], res["lifted"], err))
    print(f"{task:16s} {ok}/10  ({time.time()-t:.1f}s)  fails: {fails[:4]}")
