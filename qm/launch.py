"""Host-side launcher for a QM race, callable from the web UI backend or the shell.

    from qm.launch import launch_qm_race, qm_status
    out = launch_qm_race("can_to_bin", agents=4, seeds=[0], tries=5, label="qm")
    qm_status(out["race_id"])

    uv run python qm/launch.py launch --task can_to_bin --agents 4 --seeds 0 --label qm
    uv run python qm/launch.py status --race-id <race_id>

launch = open the race on the tracker, recall memory (GBrain + Memorable, host side), plan the
strategies, register them, write races/<race_id>/{plan,contexts,launch}.json, then start the QM root
turn with POST /v1/turns (surface "web", the robodojo-race skill does the rest). That call has to be
signed with QM's CORE_SIGNING_SECRET (+ PORTAL_IDENTITY_SECRET for the actor), read from qm/dev.env or
the environment, never printed. Without them, or if QM refuses, launch falls back to status "manual":
`qm.message` is the one line to send the QM root in its web UI.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT), str(ROOT / "qm")):
    if p not in sys.path:
        sys.path.insert(0, p)

from racetrack.client import Tracker  # noqa: E402
import plan_to_contexts as ptc  # noqa: E402


QM_URL = os.environ.get("QM_URL", "http://localhost:8081")
DEV_ENV = ROOT / "qm" / "dev.env"


def _dev_env() -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in DEV_ENV.read_text().splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def _secret(name: str) -> Optional[str]:
    return os.environ.get(name) or _dev_env().get(name) or None


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def portal_identity(principal: str, secret: str, ttl_s: int = 60) -> str:
    """QM's x-portal-identity token: compact JWS HS256 over {p, exp}, kid derived from the secret."""
    kid = _b64(hmac.new(secret.encode(), b"qm-signing-key-id", hashlib.sha256).digest())[:8]
    head = _b64(json.dumps({"alg": "HS256", "kid": kid}, separators=(",", ":")).encode())
    body = _b64(json.dumps({"p": principal, "exp": int(time.time() * 1000) + ttl_s * 1000},
                           separators=(",", ":")).encode())
    sig = _b64(hmac.new(secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest())
    return f"{head}.{body}.{sig}"


def signed_headers(secret: str, method: str, path: str, body: str = "",
                   now_s: Optional[int] = None) -> dict[str, str]:
    """QM source auth: x-signature = v0=HMAC_SHA256(secret, "v0:<ts>:<METHOD>\n<path?query>\n<body>")."""
    ts = int(time.time()) if now_s is None else now_s
    mac = hmac.new(secret.encode(), f"v0:{ts}:{method}\n{path}\n{body}".encode(), hashlib.sha256)
    return {"x-timestamp": str(ts), "x-signature": "v0=" + mac.hexdigest()}


def qm_request(method: str, path: str, body: Any = None, *, principal: Optional[str] = None,
               timeout: float = 30.0) -> tuple[int, Any]:
    """One signed call to QM's core API. Raises RuntimeError when no signing secret is configured."""
    secret = _secret("CORE_SIGNING_SECRET")
    if not secret:
        raise RuntimeError("CORE_SIGNING_SECRET not set in qm/dev.env or the environment")
    sep = "&" if "?" in path else "?"
    path = f"{path}{sep}_sourceAuthNonce={int(time.time() * 1000)}-{uuid.uuid4().hex[:12]}"
    raw = "" if body is None else json.dumps(body)
    headers = {"content-type": "application/json", **signed_headers(secret, method, path, raw)}
    psecret = _secret("PORTAL_IDENTITY_SECRET")
    if principal and psecret:
        headers["x-portal-identity"] = portal_identity(principal, psecret)
    req = urllib.request.Request(QM_URL.rstrip("/") + path, data=raw.encode() if raw else None,
                                 method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            text = r.read().decode()
            status = r.status
    except urllib.error.HTTPError as e:
        text, status = e.read().decode(errors="replace"), e.code
    try:
        return status, json.loads(text) if text else None
    except ValueError:
        return status, text[:500]


def qm_principal() -> str:
    """The QM user the race runs as (the dev portal's local principal by default)."""
    return (os.environ.get("QM_PRINCIPAL") or _dev_env().get("PORTAL_DEV_PRINCIPAL")
            or os.environ.get("USER") or "dev-admin")


def qm_auth_check() -> dict[str, Any]:
    """Dry check: a signed GET that costs nothing. ok = QM accepted our signature."""
    try:
        status, body = qm_request("GET", "/v1/swarm", principal=qm_principal(), timeout=10)
    except RuntimeError as e:
        return {"ok": False, "reason": str(e)}
    except (urllib.error.URLError, OSError) as e:
        return {"ok": False, "reason": f"QM unreachable at {QM_URL}: {e}"}
    unauthorized = status == 401 or (isinstance(body, dict) and body.get("error") == "unauthorized")
    return {"ok": not unauthorized, "status": status,
            "reason": (body.get("message") if isinstance(body, dict) else None) if unauthorized else None}


def start_qm_turn(race_id: str, message: str, contexts: list[dict[str, Any]]) -> dict[str, Any]:
    """Start the QM root turn that runs the robodojo-race skill for this race (async; returns ids)."""
    principal = qm_principal()
    text = (f"{message}. Use the robodojo-race skill as the root. The race is already prepared on the "
            f"tracker (race id {race_id}); spawn exactly these racer contexts:\n```json\n"
            f"{json.dumps(contexts)}\n```")
    body = {"surface": "web", "actor": {"externalId": principal},
            "conversation": {"kind": "dm", "threadRef": f"web:{principal}:robodojo-{race_id}"},
            "text": text, "origin": {"kind": "human"}, "addressed": True, "liveActor": True,
            "idempotencyKey": f"robodojo-{race_id}", "async": True}
    status, out = qm_request("POST", "/v1/turns?async=1", body, principal=principal)
    if status >= 400:
        raise RuntimeError(f"QM /v1/turns {status}: {json.dumps(out)[:300]}")
    out = out if isinstance(out, dict) else {}
    return {"status": "started", "http": status, "session_id": out.get("sessionId"),
            "run_id": out.get("runId"), "turn_status": out.get("status"),
            "thread_ref": body["conversation"]["threadRef"]}


def launch_qm_race(task: str = "can_to_bin", agents: int = 4, seeds: Optional[list[int]] = None,
                   tries: int = 5, label: str = "qm", *, planner: bool = True, memory: bool = True,
                   tracker_url: Optional[str] = None, start: bool = True,
                   custom: Optional[list] = None, plan: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Prepare a QM race (tracker race + plan + memory) and say how to start it. One seed per QM race:
    extra seeds are recorded but not raced (the swarm skill races one scene)."""
    seeds = list(seeds or [0])
    args = argparse.Namespace(task=task, seed=seeds[0], agents=agents, tries=tries, label=label,
                              race_id=None, plan=plan, custom=custom,
                              no_planner=not planner, no_memory=not memory,
                              url=tracker_url, sandbox_tracker_url="http://host.docker.internal:8000")
    prep = ptc.prepare(args)
    race_id = prep["race_id"]
    message = (f"Race {race_id} on {task} seed {seeds[0]}" if race_id else
               f"Race all strategies on {task} seed {seeds[0]}")
    launch = {"backend": "qm", "race_id": race_id, "task": task, "seeds": seeds, "seed": seeds[0],
              "agents": prep["agents"], "tries": tries, "label": label, "source": prep["source"],
              "memory": prep["memory"], "started": time.time(),
              "qm": {"status": "manual", "message": message, "session_id": None,
                     "note": "send `message` to the QM root (web UI); API launch needs QM's signing secret"},
              "skipped_seeds": seeds[1:]}
    race_dir = ROOT / prep["dir"]
    if start and race_id:
        try:
            contexts = json.loads((race_dir / "contexts.json").read_text())
            launch["qm"] = {**launch["qm"], **start_qm_turn(race_id, message, contexts), "note": None}
        except Exception as e:  # no secret, QM down, refused: a person starts it from QM's web UI
            launch["qm"]["error"] = f"{type(e).__name__}: {e}"
    (race_dir / "launch.json").write_text(json.dumps(launch, indent=2))
    launch["plan"] = json.loads((race_dir / "plan.json").read_text())
    launch["dir"] = prep["dir"]
    return launch


def _custom_args(custom_file: Optional[str], custom: list[str]) -> list[dict[str, Any]]:
    from racetrack.planner import load_custom
    return load_custom(custom_file, custom)


def qm_status(race_id: str, tracker_url: Optional[str] = None) -> dict[str, Any]:
    """Race progress as the tracker sees it (workers record every attempt live)."""
    launch_path = ROOT / "races" / race_id / "launch.json"
    launch = json.loads(launch_path.read_text()) if launch_path.exists() else None
    t = Tracker(tracker_url)
    try:
        board = t.leaderboard(race_id)
        agents = t._req("GET", f"/races/{race_id}/agents")
        attempts = t._req("GET", f"/races/{race_id}/attempts")
    except Exception as e:
        return {"race_id": race_id, "launch": launch, "error": f"tracker: {type(e).__name__}: {e}"}
    ids = ([a["agent_id"] for a in agents] if isinstance(agents, list)
           else list(agents))   # tracker returns {agent_id: info}; accept a list of agent dicts too
    per_agent = {a: {"attempts": 0, "solved": False} for a in ids}
    for a in attempts:
        s = per_agent.setdefault(a["agent_id"], {"attempts": 0, "solved": False})
        s["attempts"] = max(s["attempts"], a["attempt"])
        s["solved"] = s["solved"] or bool(a.get("success"))
    return {"race_id": race_id, "launch": launch, "final": board.get("final"),
            "winner": board.get("winner"), "leader": board.get("leader"), "workers": per_agent}


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default=None, help="tracker URL (default $RACETRACK_URL)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("launch")
    p.add_argument("--task", default="can_to_bin")
    p.add_argument("--agents", type=int, default=4)
    p.add_argument("--seeds", default="0", help="comma list; QM races the first")
    p.add_argument("--tries", type=int, default=5)
    p.add_argument("--label", default="qm")
    p.add_argument("--no-planner", action="store_true")
    p.add_argument("--no-memory", action="store_true")
    p.add_argument("--no-start", action="store_true", help="prepare only; don't call QM")
    p.add_argument("--custom", action="append", default=[], metavar="NAME::APPROACH",
                   help="user strategy raced verbatim as the first agents (repeatable)")
    p.add_argument("--custom-file", help='JSON list of {"name", "approach"[, "choices"]}')
    p.add_argument("--plan", help="race this plan.json (e.g. from `python -m racetrack.planner preview`)")
    p = sub.add_parser("status")
    p.add_argument("--race-id", required=True)
    sub.add_parser("auth-check", help="signed GET against QM (free); ok = signature accepted")
    args = ap.parse_args(argv)
    if args.cmd == "launch":
        out = launch_qm_race(args.task, args.agents, [int(s) for s in args.seeds.split(",") if s != ""],
                             args.tries, args.label, planner=not args.no_planner,
                             memory=not args.no_memory, tracker_url=args.url, start=not args.no_start,
                             custom=_custom_args(args.custom_file, args.custom) or None,
                             plan=json.loads(Path(args.plan).read_text()) if args.plan else None)
        out.pop("plan", None)
    elif args.cmd == "auth-check":
        out = qm_auth_check()
    else:
        out = qm_status(args.race_id, args.url)
    print(json.dumps(out, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
