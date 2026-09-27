"""CLI: run the Claude agent loop on one task + seed.

    uv run python run_agent.py --task can_to_bin --seed 0 [--tries 5] [--strategy "..."] [--example]
        [--context-file memory.md] [--fast] [--run-id ID] [--runs-dir runs] [--timeout 180]

Exit code 0 if solved, 1 otherwise (2 on setup errors such as a missing API key).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from robot_race.agent import run_agent_loop
from robot_race.tasks import TASKS


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--task", default="can_to_bin", choices=list(TASKS))
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--tries", type=int, default=5)
    ap.add_argument("--strategy", default="", help="strategy card appended to the system prompt")
    ap.add_argument("--example", action="store_true", help="add the reference policy as a worked example")
    ap.add_argument("--context-file", help="recalled memory prepended to the first user turn")
    ap.add_argument("--fast", action="store_true", help="key frames + trajectory only, no mp4")
    ap.add_argument("--run-id", help="override the generated run id")
    ap.add_argument("--runs-dir", default="runs")
    ap.add_argument("--timeout", type=float, default=180, help="per-attempt executor timeout (s)")
    a = ap.parse_args(argv)
    context = Path(a.context_file).read_text() if a.context_file else None
    try:
        s = run_agent_loop(a.task, a.seed, tries=a.tries, strategy=a.strategy or None, context=context,
                           example=a.example, fast=a.fast, run_id=a.run_id, runs_dir=a.runs_dir,
                           timeout_s=a.timeout)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"{s['run_id']}: {s['status']}  ->  {Path(a.runs_dir) / s['run_id'] / 'summary.json'}")
    return 0 if s["status"] == "solved" else 1


if __name__ == "__main__":
    sys.exit(main())
