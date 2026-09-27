"""Race tracker API.

    uvicorn server:app --port 8000        (then open http://localhost:8000)

run_agent.py (or the Phase 2 race runner) posts one Attempt per try: the executor's
result.json plus agent_id / seed / attempt. Everything else is computed here.
"""
from __future__ import annotations

import os
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional, Union

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from metrics import enrich
from models import Attempt, RaceCreate, ScoringConfig
from scoring import aggregate, compare, leader, lessons, pick_winner, race_summary, rank
from store import Store

store = Store(os.environ.get("RACETRACK_DB", "racetrack.db"))
MEDIA_ROOT = Path(os.environ.get("RACETRACK_MEDIA_ROOT", ".")).resolve()
app = FastAPI(title="Race tracker")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
HERE = Path(__file__).parent


def _race_or_404(race_id: str) -> dict[str, Any]:
    race = store.get_race(race_id)
    if not race:
        raise HTTPException(404, f"unknown race {race_id}")
    return race


def _compute(race_id: str):
    race = _race_or_404(race_id)
    cfg = ScoringConfig(**race["scoring"])
    agents = store.agents(race_id)
    by_agent: dict[str, list[Attempt]] = defaultdict(list)
    for a in store.attempts(race_id):
        by_agent[a.agent_id].append(a)
    ids = list(agents) + [a for a in by_agent if a not in agents]
    scores = rank([aggregate(aid, by_agent.get(aid, []), cfg, agents.get(aid)) for aid in ids])
    return race, cfg, by_agent, scores


# ------------------------------------------------------------------ races ---

@app.post("/races")
def create_race(req: RaceCreate):
    if req.race_id and store.get_race(req.race_id):
        raise HTTPException(409, f"race {req.race_id} already exists")
    return store.create_race(req)


@app.get("/races")
def list_races():
    out = []
    for race in store.list_races():
        _, _, _, scores = _compute(race["race_id"])
        out.append({**race, "summary": race_summary(race, scores)})
    return out


@app.get("/races/{race_id}")
def get_race(race_id: str):
    return _race_or_404(race_id)


@app.post("/races/{race_id}/close")
def close_race(race_id: str):
    race, cfg, by_agent, scores = _compute(race_id)
    winner = pick_winner(scores, final=True)
    store.close_race(race_id, winner.agent_id if winner else None)
    return {"race": store.get_race(race_id), "winner": winner,
            "lessons": lessons(scores, by_agent, cfg, final=True)}


# ----------------------------------------------------------------- agents ---

@app.post("/races/{race_id}/agents")
def add_agents(race_id: str, body: Union[dict[str, Any], list[dict[str, Any]]]):
    """Optional. Registers persona/mode/model/prompt info so the board shows more than agent ids."""
    _race_or_404(race_id)
    items = body if isinstance(body, list) else [body]
    for info in items:
        if "agent_id" not in info:
            raise HTTPException(422, "each agent needs an agent_id")
        store.upsert_agent(race_id, info)
    return {"registered": len(items)}


@app.get("/races/{race_id}/agents")
def get_agents(race_id: str):
    _race_or_404(race_id)
    return store.agents(race_id)


# --------------------------------------------------------------- attempts ---

@app.post("/races/{race_id}/attempts")
def add_attempts(race_id: str, body: Union[Attempt, list[Attempt]]):
    race = store.get_race(race_id) or store.create_race(RaceCreate(race_id=race_id))
    cfg = ScoringConfig(**race["scoring"])
    items = body if isinstance(body, list) else [body]
    out = []
    for a in items:
        a = enrich(a, cfg)
        store.ensure_agent(race_id, a.agent_id)
        store.upsert_attempt(race_id, a)
        out.append({"agent_id": a.agent_id, "seed": a.seed, "attempt": a.attempt,
                    "failure_mode": a.failure_mode})
    return {"recorded": len(items), "attempts": out}


@app.get("/races/{race_id}/attempts")
def get_attempts(race_id: str, agent_id: Optional[str] = None, seed: Optional[int] = None):
    _race_or_404(race_id)
    rows = store.attempts(race_id, agent_id)
    return [a for a in rows if seed is None or a.seed == seed]


@app.get("/races/{race_id}/attempts/{agent_id}/{seed}/{attempt}")
def get_attempt(race_id: str, agent_id: str, seed: int, attempt: int):
    a = store.get_attempt(race_id, agent_id, seed, attempt)
    if not a:
        raise HTTPException(404, "no such attempt")
    return a


@app.get("/races/{race_id}/attempts/{agent_id}/{seed}/{attempt}/media/{which}")
def get_media(race_id: str, agent_id: str, seed: int, attempt: int, which: str):
    """Serves the GIF/MP4 or key frames the executor recorded. which = video | frame0 | frame1 | ..."""
    a = store.get_attempt(race_id, agent_id, seed, attempt)
    if not a:
        raise HTTPException(404, "no such attempt")
    if which == "video":
        rel = a.video
    elif which.startswith("frame") and which[5:].isdigit() and int(which[5:]) < len(a.frames):
        rel = a.frames[int(which[5:])]
    else:
        rel = None
    if not rel:
        raise HTTPException(404, "not recorded")
    base = (a.model_extra or {}).get("result_dir")
    if Path(rel).is_absolute():
        candidates = [Path(rel)]
    else:
        candidates = ([Path(base) / rel] if base else []) + [MEDIA_ROOT / rel]
    for p in candidates:
        if p.is_file():
            return FileResponse(p)
    raise HTTPException(404, f"file not found: {rel}")


# ---------------------------------------------------------------- results ---

@app.get("/races/{race_id}/leaderboard")
def leaderboard(race_id: str):
    """`winner` is set only once the race is closed; `leader` is who is ahead right now."""
    race, cfg, _, scores = _compute(race_id)
    final = race["status"] == "closed"
    winner = pick_winner(scores, final=True) if final else None
    ahead = leader(scores)
    return {"race": race, "final": final, "scores": scores,
            "winner": winner.agent_id if winner else None,
            "leader": ahead.agent_id if ahead else None,
            "summary": race_summary(race, scores)}


@app.get("/races/{race_id}/lessons")
def get_lessons(race_id: str):
    race, cfg, by_agent, scores = _compute(race_id)
    return lessons(scores, by_agent, cfg, final=race["status"] == "closed")


@app.get("/compare")
def compare_races(race_ids: Optional[str] = None):
    ids = [r.strip() for r in race_ids.split(",")] if race_ids else [r["race_id"] for r in store.list_races()]
    summaries = []
    for rid in ids:
        race, _, _, scores = _compute(rid)
        summaries.append(race_summary(race, scores))
    return compare(summaries)


@app.get("/")
def dashboard():
    return FileResponse(HERE / "dashboard.html")
