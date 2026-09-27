"""Agent loop with a mocked Claude client and a fake executor (no network, no sim episode)."""
from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from robot_race import agent  # noqa: E402

GOOD = "def run(robot):\n    robot.open_gripper()\n"
BAD = "def run(robot):\n    robot.move_to([9, 9, 9])\n"
REPLIES = [
    "I think I should just move. No code this time.",
    f"Plan: try something.\n```python\n# draft\n```\nFinal:\n```python\n{BAD}```",
    f"Revised.\n```python\n{GOOD}```",
]


class FakeClient:
    def __init__(self, replies, fail_first=0):
        self.replies, self.calls, self.fail_first = list(replies), [], fail_first
        self.messages = self

    def create(self, **kw):
        self.calls.append(json.loads(json.dumps(kw)))  # snapshot: the loop mutates its history
        if self.fail_first:
            self.fail_first -= 1
            raise ConnectionError("flaky network")
        text = self.replies.pop(0)
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text", text=text)],
            usage=types.SimpleNamespace(input_tokens=100, output_tokens=10, cache_read_input_tokens=0,
                                        cache_creation_input_tokens=0))


def fake_run_policy(code, task, seed, out_dir, timeout_s=180, fast=False, live=True):
    from PIL import Image
    out = Path(out_dir)
    (out / "policy.py").write_text(code)
    ok = "open_gripper" in code
    keys = []
    for i in range(4):
        Image.fromarray(np.full((24, 32, 3), 40 * i, np.uint8)).save(out / f"key_{i}.png")
        keys.append(f"key_{i}.png")
    r = dict(task=task, seed=seed, success=ok, time_s=3.2, collisions=0 if ok else 2, energy_j=1.5,
             dropped=False, lifted=ok, error=None if ok else "Traceback ...\nValueError: unreachable",
             frames=keys, video=None, code_path="policy.py", item_final_pos=[0.45, 0.35, 0.05],
             calls=[f"move_to({i})" for i in range(20)], wall_s=0.1,
             media={"video": None, "poster": None, "keyframes": keys, "trajectory": None, "live": None})
    (out / "result.json").write_text(json.dumps(r))
    return r


@pytest.fixture
def env(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "robot_race.executor", types.SimpleNamespace(run_policy=fake_run_policy))
    viewer_calls = []
    monkeypatch.setitem(sys.modules, "robot_race.viewer", types.SimpleNamespace(
        write_index=lambda d: viewer_calls.append(("index", d)),
        update_manifest=lambda r="runs": viewer_calls.append(("manifest", r))))
    img = agent.png_b64(np.zeros((48, 64, 3), np.uint8))
    monkeypatch.setattr(agent, "observe_scene", lambda task, seed: ({"item": {"pos": [0.5, -0.2, 0.25]}},
                                                                     {"front": img, "top": img}))
    monkeypatch.setattr(agent, "_sleep", lambda s: None)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    return types.SimpleNamespace(runs=tmp_path / "runs", viewer_calls=viewer_calls)


def test_extract_code_picks_last_python_block():
    assert agent.extract_code(REPLIES[1]) == BAD
    assert agent.extract_code(REPLIES[0]) is None
    assert agent.extract_code("```py\nx = 1\n```\n```python\ny = 2\n```") == "y = 2\n"
    assert agent.extract_code("```\ndef run(robot):\n    pass\n```") == "def run(robot):\n    pass\n"


def test_run_id_and_slug():
    rid = agent.make_run_id("can_to_bin", 3, "Lower it gently, then release!!")
    assert rid.endswith("-can_to_bin-s3-lower-it-gently-then")
    assert len(rid.split("-s3-")[1]) <= 20
    assert agent.make_run_id("can_to_bin", 0).endswith("-can_to_bin-s0")


def test_system_prompt():
    p = agent.system_prompt("go slow")
    assert "ROBOT API" in p and "```python" in p and p.endswith("Strategy card: go slow")
    assert "Strategy card" not in agent.system_prompt(None)


def test_loop_no_code_then_fail_then_success(env):
    client = FakeClient(REPLIES, fail_first=1)
    s = agent.run_agent_loop("can_to_bin", 0, tries=5, strategy="careful grasp", context="MEMORY: go slow",
                             example=True, runs_dir=str(env.runs), client=client, verbose=False)
    run_dir = env.runs / s["run_id"]
    assert s["status"] == "solved" and s["solved_at"] == 3 and len(s["attempts"]) == 3
    assert len(client.calls) == 4  # 1 retried API error + 3 replies
    assert s["model"] == agent.DEFAULT_MODEL and s["usage"]["input_tokens"] == 300
    assert s["run_id"].endswith("-can_to_bin-s0-careful-grasp")

    # summary.json on disk == returned summary; attempts carry paths + results
    disk = json.loads((run_dir / "summary.json").read_text())
    assert disk["status"] == "solved" and [a["k"] for a in disk["attempts"]] == [1, 2, 3]
    a1, a2, a3 = disk["attempts"]
    assert a1["code_path"] is None and a1["result"]["error"] == "no python code block"
    assert a2["code_path"] == "attempt_2/policy.py" and a2["result"]["success"] is False
    assert a3["result"]["success"] is True and a3["code"] == GOOD
    for k in (1, 2, 3):
        assert (run_dir / f"attempt_{k}" / "response.md").read_text() == REPLIES[k - 1]
        assert (run_dir / f"attempt_{k}" / "result.json").exists()
    assert (run_dir / "attempt_2" / "policy.py").read_text() == BAD
    assert not list(run_dir.glob("*.tmp"))

    # events.jsonl ordering
    ev = [json.loads(l) for l in (run_dir / "events.jsonl").read_text().splitlines()]
    assert [(e["type"], e["attempt"]) for e in ev] == [
        ("run_started", None),
        ("attempt_started", 1), ("attempt_finished", 1),
        ("attempt_started", 2), ("code_generated", 2), ("attempt_finished", 2),
        ("attempt_started", 3), ("code_generated", 3), ("attempt_finished", 3),
        ("run_finished", None)]
    assert all(e["run_id"] == s["run_id"] and isinstance(e["ts"], float) for e in ev)
    assert ev[4]["code"] == BAD and ev[5]["result"]["collisions"] == 2
    assert ev[-1]["status"] == "solved" and ev[-1]["solved_at"] == 3

    # prompts: system cached + strategy; first turn has memory, task, 2 images, example
    first = client.calls[1]
    assert first["model"] == agent.DEFAULT_MODEL
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert first["system"][0]["text"].endswith("Strategy card: careful grasp")
    c0 = first["messages"][0]["content"]
    assert c0[0]["text"].startswith("Recalled memory") and "MEMORY: go slow" in c0[0]["text"]
    assert "Put the red soda can" in c0[1]["text"]
    assert sum(b["type"] == "image" for b in c0) == 2
    assert any("reference policy" in b.get("text", "") for b in c0)

    # feedback after no-code, then after the failing policy (result JSON + 4 key frames)
    last = client.calls[-1]["messages"]
    assert [m["role"] for m in last] == ["user", "assistant", "user", "assistant", "user"]
    assert "no python code block" in json.dumps(last[2]["content"])
    fb = last[4]["content"]
    assert fb[-1]["text"] == "Revise run(robot)." and fb[-1]["cache_control"] == {"type": "ephemeral"}
    res = json.loads(fb[0]["text"].split("```json\n")[1].split("\n```")[0])
    assert res["success"] is False and "unreachable" in res["error"] and res["lifted"] is False
    assert res["item_final_pos"] == [0.45, 0.35, 0.05] and len(res["calls_tail"]) == 12
    imgs = [b for b in fb if b["type"] == "image"]
    assert len(imgs) == 4 and imgs[0]["source"]["media_type"] == "image/png"
    # only the newest user turn carries a cache breakpoint
    assert "cache_control" not in json.dumps(last[2]["content"])

    # viewer refreshed after every attempt and at the end
    assert env.viewer_calls.count(("manifest", str(env.runs))) >= 4


def test_loop_exhausts_tries(env):
    s = agent.run_agent_loop("can_to_bin", 1, tries=2, runs_dir=str(env.runs), verbose=False,
                             client=FakeClient([REPLIES[1], REPLIES[1]]), run_id="fixed-id")
    assert s["run_id"] == "fixed-id" and s["status"] == "failed" and s["solved_at"] is None
    ev = [json.loads(l) for l in (env.runs / "fixed-id" / "events.jsonl").read_text().splitlines()]
    assert ev[-1]["type"] == "run_finished" and ev[-1]["status"] == "failed"


def test_api_hard_failure_marks_error(env):
    s = agent.run_agent_loop("can_to_bin", 0, tries=3, runs_dir=str(env.runs), verbose=False,
                             client=FakeClient([], fail_first=99))
    assert s["status"] == "error" and "flaky network" in s["error"]
    disk = json.loads((env.runs / s["run_id"] / "summary.json").read_text())
    assert disk["status"] == "error" and disk["attempts"] == []


def test_missing_api_key(env, monkeypatch):
    monkeypatch.setattr(agent, "_load_env", lambda: None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        agent.run_agent_loop("can_to_bin", 0, runs_dir=str(env.runs), verbose=False)
