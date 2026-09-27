from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.request

import pytest

from robot_race import serve, viewer


def _w(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(data, bytes) else "w"
    with open(path, mode) as f:
        f.write(json.dumps(data) if isinstance(data, (dict, list)) else data)


def _res(task="can_to_bin", seed=0, success=False, **kw):
    return dict(task=task, seed=seed, success=success, time_s=12.3, collisions=1, energy_j=4.5,
                dropped=False, lifted=True, error=kw.pop("error", None), **kw)


@pytest.fixture
def runs(tmp_path):
    root = tmp_path / "runs"
    # 1. solved agent run: attempt_1 failed, attempt_2 succeeded (with mp4 + poster)
    a = root / "20260927-100000-can_to_bin-s0"
    _w(a / "summary.json", dict(task="can_to_bin", seed=0, model="claude-x", strategy="careful",
                                status="solved", solved_at=2, attempts=[{"code": "def run(robot): pass"}]))
    _w(a / "attempt_1" / "result.json", _res(error="Traceback...\nValueError: <boom>"))
    _w(a / "attempt_1" / "policy.py", "def run(robot):\n    raise ValueError('x')\n")
    _w(a / "attempt_1" / "response.md", "I will **try** <this>")
    for i in range(4):
        _w(a / "attempt_1" / f"key_{i}.png", b"\x89PNG")
    _w(a / "attempt_2" / "result.json", _res(success=True))
    _w(a / "attempt_2" / "policy.py", "def run(robot):\n    robot.open_gripper()\n")
    _w(a / "attempt_2" / "attempt.mp4", bytes(range(256)) * 4)
    _w(a / "attempt_2" / "poster.jpg", b"\xff\xd8jpg")
    _w(a / "events.jsonl", "".join(json.dumps(dict(ts=i, type="t", n=i)) + "\n" for i in range(5))
       + '{"ts": 5, "type": "partial')
    # 2. running agent run: attempt_1 finished, attempt_2 in progress
    b = root / "20260927-110000-bottle_to_bin-s1"
    _w(b / "summary.json", dict(task="bottle_to_bin", seed=1, model="claude-y", strategy=None,
                                status="running", solved_at=None, attempts=[]))
    _w(b / "attempt_1" / "result.json", _res("bottle_to_bin", 1))
    _w(b / "attempt_1" / "poster.jpg", b"\xff\xd8")
    _w(b / "attempt_2" / "live.jpg", b"\xff\xd8live")
    # 3. run_policy-style dir (no summary.json)
    c = root / "20260927-090000-can_to_bin-policy"
    for s in (2, 0, 10, 1):
        _w(c / f"seed_{s}" / "result.json", _res(seed=s, success=(s == 1)))
        _w(c / f"seed_{s}" / "poster.jpg", b"\xff\xd8")
    # 4. corrupt summary (writer mid-write)
    _w(root / "20260927-120000-can_to_bin-s9" / "summary.json", '{"task": "can_to_bi')
    # noise
    _w(root / "notarun" / "readme.txt", "hi")
    return root


def test_write_index_agent_run(runs):
    run = runs / "20260927-100000-can_to_bin-s0"
    html = open(viewer.write_index(str(run))).read()
    assert 'http-equiv="refresh"' not in html
    assert html.index('id="attempt_1"') < html.index('id="attempt_2"')
    assert '<video controls muted loop playsinline preload="metadata" poster="attempt_2/poster.jpg" ' \
           'src="attempt_2/attempt.mp4">' in html
    assert "attempt_1/key_3.png" in html  # no video/poster -> key-frame strip
    assert "ValueError: &lt;boom&gt;" in html and "I will **try** &lt;this&gt;" in html
    assert "robot.open_gripper()" in html and "claude-x" in html and "careful" in html
    assert "success" in html and "12.3 s" in html
    assert str(runs) not in html  # relative paths only
    assert not [f for f in os.listdir(run) if f.endswith(".tmp")]


def test_write_index_running_and_policy(runs):
    html = open(viewer.write_index(str(runs / "20260927-110000-bottle_to_bin-s1"))).read()
    assert '<meta http-equiv="refresh" content="2">' in html
    assert 'src="attempt_2/live.jpg?t=' in html and "running" in html
    html = open(viewer.write_index(str(runs / "20260927-090000-can_to_bin-policy"))).read()
    assert 'http-equiv="refresh"' not in html
    ids = [html.index(f'id="seed_{s}"') for s in (0, 1, 2, 10)]
    assert ids == sorted(ids)  # natural sort
    assert "1/4" in html


def test_manifest(runs):
    path = viewer.update_manifest(str(runs))
    m = json.load(open(path))
    assert [e["run_id"] for e in m] == ["20260927-110000-bottle_to_bin-s1", "20260927-100000-can_to_bin-s0",
                                        "20260927-090000-can_to_bin-policy"]  # corrupt + noise skipped
    for e in m:
        assert {"run_id", "task", "seed", "model", "strategy", "status", "solved_at", "n_attempts",
                "created_at", "poster", "video"} <= set(e)
    run, solved, pol = m
    assert run["status"] == "running" and run["n_attempts"] == 2 and run["video"] is None
    assert run["poster"] == "20260927-110000-bottle_to_bin-s1/attempt_1/poster.jpg"
    assert solved["status"] == "solved" and solved["solved_at"] == 2 and solved["model"] == "claude-x"
    assert solved["video"] == "20260927-100000-can_to_bin-s0/attempt_2/attempt.mp4"
    assert solved["poster"] == "20260927-100000-can_to_bin-s0/attempt_2/poster.jpg"
    assert pol["status"] == "solved" and pol["model"] is None and pol["n_attempts"] == 4
    assert pol["poster"] == "20260927-090000-can_to_bin-policy/seed_1/poster.jpg"  # first success
    assert pol["task"] == "can_to_bin" and pol["seed"] is None and pol["seeds"] == [0, 1, 2, 10]
    home = open(runs / "index.html").read()
    assert 'href="20260927-100000-can_to_bin-s0/index.html"' in home
    assert 'http-equiv="refresh"' in home  # a run is live
    # failed policy run: best = last attempt
    d = runs / "20260927-080000-box_to_bin-policy"
    _w(d / "seed_0" / "result.json", _res(seed=0))
    _w(d / "seed_1" / "result.json", _res(seed=1))
    _w(d / "seed_1" / "key_3.png", b"\x89PNG")
    e = next(e for e in viewer.build_manifest(str(runs)) if e["run_id"] == d.name)
    assert e["status"] == "failed" and e["poster"] == f"{d.name}/seed_1/key_3.png"


def test_cli(runs):
    viewer.main([str(runs)])
    assert (runs / "index.json").exists() and (runs / "20260927-100000-can_to_bin-s0" / "index.html").exists()
    viewer.main([str(runs / "20260927-090000-can_to_bin-policy")])


def test_parse_range():
    assert serve.parse_range("bytes=0-99", 1000) == (0, 99)
    assert serve.parse_range("bytes=900-", 1000) == (900, 999)
    assert serve.parse_range("bytes=-100", 1000) == (900, 999)
    assert serve.parse_range("bytes=990-5000", 1000) == (990, 999)
    assert serve.parse_range("bytes=1000-", 1000) is None
    assert serve.parse_range("bytes=5-2", 1000) is None


@pytest.fixture
def server(runs):
    srv = serve.make_server(str(runs), port=0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def _get(url, headers=None):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {})) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def test_serve_api(server):
    st, h, body = _get(server + "/api/runs")
    assert st == 200 and h["Access-Control-Allow-Origin"] == "*" and h["Cache-Control"] == "no-store"
    assert [e["run_id"] for e in json.loads(body)][0] == "20260927-110000-bottle_to_bin-s1"
    st, _, body = _get(server + "/api/runs/20260927-100000-can_to_bin-s0")
    assert st == 200 and json.loads(body)["status"] == "solved"
    st, _, body = _get(server + "/api/runs/20260927-090000-can_to_bin-policy")
    assert st == 200 and len(json.loads(body)["attempts"]) == 4
    assert _get(server + "/api/runs/20260927-120000-can_to_bin-s9")[0] == 503
    assert _get(server + "/api/runs/nope")[0] == 404
    st, _, body = _get(server + "/api/runs/20260927-100000-can_to_bin-s0/events?since=3")
    ev = json.loads(body)
    assert st == 200 and [e["n"] for e in ev["events"]] == [3, 4] and ev["next"] == 5  # partial line held back
    ev = json.loads(_get(server + "/api/runs/20260927-100000-can_to_bin-s0/events?since=5")[2])
    assert ev == {"events": [], "next": 5}


def test_serve_static_range_and_traversal(server, runs):
    mp4 = "/20260927-100000-can_to_bin-s0/attempt_2/attempt.mp4"
    st, h, body = _get(server + mp4)
    assert st == 200 and h["Content-Type"] == "video/mp4" and len(body) == 1024 and h["Accept-Ranges"] == "bytes"
    st, h, body = _get(server + mp4, {"Range": "bytes=100-199"})
    assert st == 206 and h["Content-Range"] == "bytes 100-199/1024" and body == bytes(range(100, 200))
    st, h, body = _get(server + mp4, {"Range": "bytes=-24"})
    assert st == 206 and h["Content-Range"] == "bytes 1000-1023/1024" and len(body) == 24
    st, h, _ = _get(server + mp4, {"Range": "bytes=5000-"})
    assert st == 416 and h["Content-Range"] == "bytes */1024"
    st, h, _ = _get(server + "/20260927-110000-bottle_to_bin-s1/attempt_2/live.jpg")
    assert st == 200 and h["Cache-Control"] == "no-store" and h["Content-Type"] == "image/jpeg"
    st, h, _ = _get(server + "/20260927-100000-can_to_bin-s0/events.jsonl")
    assert st == 200 and h["Cache-Control"] == "no-store" and h["Content-Type"] == "application/x-ndjson"
    (runs.parent / "secret.txt").write_text("nope")
    for p in ("/../secret.txt", "/%2e%2e/secret.txt", "/20260927-100000-can_to_bin-s0/..%2f..%2fsecret.txt",
              "/api/runs/..%2f"):
        st, _, body = _get(server + p)
        assert st in (403, 404) and b"nope" not in body, p
