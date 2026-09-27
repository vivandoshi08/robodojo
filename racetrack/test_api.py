import json
import os
import tempfile
from pathlib import Path

os.environ["RACETRACK_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")

from fastapi.testclient import TestClient  # noqa: E402

from client import Tracker  # noqa: E402
from ingest import identify  # noqa: E402
from server import app  # noqa: E402

c = TestClient(app)

# Exactly the result.json shape frozen in the Phase 1 plan.
def result(success, **kw):
    base = {"success": success, "time_s": 12.0, "collisions": 0, "energy": 30.0, "dropped": False,
            "error": None, "frames": ["f0.png", "f1.png", "f2.png"], "video": "attempt.gif",
            "code": "robot.move_to(robot.get_state()['objects']['can'])"}
    base.update(kw)
    return base


def post(rid, agent, seed, attempt, res):
    return c.post(f"/races/{rid}/attempts", json={**res, "agent_id": agent, "seed": seed, "attempt": attempt})


def test_phase2_race_flow():
    rid = c.post("/races", json={"label": "cold", "task": "can_to_bin",
                                 "scoring": {"seeds_per_agent": 2}}).json()["race_id"]
    c.post(f"/races/{rid}/agents", json=[{"agent_id": "agent-1", "persona": "careful", "mode": "explore"},
                                         {"agent_id": "agent-2", "persona": "fast"}])
    r = post(rid, "agent-1", 0, 1, result(False, dropped=True)).json()
    assert r["attempts"][0]["failure_mode"] == "dropped"
    post(rid, "agent-1", 0, 2, result(True))
    post(rid, "agent-2", 0, 1, result(True, time_s=8.0))

    lb = c.get(f"/races/{rid}/leaderboard").json()
    assert lb["winner"] is None and lb["leader"] == "agent-2"
    a1 = next(s for s in lb["scores"] if s["agent_id"] == "agent-1")
    assert a1["persona"] == "careful" and a1["mean_tries_to_solve"] == 2.0 and not a1["complete"]
    assert "code" not in json.dumps(a1["seeds"])            # leaderboard stays light

    post(rid, "agent-1", 1, 1, result(True))
    for k in range(1, 6):                                    # agent-2 burns all 5 tries on seed 1
        post(rid, "agent-2", 1, k, result(False, error="Traceback...\nTimeoutError: policy exceeded 30 s"))
    post(rid, "agent-2", 1, 5, result(False, error="Traceback...\nTimeoutError: policy exceeded 30 s"))  # retry-safe

    closed = c.post(f"/races/{rid}/close").json()
    assert closed["race"]["winner_agent"] == "agent-1"
    skill = closed["lessons"]["skill"]
    assert skill["agent_id"] == "agent-1" and skill["code"].startswith("robot.move_to")
    lb = c.get(f"/races/{rid}/leaderboard").json()
    a2 = next(s for s in lb["scores"] if s["agent_id"] == "agent-2")
    assert a2["attempts_total"] == 6 and a2["failure_modes"] == {"timeout": 5}
    assert a2["top_error"] == "TimeoutError: policy exceeded 30 s"

    full = c.get(f"/races/{rid}/attempts/agent-1/0/2").json()
    assert full["code"] and full["success"]
    assert any(r["race_id"] == rid for r in c.get("/compare").json()["races"])


def test_unknown_race_and_agent_are_auto_created():
    assert post("phase1", "agent-1", 0, 1, result(True)).status_code == 200
    assert c.get("/races/phase1/leaderboard").json()["scores"][0]["agent_id"] == "agent-1"


def test_result_json_path_resolves_media(tmp_path: Path):
    d = tmp_path / "seed0" / "attempt1"
    d.mkdir(parents=True)
    (d / "attempt.gif").write_bytes(b"GIF89a")
    (d / "result.json").write_text(json.dumps(result(True)))
    body = {**Tracker.load_result(d / "result.json"), "agent_id": "agent-1", "seed": 0, "attempt": 1}
    c.post("/races/media-test/attempts", json=body)
    r = c.get("/races/media-test/attempts/agent-1/0/1/media/video")
    assert r.status_code == 200 and r.content == b"GIF89a"
    assert c.get("/races/media-test/attempts/agent-1/0/1/media/frame0").status_code == 404  # not on disk


def test_ingest_identifies_from_folders():
    assert identify(Path("agent-2/seed_3/attempt_4"), {}, "agent-1") == ("agent-2", 3, 4)
    assert identify(Path("seed0/try2"), {}, "agent-1") == ("agent-1", 0, 2)
    assert identify(Path("x"), {"seed": 1, "attempt": 2}, "agent-1") == ("agent-1", 1, 2)
    assert identify(Path("seed0"), {}, "agent-1") is None


def test_dashboard_served():
    assert c.get("/").status_code == 200
