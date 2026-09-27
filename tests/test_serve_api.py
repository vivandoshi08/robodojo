"""serve.py dojo API: race launch validation, cross-origin guard, races composite, QM fail-soft."""
import json
import threading
import urllib.error
import urllib.request

import pytest

from robot_race import serve


def test_validate_race_defaults_and_bounds():
    p = serve.validate_race({"task": "can_to_bin"})
    assert p == {"task": "can_to_bin", "agents": 4, "seeds": "0", "tries": 5, "label": "", "memory": "tracker",
                 "backend": "local"}
    assert serve.validate_race({"agents": 2, "seeds": "0-2", "backend": "qm"})["backend"] == "qm"


@pytest.mark.parametrize("body", [
    {"task": "nope"},
    {"agents": 0}, {"agents": 7}, {"agents": "4"}, {"agents": True},
    {"tries": 6},
    {"seeds": "0; rm -rf /"}, {"seeds": "a"}, {"seeds": "3-1"},
    {"agents": 6, "seeds": "0-2"},          # 18 agent loops > MAX_JOBS
    {"label": "x" * 41}, {"label": "$(id)"},
    {"memory": "/etc/passwd"},
    {"backend": "k8s"},
    [],
])
def test_validate_race_rejects(body):
    with pytest.raises(ValueError):
        serve.validate_race(body)


def test_race_cmd_is_an_arg_list():
    p = serve.validate_race({"label": "cold run", "seeds": "0,2"})
    cmd = serve.race_cmd(p, "/r", "/races")
    assert cmd[1].endswith("run_race.py")
    assert cmd[cmd.index("--label") + 1] == "cold run"  # one argv entry, no shell
    assert "--fast" in cmd and cmd[cmd.index("--runs-dir") + 1] == "/r"


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(serve, "TRACKER_URL", "http://127.0.0.1:9")  # tracker down
    runs, races = tmp_path / "runs", tmp_path / "races"
    (runs / "r1").mkdir(parents=True)
    (runs / "r1" / "summary.json").write_text(json.dumps({
        "run_id": "r1", "task": "can_to_bin", "seed": 0, "status": "failed",
        "race": {"race_id": "race-a", "agent_id": "agent-1"},
        "attempts": [{"k": 1, "code": "secret", "result": {"success": False, "error": "boom"}}]}))
    (races / "race-a").mkdir(parents=True)
    (races / "race-a" / "plan.json").write_text(json.dumps({"strategies": [{"agent_id": "agent-1", "name": "A"}]}))
    srv = serve.make_server(str(runs), port=0, races_root=str(races))
    started = []
    monkeypatch.setattr(serve.Launcher, "start", lambda self, p: started.append(p) or {"launch_id": "x", **p})
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", started
    srv.shutdown()
    srv.server_close()


def _post(url, body, headers=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, json.load(e)


def test_post_race(server):
    base, started = server
    assert _post(base + "/api/race", {"task": "nope"})[0] == 400
    assert _post(base + "/api/race", {}, {"Origin": "http://evil.example"})[0] == 403
    status, body = _post(base + "/api/race", {"agents": 2})
    assert status == 202 and started[-1]["agents"] == 2


def test_races_composite_and_qm_failsoft(server):
    base, _ = server
    with urllib.request.urlopen(base + "/api/races") as r:
        j = json.load(r)
    assert j["tracker"] is None and [x["race_id"] for x in j["races"]] == ["race-a"]
    with urllib.request.urlopen(base + "/api/races/race-a") as r:
        d = json.load(r)
    assert d["plan"]["strategies"][0]["agent_id"] == "agent-1" and d["tracker"] is None
    (run,) = d["runs"]
    assert run["attempts"][0]["error"] == "boom" and "code" not in run["attempts"][0]
    with urllib.request.urlopen(base + "/api/races/race-a/qm") as r:
        assert json.load(r)["available"] in (False, True)
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(base + "/api/races/race-a/leaderboard")  # tracker down -> 502
    assert e.value.code == 502


def test_ui_served_at_root(server):
    base, _ = server
    with urllib.request.urlopen(base + "/") as r:
        assert b"Robo Dojo" in r.read()
