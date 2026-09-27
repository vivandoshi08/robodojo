"""racetrack planner + tracker wired into the agent loop (mocked Claude, fake executor, in-process tracker)."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("RACETRACK_DB", os.path.join(tempfile.mkdtemp(), "race_test.db"))

from fastapi.testclient import TestClient  # noqa: E402

import run_agent  # noqa: E402
from racetrack.client import Tracker  # noqa: E402
from racetrack.planner import sim_task, strategy_prompt  # noqa: E402
from robot_race import agent  # noqa: E402
from robot_race.interfaces import API_DOC  # noqa: E402
from test_agent import BAD, GOOD, FakeClient, env  # noqa: E402,F401  (env is a fixture)

PLAN = {"task_id": "can_to_bin", "model": "fake-planner", "race_id": None,
        "axes": [{"name": "release", "why_it_matters": "bounce", "options": ["drop", "lower"]}],
        "strategies": [
            {"agent_id": "agent-1", "name": "Lower then open", "mode": "explore", "choices": {"release": "lower"},
             "approach": "Grasp from above, lower into the bin, then open.", "risks": "rim hit"},
            {"agent_id": "agent-2", "name": "Drop from height", "mode": "explore", "choices": {"release": "drop"},
             "approach": "Grasp from above, open over the bin.", "risks": "bounce out"}]}


class AppTracker(Tracker):
    """Tracker whose HTTP calls go to the FastAPI app in-process."""
    def __init__(self):
        super().__init__("http://test")
        from server import app
        self.http = TestClient(app)

    def _req(self, method, path, body=None):
        r = self.http.request(method, path, json=body)
        r.raise_for_status()
        return r.json()


def test_normalize_maps_robot_race_result(tmp_path):
    (tmp_path / "policy.py").write_text(GOOD)
    (tmp_path / "result.json").write_text(json.dumps({"success": True, "energy_j": 2.5, "code_path": "policy.py"}))
    data = Tracker.load_result(tmp_path / "result.json")
    assert data["energy"] == 2.5 and data["code"] == GOOD and data["result_dir"] == str(tmp_path)
    assert Tracker.normalize({"energy": 1.0, "energy_j": 9.0})["energy"] == 1.0  # never overrides


def test_sim_task_uses_the_agents_api_doc():
    t = sim_task("can_to_bin")
    assert t["robot_api"] == API_DOC and t["task_id"] == "can_to_bin" and "can" in t["description"]


def test_race_records_every_attempt_and_picks_the_skill(env):
    tracker = AppTracker()
    race_id = tracker.start_race(label="test", task="can_to_bin", scoring={"seeds_per_agent": 1})
    replies = {"agent-1": [f"```python\n{GOOD}```"],
               "agent-2": [f"```python\n{BAD}```", f"```python\n{BAD}```"]}
    for s in PLAN["strategies"]:
        aid = s["agent_id"]
        summary = agent.run_agent_loop(
            "can_to_bin", 0, tries=2, strategy=strategy_prompt(s, PLAN), client=FakeClient(replies[aid]),
            runs_dir=str(env.runs), verbose=False, race={"race_id": race_id, "agent_id": aid, "strategy_name": s["name"]},
            on_attempt=lambda k, res, adir, aid=aid: tracker.record_attempt(race_id, aid, 0, k, adir / "result.json"))
        assert summary["race"]["agent_id"] == aid and summary["hints"]["strategy"]
        assert slug_of(s["name"]) in summary["run_id"]
    out = tracker.close_race(race_id)
    assert out["winner"]["agent_id"] == "agent-1"
    assert out["lessons"]["skill"]["code"] == GOOD
    board = tracker.leaderboard(race_id)
    two = next(a for a in board["scores"] if a["agent_id"] == "agent-2")
    assert two["attempts_total"] == 2 and two["seeds_solved"] == 0


def slug_of(name):
    return agent.slugify(name)


def test_on_attempt_errors_never_stop_the_run(env):
    def boom(*a):
        raise RuntimeError("tracker exploded")
    s = agent.run_agent_loop("can_to_bin", 0, tries=1, client=FakeClient([f"```python\n{GOOD}```"]),
                             runs_dir=str(env.runs), verbose=False, on_attempt=boom)
    assert s["status"] == "solved"


def test_run_agent_plan_flag_passes_the_strategy_block(tmp_path, monkeypatch):
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({**PLAN, "race_id": "race-x"}))
    seen = {}
    monkeypatch.setattr(run_agent, "run_agent_loop", lambda *a, **kw: seen.update(kw) or
                        {"run_id": "r", "status": "solved"})
    assert run_agent.main(["--plan", str(plan), "--agent-id", "agent-2", "--runs-dir", str(tmp_path)]) == 0
    assert seen["strategy"] == strategy_prompt(PLAN["strategies"][1], PLAN)
    assert seen["race"]["race_id"] == "race-x" and seen["race"]["strategy_name"] == "Drop from height"
    assert callable(seen["on_attempt"])
    with pytest.raises(SystemExit):
        run_agent.main(["--plan", str(plan), "--agent-id", "agent-9"])
