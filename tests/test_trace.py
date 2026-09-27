"""Audit trail: transcript capture, observation, provenance, trace.html, /transcript API, scripted proof run."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
import threading
import types
import urllib.request
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from robot_race import agent, executor, serve, trace  # noqa: E402
from test_agent import BAD, GOOD, FakeClient, fake_run_policy  # noqa: E402

REPLIES = [f"Plan A.\n```python\n{BAD}```", f"Plan B.\n```python\n{GOOD}```"]
HAS_ASSETS = (ROOT / "assets" / "menagerie" / "franka_emika_panda" / "panda.xml").exists()


@pytest.fixture
def fake_env(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "robot_race.executor", types.SimpleNamespace(run_policy=fake_run_policy))
    front = agent.png_b64(np.full((48, 64, 3), 7, np.uint8))
    top = agent.png_b64(np.full((48, 64, 3), 99, np.uint8))
    monkeypatch.setattr(agent, "observe_scene", lambda task, seed: ({"item": {"pos": [0.5, -0.2, 0.25]}},
                                                                     {"front": front, "top": top}))
    monkeypatch.setattr(agent, "_sleep", lambda s: None)
    return types.SimpleNamespace(runs=tmp_path / "runs", front=front, top=top)


def _sent_images(kw) -> list[bytes]:
    return [base64.b64decode(b["source"]["data"]) for m in kw["messages"] if isinstance(m["content"], list)
            for b in m["content"] if b.get("type") == "image"]


def test_transcript_capture(fake_env):
    client = FakeClient(REPLIES, fail_first=1)
    s = agent.run_agent_loop("can_to_bin", 0, tries=3, runs_dir=str(fake_env.runs), client=client, verbose=False)
    run_dir = fake_env.runs / s["run_id"]
    assert s["status"] == "solved" and s["transcript"] == "transcript.jsonl" and s["trace"] == "trace.html"
    assert s["observation"] == {"front": "observation/front.png", "top": "observation/top.png",
                                "state": "observation/state.json"}
    assert (run_dir / "observation/front.png").read_bytes() == base64.b64decode(fake_env.front)
    assert json.loads((run_dir / "observation/state.json").read_text())["item"]["pos"] == [0.5, -0.2, 0.25]

    lines = [json.loads(l) for l in (run_dir / "transcript.jsonl").read_text().splitlines()]
    assert [(l["turn"], l["attempt"]) for l in lines] == [(1, 1), (2, 2)]
    sent = [client.calls[1], client.calls[2]]  # calls[0] was the retried network error
    for line, kw in zip(lines, sent):
        d = run_dir / line["dir"]
        req = json.loads((d / "request.json").read_text())
        assert req["model"] == kw["model"] and req["max_tokens"] == kw["max_tokens"] == agent.MAX_TOKENS
        assert req["system"] == kw["system"]
        imgs = _sent_images(kw)
        assert [(run_dir / p).read_bytes() for p in line["images"]] == imgs
        assert line["image_sha256"] == [hashlib.sha256(b).hexdigest() for b in imgs]
        blocks = [b for m in req["messages"] if isinstance(m["content"], list) for b in m["content"]
                  if b["type"] == "image"]
        assert [b["file"] for b in blocks] == [f"img_{i}.png" for i in range(len(imgs))]
        assert all("source" not in b and len(b["sha256"]) == 64 for b in blocks)
        # everything except the image payloads is verbatim
        strip = lambda ms: json.dumps([[{k: v for k, v in b.items() if k not in ("source", "file", "sha256",
                                                                                   "media_type", "bytes")}
                                         for b in (m["content"] if isinstance(m["content"], list)
                                                   else [{"text": m["content"]}])]
                                        for m in ms])
        assert strip(req["messages"]) == strip(kw["messages"])
        assert json.loads((d / "response.json").read_text())["content"][0]["text"] == REPLIES[line["turn"] - 1]
        meta = json.loads((d / "meta.json").read_text())
        assert meta["turn"] == line["turn"] and meta["latency_s"] >= 0 and meta["finished"] >= meta["started"]
    assert lines[0]["retries"] == 1 and lines[1]["retries"] == 0
    assert len(lines[0]["images"]) == 2 and len(lines[1]["images"]) == 6 and len(lines[1]["new_images"]) == 4
    assert s["attempts"][0]["code_sha256"] == agent.sha256_hex(BAD)

    ev = [json.loads(l) for l in (run_dir / "events.jsonl").read_text().splitlines()]
    req_ev = [e for e in ev if e["type"] == "model_request"]
    resp_ev = [e for e in ev if e["type"] == "model_response"]
    assert [e["n_images"] for e in req_ev] == [2, 6] and [e["turn"] for e in resp_ev] == [1, 2]
    assert resp_ev[0]["usage"]["input_tokens"] == 100 and all(len(json.dumps(e)) < 2000 for e in req_ev + resp_ev)

    html = (run_dir / "trace.html").read_text()
    assert "Turn 2" in html and "Feedback on attempt 1" in html and "transcript/turn_1/img_0.png" in html
    assert "!= FILE" not in html and "/Users/" not in html


def test_api_failure_recorded(fake_env):
    s = agent.run_agent_loop("can_to_bin", 0, tries=2, runs_dir=str(fake_env.runs), verbose=False,
                             client=FakeClient([], fail_first=99))
    run_dir = fake_env.runs / s["run_id"]
    (line,) = [json.loads(l) for l in (run_dir / "transcript.jsonl").read_text().splitlines()]
    assert line["response"] is None and "flaky network" in line["error"] and line["retries"] == 2
    assert (run_dir / "transcript/turn_1/request.json").exists() and "flaky" in (run_dir / "trace.html").read_text()


def test_serve_transcript_endpoint(fake_env):
    s = agent.run_agent_loop("can_to_bin", 0, tries=3, runs_dir=str(fake_env.runs), client=FakeClient(REPLIES),
                             verbose=False)
    srv = serve.make_server(str(fake_env.runs), port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{srv.server_address[1]}/api/runs/{s['run_id']}/transcript"
        body = json.loads(urllib.request.urlopen(url).read())
        assert [t["turn"] for t in body["turns"]] == [1, 2] and body["turns"][0]["request"].endswith("request.json")
    finally:
        srv.shutdown()


def test_scrub_paths():
    tb = f'File "{executor.ROOT}/robot_race/tasks.py", line 3\nFile "/opt/x/lib/python3.11/site-packages/np/a.py"'
    out = executor.scrub_paths(tb + f"\n{os.path.expanduser('~')}/foo")
    assert 'File "robot_race/tasks.py"' in out and "<site-packages>/np/a.py" in out and "~/foo" in out
    assert executor.ROOT not in out and executor.scrub_paths(None) is None


@pytest.mark.skipif(not HAS_ASSETS, reason="Panda assets missing (bash scripts/fetch_assets.sh)")
def test_policy_exception_in_calls_json(tmp_path):
    code = "def run(robot):\n    robot.open_gripper()\n    robot.wait(0.2)\n    raise ValueError('boom')\n"
    res = executor.run_policy(code, "can_to_bin", 0, str(tmp_path), fast=True, live=False,
                              response_code_sha256=agent.sha256_hex(code))
    assert res["media"]["calls_json"] == "calls.json" and res["media"]["provenance"] == "provenance.json"
    calls = json.loads((tmp_path / "calls.json").read_text())
    assert [c["call"] for c in calls["calls"]] == ["open_gripper", "wait"]
    assert calls["calls"][1]["t_start"] == pytest.approx(0.4) and calls["calls"][1]["t_end"] == pytest.approx(0.6)
    ex = calls["exception"]
    assert ex["type"] == "ValueError" and ex["sim_time"] == pytest.approx(0.6) and ex["in_call"] is None
    prov = json.loads((tmp_path / "provenance.json").read_text())
    assert prov["code_matches_response"] is True and prov["policy_sha256"] == agent.sha256_hex(code)
    assert "/Users/" not in (tmp_path / "result.json").read_text()


@pytest.mark.skipif(not HAS_ASSETS, reason="Panda assets missing (bash scripts/fetch_assets.sh)")
def test_scripted_proof_run_real_executor(tmp_path):
    """Real agent loop + real executor + sim (fast: no mp4), scripted client; every audit check must pass."""
    import trace_demo
    run_dir, client, s = trace_demo.run_demo(str(tmp_path / "runs"), 0, fast=True, verbose=False)
    failed = [name for name, ok in trace_demo.verify(run_dir, client.received) if not ok]
    assert not failed, failed
    html = open(os.path.join(run_dir, "trace.html")).read()
    assert html.count("badge ok\">code executed == code in reply") == 2 and "this block was executed" in html
    assert "no attempt.mp4" in html  # fast mode: honest about not being able to seek


def test_trace_tolerates_partial_run(tmp_path):
    d = tmp_path / "r1"
    d.mkdir()
    (d / "summary.json").write_text(json.dumps({"status": "running", "task": "can_to_bin", "attempts": []}))
    (d / "transcript.jsonl").write_text('{"turn": 1, "attempt": 1, "dir": "transcript/turn_1", "req')  # mid-write
    html = open(trace.write_trace(str(d))).read()
    assert "running" in html and 'http-equiv="refresh"' in html
