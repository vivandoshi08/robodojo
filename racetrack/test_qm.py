"""QM path: tracker CLI, plan -> spawn contexts through the tracker, QM run-id parsing in ingest."""
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("RACETRACK_DB", os.path.join(tempfile.mkdtemp(), "test.db"))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from ingest import identify  # noqa: E402
import server  # noqa: E402
from server import app  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "qm"))

import racetrack.client as rc  # noqa: E402
import plan_to_contexts as ptc  # noqa: E402

c = TestClient(app)


@pytest.fixture(autouse=True)
def tracker_via_testclient(monkeypatch):
    def _req(self, method, path, body=None):
        r = c.request(method, path, json=body)
        r.raise_for_status()
        return r.json()
    monkeypatch.setattr(rc.Tracker, "_req", _req)


class FakeHooks:
    """Stands in for robot_race.memory_hooks (GBrain + Memorable live on the host)."""
    def __init__(self):
        self.post = []

    def pre_race_context(self, task, race_id=None):
        return f"# Memory for {task}\n- lower it into the bin", {"episodes": 3}

    def post_race(self, race_id):
        self.post.append(race_id)
        return {"distilled": True}


@pytest.fixture(autouse=True)
def fake_memory(monkeypatch, tmp_path):
    hooks = FakeHooks()
    monkeypatch.setattr(server, "_memory_hooks", lambda: hooks)
    monkeypatch.setattr(server, "RACES_DIR", tmp_path / "races")
    return hooks


def cli(capsys, *argv):
    assert rc.main(list(argv)) == 0
    return json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_cli_start_record_close(tmp_path, capsys):
    race = cli(capsys, "start", "--label", "qm-test", "--task", "can_to_bin")["race_id"]
    for k, ok in ((1, False), (2, True)):
        d = tmp_path / f"place-a{k}" / "seed_0"
        d.mkdir(parents=True)
        (d / "policy.py").write_text("def run(robot): pass\n")
        (d / "result.json").write_text(json.dumps({
            "task": "can_to_bin", "seed": 0, "success": ok, "time_s": 9.0, "collisions": 0,
            "energy_j": 20.0, "dropped": not ok, "lifted": True, "error": None, "frames": [],
            "video": None, "code_path": "policy.py"}))
        assert cli(capsys, "record", "--race-id", race, "--agent-id", "place", "--seed", "0",
                   "--attempt", str(k), "--result", str(d / "result.json")) == {"recorded": True}
    out = cli(capsys, "close", "--race-id", race)
    assert out["winner"]["agent_id"] == "place" and out["memory_post"] == "scheduled"
    assert cli(capsys, "leaderboard", "--race-id", race)["final"] is True


def test_cli_reports_unreachable_tracker(monkeypatch, capsys):
    import urllib.error

    def down(self, method, path, body=None):
        raise urllib.error.URLError("connection refused")
    monkeypatch.setattr(rc.Tracker, "_req", down)
    rc_main = rc.main(["--url", "http://127.0.0.1:9", "leaderboard", "--race-id", "x"])
    assert rc_main == 1 and "unreachable" in capsys.readouterr().out


def test_close_runs_post_race_in_background(fake_memory, capsys):
    race = cli(capsys, "start", "--label", "qm-post")["race_id"]
    cli(capsys, "close", "--race-id", race)
    import threading
    for t in threading.enumerate():
        if t.name == f"post_race-{race}":
            t.join(5)
    assert fake_memory.post == [race]


def test_context_endpoint_caches_and_fails_open(monkeypatch, capsys):
    race = cli(capsys, "start", "--label", "qm-ctx", "--task", "bottle_to_bin")["race_id"]
    out = c.get(f"/races/{race}/context").json()
    assert out["task"] == "bottle_to_bin" and out["context"].startswith("# Memory for bottle_to_bin")
    assert out["memory"] == {"episodes": 3} and out["cached"] is False
    assert c.get(f"/races/{race}/context").json()["cached"] is True

    def boom():
        raise ImportError("no memory_hooks")
    monkeypatch.setattr(server, "_memory_hooks", boom)
    race2 = cli(capsys, "start", "--label", "qm-ctx2")["race_id"]
    out = c.get(f"/races/{race2}/context").json()
    assert out["context"] == "" and "no memory_hooks" in out["error"]


def test_cards_plan_prepare_and_contexts(monkeypatch, capsys):
    monkeypatch.setattr(ptc, "ROOT", Path(tempfile.mkdtemp()))
    assert ptc.main(["prepare", "--task", "can_to_bin", "--seed", "1", "--agents", "2",
                     "--no-planner", "--label", "qm-cards"]) == 0
    prep = json.loads(capsys.readouterr().out)
    assert prep["source"] == "strategies.json" and prep["agents"] == ["place", "drop"] and prep["memory"]
    assert ptc.main(["contexts", "--race-id", prep["race_id"]]) == 0
    ctxs = json.loads(capsys.readouterr().out)
    assert [x["agent_id"] for x in ctxs] == ["place", "drop"]
    x = ctxs[0]
    assert x["role"] == "racer" and x["race_id"] == prep["race_id"] and x["seed"] == 1
    assert "Pick and place" in x["strategy_prompt"] and x["context"].startswith("# Memory for can_to_bin")
    assert x["tracker_url"] == "http://host.docker.internal:8000"


def test_ingest_parses_qm_run_ids():
    assert identify(Path("qm-sbx-1/place-a2/seed_0"), {}, "agent-1") == ("place", 0, 2)
    assert identify(Path("c/push_off-a5/seed_3"), {}, "agent-1") == ("push_off", 3, 5)
    assert identify(Path("agent-2/seed0/attempt3"), {}, "x") == ("agent-2", 0, 3)
    assert identify(Path("c/agent-1-a2/seed_1"), {}, "x") == ("agent-1", 1, 2)


def test_launch_and_status(monkeypatch, tmp_path, capsys):
    import launch as ql
    monkeypatch.setattr(ptc, "ROOT", tmp_path)
    monkeypatch.setattr(ql, "ROOT", tmp_path)
    out = ql.launch_qm_race("can_to_bin", agents=2, seeds=[3, 4], label="qm-launch", planner=False)
    race = out["race_id"]
    assert out["qm"]["message"] == f"Race {race} on can_to_bin seed 3" and out["skipped_seeds"] == [4]
    saved = json.loads((tmp_path / "races" / race / "launch.json").read_text())
    assert saved["backend"] == "qm" and saved["agents"] == ["place", "drop"] and saved["memory"]
    d = tmp_path / "r"
    d.mkdir()
    (d / "result.json").write_text(json.dumps({"seed": 3, "success": True, "time_s": 5.0}))
    cli(capsys, "record", "--race-id", race, "--agent-id", "drop", "--seed", "3", "--attempt", "2",
        "--result", str(d / "result.json"))
    st = ql.qm_status(race)
    assert st["workers"]["drop"] == {"attempts": 2, "solved": True}
    assert st["workers"]["place"] == {"attempts": 0, "solved": False} and st["launch"]["seed"] == 3
