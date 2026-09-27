"""SQLite persistence. One file, no server, safe to share across threads."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Optional

from models import Attempt, RaceCreate

SCHEMA = """
CREATE TABLE IF NOT EXISTS races (
    race_id TEXT PRIMARY KEY,
    label TEXT, task TEXT,
    status TEXT DEFAULT 'open',
    config_json TEXT, winner_agent TEXT,
    created_at REAL, closed_at REAL
);
CREATE TABLE IF NOT EXISTS agents (
    race_id TEXT, agent_id TEXT, info_json TEXT, created_at REAL,
    PRIMARY KEY (race_id, agent_id)
);
CREATE TABLE IF NOT EXISTS attempts (
    race_id TEXT, agent_id TEXT, seed INTEGER, attempt INTEGER,
    result_json TEXT, created_at REAL,
    PRIMARY KEY (race_id, agent_id, seed, attempt)
);
"""


class Store:
    def __init__(self, path: str = "racetrack.db"):
        self._db = sqlite3.connect(path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.commit()

    def _exec(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
            self._db.commit()
            return rows

    # ------------------------------------------------------------ races ---
    def create_race(self, req: RaceCreate) -> dict[str, Any]:
        race_id = req.race_id
        if not race_id:
            n = self._exec("SELECT COUNT(*) AS n FROM races")[0]["n"] + 1
            while self.get_race(f"race-{n:03d}"):
                n += 1
            race_id = f"race-{n:03d}"
        self._exec("INSERT INTO races (race_id, label, task, config_json, created_at) VALUES (?, ?, ?, ?, ?)",
                   (race_id, req.label, req.task, req.scoring.model_dump_json(), time.time()))
        return self.get_race(race_id)

    def get_race(self, race_id: str) -> Optional[dict[str, Any]]:
        rows = self._exec("SELECT * FROM races WHERE race_id = ?", (race_id,))
        if not rows:
            return None
        race = dict(rows[0])
        race["scoring"] = json.loads(race.pop("config_json"))
        return race

    def list_races(self) -> list[dict[str, Any]]:
        ids = [r["race_id"] for r in self._exec("SELECT race_id FROM races ORDER BY created_at")]
        return [self.get_race(i) for i in ids]

    def close_race(self, race_id: str, winner_agent: Optional[str]) -> None:
        self._exec("UPDATE races SET status = 'closed', winner_agent = ?, closed_at = ? WHERE race_id = ?",
                   (winner_agent, time.time(), race_id))

    # ----------------------------------------------------------- agents ---
    def upsert_agent(self, race_id: str, info: dict[str, Any]) -> None:
        self._exec("INSERT INTO agents (race_id, agent_id, info_json, created_at) VALUES (?, ?, ?, ?) "
                   "ON CONFLICT(race_id, agent_id) DO UPDATE SET info_json = excluded.info_json",
                   (race_id, info["agent_id"], json.dumps(info), time.time()))

    def ensure_agent(self, race_id: str, agent_id: str) -> None:
        if not self._exec("SELECT 1 FROM agents WHERE race_id = ? AND agent_id = ?", (race_id, agent_id)):
            self.upsert_agent(race_id, {"agent_id": agent_id})

    def agents(self, race_id: str) -> dict[str, dict[str, Any]]:
        rows = self._exec("SELECT agent_id, info_json FROM agents WHERE race_id = ? ORDER BY created_at", (race_id,))
        return {r["agent_id"]: json.loads(r["info_json"]) for r in rows}

    # --------------------------------------------------------- attempts ---
    def upsert_attempt(self, race_id: str, a: Attempt) -> None:
        """Re-posting the same (agent, seed, attempt) replaces it, so retries are safe."""
        self._exec("INSERT INTO attempts (race_id, agent_id, seed, attempt, result_json, created_at) "
                   "VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(race_id, agent_id, seed, attempt) DO UPDATE SET "
                   "result_json = excluded.result_json, created_at = excluded.created_at",
                   (race_id, a.agent_id, a.seed, a.attempt, a.model_dump_json(), time.time()))

    def attempts(self, race_id: str, agent_id: Optional[str] = None) -> list[Attempt]:
        sql, args = "SELECT result_json FROM attempts WHERE race_id = ?", [race_id]
        if agent_id:
            sql += " AND agent_id = ?"
            args.append(agent_id)
        rows = self._exec(sql + " ORDER BY created_at", tuple(args))
        return [Attempt.model_validate_json(r["result_json"]) for r in rows]

    def get_attempt(self, race_id: str, agent_id: str, seed: int, attempt: int) -> Optional[Attempt]:
        rows = self._exec("SELECT result_json FROM attempts WHERE race_id = ? AND agent_id = ? AND seed = ? "
                          "AND attempt = ?", (race_id, agent_id, seed, attempt))
        return Attempt.model_validate_json(rows[0]["result_json"]) if rows else None
