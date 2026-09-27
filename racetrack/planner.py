"""LLM strategy planner: decides what each agent in a race should try. Nothing task-specific here.

One model call reads the task, the robot API, optionally the scene (state + image), and memory
from past races. It names the 2-5 decisions that matter most for THIS task and API, then gives
each agent a different combination. A new simulation only needs a new task file and its API.

    python planner.py --task-file tasks/can_to_bin.json --agents 4 --api ../interfaces.py \
                      --race cold --out plan.json

For this repo's MuJoCo sim, --sim-task builds the task from robot_race (TASKS text + the real
API_DOC) and renders the scene itself (get_state() + front camera):

    uv run python -m racetrack.planner --sim-task can_to_bin --seed 0 --agents 4 --out plan.json

Each agent's loop then adds its strategy to the prompt (run_agent.py --plan plan.json --agent-id agent-1):

    from racetrack.planner import load_strategy_prompt
    prompt += load_strategy_prompt("plan.json", agent_id)
"""
from __future__ import annotations

import argparse
import base64
import json
import mimetypes
import os
import sys
from pathlib import Path
from typing import Any, Optional

DEFAULT_MODEL = os.environ.get("RACE_PLANNER_MODEL", "claude-opus-5-5")

SYSTEM = """You plan a race between {n} robot agents that all attempt the same task.
Each agent is an LLM that writes a Python policy against the robot API below, runs it in \
simulation, sees the result, and revises it, for up to {max_tries} tries per seed. The best agent \
wins and its policy is saved as a reusable skill. Your job is to make the agents take genuinely \
different strategies, so the race explores the space instead of running {n} copies of one idea.

1. Identify 2 to 5 decision axes that most affect success for THIS task with THIS robot API \
(for example: how to approach and grasp, speeds, heights, orientation, order of steps, how to \
release, how to check progress and recover). Every option must be expressible with the API as \
documented. Never invent API functions.
2. Give each of the {n} agents a different combination of options. No two strategies may make \
the same choice on every axis; prefer combinations that differ on at least two axes. Use the \
exact axis names and option strings in each strategy's `choices`.
3. For each strategy write: a short name, the approach as concrete steps the agent can implement \
with the API, starting parameter values the agent can tune between tries, and the main risk.
{memory_rule}
Call submit_plan with the result."""

MEMORY_RULE_NONE = "\nThere is no memory from earlier races: every strategy has mode \"explore\".\n"
MEMORY_RULE = """
Memory from earlier races is provided. Exactly {exploit} strategies must have mode "exploit": \
each refines the best proven skill (name it in `builds_on`) by changing one or two choices to \
fix the failures the lessons describe. The other {explore} have mode "explore": new combinations \
that avoid failures the lessons already reported.
"""

PLAN_TOOL = {
    "name": "submit_plan",
    "description": "Submit the decision axes and one distinct strategy per agent.",
    "input_schema": {
        "type": "object",
        "properties": {
            "axes": {
                "type": "array", "minItems": 2, "maxItems": 5,
                "items": {"type": "object", "properties": {
                    "name": {"type": "string"},
                    "why_it_matters": {"type": "string"},
                    "options": {"type": "array", "minItems": 2, "items": {"type": "string"}},
                }, "required": ["name", "why_it_matters", "options"]},
            },
            "strategies": {
                "type": "array",
                "items": {"type": "object", "properties": {
                    "name": {"type": "string", "description": "2-5 word label, unique"},
                    "mode": {"type": "string", "enum": ["explore", "exploit"]},
                    "choices": {"type": "object", "additionalProperties": {"type": "string"},
                                "description": "axis name -> chosen option, one entry per axis"},
                    "approach": {"type": "string",
                                 "description": "2-5 sentences of concrete steps using the robot API"},
                    "starting_parameters": {"type": "object",
                                            "description": "parameter name -> starting value"},
                    "risks": {"type": "string", "description": "the main way this could fail"},
                    "builds_on": {"type": "string", "description": "skill/lesson refined; empty if none"},
                }, "required": ["name", "mode", "choices", "approach", "risks"]},
            },
            "coverage_rationale": {"type": "string",
                                   "description": "one or two sentences on why this spread covers the space"},
        },
        "required": ["axes", "strategies", "coverage_rationale"],
    },
}


# ------------------------------------------------------------------ inputs ---

def load_task(task_file: Optional[str] = None, description: Optional[str] = None,
              api_file: Optional[str] = None) -> dict[str, Any]:
    task: dict[str, Any] = json.loads(Path(task_file).read_text()) if task_file else {}
    if description:
        task["description"] = description
    if api_file:
        task["robot_api"] = Path(api_file).read_text()        # e.g. the sim's interfaces.py
    task.setdefault("task_id", "task")
    task.setdefault("max_attempts", 5)
    if not task.get("description"):
        raise ValueError("task needs a description (--task or a task file)")
    if not task.get("robot_api"):
        raise ValueError("task needs the robot API (a task file with robot_api, or --api interfaces.py)")
    return task


def sim_task(task_id: str, observation: str = "telemetry") -> dict[str, Any]:
    """Planner task for a robot_race task: its text, the success check, and the exact API_DOC the
    agents see. Keeps the planner and the agents on one API description."""
    from robot_race.interfaces import api_doc
    from robot_race.tasks import TASKS
    return {"task_id": task_id, "description": TASKS[task_id]["text"],
            "success": "The item comes to rest inside the bin (Env.check_success()). Ranking: solved seeds, "
                       "then fewer tries, less sim time, fewer collisions, less energy.",
            "robot_api": api_doc(observation), "max_attempts": 5}


def observe_sim(task_id: str, seed: int, out_dir: Path,
                observation: str = "telemetry") -> tuple[dict[str, Any], str]:
    """Initial get_state() + front camera PNG from a fresh episode, the same view the agents get first."""
    from robot_race.agent import observe_scene
    state, images = observe_scene(task_id, seed, observation=observation)
    out_dir.mkdir(parents=True, exist_ok=True)
    image = out_dir / f"plan_scene_{task_id}_s{seed}.png"
    image.write_bytes(base64.b64decode(images["front"]))
    return state, str(image)


def memory_from_tracker(tracker, task_id: str, races: int = 1) -> Optional[dict[str, Any]]:
    """Lessons + winning skill from the latest closed races on this task, as stored by the tracker.
    Any other memory source (Memorable, GBrain) can return the same shape instead."""
    try:
        closed = [r for r in tracker.races()
                  if r.get("task") == task_id and r.get("status") == "closed"]
    except Exception as e:                                    # tracker down -> plan without memory
        print(f"[planner] no memory: tracker unreachable ({e})", file=sys.stderr)
        return None
    lessons, skills = [], []
    for race in closed[-races:]:
        ls = tracker.lessons(race["race_id"])
        lessons += [f"[{race['race_id']}] {l}" for l in ls.get("lines", [])]
        if ls.get("skill_eligible") and ls.get("skill") and ls["skill"].get("code"):
            skills.append({**ls["skill"], "race_id": race["race_id"]})
    return {"lessons": lessons, "skills": skills} if (lessons or skills) else None


def _user_content(task: dict[str, Any], n: int, state: Optional[dict], image: Optional[str],
                  memory: Optional[dict]) -> list[dict[str, Any]]:
    parts = [f"# Task\n{task['description']}"]
    if task.get("success"):
        parts.append(f"# Success means\n{task['success']}")
    parts.append(f"# Robot API\n```python\n{task['robot_api'].strip()}\n```")
    if state:
        parts.append(f"# Scene state from get_state()\n```json\n{json.dumps(state, indent=1)}\n```")
    if memory:
        mem = ["# Memory from earlier races"]
        mem += [f"- {l}" for l in memory.get("lessons", [])[:40]]
        for s in memory.get("skills", [])[:2]:
            mem.append(f"\nProven skill from {s.get('race_id', 'a past race')} "
                       f"(strategy \"{s.get('persona') or s.get('agent_id')}\", solve rate "
                       f"{s.get('solve_rate')}, {s.get('mean_tries_to_solve')} tries on average):\n"
                       f"```python\n{(s.get('code') or '')[:3000]}\n```")
        parts.append("\n".join(mem))
    parts.append(f"Plan exactly {n} strategies.")
    content: list[dict[str, Any]] = []
    if image:
        media = mimetypes.guess_type(image)[0] or "image/png"
        content.append({"type": "image", "source": {"type": "base64", "media_type": media,
                                                    "data": base64.b64encode(Path(image).read_bytes()).decode()}})
    content.append({"type": "text", "text": "\n\n".join(parts)})
    return content


# -------------------------------------------------------------- validation ---

def validate(plan: dict[str, Any], n: int, n_exploit: int) -> list[str]:
    problems = []
    axes = [a["name"] for a in plan.get("axes", [])]
    strategies = plan.get("strategies", [])
    if len(strategies) != n:
        problems.append(f"expected {n} strategies, got {len(strategies)}")
    names = [s.get("name", "").strip().lower() for s in strategies]
    if len(set(names)) != len(names):
        problems.append("strategy names must be unique")
    seen: dict[tuple, str] = {}
    for s in strategies:
        choices = {k.strip().lower(): str(v).strip().lower() for k, v in (s.get("choices") or {}).items()}
        missing = [a for a in axes if a.strip().lower() not in choices]
        if missing:
            problems.append(f"strategy '{s.get('name')}' has no choice for axes: {', '.join(missing)}")
        key = tuple(choices.get(a.strip().lower(), "") for a in axes)
        if key in seen:
            problems.append(f"strategies '{seen[key]}' and '{s.get('name')}' make the same choice on every axis")
        seen[key] = s.get("name")
    exploit = sum(1 for s in strategies if s.get("mode") == "exploit")
    if exploit != n_exploit:
        problems.append(f"expected {n_exploit} exploit strategies, got {exploit}")
    return problems


# --------------------------------------------------------------- planning ---

def plan_strategies(task: dict[str, Any], n: int, *, state: Optional[dict] = None,
                    image: Optional[str] = None, memory: Optional[dict] = None,
                    n_exploit: Optional[int] = None, model: str = DEFAULT_MODEL,
                    client=None, max_repairs: int = 1) -> dict[str, Any]:
    """Returns {"task_id", "axes", "strategies": [{agent_id, name, mode, choices, approach, ...}],
    "coverage_rationale", "model", "warnings"}."""
    if client is None:
        import anthropic
        client = anthropic.Anthropic()
    has_skill = bool(memory and memory.get("skills"))
    if n_exploit is None:
        n_exploit = (n + 1) // 2 if has_skill else 0
    n_exploit = min(n_exploit, n) if has_skill else 0
    memory_rule = (MEMORY_RULE.format(exploit=n_exploit, explore=n - n_exploit) if has_skill
                   else MEMORY_RULE_NONE if not memory else
                   "\nLessons from earlier races are provided but no proven skill: every strategy has "
                   "mode \"explore\" and should avoid the failures the lessons describe.\n")
    system = SYSTEM.format(n=n, max_tries=task.get("max_attempts", 5), memory_rule=memory_rule)
    messages: list[dict[str, Any]] = [{"role": "user", "content": _user_content(task, n, state, image, memory)}]

    # tool_choice "auto": Opus 5.5 / Fable 5.1 reject forced tool use (type "tool"/"any" -> 400), so the
    # system prompt asks for submit_plan and a reply without the call gets one nudge.
    plan, problems = {}, []
    for _ in range(max_repairs + 2):
        resp = client.messages.create(model=model, max_tokens=16000, system=system, tools=[PLAN_TOOL],
                                      tool_choice={"type": "auto"}, messages=messages)
        block = next((b for b in resp.content if getattr(b, "type", None) == "tool_use"), None)
        if block is None:
            problems = ["the planner replied without calling submit_plan"]
            messages += [{"role": "assistant", "content": resp.content},
                         {"role": "user", "content": "Call submit_plan now with the full plan."}]
            continue
        plan = dict(block.input)
        problems = validate(plan, n, n_exploit)
        if not problems:
            break
        messages.append({"role": "assistant", "content": resp.content})
        messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": block.id, "is_error": True,
                                                      "content": "Fix these and call submit_plan again:\n- "
                                                                 + "\n- ".join(problems)}]})
    strategies = [{"agent_id": f"agent-{i + 1}", **s} for i, s in enumerate(plan.get("strategies", [])[:n])]
    return {"task_id": task["task_id"], "axes": plan.get("axes", []), "strategies": strategies,
            "coverage_rationale": plan.get("coverage_rationale", ""), "model": model,
            "used_memory": bool(memory), "warnings": problems}


# ------------------------------------------------------ handing to agents ---

def strategy_prompt(strategy: dict[str, Any], plan: Optional[dict[str, Any]] = None) -> str:
    """The block each agent adds to its own prompt. Keeps agents on their strategy across retries."""
    n = len((plan or {}).get("strategies", [])) or "several"
    lines = [f"## Your strategy in this race: {strategy['name']} ({strategy.get('agent_id', '')})",
             f"You are one of {n} agents racing on this task. Each agent was assigned a different "
             "strategy so the race covers different approaches. Implement yours:",
             "", strategy["approach"].strip(), ""]
    if strategy.get("choices"):
        lines.append("Decisions fixed for you:")
        lines += [f"- {axis}: {choice}" for axis, choice in strategy["choices"].items()]
    if strategy.get("starting_parameters"):
        params = ", ".join(f"{k}={v}" for k, v in strategy["starting_parameters"].items())
        lines.append(f"Starting parameters (tune them between tries if the results say so): {params}")
    if strategy.get("risks"):
        lines.append(f"Main risk to watch for: {strategy['risks']}")
    if strategy.get("builds_on"):
        lines.append(f"This refines: {strategy['builds_on']}")
    lines.append("When a try fails, fix it within this strategy (adjust parameters, add checks) "
                 "rather than switching to a different approach.")
    return "\n".join(lines) + "\n"


def load_strategy_prompt(plan_path: str, agent_id: str) -> str:
    plan = json.loads(Path(plan_path).read_text())
    strategy = next((s for s in plan["strategies"] if s["agent_id"] == agent_id), None)
    if strategy is None:
        raise KeyError(f"{agent_id} is not in {plan_path}")
    return strategy_prompt(strategy, plan)


def register_plan(tracker, race_id: str, plan: dict[str, Any]) -> None:
    """Show each agent's strategy on the dashboard."""
    for s in plan["strategies"]:
        tracker.register_agent(race_id, s["agent_id"], persona=s["name"], mode=s.get("mode"),
                               strategy=s.get("approach"), choices=s.get("choices"),
                               risks=s.get("risks"), builds_on=s.get("builds_on") or None,
                               planner_model=plan.get("model"))


# -------------------------------------------------------------------- CLI ---

def _tracker(url: Optional[str] = None):
    try:
        from racetrack.client import Tracker          # imported as a package (repo root on sys.path)
    except ImportError:
        from client import Tracker                    # run from inside racetrack/
    return Tracker(url)


def main():
    ap = argparse.ArgumentParser(description="Plan distinct strategies for a race.")
    ap.add_argument("--sim-task", help="robot_race task id (e.g. can_to_bin): task text + API_DOC + scene "
                                          "from the sim; replaces --task-file/--api/--state/--image")
    ap.add_argument("--seed", type=int, default=0, help="with --sim-task: seed of the scene shown to the planner")
    ap.add_argument("--task-file", help="JSON with description, success, robot_api, max_attempts")
    ap.add_argument("--task", help="task description (overrides the file's)")
    ap.add_argument("--api", help="file documenting the robot API, e.g. the sim's interfaces.py")
    ap.add_argument("--agents", type=int, default=4)
    ap.add_argument("--state", help="JSON from get_state() for the scene")
    ap.add_argument("--image", help="camera image of the scene")
    ap.add_argument("--memory", default="tracker",
                    help="'tracker' (lessons from past races on this task), 'none', or a JSON file")
    ap.add_argument("--exploit", type=int, default=None, help="how many agents refine the proven skill")
    ap.add_argument("--race", metavar="LABEL", help="start a tracker race with this label and register the agents")
    ap.add_argument("--url", default=None, help="tracker URL")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--out", default="plan.json")
    args = ap.parse_args()

    tracker = _tracker(args.url)
    client = None
    if args.sim_task:
        from robot_race.agent import _load_env, make_client
        _load_env()
        client = make_client()
        task = sim_task(args.sim_task)
        if args.task:
            task["description"] = args.task
        state, args.image = observe_sim(args.sim_task, args.seed, Path(args.out).resolve().parent)
    else:
        task = load_task(args.task_file, args.task, args.api)
        state = json.loads(Path(args.state).read_text()) if args.state else None
    memory = (None if args.memory == "none" else
              memory_from_tracker(tracker, task["task_id"]) if args.memory == "tracker" else
              json.loads(Path(args.memory).read_text()))

    plan = plan_strategies(task, args.agents, state=state, image=args.image, memory=memory,
                           n_exploit=args.exploit, model=args.model, client=client)
    if args.race:
        plan["race_id"] = tracker.start_race(label=args.race, task=task["task_id"],
                                             scoring={"max_attempts": task["max_attempts"]})
        register_plan(tracker, plan["race_id"], plan)
    Path(args.out).write_text(json.dumps(plan, indent=2))

    print(f"{len(plan['strategies'])} strategies for {task['task_id']}"
          f"{' (race ' + plan['race_id'] + ')' if plan.get('race_id') else ''} -> {args.out}")
    print("axes:", "; ".join(f"{a['name']} [{' | '.join(a['options'])}]" for a in plan["axes"]))
    for s in plan["strategies"]:
        print(f"  {s['agent_id']} [{s['mode']}] {s['name']}: " + ", ".join(f"{v}" for v in s["choices"].values()))
    for w in plan["warnings"]:
        print("  warning:", w)


if __name__ == "__main__":
    main()
