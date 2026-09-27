"""Integrity: by default the model gets only the environment; hints/scripted runs are recorded and flagged;
the executor only runs the exact code block from the model reply (docs/INTEGRITY.md)."""
from __future__ import annotations

import hashlib
import inspect
import json
import sys
import types
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from robot_race import agent, executor, viewer  # noqa: E402
from robot_race.interfaces import API_DOC  # noqa: E402
from test_agent import BAD, GOOD, FakeClient, fake_run_policy  # noqa: E402

REPLIES = [f"Plan A.\n```python\n{BAD}```", f"Plan B.\n```python\n{GOOD}```"]
# Lines of the hand-written solution that can't appear in a default request by coincidence (API_DOC names
# the same calls, e.g. robot.open_gripper(), and the canned replies here are in the history).
_LEGIT = API_DOC + agent.OUTPUT_RULES + "".join(REPLIES)
REF_LINES = [l.strip() for l in agent.REFERENCE_POLICY.read_text().splitlines()
             if len(l.strip()) > 12 and l.strip() not in _LEGIT and not l.strip().startswith(("import", "def run"))]
NO_HINTS = {"example": False, "strategy": None, "context": False}


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Real viewer + trace, fake executor and scene (no sim, no network)."""
    monkeypatch.setitem(sys.modules, "robot_race.executor", types.SimpleNamespace(run_policy=fake_run_policy))
    img = agent.png_b64(np.full((48, 64, 3), 5, np.uint8))
    monkeypatch.setattr(agent, "observe_scene", lambda task, seed: ({"item": {"pos": [0.5, -0.2, 0.25]}},
                                                                     {"front": img, "top": img}))
    monkeypatch.setattr(agent, "_sleep", lambda s: None)
    return types.SimpleNamespace(runs=tmp_path / "runs")


def _all_request_text(run_dir: Path) -> str:
    return "\n".join(p.read_text() for p in sorted(run_dir.glob("transcript/turn_*/request.json")))


def test_default_run_has_no_hints_and_no_reference_code(env):
    s = agent.run_agent_loop("can_to_bin", 0, tries=2, runs_dir=str(env.runs), client=FakeClient(REPLIES),
                             verbose=False)
    run_dir = env.runs / s["run_id"]
    disk = json.loads((run_dir / "summary.json").read_text())
    assert disk["hints"] == NO_HINTS and disk["hinted"] is False and disk["example"] is False
    # nothing from the hand-written solution anywhere in what was sent
    sent = _all_request_text(run_dir)
    assert REF_LINES and not [l for l in REF_LINES if l in sent]
    assert "reference policy" not in sent.lower() and "worked example" not in sent.lower()
    assert "Strategy card" not in sent and "Recalled memory" not in sent
    # system prompt = role + API_DOC + output rules, nothing else
    req = json.loads((run_dir / "transcript/turn_1/request.json").read_text())
    assert req["system"][0]["text"] == "\n\n".join([agent.SYSTEM_ROLE, API_DOC, agent.OUTPUT_RULES])
    # first user turn: task + state, two captioned images, "Write run(robot)."
    c0 = req["messages"][0]["content"]
    assert [b["type"] for b in c0] == ["text", "text", "image", "text", "image", "text"]
    assert c0[0]["text"].startswith("TASK: ") and c0[-1]["text"] == "Write run(robot)."
    # no hint banner on the pages; manifest says not hinted
    assert "Hint given" not in (run_dir / "trace.html").read_text()
    assert "Hint given" not in (run_dir / "index.html").read_text()
    entry = next(e for e in json.loads((env.runs / "index.json").read_text()) if e["run_id"] == s["run_id"])
    assert entry["hinted"] is False and entry["hints"] == NO_HINTS


def test_feedback_is_measured_results_only(tmp_path):
    r = fake_run_policy(BAD, "can_to_bin", 0, str(tmp_path))
    fb = agent.feedback_turn(1, r, tmp_path)
    texts = [b["text"] for b in fb if b["type"] == "text"]
    assert len(texts) == 3 and texts[0].startswith("Attempt 1 result:\n```json\n")
    assert texts[1].startswith("Key frames (front camera)") and texts[2] == "Revise run(robot)."
    payload = json.loads(texts[0].split("```json\n")[1].split("\n```")[0])
    assert set(payload) == set(agent.FEEDBACK_KEYS) | {"calls_tail"}


def test_example_hint_recorded_and_bannered(env, monkeypatch):
    import run_agent
    monkeypatch.setattr(agent, "make_client", lambda: FakeClient(REPLIES))  # the "real client" path
    rc = run_agent.main(["--task", "can_to_bin", "--seed", "0", "--tries", "2", "--example",
                         "--runs-dir", str(env.runs), "--run-id", "ex"])
    assert rc == 0
    run_dir = env.runs / "ex"
    s = json.loads((run_dir / "summary.json").read_text())
    assert s["hints"] == {"example": True, "strategy": None, "context": False} and s["hinted"] is True
    assert s["scripted"] is False  # run_agent_loop created the client itself
    assert any(l in _all_request_text(run_dir) for l in REF_LINES)  # the example really was sent
    banner = "Hint given: reference example in prompt"
    assert banner in (run_dir / "trace.html").read_text()
    assert banner in (run_dir / "index.html").read_text()
    assert banner in (env.runs / "index.html").read_text()
    entry = next(e for e in json.loads((env.runs / "index.json").read_text()) if e["run_id"] == "ex")
    assert entry["hinted"] is True and entry["scripted"] is False


def test_strategy_and_context_hints(env):
    s = agent.run_agent_loop("can_to_bin", 0, tries=2, runs_dir=str(env.runs), client=FakeClient(REPLIES),
                             strategy="  go slow ", context="MEMORY", verbose=False)
    assert s["hints"] == {"example": False, "strategy": "go slow", "context": True} and s["hinted"] is True
    html = (env.runs / s["run_id"] / "trace.html").read_text()
    assert "strategy card: go slow" in html and "recalled memory" in html


def test_injected_client_is_scripted(env):
    s = agent.run_agent_loop("can_to_bin", 0, tries=2, runs_dir=str(env.runs), client=FakeClient(REPLIES),
                             verbose=False)
    run_dir = env.runs / s["run_id"]
    assert s["scripted"] is True and json.loads((run_dir / "summary.json").read_text())["scripted"] is True
    assert "SCRIPTED DEMO, NOT A MODEL" in (run_dir / "trace.html").read_text()
    assert "SCRIPTED DEMO, NOT A MODEL" in (run_dir / "index.html").read_text()
    assert "SCRIPTED DEMO, NOT A MODEL" in (env.runs / "index.html").read_text()
    entry = next(e for e in json.loads((env.runs / "index.json").read_text()) if e["run_id"] == s["run_id"])
    assert entry["scripted"] is True


def test_scripted_flag_real_vs_injected():
    import anthropic
    assert agent.is_scripted_client(None) is False  # loop creates the real client via make_client()
    assert agent.is_scripted_client(FakeClient([])) is True
    assert agent.is_scripted_client(anthropic.Anthropic(api_key="sk-ant-test")) is False  # no request made


def test_make_client_path_not_scripted(env, monkeypatch):
    monkeypatch.setattr(agent, "make_client", lambda: FakeClient(REPLIES))
    s = agent.run_agent_loop("can_to_bin", 0, tries=2, runs_dir=str(env.runs), verbose=False)
    assert s["scripted"] is False and s["hinted"] is False


def test_trace_demo_never_defaults_to_public_runs():
    import trace_demo
    assert trace_demo.DEFAULT_RUNS_DIR == "runs_demo"
    assert inspect.signature(trace_demo.run_demo).parameters["runs_dir"].default == "runs_demo"
    assert trace_demo.build_parser().parse_args([]).runs_dir == "runs_demo"
    assert "runs_demo/" in (ROOT / ".gitignore").read_text().splitlines()


def test_legacy_summary_banners():
    # summaries written before "hints" existed still get flagged from their top-level fields
    assert viewer.integrity_banners({"example": True, "strategy": ""}) == [
        ("hint", "Hint given: reference example in prompt")]
    assert viewer.integrity_banners({"kind": "policy"}, "policy") == [("warn", "HAND-WRITTEN POLICY, NOT A MODEL")]
    assert viewer.integrity_banners({"hints": NO_HINTS, "scripted": False}) == []


def test_sha_mismatch_refused(tmp_path):
    code = "def run(robot):\n    robot.open_gripper()\n"
    wrong = hashlib.sha256(b"something else the model never wrote").hexdigest()
    res = executor.run_policy(code, "can_to_bin", 0, str(tmp_path), timeout_s=60, fast=True, live=False,
                              response_code_sha256=wrong)
    assert res["success"] is False and executor.MISMATCH_ERROR in res["error"]
    prov = json.loads((tmp_path / "provenance.json").read_text())
    assert prov["refused"] is True and prov["code_matches_response"] is False and prov["completed"] is False
    assert prov["policy_sha256"] == hashlib.sha256(code.encode()).hexdigest()
    assert not (tmp_path / "trajectory.npz").exists() and not (tmp_path / "calls.json").exists()


def test_policy_file_bytes_equal_reply_code(tmp_path, monkeypatch):
    """run_policy writes the code byte-for-byte (no newline translation), so the agent's sha always matches."""
    code = "def run(robot):\r\n    pass\n"
    monkeypatch.setattr(executor.subprocess, "run", lambda *a, **k: (_ for _ in ()).throw(
        executor.subprocess.TimeoutExpired("x", 1)))
    executor.run_policy(code, "can_to_bin", 0, str(tmp_path), response_code_sha256=agent.sha256_hex(code))
    assert hashlib.sha256((tmp_path / "policy.py").read_bytes()).hexdigest() == agent.sha256_hex(code)
    assert json.loads((tmp_path / "provenance.json").read_text())["code_matches_response"] is True
