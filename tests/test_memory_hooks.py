"""Memory hooks: result.json -> Episode, episode recall -> context, fail-open without binaries/keys."""
import json

import pytest

from robot_race import memory_hooks as mh


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    monkeypatch.setenv("ROBODOJO_MEMORY_ROOT", str(tmp_path / "mem"))
    monkeypatch.setenv("MEMORABLE_API_KEY", "")  # blank beats .env: never ingest test episodes
    return tmp_path


def _attempt(tmp_path, name, result):
    d = tmp_path / "runs" / name / "attempt_1"
    d.mkdir(parents=True)
    (d / "policy.py").write_text("def run(robot):\n    pass\n")
    (d / "result.json").write_text(json.dumps(result))
    return d


def test_outcomes_and_reasons():
    assert mh.outcome({"success": True}, "can_to_bin") == "in"
    assert mh.outcome({"lifted": False}, "can_to_bin") == "no_grasp"
    assert mh.outcome({"lifted": True, "dropped": True}, "can_to_bin") == "dropped_early"
    assert mh.outcome({"lifted": True, "item_final_pos": [0.45, 0.30, 0.1]}, "can_to_bin") == "rim_out"
    assert mh.outcome({"lifted": True, "item_final_pos": [0.0, -0.5, 0.2]}, "can_to_bin") == "miss"
    assert mh.failure_reason({"success": True}) is None
    assert mh.failure_reason({"error": "Traceback\nValueError: x"}) == "crashed (ValueError: x)"
    assert mh.failure_reason({"lifted": False}) == "never lifted the item"


def test_record_and_recall(isolated_store):
    fail = _attempt(isolated_store, "r1", {"success": False, "lifted": False, "time_s": 9.0})
    win = _attempt(isolated_store, "r2", {"success": True, "lifted": True, "time_s": 7.5, "collisions": 1})
    out = mh.on_attempt_memorable(1, json.loads((fail / "result.json").read_text()), fail,
                                  task="can_to_bin", seed=0, strategy="top-grasp", race_id="race-1",
                                  agent_id="agent-1")
    assert out["success"] is False and not out["ingested"]
    mh.on_attempt_memorable(1, json.loads((win / "result.json").read_text()), win, task="can_to_bin",
                            seed=1, strategy="top-grasp", race_id="race-1", agent_id="agent-1")
    eps = mh.task_episodes("can_to_bin")
    assert len(eps) == 2 and eps[0].extra["code_sha256"]
    assert mh.task_episodes("box_to_bin") == []
    text, stats = mh.episode_summary("can_to_bin")
    assert stats["episodes"] == 2 and stats["successes"] == 1
    assert "top-grasp" in text and "never lifted the item" in text

    ctx, info = mh.recall("can_to_bin", think=False)
    assert info["has_memory"] and "Episode memory" in ctx
    assert info["memorable_hits"] == []  # no key -> no cloud recall


def test_cold_start_has_no_episode_memory(monkeypatch):
    monkeypatch.setattr(mh, "gbrain_skill", lambda: "")
    ctx, info = mh.recall("paper_to_bin")
    assert ctx == "" and not info["has_memory"]


def test_chain_keeps_going_after_a_failing_hook():
    seen = []

    def boom(k, r, d):
        raise RuntimeError("tracker down")

    hook = mh.chain(None, boom, lambda k, r, d: seen.append(k))
    hook(3, {}, None)
    assert seen == [3]
    assert mh.chain(None, None) is None


def test_distill_without_bun(monkeypatch, tmp_path):
    monkeypatch.setattr(mh, "_bun", lambda: None)
    assert mh.distill(tmp_path) == {"distilled": False, "error": "bun not found"}


class _Proc:
    def __init__(self, stdout="", returncode=0, stderr=""):
        self.stdout, self.returncode, self.stderr = stdout, returncode, stderr


def test_think_goes_into_context_and_planner(monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if cmd[1] == "think":
            return _Proc(json.dumps({"answer": "## What worked\n- lower the can into the bin\n- avoid tossing",
                                     "citations": [{"page_slug": "procedures/trash-to-bin"}], "gaps": ["x"]}))
        return _Proc()  # put

    monkeypatch.setattr(mh, "_gbrain", lambda: "/bin/gbrain")
    monkeypatch.setattr(mh.subprocess, "run", fake_run)
    monkeypatch.setattr(mh, "gbrain_skill", lambda: "## Proven rules\n_Distilled from 6 finished runs_")
    ctx, info = mh.recall("can_to_bin", race_id="r1")
    assert "## Past races vs now (gbrain think)" in ctx and "lower the can" in ctx
    assert info["think"]["saved_slug"] == "thinks/r1-can_to_bin"
    assert info["think"]["cited_pages"] == ["procedures/trash-to-bin"]
    assert any(c[1] == "put" and c[2] == "thinks/r1-can_to_bin" for c in calls)
    assert mh.think_to_lessons(info) == ["[gbrain think] lower the can into the bin", "[gbrain think] avoid tossing"]


def test_think_fails_open(monkeypatch):
    monkeypatch.setattr(mh, "_gbrain", lambda: None)
    monkeypatch.setattr(mh, "gbrain_skill", lambda: "_Distilled from 1 finished runs_")
    ctx, info = mh.recall("can_to_bin")
    assert info["think"]["used"] is False and "Distilled skill" in ctx


def test_post_race_writes_race_page_from_files(monkeypatch, tmp_path):
    d = tmp_path / "races" / "r9"
    d.mkdir(parents=True)
    (d / "plan.json").write_text(json.dumps({"task_id": "can_to_bin", "strategies": [
        {"agent_id": "agent-1", "name": "Top grasp", "mode": "explore", "choices": {"grasp": "top"},
         "approach": "descend and pinch"}]}))
    (d / "race.json").write_text(json.dumps({"task": "can_to_bin", "runs": [{"status": "solved"}]}))
    (d / "close.json").write_text(json.dumps({
        "winner": {"agent_id": "agent-1"},
        "lessons": {"lines": ["agent-1 solved 1/1"], "skill": {"agent_id": "agent-1", "seed": 0, "attempt": 2,
                                                              "code": "def run(robot):\n    pass"}},
        "scores": [{"rank": 1, "agent_id": "agent-1", "score": 90.0, "seeds_solved": 1, "seeds_target": 1,
                    "first_try_rate": 0.0}]}))
    puts = {}
    monkeypatch.setattr(mh, "distill", lambda runs_dir: {"distilled": True})
    monkeypatch.setattr(mh, "gbrain_put", lambda slug, md: puts.setdefault(slug, md) is not None)
    out = mh.post_race("r9", races_dir=tmp_path / "races")
    page = puts["races/r9"]
    assert out["race_page"]["saved"] and out["race_page"]["source"] == "files"
    for s in ("Top grasp", "[[procedures/trash-to-bin]]", "agent-1 solved 1/1", "def run(robot)", "| 1 | agent-1"):
        assert s in page
    assert (d / "race_page.md").exists() and "post_race" in json.loads((d / "memory.json").read_text())


def test_post_race_falls_back_to_tracker(monkeypatch, tmp_path):
    import racetrack.client as rc

    class FakeTracker:
        def __init__(self, *a, **k): pass
        def leaderboard(self, rid): return {"winner": {"agent_id": "agent-2"}, "scores": []}
        def lessons(self, rid): return {"lines": ["qm lesson"]}
        def _req(self, m, path): return [{"agent_id": "agent-2", "persona": "Toss"}] if path.endswith("agents") \
            else {"task": "can_to_bin"}

    monkeypatch.setattr(rc, "Tracker", FakeTracker)
    monkeypatch.setattr(mh, "distill", lambda runs_dir: {"distilled": True})
    puts = {}
    monkeypatch.setattr(mh, "gbrain_put", lambda slug, md: puts.setdefault(slug, md) is not None)
    out = mh.post_race("qm-race", races_dir=tmp_path / "races")
    assert out["race_page"]["source"] == "tracker"
    assert "qm lesson" in puts["races/qm-race"] and "Toss" in puts["races/qm-race"]
