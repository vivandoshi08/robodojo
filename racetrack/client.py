"""Tiny client for run_agent.py and the Phase 2 race runner. Standard library only.

Phase 1 needs one line in run_agent.py, right after the executor writes result.json:

    from client import Tracker
    tracker = Tracker()                                   # http://localhost:8000
    ...
    tracker.record_attempt("phase1", "agent-1", seed, attempt, result_json_path)

`result` can be the result.json path (frames/video are then resolved next to it, so the
dashboard can play the GIF) or the dict you already have in memory.

Phase 2 race runner:

    race_id = tracker.start_race(label="cold", task="can_to_bin")
    tracker.register_agent(race_id, "agent-1", persona="careful", model="claude-...")   # optional
    ...record_attempt(race_id, agent_id, seed, attempt, result)...
    out = tracker.close_race(race_id)     # winner, lessons, and the winning policy code

record_attempt never raises: if the tracker is down the attempt is appended to
runs_backup.jsonl and replay_backup() re-sends it. A dead dashboard can't kill a run.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional, Union


class Tracker:
    def __init__(self, base_url: Optional[str] = None, timeout: float = 3.0,
                 backup_path: str = "runs_backup.jsonl"):
        self.base_url = (base_url or os.environ.get("RACETRACK_URL", "http://localhost:8000")).rstrip("/")
        self.timeout = timeout
        self.backup = Path(backup_path)

    def _req(self, method: str, path: str, body: Any = None) -> Any:
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base_url + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return json.loads(resp.read() or b"null")

    # -------------------------------------------------------------- races ---
    def start_race(self, label: str = "", task: str = "can_to_bin", race_id: Optional[str] = None,
                   scoring: Optional[dict[str, Any]] = None) -> str:
        body: dict[str, Any] = {"label": label, "task": task}
        if race_id:
            body["race_id"] = race_id
        if scoring:
            body["scoring"] = scoring
        return self._req("POST", "/races", body)["race_id"]

    def register_agent(self, race_id: str, agent_id: str, **info: Any) -> None:
        self._req("POST", f"/races/{race_id}/agents", {"agent_id": agent_id, **info})

    def close_race(self, race_id: str) -> dict[str, Any]:
        return self._req("POST", f"/races/{race_id}/close")

    # ----------------------------------------------------------- attempts ---
    @staticmethod
    def load_result(result: Union[dict[str, Any], str, Path]) -> dict[str, Any]:
        """Accept the result.json path or dict. With a path, remember its folder so the
        tracker can find the GIF and key frames the executor wrote next to it."""
        if isinstance(result, (str, Path)):
            p = Path(result).resolve()
            data = json.loads(p.read_text())
            data.setdefault("result_dir", str(p.parent))
        else:
            data = dict(result)
        return Tracker.normalize(data)

    @staticmethod
    def normalize(data: dict[str, Any]) -> dict[str, Any]:
        """robot_race's result.json says energy_j and code_path (policy.py, relative to the attempt
        dir); the tracker wants energy and the code text. Fill those in without overriding anything."""
        if data.get("energy") is None and data.get("energy_j") is not None:
            data["energy"] = data["energy_j"]
        if not data.get("code") and data.get("code_path"):
            p = Path(data["code_path"])
            if not p.is_absolute() and data.get("result_dir"):
                p = Path(data["result_dir"]) / p
            try:
                data["code"] = p.read_text()
            except OSError:
                pass
        return data

    def record_attempt(self, race_id: str, agent_id: str, seed: int, attempt: int,
                       result: Union[dict[str, Any], str, Path], **extra: Any) -> bool:
        try:
            body = {**self.load_result(result), **extra,
                    "agent_id": agent_id, "seed": int(seed), "attempt": int(attempt)}
        except (OSError, ValueError) as e:
            print(f"[tracker] could not read result ({e}); skipped", file=sys.stderr)
            return False
        try:
            self._req("POST", f"/races/{race_id}/attempts", body)
            return True
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            with self.backup.open("a") as f:
                f.write(json.dumps({"race_id": race_id, "attempt": body}) + "\n")
            print(f"[tracker] offline ({e}); saved attempt to {self.backup}", file=sys.stderr)
            return False

    def replay_backup(self) -> int:
        if not self.backup.exists():
            return 0
        items = [json.loads(l) for l in self.backup.read_text().splitlines() if l.strip()]
        for item in items:
            self._req("POST", f"/races/{item['race_id']}/attempts", item["attempt"])
        self.backup.unlink()
        return len(items)

    # ------------------------------------------------------------ results ---
    def races(self) -> list[dict[str, Any]]:
        return self._req("GET", "/races")

    def leaderboard(self, race_id: str) -> dict[str, Any]:
        return self._req("GET", f"/races/{race_id}/leaderboard")

    def lessons(self, race_id: str) -> dict[str, Any]:
        return self._req("GET", f"/races/{race_id}/lessons")

    def compare(self, *race_ids: str) -> dict[str, Any]:
        q = f"?race_ids={','.join(race_ids)}" if race_ids else ""
        return self._req("GET", f"/compare{q}")
