"""CLI: plan distinct strategies with the racetrack planner, then race one agent loop per strategy.

    uv run python run_race.py --task can_to_bin --agents 4 --seeds 0-2 [--tries 5] [--fast]
        [--plan races/<id>/plan.json] [--memory tracker|none|<file.json>] [--exploit N]
        [--label cold] [--jobs 4] [--tracker-url URL] [--planner-model M] [--runs-dir runs] [--no-brain]

1. Plan: one planner call (racetrack/planner.py) sees the task text, the agents' exact API_DOC,
   get_state() and the front camera, plus memory from past races, and assigns each agent a different
   strategy. Skipped with --plan.
2. Race: one `run_agent.py --plan ... --agent-id ...` process per (agent, seed); every attempt goes to
   the tracker (start it with `cd racetrack && uv run uvicorn server:app --port 8000`).
3. Close: the tracker picks the winner and returns lessons + the winning skill (close.json).

Brain (on by default, --no-brain to race cold): before the race, GBrain's distilled skill + the Memorable
episode memory for this task go to every racer as --context-file (races/<id>/context.md); each attempt is
stored as a Memorable episode; after the race `gbrain distill` folds the new runs into the skill.
What was recalled and stored is in races/<id>/memory.json (robot_race/memory_hooks.py).

Everything lands in races/<race_id>/ (plan.json, race.json, close.json, one log per racer); the runs
themselves are ordinary agent runs in runs/<run_id>/, with summary.json["race"] naming race and agent.
Without a tracker the race still runs: attempts go to runs_backup.jsonl and race.json ranks by solves.
Strategy cards are hints: every racer's run is recorded as hinted (docs/INTEGRITY.md).
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from racetrack.client import Tracker
from racetrack.planner import (DEFAULT_MODEL as PLANNER_MODEL, load_custom, memory_from_tracker,
                               observe_sim, plan_strategies, register_plan, sim_task)
from robot_race.agent import _load_env, make_client, slugify
from robot_race.tasks import TASKS

REPO = Path(__file__).resolve().parent


def parse_seeds(text: str) -> list[int]:
    out: list[int] = []
    for part in text.split(","):
        lo, _, hi = part.partition("-")
        out += list(range(int(lo), int(hi or lo) + 1))
    return out


def tracker_up(tracker: Tracker) -> bool:
    try:
        tracker.races()
        return True
    except Exception:  # noqa: BLE001
        return False


def make_plan(a, tracker: Tracker, up: bool, race_dir: Path, brain_lessons: list[str] | None = None) -> dict:
    task = sim_task(a.task)
    memory = (None if a.memory == "none" else
              (memory_from_tracker(tracker, a.task) if up else None) if a.memory == "tracker" else
              json.loads(Path(a.memory).read_text()))
    if brain_lessons and a.memory != "none":  # gbrain think over past races, so strategy choice sees it
        memory = memory or {"lessons": [], "skills": []}
        memory["lessons"] = list(brain_lessons) + list(memory.get("lessons") or [])
    custom = load_custom(a.custom_file, a.custom)
    if len(custom) >= a.agents:  # every racer is user-authored: no planner call
        print(f"racing {a.agents} user strategies for {a.task} (no planner)", flush=True)
        return plan_strategies(task, a.agents, memory=memory, custom=custom)
    state, image = observe_sim(a.task, a.seeds[0], race_dir)
    print(f"planning {a.agents - len(custom)} strategies for {a.task} with {a.planner_model}"
          f"{' (with memory)' if memory else ''}{f' + {len(custom)} user strategies' if custom else ''} ...",
          flush=True)
    return plan_strategies(task, a.agents, state=state, image=image, memory=memory, n_exploit=a.exploit,
                           model=a.planner_model, client=make_client(), custom=custom)


def run_racer(a, plan_path: Path, race_id: str, agent_id: str, name: str, seed: int, race_dir: Path,
              context_path: Path | None = None) -> dict:
    run_id = f"{datetime.now():%Y%m%d-%H%M%S}-{a.task}-s{seed}-{slugify(name)}-{agent_id}"
    cmd = [sys.executable, str(REPO / "run_agent.py"), "--task", a.task, "--seed", str(seed),
           "--tries", str(a.tries), "--plan", str(plan_path), "--agent-id", agent_id, "--race-id", race_id,
           "--run-id", run_id, "--runs-dir", a.runs_dir, "--timeout", str(a.timeout)]
    if a.fast:
        cmd.append("--fast")
    cmd.append("--memory" if a.brain else "--no-memory")
    if context_path is not None:
        cmd += ["--context-file", str(context_path)]
    if a.tracker_url:
        cmd += ["--tracker-url", a.tracker_url]
    log = race_dir / f"{agent_id}-s{seed}.log"
    print(f"  start {agent_id} ({name}) seed {seed} -> {a.runs_dir}/{run_id}", flush=True)
    with log.open("w") as f:
        rc = subprocess.run(cmd, cwd=REPO, stdout=f, stderr=subprocess.STDOUT).returncode
    summary_path = Path(a.runs_dir) / run_id / "summary.json"
    s = json.loads(summary_path.read_text()) if summary_path.exists() else {}
    row = {"agent_id": agent_id, "strategy": name, "seed": seed, "run_id": run_id, "exit_code": rc,
           "status": s.get("status", "error"), "solved_at": s.get("solved_at"),
           "attempts": len(s.get("attempts", [])), "log": log.name}
    print(f"  done  {agent_id} seed {seed}: {row['status']}"
          + (f" at try {row['solved_at']}" if row["solved_at"] else ""), flush=True)
    return row


def local_ranking(rows: list[dict]) -> list[dict]:
    """Fallback when no tracker: solved seeds, then fewer tries on solved seeds."""
    by: dict[str, dict] = {}
    for r in rows:
        b = by.setdefault(r["agent_id"], {"agent_id": r["agent_id"], "strategy": r["strategy"], "solved": 0,
                                          "seeds": 0, "tries_to_solve": []})
        b["seeds"] += 1
        if r["status"] == "solved":
            b["solved"] += 1
            b["tries_to_solve"].append(r["solved_at"])
    ranked = sorted(by.values(), key=lambda b: (-b["solved"], sum(b["tries_to_solve"]) / max(1, b["solved"])))
    return [{**b, "rank": i + 1} for i, b in enumerate(ranked)]


def render_winner(rows: list[dict], winner_id: str | None, runs_dir: str) -> str | None:
    """Background mp4 of the winner's solving attempt (races run --fast, so no mp4 exists yet).
    Detached `python -m robot_race.replay` at the executor's 640x480 @ 30; fail-open."""
    solved = [r for r in rows if r["status"] == "solved" and r.get("solved_at")]
    pick = next((r for r in solved if r["agent_id"] == winner_id), None) or (solved[0] if solved else None)
    if not pick:
        return None
    adir = Path(runs_dir) / pick["run_id"] / f"attempt_{pick['solved_at']}"
    if not (adir / "trajectory.npz").exists() or (adir / "attempt.mp4").exists():
        return None
    try:
        with (adir / ".render.log").open("w") as log:
            subprocess.Popen([sys.executable, "-m", "robot_race.replay", str(adir), "--width", "640",
                              "--height", "480", "--fps", "30"], cwd=REPO, stdout=log, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True)
    except OSError as e:
        print(f"render: could not start replay for {adir}: {e}")
        return None
    print(f"render: winner video -> {adir / 'attempt.mp4'} (background)")
    return str(adir)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--task", default="can_to_bin", choices=list(TASKS))
    ap.add_argument("--agents", type=int, default=4)
    ap.add_argument("--seeds", default="0", help="e.g. 0-2 or 0,3,5")
    ap.add_argument("--tries", type=int, default=5)
    ap.add_argument("--plan", help="reuse an existing plan.json instead of calling the planner")
    ap.add_argument("--memory", default="tracker", help="'tracker' (past races on this task), 'none', or a JSON file")
    ap.add_argument("--exploit", type=int, default=None, help="agents that refine the proven skill (default: half)")
    ap.add_argument("--label", default="", help="race label on the dashboard, e.g. cold / warm")
    ap.add_argument("--jobs", type=int, default=0, help="parallel agent loops (default: one per agent)")
    ap.add_argument("--fast", action="store_true", help="no mp4 per attempt (re-render the winner with replay)")
    ap.add_argument("--timeout", type=float, default=180, help="per-attempt executor timeout (s)")
    ap.add_argument("--tracker-url", default=None)
    ap.add_argument("--planner-model", default=PLANNER_MODEL)
    ap.add_argument("--runs-dir", default="runs")
    ap.add_argument("--races-dir", default="races")
    ap.add_argument("--custom-file", help='JSON list of user strategies {"name", "approach"[, "choices"]}: '
                    "raced as the first agents; the planner fills the rest")
    ap.add_argument("--custom", action="append", default=[], metavar="NAME::APPROACH",
                    help='user strategy, e.g. --custom "Sweep::push the can off the table edge into the bin"')
    ap.add_argument("--brain", action=argparse.BooleanOptionalAction, default=True,
                    help="GBrain skill + Memorable episodes as racer context, record episodes, distill after")
    a = ap.parse_args(argv)
    a.seeds = parse_seeds(a.seeds)
    _load_env()

    tracker = Tracker(a.tracker_url)
    up = tracker_up(tracker)
    if not up:
        print(f"tracker not reachable at {tracker.base_url}: racing without it "
              "(attempts saved to runs_backup.jsonl; no memory, no close)", file=sys.stderr)

    tmp_dir = Path(a.races_dir) / f"_planning-{datetime.now():%Y%m%d-%H%M%S}"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    # Brain, before planning: GBrain skill + gbrain think + Memorable episodes (robot_race/memory_hooks.py)
    context, memory = "", {"brain": a.brain}
    if a.brain:
        from robot_race import memory_hooks
        print("memory: recalling past races (gbrain brief + think, memorable episodes) ...", flush=True)
        context, memory["recalled"] = memory_hooks.recall(a.task)
        th = memory["recalled"].get("think") or {}
        print(f"memory: think {'used, saved ' + str(th.get('saved_slug')) if th.get('used') else 'skipped: ' + str(th.get('error'))}"
              f"; {memory['recalled']['episodes'].get('episodes', 0)} episodes", flush=True)
    brain_lessons = memory_hooks.think_to_lessons(memory["recalled"]) if a.brain else None
    try:
        plan = (json.loads(Path(a.plan).read_text()) if a.plan
                else make_plan(a, tracker, up, tmp_dir, brain_lessons))
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if plan.get("task_id") not in (None, "task", a.task):
        print(f"warning: plan was made for {plan['task_id']}, racing {a.task}", file=sys.stderr)
    for w in plan.get("warnings", []):
        print(f"  plan warning: {w}", file=sys.stderr)

    if up:
        race_id = tracker.start_race(label=a.label, task=a.task,
                                     scoring={"max_attempts": a.tries, "seeds_per_agent": len(a.seeds)})
        register_plan(tracker, race_id, plan)
    else:
        race_id = f"{datetime.now():%Y%m%d-%H%M%S}-{a.task}"
    plan["race_id"] = race_id
    race_dir = Path(a.races_dir) / race_id
    tmp_dir.rename(race_dir)  # planner scene image moves with it
    plan_path = (race_dir / "plan.json").resolve()
    plan_path.write_text(json.dumps(plan, indent=2))

    print(f"race {race_id}: {len(plan['strategies'])} agents x seeds {a.seeds}, {a.tries} tries -> {race_dir}")
    for s in plan["strategies"]:
        print(f"  {s['agent_id']} [{s.get('mode', 'explore')}] {s['name']}: "
              + "; ".join(f"{k}={v}" for k, v in (s.get("choices") or {}).items()))

    context_path = None
    if a.brain:
        if context:
            context_path = (race_dir / "context.md").resolve()
            context_path.write_text(context)
            print(f"memory: recalled context -> {context_path}")
        else:
            print("memory: nothing to recall yet for this task (cold start)")
        memory["context_file"] = context_path.name if context_path else None
        memory["planner_lessons_from_think"] = len(brain_lessons or [])

    jobs = [(s["agent_id"], s["name"], seed) for seed in a.seeds for s in plan["strategies"]]
    started = time.time()
    with ThreadPoolExecutor(max_workers=a.jobs or len(plan["strategies"])) as pool:
        rows = list(pool.map(lambda j: run_racer(a, plan_path, race_id, *j, race_dir, context_path), jobs))

    race = {"race_id": race_id, "task": a.task, "seeds": a.seeds, "tries": a.tries, "label": a.label,
            "planner_model": plan.get("model"), "tracker": tracker.base_url if up else None,
            "wall_s": round(time.time() - started, 1), "runs": rows, "local_ranking": local_ranking(rows)}
    (race_dir / "race.json").write_text(json.dumps(race, indent=2))

    if up:
        out = tracker.close_race(race_id)
        (race_dir / "close.json").write_text(json.dumps(out, indent=2))
        board = tracker.leaderboard(race_id)
        print(f"\nleaderboard {race_id}:")
        for ag in board.get("scores", []):
            print(f"  #{ag.get('rank')} {ag['agent_id']} {ag.get('persona') or ''}: score {ag['score']:.1f}, "
                  f"solved {ag['seeds_solved']}/{ag['seeds_target']}, first-try {ag['first_try_rate']:.0%}")
        winner = (out or {}).get("winner") or {}
        print(f"winner: {winner.get('agent_id')} {winner.get('persona') or ''}  "
              f"(lessons + skill -> {race_dir / 'close.json'})")
    else:
        print(f"\nranking {race_id} (local, no tracker):")
        for b in race["local_ranking"]:
            print(f"  #{b['rank']} {b['agent_id']} {b['strategy']}: solved {b['solved']}/{b['seeds']}")
    if a.fast:
        win = ((out or {}).get("winner") or {}).get("agent_id") if up else None
        render_winner(rows, win or (race["local_ranking"][0]["agent_id"] if race["local_ranking"] else None),
                      a.runs_dir)
    if a.brain:
        memory["stored"] = {"episodes": sum(r["attempts"] for r in rows),
                            "store": str(memory_hooks.recorder().settings.store_root)}
        (race_dir / "memory.json").write_text(json.dumps(memory, indent=2, default=str))
        memory_hooks.post_race(race_id, runs_dir=a.runs_dir, races_dir=a.races_dir)  # merges post_race in
        print(f"memory -> {race_dir / 'memory.json'}")
    print(f"race -> {race_dir / 'race.json'}")
    return 0 if any(r["status"] == "solved" for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
