"""Host-side launcher for a QM race, callable from the web UI backend or the shell.

    from qm.launch import launch_qm_race, qm_status
    out = launch_qm_race("can_to_bin", agents=4, seeds=[0], tries=5, label="qm")
    qm_status(out["race_id"])

    uv run python qm/launch.py launch --task can_to_bin --agents 4 --seeds 0 --label qm
    uv run python qm/launch.py status --race-id <race_id>

launch = open the race on the tracker, recall memory (GBrain + Memorable, host side), plan the
strategies, register them, write races/<race_id>/{plan,contexts,launch}.json. Starting the QM root
session is NOT automated yet (QM's API needs a signed request; see qm/README.md "Launching from the
web UI"): launch returns `qm.message`, the one line to send the QM root, and the race shows up on
the tracker as soon as its workers record attempts.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT), str(ROOT / "qm")):
    if p not in sys.path:
        sys.path.insert(0, p)

from racetrack.client import Tracker  # noqa: E402
import plan_to_contexts as ptc  # noqa: E402


def launch_qm_race(task: str = "can_to_bin", agents: int = 4, seeds: Optional[list[int]] = None,
                   tries: int = 5, label: str = "qm", *, planner: bool = True, memory: bool = True,
                   tracker_url: Optional[str] = None) -> dict[str, Any]:
    """Prepare a QM race (tracker race + plan + memory) and say how to start it. One seed per QM race:
    extra seeds are recorded but not raced (the swarm skill races one scene)."""
    seeds = list(seeds or [0])
    args = argparse.Namespace(task=task, seed=seeds[0], agents=agents, tries=tries, label=label,
                              race_id=None, plan=None, no_planner=not planner, no_memory=not memory,
                              url=tracker_url, sandbox_tracker_url="http://host.docker.internal:8000")
    prep = ptc.prepare(args)
    race_id = prep["race_id"]
    message = (f"Race {race_id} on {task} seed {seeds[0]}" if race_id else
               f"Race all strategies on {task} seed {seeds[0]}")
    launch = {"backend": "qm", "race_id": race_id, "task": task, "seeds": seeds, "seed": seeds[0],
              "agents": prep["agents"], "tries": tries, "label": label, "source": prep["source"],
              "memory": prep["memory"], "started": time.time(),
              "qm": {"status": "manual", "message": message, "session_id": None,
                     "note": "send `message` to the QM root (web UI); API launch needs QM's signing secret"},
              "skipped_seeds": seeds[1:]}
    race_dir = ROOT / prep["dir"]
    (race_dir / "launch.json").write_text(json.dumps(launch, indent=2))
    launch["plan"] = json.loads((race_dir / "plan.json").read_text())
    launch["dir"] = prep["dir"]
    return launch


def qm_status(race_id: str, tracker_url: Optional[str] = None) -> dict[str, Any]:
    """Race progress as the tracker sees it (workers record every attempt live)."""
    launch_path = ROOT / "races" / race_id / "launch.json"
    launch = json.loads(launch_path.read_text()) if launch_path.exists() else None
    t = Tracker(tracker_url)
    try:
        board = t.leaderboard(race_id)
        agents = t._req("GET", f"/races/{race_id}/agents")
        attempts = t._req("GET", f"/races/{race_id}/attempts")
    except Exception as e:
        return {"race_id": race_id, "launch": launch, "error": f"tracker: {type(e).__name__}: {e}"}
    per_agent = {a: {"attempts": 0, "solved": False} for a in agents}
    for a in attempts:
        s = per_agent.setdefault(a["agent_id"], {"attempts": 0, "solved": False})
        s["attempts"] = max(s["attempts"], a["attempt"])
        s["solved"] = s["solved"] or bool(a.get("success"))
    return {"race_id": race_id, "launch": launch, "final": board.get("final"),
            "winner": board.get("winner"), "leader": board.get("leader"), "workers": per_agent}


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=None, help="tracker URL (default $RACETRACK_URL)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("launch")
    p.add_argument("--task", default="can_to_bin")
    p.add_argument("--agents", type=int, default=4)
    p.add_argument("--seeds", default="0", help="comma list; QM races the first")
    p.add_argument("--tries", type=int, default=5)
    p.add_argument("--label", default="qm")
    p.add_argument("--no-planner", action="store_true")
    p.add_argument("--no-memory", action="store_true")
    p = sub.add_parser("status")
    p.add_argument("--race-id", required=True)
    args = ap.parse_args(argv)
    if args.cmd == "launch":
        out = launch_qm_race(args.task, args.agents, [int(s) for s in args.seeds.split(",") if s != ""],
                             args.tries, args.label, planner=not args.no_planner,
                             memory=not args.no_memory, tracker_url=args.url)
        out.pop("plan", None)
    else:
        out = qm_status(args.race_id, args.url)
    print(json.dumps(out, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
