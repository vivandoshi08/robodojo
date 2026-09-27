"""CLI: run the Claude agent loop on one task + seed.

    uv run python run_agent.py --task can_to_bin --seed 0 [--tries 5] [--strategy "..."] [--example]
        [--context-file memory.md] [--fast] [--run-id ID] [--runs-dir runs] [--timeout 180]
        [--plan plan.json --agent-id agent-1 [--race-id ID] [--tracker-url URL]]

Exit code 0 if solved, 1 otherwise (2 on setup errors such as a missing API key).

By default the model gets only the environment: task text, API_DOC, get_state() and camera images.
--example / --strategy / --context-file are opt-in hints; each is recorded in summary.json["hints"]
(and "hinted" in runs/index.json) and shown as a banner on the run pages. See docs/INTEGRITY.md.

--plan/--agent-id: this loop is one racer; its strategy card is the planner's strategy block for that
agent (racetrack/planner.py, recorded as the strategy hint). With a race id (--race-id, or the plan's
race_id), every attempt is also sent to the racetrack tracker; a down tracker never stops the run.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from robot_race.agent import run_agent_loop
from robot_race.tasks import TASKS


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--task", default="can_to_bin", choices=list(TASKS))
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--tries", type=int, default=5)
    ap.add_argument("--strategy", default="", help="HINT (recorded): strategy card appended to the system prompt")
    ap.add_argument("--example", action="store_true", help="HINT (recorded, off by default): put the hand-written reference policy in the prompt")
    ap.add_argument("--context-file", help="HINT (recorded): recalled memory prepended to the first user turn")
    ap.add_argument("--fast", action="store_true", help="key frames + trajectory only, no mp4")
    ap.add_argument("--run-id", help="override the generated run id")
    ap.add_argument("--runs-dir", default="runs")
    ap.add_argument("--observation", default="telemetry", choices=["telemetry", "oracle"],
                    help="telemetry = real-robot sensors + cameras (default); oracle = also ground-truth item pose (a hint)")
    ap.add_argument("--timeout", type=float, default=180, help="per-attempt executor timeout (s)")
    ap.add_argument("--plan", help="racetrack plan.json: use the strategy the planner assigned to --agent-id")
    ap.add_argument("--agent-id", help="agent id in --plan (e.g. agent-1)")
    ap.add_argument("--race-id", help="racetrack race to record attempts in (default: the plan's race_id)")
    ap.add_argument("--tracker-url", help="racetrack server (default: $RACETRACK_URL or http://localhost:8000)")
    a = ap.parse_args(argv)
    context = Path(a.context_file).read_text() if a.context_file else None
    strategy, race, on_attempt = a.strategy or None, None, None
    if a.plan:
        from racetrack.planner import strategy_prompt
        if strategy or not a.agent_id:
            ap.error("--plan needs --agent-id and replaces --strategy")
        plan = json.loads(Path(a.plan).read_text())
        mine = next((x for x in plan["strategies"] if x["agent_id"] == a.agent_id), None)
        if mine is None:
            ap.error(f"{a.agent_id} is not in {a.plan}")
        strategy = strategy_prompt(mine, plan)
        race = {"race_id": a.race_id or plan.get("race_id"), "agent_id": a.agent_id,
                "strategy_name": mine["name"], "mode": mine.get("mode"), "plan": Path(a.plan).name}
        if race["race_id"]:
            from racetrack.client import Tracker
            tracker = Tracker(a.tracker_url)
            on_attempt = lambda k, result, adir: tracker.record_attempt(  # noqa: E731
                race["race_id"], a.agent_id, a.seed, k, adir / "result.json", run_id=adir.parent.name)
    try:
        s = run_agent_loop(a.task, a.seed, tries=a.tries, strategy=strategy, context=context,
                           example=a.example, fast=a.fast, run_id=a.run_id, runs_dir=a.runs_dir,
                           timeout_s=a.timeout, observation=a.observation, race=race, on_attempt=on_attempt)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"{s['run_id']}: {s['status']}  ->  {Path(a.runs_dir) / s['run_id'] / 'summary.json'}")
    return 0 if s["status"] == "solved" else 1


if __name__ == "__main__":
    sys.exit(main())
