"""Load result.json files that already exist on disk, with no change to run_agent.py.

    python ingest.py runs/ --race phase1
    python ingest.py runs/ --race phase1 --agent agent-1 --watch     # keep picking up new attempts

Identity comes from fields inside result.json if present (agent_id, seed, attempt), otherwise
from the folder names: "seed0" / "seed_0", "attempt2" / "try_2", "agent-1" / "agent_a", and QM
sandbox run ids "<strategy>-a<k>" (runs/qm/<container>/place-a2/seed_0/result.json -> agent "place",
attempt 2; see qm/collect_runs.sh).
Files it can't place are listed and skipped.
"""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
from typing import Optional

from client import Tracker

SEED = re.compile(r"seed[_-]?(\d+)", re.I)
ATTEMPT = re.compile(r"(?:attempt|try)[_-]?(\d+)", re.I)
AGENT = re.compile(r"(agent[_-]?[A-Za-z0-9]+)", re.I)
QM_RUN = re.compile(r"(?:^|/)([A-Za-z0-9_.]+(?:-[A-Za-z0-9_.]+)*?)-a(\d+)(?=/|$)")


def identify(rel_dir: Path, data: dict, default_agent: str) -> Optional[tuple[str, int, int]]:
    """rel_dir is the result.json folder relative to the ingest root, so the user's own
    directory names (e.g. ~/run_agent_out) can't be mistaken for ids."""
    text = rel_dir.as_posix()
    seed = data.get("seed")
    attempt = data.get("attempt")
    agent = data.get("agent_id")
    if (qm := QM_RUN.findall(text)):
        if attempt is None:
            attempt = int(qm[-1][1])
        if agent is None and not AGENT.search(text):
            agent = qm[-1][0]
    if seed is None and (m := SEED.findall(text)):
        seed = int(m[-1])
    if attempt is None and (m := ATTEMPT.findall(text)):
        attempt = int(m[-1])
    if agent is None:
        m = AGENT.findall(text)
        agent = m[-1] if m else default_agent
    if seed is None or attempt is None:
        return None
    return agent, int(seed), int(attempt)


def ingest(root: Path, tracker: Tracker, race_id: str, default_agent: str, seen: set) -> int:
    sent = 0
    for p in sorted(root.rglob("result.json")):
        key = (str(p), p.stat().st_mtime)
        if key in seen:
            continue
        seen.add(key)
        try:
            data = json.loads(p.read_text())
        except (OSError, ValueError) as e:
            print(f"skip {p}: {e}")
            continue
        ident = identify(p.parent.relative_to(root), data, default_agent)
        if not ident:
            print(f"skip {p}: can't tell seed/attempt from the path or the file")
            continue
        agent, seed, attempt = ident
        if tracker.record_attempt(race_id, agent, seed, attempt, p):
            sent += 1
    return sent


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("root", type=Path)
    ap.add_argument("--race", default="phase1")
    ap.add_argument("--agent", default="agent-1", help="agent id when the path doesn't name one")
    ap.add_argument("--url", default=None)
    ap.add_argument("--watch", action="store_true", help="poll for new result.json files every 2 s")
    args = ap.parse_args()
    tracker, seen = Tracker(args.url), set()
    while True:
        n = ingest(args.root, tracker, args.race, args.agent, seen)
        if n or not args.watch:
            print(f"recorded {n} attempt(s) into {args.race}")
        if not args.watch:
            break
        time.sleep(2)


if __name__ == "__main__":
    main()
