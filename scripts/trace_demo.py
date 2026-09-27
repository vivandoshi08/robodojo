"""Proof run without an API key: the REAL agent loop, executor and sim, with a scripted client in place of Claude.

Turn 1 replies with a deliberately flawed policy (releases 0.25 m past the bin: the can misses);
turn 2 replies with the reference policy. Then every link of the audit chain is checked from the files:
request bytes == what the client received, sha256s, code ran == code in reply, calls.json timing vs. the
trajectory, click-to-seek mapping vs. the actual mp4, and no local paths in any file.

python scripts/trace_demo.py [--runs-dir runs_demo] [--seed 0] [--fast]   -> prints the run dir + check results

Writes to runs_demo/ by default, never to the public runs/: the replies are canned (turn 2 is the hand-written
reference policy), so this is NOT a model run. summary.json has scripted: true and trace.html / the run pages
show a "SCRIPTED DEMO, NOT A MODEL" banner.
"""
from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import json
import os
import sys
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from robot_race import agent  # noqa: E402

DEFAULT_RUNS_DIR = "runs_demo"  # gitignored; keep scripted runs out of the public runs/ tree
MODEL = "scripted-fake-client"  # never pretend to be Claude
REFERENCE = "\n".join(l for l in open(os.path.join(ROOT, "policies", "reference_pick_and_drop.py")).read().splitlines()
                      if l.strip() != "import numpy as np").strip() + "\n"
FLAWED = '''def run(robot):
    s = robot.get_state()
    p, b = s["item"]["pos"], s["bin"]
    robot.open_gripper()
    robot.move_to([p[0], p[1], p[2] + 0.15], speed=0.4)
    robot.move_to([p[0], p[1], p[2] + 0.005], speed=0.1)
    robot.close_gripper()
    robot.move_to([p[0], p[1], p[2] + 0.25], speed=0.15)
    # FLAW (deliberate): release 0.25 m beyond the bin center along +x
    robot.move_to([b["center"][0] + 0.25, b["center"][1], b["rim_height"] + 0.15], speed=0.2)
    robot.open_gripper()
    robot.wait(0.5)
'''
REPLIES = [
    "Plan: grasp the can from above, lift, carry it past the bin center to leave room for momentum, release.\n\n"
    f"```python\n{FLAWED}```",
    "The key frames show the can landing on the floor beyond the bin: I released too far along +x. "
    "Release directly above the bin center instead.\n\n"
    f"```python\n{REFERENCE}```",
]


class ScriptedClient:
    """Stands in for anthropic.Anthropic(): client.messages.create(**kw) -> response-shaped object.
    Keeps a deep copy of every request exactly as received, for byte-level comparison."""

    def __init__(self, replies):
        self.replies, self.received = list(replies), []
        self.messages = self

    def create(self, **kw):
        self.received.append(copy.deepcopy(kw))
        n = len(self.received)
        text = self.replies.pop(0)
        return types.SimpleNamespace(
            id=f"msg_scripted_{n}", type="message", role="assistant", model=kw["model"], stop_reason="end_turn",
            stop_sequence=None, content=[types.SimpleNamespace(type="text", text=text)],
            usage=types.SimpleNamespace(input_tokens=0, output_tokens=0, cache_read_input_tokens=0,
                                        cache_creation_input_tokens=0))


def run_demo(runs_dir: str = DEFAULT_RUNS_DIR, seed: int = 0, fast: bool = False, verbose: bool = True):
    client = ScriptedClient(REPLIES)
    s = agent.run_agent_loop("can_to_bin", seed, tries=2, runs_dir=runs_dir, client=client, model=MODEL,
                             fast=fast, verbose=verbose)
    return os.path.join(runs_dir, s["run_id"]), client, s


def _inline(obj, turn_dir: str):
    """request.json with every image block turned back into the base64 block that was sent."""
    if isinstance(obj, list):
        return [_inline(v, turn_dir) for v in obj]
    if not isinstance(obj, dict):
        return obj
    if obj.get("type") == "image" and "file" in obj:
        with open(os.path.join(turn_dir, obj["file"]), "rb") as f:
            data = base64.b64encode(f.read()).decode()
        rest = {k: v for k, v in obj.items() if k not in ("file", "sha256", "media_type", "bytes")}
        return {**rest, "source": {"type": "base64", "media_type": obj["media_type"], "data": data}}
    return {k: _inline(v, turn_dir) for k, v in obj.items()}


def verify(run_dir: str, received: list[dict]) -> list[tuple[str, bool]]:
    import numpy as np
    from robot_race import replay
    checks: list[tuple[str, bool]] = []
    add = lambda name, ok: checks.append((name, bool(ok)))
    turns = [json.loads(l) for l in open(os.path.join(run_dir, "transcript.jsonl")) if l.strip()]
    add(f"one transcript line per model call ({len(turns)})", len(turns) == len(received) > 0)
    for t, sent in zip(turns, received):
        n, tdir = t["turn"], os.path.join(run_dir, t["dir"])
        req = json.load(open(os.path.join(tdir, "request.json")))
        add(f"turn {n}: request.json (images re-inlined from img_*.png) == kwargs the client received",
            _inline(req, tdir) == sent)
        sent_imgs = [base64.b64decode(b["source"]["data"]) for m in sent["messages"] if isinstance(m["content"], list)
                     for b in m["content"] if b.get("type") == "image"]
        files = [open(os.path.join(run_dir, p), "rb").read() for p in t["images"]]
        add(f"turn {n}: {len(files)} img files byte-identical to the images sent", files == sent_imgs)
        add(f"turn {n}: sha256 in transcript == sha256 of sent bytes",
            t["image_sha256"] == [hashlib.sha256(b).hexdigest() for b in sent_imgs])
        resp = json.load(open(os.path.join(tdir, "response.json")))
        add(f"turn {n}: response.json text == scripted reply", resp["content"][0]["text"] == REPLIES[n - 1])
    obs = open(os.path.join(run_dir, "observation", "front.png"), "rb").read()
    add("observation/front.png == first image sent in turn 1",
        obs == base64.b64decode(received[0]["messages"][0]["content"][2]["source"]["data"]))
    summary = json.load(open(os.path.join(run_dir, "summary.json")))
    for a in summary["attempts"]:
        k, adir = a["k"], os.path.join(run_dir, f"attempt_{a['k']}")
        prov = json.load(open(os.path.join(adir, "provenance.json")))
        code = agent.extract_code(REPLIES[k - 1])
        ran = open(os.path.join(adir, "policy.py"), "rb").read()
        add(f"attempt {k}: code_matches_response is true", prov["code_matches_response"] is True)
        add(f"attempt {k}: sha256(policy.py) == sha256(code block in reply) == provenance",
            hashlib.sha256(ran).hexdigest() == agent.sha256_hex(code) == prov["policy_sha256"]
            == prov["response_code_sha256"] == a["code_sha256"])
        calls = json.load(open(os.path.join(adir, "calls.json")))["calls"]
        z = np.load(os.path.join(adir, "trajectory.npz"))
        t = z["t"]
        ts = [(c["t_start"], c["t_end"]) for c in calls]
        add(f"attempt {k}: {len(calls)} calls, times monotonic (t_start<=t_end<=next t_start)",
            calls and all(a0 <= a1 for a0, a1 in ts) and all(ts[i][1] <= ts[i + 1][0] + 1e-9 for i in range(len(ts) - 1)))
        add(f"attempt {k}: call times within trajectory [{t[0]:.2f}, {t[-1]:.2f}]",
            all(t[0] - 1e-6 <= a0 and a1 <= t[-1] + 1e-6 for a0, a1 in ts))
        mp4 = os.path.join(adir, "attempt.mp4")
        if os.path.exists(mp4):
            import imageio_ffmpeg
            fps = prov["video"]["fps"]
            vt = replay.video_times(t, fps)
            nframes, secs = imageio_ffmpeg.count_frames_and_secs(mp4)
            add(f"attempt {k}: mp4 has {nframes} frames == len(video_times) {len(vt)}; frame i = sim t i/fps",
                nframes == len(vt) == prov["video"]["n_frames"] and np.allclose(vt[:-1], np.arange(len(vt) - 1) / fps))
            # the viewer seeks to (round(t*fps)+0.5)/fps -> shows frame round(t*fps) -> nearest sample to that time
            idx = replay._nearest(t, vt)
            errs = [abs(t[idx[min(round(c0 * fps), len(vt) - 1)]] - c0) for c0, _ in ts]
            add(f"attempt {k}: click-to-seek lands on a frame within {max(errs):.3f}s of each call's t_start "
                f"(<= half a frame + half a sample)", max(errs) <= 0.5 / fps + 0.01 + 1e-9)
    add("summary.scripted is true (injected client) and hints are all off",
        summary.get("scripted") is True and summary.get("hints") == {"example": False, "strategy": None, "context": False})
    add("trace.html shows the SCRIPTED DEMO banner",
        "SCRIPTED DEMO, NOT A MODEL" in open(os.path.join(run_dir, "trace.html")).read())
    add("attempt 1 failed (flawed policy), attempt 2 solved",
        [a["result"]["success"] for a in summary["attempts"]] == [False, True] and summary["status"] == "solved")
    leaks = []
    for dp, _, fs in os.walk(run_dir):
        for f in fs:
            data = open(os.path.join(dp, f), "rb").read()
            if b"/Users/" in data or b"sk-ant" in data or os.path.expanduser("~").encode() in data:
                leaks.append(os.path.relpath(os.path.join(dp, f), run_dir))
    add(f"no local paths / API keys in any file under the run dir ({leaks or 'none'})", not leaks)
    add("trace.html written", os.path.isfile(os.path.join(run_dir, "trace.html")))
    return checks


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--runs-dir", "--runs", dest="runs_dir", default=DEFAULT_RUNS_DIR,
                    help="output root (default runs_demo/, never the public runs/)")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--fast", action="store_true", help="no mp4 (skips the seek checks)")
    return ap


def main():
    a = build_parser().parse_args()
    run_dir, client, _ = run_demo(a.runs_dir, a.seed, a.fast)
    checks = verify(run_dir, client.received)
    for name, ok in checks:
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print(f"run dir: {run_dir}\ntrace:   {os.path.join(run_dir, 'trace.html')}")
    sys.exit(0 if all(ok for _, ok in checks) else 1)


if __name__ == "__main__":
    main()
