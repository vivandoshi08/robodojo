"""serve.py on-demand attempt.mp4: status/start routes, idempotency, path validation, concurrency cap."""
import json
import sys
import threading
import time
import urllib.error
import urllib.request

import pytest

from robot_race import serve


def _call(url, method="GET", headers=None):
    req = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


@pytest.fixture
def fake_replay(monkeypatch, tmp_path):
    """Swap `python -m robot_race.replay` for a script that writes attempt.mp4 after a short delay."""
    script = tmp_path / "fake_replay.py"
    script.write_text("import sys, time, os\ntime.sleep(0.3)\n"
                      "open(os.path.join(sys.argv[-7], 'attempt.mp4'), 'wb').write(b'mp4')\n")
    real = serve.subprocess.Popen

    def popen(cmd, **kw):
        assert cmd[1:3] == ["-m", "robot_race.replay"] and "--fps" in cmd
        return real([sys.executable, str(script), *cmd[3:]], **{k: v for k, v in kw.items() if k != "cwd"})
    monkeypatch.setattr(serve.subprocess, "Popen", popen)


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr(serve, "TRACKER_URL", "http://127.0.0.1:9")
    runs = tmp_path / "runs"
    a1 = runs / "r1" / "attempt_1"
    a1.mkdir(parents=True)
    (a1 / "trajectory.npz").write_bytes(b"x")
    (a1 / "result.json").write_text(json.dumps({"success": True, "video": None, "media": {"video": None}}))
    (runs / "r1" / "attempt_2").mkdir()          # never reached the sim
    (runs / "r1" / "summary.json").write_text(json.dumps({"run_id": "r1", "attempts": []}))
    srv = serve.make_server(str(runs), port=0, races_root=str(tmp_path / "races"))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", a1
    srv.shutdown()
    srv.server_close()


def test_render_lifecycle(server, fake_replay):
    base, a1 = server
    url = f"{base}/api/runs/r1/attempt_1/render"
    assert _call(url) == (200, {"state": "none", "video": None})
    code, st = _call(url, "POST")
    assert code == 202 and st["state"] == "pending"
    assert _call(url, "POST")[1]["state"] == "pending"          # idempotent: still one job
    for _ in range(50):
        st = _call(url)[1]
        if st["state"] == "done":
            break
        time.sleep(0.1)
    assert st == {"state": "done", "video": "attempt.mp4"}
    r = json.loads((a1 / "result.json").read_text())
    assert r["video"] == "attempt.mp4" and r["media"]["video"] == "attempt.mp4"
    assert _call(url, "POST") == (200, {"state": "done", "video": "attempt.mp4"})


def test_render_unavailable_without_trajectory(server):
    base, _ = server
    code, st = _call(f"{base}/api/runs/r1/attempt_2/render", "POST")
    assert code == 200 and st["state"] == "unavailable"


@pytest.mark.parametrize("path", ["/api/runs/nope/attempt_1/render", "/api/runs/r1/attempt_x/render",
                                  "/api/runs/r1/attempt_9/render", "/api/runs/r1/..%2F..%2Fetc/render"])
def test_render_rejects_bad_targets(server, path):
    base, _ = server
    assert _call(base + path, "POST")[0] in (403, 404)


def test_render_refuses_cross_origin(server):
    base, _ = server
    code, _ = _call(f"{base}/api/runs/r1/attempt_1/render", "POST", {"Origin": "http://evil.example"})
    assert code == 403


def test_render_cap(tmp_path, monkeypatch):
    class Slow:
        def poll(self):
            return None
    monkeypatch.setattr(serve.subprocess, "Popen", lambda *a, **k: Slow())
    r = serve.Renderer(max_jobs=1)
    dirs = []
    for i in range(2):
        d = tmp_path / f"a{i}"
        d.mkdir()
        (d / "trajectory.npz").write_bytes(b"x")
        dirs.append(str(d))
    assert r.start(dirs[0])["state"] == "pending"
    with pytest.raises(RuntimeError):
        r.start(dirs[1])


def test_no_launch_server_has_no_renderer(tmp_path):
    srv = serve.make_server(str(tmp_path / "runs"), port=0, launch=False)
    try:
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        (tmp_path / "runs" / "r" / "attempt_1").mkdir(parents=True)
        (tmp_path / "runs" / "r" / "attempt_1" / "trajectory.npz").write_bytes(b"x")
        code, _ = _call(f"http://127.0.0.1:{srv.server_address[1]}/api/runs/r/attempt_1/render", "POST")
        assert code == 503
    finally:
        srv.shutdown()
        srv.server_close()
