"""run_policy.py: seed parsing and run_batch artifacts, with a fake executor (step A not needed)."""
from __future__ import annotations

import json
import os
import sys
import threading
import types

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import run_policy  # noqa: E402
from robot_race.interfaces import RESULT_KEYS  # noqa: E402

POLICY = os.path.join(ROOT, "policies", "reference_pick_and_drop.py")


@pytest.mark.parametrize("s,want", [("0-9", list(range(10))), ("0,2,5", [0, 2, 5]), ("3", [3]),
                                    ("0-2,7", [0, 1, 2, 7]), (" 4 , 4,1", [4, 1])])
def test_parse_seeds(s, want):
    assert run_policy.parse_seeds(s) == want


@pytest.mark.parametrize("s", ["", "5-2", "a"])
def test_parse_seeds_bad(s):
    with pytest.raises(ValueError):
        run_policy.parse_seeds(s)


def test_parse_tasks():
    assert run_policy.parse_tasks("all")[0] == "can_to_bin" and len(run_policy.parse_tasks("all")) == 5
    assert run_policy.parse_tasks("can_to_bin,box_to_bin") == ["can_to_bin", "box_to_bin"]
    with pytest.raises(ValueError):
        run_policy.parse_tasks("nope")


@pytest.fixture
def fake_executor(monkeypatch):
    calls = []

    def run_policy_fake(code, task, seed, out_dir, timeout_s=180, fast=False, live=True, observation="oracle"):
        calls.append(dict(task=task, seed=seed, out_dir=out_dir, timeout_s=timeout_s, fast=fast,
                          thread=threading.get_ident()))
        if seed == 3:
            raise RuntimeError("executor exploded")
        ok = seed % 2 == 0
        res = {k: None for k in RESULT_KEYS}
        res.update(task=task, seed=seed, success=ok, time_s=10.0 + seed, collisions=seed % 3, energy_j=1.5,
                   dropped=False, lifted=True, error=None if ok else "Traceback...\nValueError: missed the bin",
                   code_path=os.path.join(out_dir, "policy.py"), item_final_pos=[0, 0, 0], calls=[], wall_s=0.1,
                   media={})
        with open(os.path.join(out_dir, "result.json"), "w") as f:
            json.dump(res, f)
        return res

    mod = types.ModuleType("robot_race.executor")
    mod.run_policy = run_policy_fake
    monkeypatch.setitem(sys.modules, "robot_race.executor", mod)
    import robot_race
    monkeypatch.setattr(robot_race, "executor", mod, raising=False)
    viewer = types.ModuleType("robot_race.viewer")
    viewer.calls = []
    viewer.write_index = lambda d: viewer.calls.append(("index", d)) or os.path.join(d, "index.html")
    viewer.update_manifest = lambda r="runs": viewer.calls.append(("manifest", r)) or os.path.join(r, "index.json")
    monkeypatch.setitem(sys.modules, "robot_race.viewer", viewer)
    monkeypatch.setattr(robot_race, "viewer", viewer, raising=False)
    return calls, viewer


def _events(run_dir):
    return [json.loads(l) for l in open(os.path.join(run_dir, "events.jsonl"))]


def test_run_batch_mixed(tmp_path, fake_executor, capsys):
    calls, viewer = fake_executor
    s = run_policy.run_batch(POLICY, ["can_to_bin"], "0-4", jobs=3, fast=True, timeout_s=9, run_id="r1",
                             runs_dir=str(tmp_path))
    run_dir = tmp_path / "r1"
    assert sorted(c["seed"] for c in calls) == [0, 1, 2, 3, 4]
    assert all(c["fast"] and c["timeout_s"] == 9 for c in calls)
    assert {os.path.basename(c["out_dir"]) for c in calls} == {f"seed_{i}" for i in range(5)}

    disk = json.load(open(run_dir / "summary.json"))
    assert disk["status"] == s["status"] == "failed" and disk["kind"] == "policy"
    assert disk["success_rate"] == 0.6 and disk["success_rate_by_task"] == {"can_to_bin": 0.6}
    assert disk["policy"] == "reference_pick_and_drop" and disk["task"] == "can_to_bin" and disk["seeds"] == [0, 1, 2, 3, 4]
    assert [r["seed"] for r in disk["results"]] == [0, 1, 2, 3, 4]
    assert "executor exploded" in disk["results"][3]["error"] and disk["results"][3]["success"] is False
    assert not list(run_dir.glob("*.tmp"))

    ev = _events(run_dir)
    assert ev[0]["type"] == "run_started" and ev[-1]["type"] == "run_finished" and ev[-1]["status"] == "failed"
    fin = [e for e in ev if e["type"] == "attempt_finished"]
    assert sorted(e["attempt"] for e in fin) == [0, 1, 2, 3, 4]
    assert all(e["run_id"] == "r1" and isinstance(e["ts"], float) and "result" in e for e in ev[1:-1])
    assert ev[0]["attempt"] is None and ev[-1]["attempt"] is None

    out = capsys.readouterr().out
    assert "task" in out and "collisions" in out and "missed the bin" in out and "3/5" in out
    assert viewer.calls == [("index", str(run_dir)), ("manifest", str(tmp_path))]


def test_run_batch_all_solved_and_cli(tmp_path, fake_executor):
    calls, _ = fake_executor
    rc = run_policy.main([POLICY, "--task", "can_to_bin,box_to_bin", "--seeds", "0,2", "--jobs", "2",
                          "--run-id", "r2", "--runs-dir", str(tmp_path)])
    assert rc == 0
    s = json.load(open(tmp_path / "r2" / "summary.json"))
    assert s["status"] == "solved" and s["success_rate"] == 1.0 and s["task"] == "all"
    assert {os.path.basename(c["out_dir"]) for c in calls} == {"can_to_bin__seed_0", "can_to_bin__seed_2",
                                                              "box_to_bin__seed_0", "box_to_bin__seed_2"}
    assert run_policy.main([POLICY, "--seeds", "1", "--run-id", "r3", "--runs-dir", str(tmp_path)]) == 1


def test_default_run_id(tmp_path, fake_executor):
    s = run_policy.run_batch(POLICY, "can_to_bin", [0], runs_dir=str(tmp_path), quiet=True)
    assert s["run_id"].endswith("-policy-reference_pick_and_drop-can_to_bin")
    assert len(s["run_id"].split("-policy-")[0]) == len("20260101-120000")


def test_format_table_alignment():
    t = run_policy.format_table([dict(task="can_to_bin", seed=0, success=True, time_s=1.234, wall_s=2.0),
                                 dict(task="box_to_bin", seed=10, success=False, error="x" * 200)])
    lines = t.splitlines()
    assert lines[0].startswith("task") and set(lines[1]) <= {"-", " "}
    assert lines[2].index("0") == lines[3].index("10") and "1.23" in lines[2] and "..." in lines[3]
