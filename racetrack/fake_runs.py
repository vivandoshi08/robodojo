"""STAND-IN for the real agent loop, so the planner -> tracker -> dashboard path can be tested now.

It fakes each agent's tries (writing Phase 1-format result.json files + a placeholder GIF into
fake_runs/) and records them the way run_agent.py will. Strategies come from a planner file;
nothing about them is hardcoded here. Delete this once the real loop runs.

    python planner.py --task-file tasks/can_to_bin.json --agents 4 --race cold --out cold.json
    python fake_runs.py --plan cold.json
    python planner.py --task-file tasks/can_to_bin.json --agents 4 --race warm --out warm.json   # uses cold's lessons
    python fake_runs.py --plan warm.json --warm
"""
from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path

from client import Tracker

OUT = Path("fake_runs")
FAILURES = ["dropped", "code_error", "missed", "timeout"]
ERRORS = {"code_error": ["Traceback (most recent call last):\nKeyError: 'can_handle'",
                         "Traceback (most recent call last):\nNameError: name 'np' is not defined"],
          "timeout": ["policy exceeded 30 s and was killed"]}


def placeholder_gif() -> bytes:
    from io import BytesIO
    from PIL import Image, ImageDraw
    frames = []
    for i in range(6):
        im = Image.new("RGB", (256, 256), (236, 235, 230))
        d = ImageDraw.Draw(im)
        d.rectangle([150, 150, 230, 230], outline=(90, 90, 90), width=3)
        d.ellipse([170, 40 + i * 28, 200, 70 + i * 28], fill=(42, 120, 214))
        d.text((10, 10), "fake run", fill=(90, 90, 90))
        frames.append(im)
    buf = BytesIO()
    frames[0].save(buf, format="GIF", save_all=True, append_images=frames[1:], duration=150, loop=0)
    return buf.getvalue()


def fake_attempt(race: str, agent: dict, seed: int, attempt: int, warm: bool, gif: bytes) -> Path:
    # Each strategy gets an arbitrary (but stable) skill level and failure habit from its name.
    trait = random.Random(agent.get("name", agent["agent_id"]))
    base, speed, habit = trait.uniform(0.1, 0.4), trait.uniform(8, 17), trait.choice(FAILURES)
    if warm:
        base = 0.85 if agent.get("mode") == "exploit" else base + 0.35
    rng = random.Random(f"{race}:{agent['agent_id']}:{seed}:{attempt}")
    success = rng.random() < base + 0.15 * (attempt - 1)
    mode = None if success else (habit if rng.random() < 0.6 else rng.choice(FAILURES))
    d = OUT / race / agent["agent_id"] / f"seed{seed}" / f"attempt{attempt}"
    d.mkdir(parents=True, exist_ok=True)
    (d / "attempt.gif").write_bytes(gif)
    result = {
        "success": success, "time_s": round(rng.gauss(speed, 1.0), 2),
        "collisions": 1 if rng.random() < 0.1 else 0, "energy": round(rng.uniform(20, 50), 1),
        "dropped": mode == "dropped", "error": rng.choice(ERRORS[mode]) if mode in ERRORS else None,
        "frames": ["f0.png", "f1.png", "f2.png"], "video": "attempt.gif",
        "code": f"# {agent.get('name', agent['agent_id'])}, seed {seed}, try {attempt}\n"
                f"# {agent.get('approach', '')}\ns = robot.get_state()\n...\n",
    }
    (d / "result.json").write_text(json.dumps(result, indent=2))
    return d / "result.json"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", help="plan.json from planner.py (its race_id is used if present)")
    ap.add_argument("--agents", type=int, default=4, help="agents to fake when there is no plan")
    ap.add_argument("--warm", action="store_true", help="pretend memory helped")
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--tries", type=int, default=5)
    ap.add_argument("--delay", type=float, default=0.0)
    ap.add_argument("--url", default=None)
    args = ap.parse_args()

    tracker, gif = Tracker(args.url), placeholder_gif()
    plan = json.loads(Path(args.plan).read_text()) if args.plan else None
    agents = plan["strategies"] if plan else [{"agent_id": f"agent-{i + 1}"} for i in range(args.agents)]
    race_id = (plan or {}).get("race_id") or tracker.start_race(
        label="warm" if args.warm else "cold", task=(plan or {}).get("task_id", "can_to_bin"))

    solved: set = set()
    for seed in range(args.seeds):
        for attempt in range(1, args.tries + 1):         # agents advance together, like a parallel race
            for agent in agents:
                if (agent["agent_id"], seed) in solved:
                    continue
                path = fake_attempt(race_id, agent, seed, attempt, args.warm, gif)
                tracker.record_attempt(race_id, agent["agent_id"], seed, attempt, path)
                if json.loads(path.read_text())["success"]:
                    solved.add((agent["agent_id"], seed))
                if args.delay:
                    time.sleep(args.delay)
    out = tracker.close_race(race_id)
    print(f"== {race_id}")
    for line in out["lessons"]["lines"]:
        print("  -", line)


if __name__ == "__main__":
    main()
