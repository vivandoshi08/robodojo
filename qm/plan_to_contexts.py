"""Turn a race plan into QM swarm spawn contexts, with the tracker as the hand-off point.

QM sandboxes can't see the host's files and carry no API key, so everything a racer needs goes
through the racetrack tracker (reachable from a sandbox at http://host.docker.internal:8000):

    prepare   (host, or the QM root as a fallback) plan strategies, start the race on the tracker,
              register one agent per strategy with its full prompt + recalled memory (the tracker's
              GET /races/{id}/context, computed on the host from GBrain + Memorable), print race_id.
                uv run python qm/plan_to_contexts.py prepare --task can_to_bin --seed 0 --agents 4 --label qm
    contexts  (QM root, inside its sandbox) read the race's agents back from the tracker and print the
              `contexts` list for POST /v1/swarm {"action": "spawn"}.
                uv run --no-sync python qm/plan_to_contexts.py contexts --race-id <race_id>

Every step fails open: no planner (no key, no network) -> the static cards in qm/strategies.json;
no memory on the tracker -> no recalled context; no tracker -> contexts straight from the plan (race_id null).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from racetrack.client import Tracker  # noqa: E402
from racetrack.planner import strategy_prompt  # noqa: E402

STRATEGIES = ROOT / "qm" / "strategies.json"


def _log(msg: str) -> None:
    print(f"[plan_to_contexts] {msg}", file=sys.stderr)


def plan_from_cards(path: Path = STRATEGIES, n: Optional[int] = None) -> dict[str, Any]:
    """qm/strategies.json cards in the planner's plan.json shape (agent_id = card id)."""
    cards = json.loads(Path(path).read_text())[: n or None]
    return {"strategies": [{"agent_id": c["id"], "name": c["name"], "mode": "explore",
                            "approach": c["card"], "choices": {}} for c in cards],
            "model": None, "source": "strategies.json", "warnings": []}


def make_plan(task: str, seed: int, n: int, out_dir: Path, planner: bool = True) -> dict[str, Any]:
    """LLM planner (memory from past races on the tracker) when it can run, else the static cards."""
    if planner:
        try:
            from racetrack.planner import memory_from_tracker, observe_sim, plan_strategies, sim_task
            from robot_race.agent import _load_env, make_client
            _load_env()
            client = make_client()
            t = sim_task(task)
            state, image = observe_sim(task, seed, out_dir)
            memory = memory_from_tracker(Tracker(), task)
            plan = plan_strategies(t, n, state=state, image=image, memory=memory, client=client)
            plan["source"] = "planner"
            return plan
        except Exception as e:  # no key, no network, planner error: race with the cards instead
            _log(f"planner unavailable ({type(e).__name__}: {e}); using {STRATEGIES.name}")
    return plan_from_cards(n=n)


def memory_context(tracker: Tracker, race_id: Optional[str], task: str) -> Optional[str]:
    """Recalled memory from the tracker (GET /races/{id}/context): the host computes it from GBrain and
    Memorable, so this works the same from a QM sandbox, which has neither."""
    if not race_id:
        return None
    try:
        out = tracker._req("GET", f"/races/{race_id}/context?task={task}")
        if out.get("error"):
            _log(f"no recalled memory ({out['error']})")
        return (out.get("context") or "").strip() or None
    except Exception as e:
        _log(f"no recalled memory ({type(e).__name__}: {e})")
        return None


def build_contexts(plan: dict[str, Any], task: str, seed: int, race_id: Optional[str],
                   context: Optional[str], tracker_url: str) -> list[dict[str, Any]]:
    out = []
    for s in plan["strategies"]:
        out.append({"role": "racer", "task": task, "seed": seed, "race_id": race_id,
                    "agent_id": s["agent_id"], "strategy_name": s["name"],
                    "strategy_prompt": s.get("prompt") or strategy_prompt(s, plan),
                    "context": context, "tracker_url": tracker_url})
    return out


def prepare(args) -> dict[str, Any]:
    out_dir = ROOT / "races" / (args.label or "qm")
    out_dir.mkdir(parents=True, exist_ok=True)
    plan = (json.loads(Path(args.plan).read_text()) if args.plan else
            make_plan(args.task, args.seed, args.agents, out_dir, planner=not args.no_planner))
    tracker = Tracker(args.url)
    race_id, context = None, None
    try:
        race_id = tracker.start_race(label=args.label or "qm", task=args.task, race_id=args.race_id,
                                     scoring={"max_attempts": args.tries})
        context = None if args.no_memory else memory_context(tracker, race_id, args.task)
        for s in plan["strategies"]:
            tracker.register_agent(race_id, s["agent_id"], persona=s["name"], mode=s.get("mode"),
                                   strategy=s.get("approach"), choices=s.get("choices"),
                                   risks=s.get("risks"), planner_model=plan.get("model"),
                                   prompt=strategy_prompt(s, plan), context=context,
                                   task=args.task, seed=args.seed, source=plan.get("source", "planner"))
    except Exception as e:
        _log(f"tracker unreachable at {tracker.base_url} ({e}); contexts carry race_id null")
    plan["race_id"] = race_id
    race_dir = ROOT / "races" / race_id if race_id else out_dir
    race_dir.mkdir(parents=True, exist_ok=True)
    (race_dir / "plan.json").write_text(json.dumps(plan, indent=2))
    if context:
        (race_dir / "context.md").write_text(context)
    contexts = build_contexts(plan, args.task, args.seed, race_id, context, args.sandbox_tracker_url)
    (race_dir / "contexts.json").write_text(json.dumps(contexts, indent=2))
    return {"race_id": race_id, "task": args.task, "seed": args.seed, "source": plan.get("source"),
            "agents": [s["agent_id"] for s in plan["strategies"]], "memory": bool(context),
            "dir": str(race_dir.relative_to(ROOT))}


def contexts_from_tracker(args) -> list[dict[str, Any]]:
    agents = Tracker(args.url)._req("GET", f"/races/{args.race_id}/agents")
    out = []
    for agent_id, info in agents.items():
        s = {"agent_id": agent_id, "name": info.get("persona") or agent_id,
             "approach": info.get("strategy") or "", "choices": info.get("choices") or {},
             "risks": info.get("risks"), "prompt": info.get("prompt")}
        out += build_contexts({"strategies": [s]}, info.get("task") or args.task,
                              info.get("seed", args.seed), args.race_id, info.get("context"),
                              args.sandbox_tracker_url)
    return out


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=None, help="tracker URL for this process (default $RACETRACK_URL)")
    ap.add_argument("--sandbox-tracker-url", default="http://host.docker.internal:8000",
                    help="tracker URL the racers' sandboxes use")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("--task", default="can_to_bin")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--agents", type=int, default=4)
    p.add_argument("--tries", type=int, default=5)
    p.add_argument("--label", default="qm")
    p.add_argument("--race-id", default=None)
    p.add_argument("--plan", default=None, help="use this plan.json instead of planning")
    p.add_argument("--no-planner", action="store_true", help="skip the LLM planner, race the static cards")
    p.add_argument("--no-memory", action="store_true", help="cold race: no GBrain/Memorable context")
    p = sub.add_parser("contexts")
    p.add_argument("--race-id", required=True)
    p.add_argument("--task", default="can_to_bin")
    p.add_argument("--seed", type=int, default=0)
    args = ap.parse_args(argv)

    if args.cmd == "prepare":
        print(json.dumps(prepare(args)))
    else:
        try:
            print(json.dumps(contexts_from_tracker(args)))
        except Exception as e:
            print(json.dumps({"error": f"could not read race {args.race_id} from the tracker: {e}"}))
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
