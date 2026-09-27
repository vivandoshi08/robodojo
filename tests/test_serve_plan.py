"""serve.py strategy board: custom strategy validation, plan preview jobs, launching a reviewed plan."""
import json
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from robot_race import serve

PLAN = {"task_id": "can_to_bin", "coverage_rationale": "cover grasp vs push",
        "axes": [{"name": "contact", "options": ["grasp", "push"]}],
        "strategies": [{"agent_id": "agent-1", "name": "Sweep", "mode": "custom", "approach": "push it"},
                       {"agent_id": "agent-2", "name": "Rim drop", "mode": "explore", "approach": "grasp, drop"},
                       {"agent_id": "agent-3", "name": "Deep place", "mode": "exploit", "approach": "lower in"}]}


def _call(url, body=None, method=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method or ("POST" if data else "GET"),
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def test_validate_custom_limits():
    assert serve.validate_custom([{"name": " Sweep ", "approach": " push it "}]) == [
        {"name": "Sweep", "approach": "push it"}]
    assert serve.validate_custom([{"approach": "x"}])[0]["name"] == "Custom 1"
    for bad in ([{"name": "a", "approach": ""}], [{"name": "a", "approach": "x" * 1001}],
                [{"name": "n" * 61, "approach": "x"}], ["a::b"], [{"approach": 3}], {"approach": "x"},
                [{"approach": "x"}] * 7):
        with pytest.raises(ValueError):
            serve.validate_custom(bad)


def test_validate_race_plan_and_custom():
    p = serve.validate_race({"plan_id": "plan-1", "strategies": [{"agent_id": "agent-2"}, {"agent_id": "agent-1"}]})
    assert p["agents"] == 2 and p["plan_id"] == "plan-1"
    with pytest.raises(ValueError):
        serve.validate_race({"plan_id": "../x"})
    with pytest.raises(ValueError):
        serve.validate_race({"plan_id": "p", "strategies": []})
    with pytest.raises(ValueError):
        serve.validate_race({"plan_id": "p", "strategies": [{"agent_id": "a"}, {"agent_id": "a"}]})
    assert serve.validate_race({"agents": 2, "custom": [{"name": "S", "approach": "push"}]})["custom"]
    with pytest.raises(ValueError):
        serve.validate_race({"agents": 1, "custom": [{"approach": "a"}, {"approach": "b"}]})


def test_final_plan_keeps_order_edits_custom_only():
    out = serve.final_plan(PLAN, [{"agent_id": "agent-3"}, {"agent_id": "agent-1", "approach": "push harder"}])
    assert [s["name"] for s in out["strategies"]] == ["Deep place", "Sweep"]
    assert [s["agent_id"] for s in out["strategies"]] == ["agent-1", "agent-2"]
    assert out["strategies"][1]["approach"] == "push harder" and out["strategies"][1]["planned_as"] == "agent-1"
    assert out["edited"] is True
    with pytest.raises(ValueError):
        serve.final_plan(PLAN, [{"agent_id": "agent-2", "approach": "rewrite a planner card"}])
    with pytest.raises(ValueError):
        serve.final_plan(PLAN, [{"agent_id": "agent-9"}])
    assert serve.final_plan(PLAN, None) is PLAN


def test_race_cmd_plan_and_custom_files():
    assert serve.race_cmd({**serve.validate_race({}), "plan_path": "/p.json"}, "/r", "/races")[-2:] == ["--plan", "/p.json"]
    assert serve.race_cmd({**serve.validate_race({}), "custom_path": "/c.json"}, "/r", "/races")[-2:] == [
        "--custom-file", "/c.json"]


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(serve, "TRACKER_URL", "http://127.0.0.1:9")
    races = tmp_path / "races"
    srv = serve.make_server(str(tmp_path / "runs"), port=0, races_root=str(races))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", races
    srv.shutdown()
    srv.server_close()


def test_plan_preview_job(server, monkeypatch, tmp_path):
    base, races = server
    seen = {}
    real = serve.subprocess.Popen
    script = tmp_path / "fake_planner.py"
    script.write_text("import sys, json, time\ntime.sleep(0.2)\n"
                      f"open(sys.argv[sys.argv.index('--out') + 1], 'w').write(json.dumps({PLAN!r}))\n")

    def popen(cmd, **kw):
        seen["cmd"] = cmd
        return real([sys.executable, str(script), *cmd[3:]], **{k: v for k, v in kw.items() if k != "cwd"})
    monkeypatch.setattr(serve.subprocess, "Popen", popen)
    code, j = _call(base + "/api/plan", {"task": "can_to_bin", "agents": 3, "memory": "none",
                                         "custom": [{"name": "Sweep", "approach": "push it; rm -rf / $(id)"}]})
    assert code == 202 and j["state"] == "pending" and j["llm"] is True
    cmd = seen["cmd"]
    assert cmd[1:4] == ["-m", "racetrack.planner", "preview"] and "--custom-file" in cmd
    assert not any("rm -rf" in c for c in cmd)                                  # approach only in the file
    custom = json.loads(open(cmd[cmd.index("--custom-file") + 1]).read())
    assert custom == [{"name": "Sweep", "approach": "push it; rm -rf / $(id)"}]
    assert _call(base + "/api/plan", {"task": "can_to_bin"})[0] == 409          # one preview at a time
    for _ in range(50):
        code, st = _call(f"{base}/api/plan/{j['plan_id']}")
        if st["state"] != "pending":
            break
        time.sleep(0.1)
    assert st["state"] == "done" and st["plan"]["strategies"][0]["name"] == "Sweep"
    assert _call(base + "/api/plan/nope")[0] == 404


def test_plan_rejects_bad_requests(server):
    base, _ = server
    for body in ({"task": "nope"}, {"agents": 9}, {"agents": 1, "custom": [{"approach": "a"}, {"approach": "b"}]},
                 {"custom": [{"approach": "x" * 1001}]}):
        assert _call(base + "/api/plan", body)[0] == 400
    assert _call(base + "/api/plan", {}, headers={"Origin": "http://evil.example"})[0] == 403


def test_launch_reviewed_plan(tmp_path):
    races = tmp_path / "races"
    (races / "_plans").mkdir(parents=True)
    (races / "_plans" / "plan-1.json").write_text(json.dumps(PLAN))
    started = {}
    L = serve.Launcher(str(tmp_path / "runs"), str(races))

    class P:
        def poll(self):
            return None
    orig = serve.subprocess.Popen
    serve.subprocess.Popen = lambda cmd, **kw: started.setdefault("cmd", cmd) and P()
    try:
        out = L.start(serve.validate_race({"plan_id": "plan-1", "strategies": [
            {"agent_id": "agent-1", "name": "Sweep 2", "approach": "push slower"}, {"agent_id": "agent-3"}]}))
    finally:
        serve.subprocess.Popen = orig
    assert out["params"]["agents"] == 2 and "plan" not in out["params"] and "plan_path" not in out["params"]
    cmd = started["cmd"]
    final = json.loads(open(cmd[cmd.index("--plan") + 1]).read())
    assert [(s["agent_id"], s["name"]) for s in final["strategies"]] == [("agent-1", "Sweep 2"), ("agent-2", "Deep place")]
    with pytest.raises(ValueError):
        serve.Launcher(str(tmp_path / "runs"), str(races)).start(serve.validate_race({"plan_id": "plan-missing"}))


def test_qm_launch_gets_plan(tmp_path, monkeypatch):
    races = tmp_path / "races"
    (races / "_plans").mkdir(parents=True)
    (races / "_plans" / "plan-1.json").write_text(json.dumps(PLAN))
    got = {}

    class FakeQM:
        @staticmethod
        def launch_qm_race(task, agents, seeds, tries, label, **kw):
            got.update(kw, agents=agents)
            return {"race_id": "race-9"}
    monkeypatch.setattr(serve, "qm_module", lambda: FakeQM)
    L = serve.Launcher(str(tmp_path / "runs"), str(races))
    L.start(serve.validate_race({"backend": "qm", "plan_id": "plan-1", "strategies": [{"agent_id": "agent-2"}]}))
    for t in list(L.qm.values()):
        t["thread"].join(5)
    assert got["agents"] == 1 and [s["name"] for s in got["plan"]["strategies"]] == ["Rim drop"]
