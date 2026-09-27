"""Claude retry loop: see the scene, write run(robot), run it in the sim, read the score + key frames, revise.

Writes the web artifact contract (CLAUDE.md section 8) live: runs/<run_id>/summary.json (atomic) and
events.jsonl (append-only), plus attempt_<k>/response.md next to what the executor writes.
Audit trail: observation/ (the initial images + state the model got) and transcript/turn_<n>/ (every
request exactly as sent, images as the exact PNG bytes, the raw response, timing) -> trace.html.
Importable: the website backend / race orchestrator call run_agent_loop() in-process.
"""
from __future__ import annotations

import base64
import hashlib
import importlib
import io
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

from .interfaces import api_doc
from .tasks import TASKS
from .viewer import hint_labels, is_hinted

DEFAULT_MODEL = "claude-sonnet-5"  # pinned for reproducible races; override with ANTHROPIC_MODEL
MAX_TOKENS = 8192
API_RETRIES = 3               # our retries on top of the SDK's own (max_retries=2)
REPO = Path(__file__).resolve().parent.parent
# Hand-written solution. Reaches the model ONLY with example=True (--example), which is recorded in
# summary.json["hints"] and shown as a banner on trace.html / the run pages. Never used otherwise.
REFERENCE_POLICY = REPO / "policies" / "reference_pick_and_drop.py"
# Measured outcome sent back after an attempt. item_final_pos is simulator ground truth: only in oracle mode.
FEEDBACK_KEYS = ["success", "error", "time_s", "collisions", "energy_j", "lifted", "dropped"]
_sleep = time.sleep           # patched in tests

SYSTEM_ROLE = ("You are a robotics engineer writing Python control code for a simulated Franka Panda arm. "
               "Your code runs in a MuJoCo physics simulation; you then see its result and key frames "
               "and can revise it.")
OUTPUT_RULES = """OUTPUT RULES
- Reply with a short plan, then exactly one ```python code block that defines run(robot).
- Only `robot`, `np` (numpy) and `math` are available. np and math are pre-injected: do not import anything.
- No file, network or other I/O, no threads or subprocesses. Don't call run(robot) yourself.
- The whole episode must finish within the 40 s sim-time limit."""


def system_prompt(strategy: str | None = None, observation: str = "telemetry") -> str:
    parts = [SYSTEM_ROLE, api_doc(observation), OUTPUT_RULES]
    if strategy:
        parts.append(f"Strategy card: {strategy.strip()}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------- helpers

def slugify(text: str, n: int = 20) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:n].strip("-")


def make_run_id(task: str, seed: int, strategy: str | None = None) -> str:
    rid = f"{datetime.now():%Y%m%d-%H%M%S}-{task}-s{seed}"
    slug = slugify(strategy or "")
    return f"{rid}-{slug}" if slug else rid


def extract_code(text: str) -> str | None:
    """Last ```python block; falls back to the last bare ``` block that defines run()."""
    blocks = re.findall(r"```(?:python3?|py)[ \t]*\n(.*?)```", text, re.S)
    if not blocks:
        blocks = [b for b in re.findall(r"```[ \t]*\n(.*?)```", text, re.S) if "def run" in b]
    return blocks[-1].strip() + "\n" if blocks else None


def write_json_atomic(path: Path, obj) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2, default=str))
    os.replace(tmp, path)


def png_b64(img) -> str:
    from PIL import Image
    buf = io.BytesIO()
    Image.fromarray(img).save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def image_block(b64: str) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64}}


def text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def observe_scene(task: str, seed: int, size=(640, 480), observation: str = "telemetry") -> tuple[dict, dict[str, str]]:
    """Initial get_state() + front/top renders (base64 PNG) and their camera calibration, from a fresh episode."""
    from .robot import SimRobot
    from .tasks import Env
    env = Env(task, seed, record=False)
    try:
        robot = SimRobot(env, observation=observation)
        state = robot.get_state()
        state["camera_calibration"] = {v: robot.get_camera(v, *size) for v in ("front", "top")}
        images = {v: png_b64(env.render(v, *size)) for v in ("front", "top")}
    finally:
        env.close()
    return state, images


def first_turn(task: str, state: dict, images: dict[str, str], context: str | None, example: bool) -> list:
    content = []
    if context:
        content.append(text_block("Recalled memory from earlier runs (skills, past failures); use it if relevant:\n"
                                  + context.strip()))
    content.append(text_block(f"TASK: {TASKS[task]['text']}\n\nInitial robot.get_state():\n"
                              f"```json\n{json.dumps(state, indent=1)}\n```"))
    for view in ("front", "top"):
        content += [text_block(f"{view} camera:"), image_block(images[view])]
    if example:
        code = "\n".join(l for l in REFERENCE_POLICY.read_text().splitlines() if l.strip() != "import numpy as np")
        content.append(text_block("Worked example: a reference policy that solves the basic pick-and-drop "
                                  f"(adapt it; don't assume it is optimal):\n```python\n{code.strip()}\n```"))
    content.append(text_block("Write run(robot)."))
    return content


def feedback_turn(k: int, result: dict, attempt_dir: Path, no_code: bool = False,
                  observation: str = "telemetry") -> list:
    fb = {key: result.get(key) for key in FEEDBACK_KEYS + (["item_final_pos"] if observation == "oracle" else [])}
    fb["calls_tail"] = (result.get("calls") or [])[-12:]
    content = [text_block(f"Attempt {k} result:\n```json\n{json.dumps(fb, default=str)}\n```")]
    if no_code:
        content.append(text_block("Your reply had no ```python code block, so nothing ran."))
    frames = _keyframes(result, attempt_dir)
    if frames:
        content.append(text_block("Key frames (front camera) at t = 0, 1/3, 2/3 and the end of the episode:"))
        content += [image_block(base64.b64encode(p.read_bytes()).decode()) for p in frames]
    content.append(text_block("Revise run(robot)."))
    return content


def _keyframes(result: dict, attempt_dir: Path) -> list[Path]:
    rel = (result.get("media") or {}).get("keyframes") or []
    paths = [attempt_dir / r for r in rel if r] or sorted(attempt_dir.glob("key_*.png"))
    return [p for p in paths if p.exists()][:4]


def _with_cache_breakpoint(messages: list) -> list:
    """Copy of the history with a cache breakpoint on the last block of the newest user turn."""
    msgs = [dict(m) for m in messages]
    last = msgs[-1]
    if last["role"] == "user" and isinstance(last["content"], list) and last["content"]:
        last["content"] = list(last["content"])
        last["content"][-1] = {**last["content"][-1], "cache_control": {"type": "ephemeral"}}
    return msgs


def _get(obj, name, default=None):
    return obj.get(name, default) if isinstance(obj, dict) else getattr(obj, name, default)


def build_request(model: str, system: str, messages: list) -> dict:
    """The exact kwargs for client.messages.create (also what transcript/turn_<n>/request.json records)."""
    return dict(model=model, max_tokens=MAX_TOKENS,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=_with_cache_breakpoint(messages))


def call_model(client, model: str, system: str, messages: list, transcript: Transcript | None = None,
               attempt: int | None = None) -> tuple[str, dict]:
    """messages.create with prompt caching and a few retries on transient errors -> (text, usage).
    With a transcript, the request (as sent), the raw response and timing are written to disk."""
    import anthropic
    fatal = (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.BadRequestError,
             anthropic.NotFoundError)
    req, errors = build_request(model, system, messages), []
    if transcript:
        transcript.request(req, attempt)
    try:
        for i in range(API_RETRIES):
            try:
                resp = client.messages.create(**req)
                break
            except fatal:
                raise
            except Exception as e:  # noqa: BLE001  (connection, rate limit, overloaded, 5xx)
                errors.append(f"{type(e).__name__}: {e}")
                if i == API_RETRIES - 1:
                    raise
                wait = 2.0 * 2 ** i
                print(f"  API error ({type(e).__name__}: {e}); retrying in {wait:.0f}s")
                _sleep(wait)
    except BaseException as e:
        if transcript:
            transcript.failed(f"{type(e).__name__}: {e}", errors)
        raise
    text = "".join(_get(b, "text", "") for b in _get(resp, "content", []) if _get(b, "type") == "text")
    u = _get(resp, "usage")
    usage = {k: int(_get(u, k, 0) or 0) for k in
             ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}
    if transcript:
        transcript.response(resp, usage, errors)
    return text, usage


def sha256_hex(data: bytes | str) -> str:
    return hashlib.sha256(data.encode("utf-8") if isinstance(data, str) else data).hexdigest()


def _plain(obj):
    """Raw SDK response -> JSON-able dict (pydantic model_dump, or attribute walk for test doubles)."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump(mode="json")
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    if hasattr(obj, "__dict__"):
        return {k: _plain(v) for k, v in vars(obj).items() if not k.startswith("_")}
    return obj if isinstance(obj, (str, int, float, bool)) or obj is None else repr(obj)


class Transcript:
    """runs/<run_id>/transcript/turn_<n>/ per model call + run-level transcript.jsonl (one line per turn).

    request.json = the kwargs passed to messages.create, verbatim, except every base64 image block becomes
    {"type": "image", "file": "img_<i>.png", "sha256", "media_type", "bytes"}; img_<i>.png holds the decoded
    bytes of exactly that block (i = order of appearance in the request)."""

    def __init__(self, run: _Run):
        self.run, self.n, self.cur = run, 0, None
        self.root = run.dir / "transcript"

    def _externalize(self, obj, d: Path, images: list):
        if isinstance(obj, list):
            return [self._externalize(v, d, images) for v in obj]
        if not isinstance(obj, dict):
            return obj
        src = obj.get("source")
        if obj.get("type") == "image" and isinstance(src, dict) and src.get("type") == "base64":
            raw = base64.b64decode(src["data"])
            name = f"img_{len(images)}.png"
            (d / name).write_bytes(raw)
            images.append({"file": name, "sha256": sha256_hex(raw), "bytes": len(raw)})
            out = {k: v for k, v in obj.items() if k != "source"}
            out.update(file=name, sha256=images[-1]["sha256"], media_type=src.get("media_type"), bytes=len(raw))
            return out
        return {k: self._externalize(v, d, images) for k, v in obj.items()}

    def request(self, req: dict, attempt: int | None) -> None:
        self.n += 1
        d = self.root / f"turn_{self.n}"
        d.mkdir(parents=True, exist_ok=True)
        images: list = []
        body = self._externalize(req, d, images)
        write_json_atomic(d / "request.json", body)
        last = body["messages"][-1]["content"] if body["messages"] else []
        new = [b["file"] for b in last if isinstance(b, dict) and b.get("type") == "image"] \
            if isinstance(last, list) else []
        self.cur = dict(turn=self.n, attempt=attempt, dir=d, images=images, new=new, started=time.time())
        self.run.event("model_request", attempt, turn=self.n, n_images=len(images), n_new_images=len(new),
                       n_messages=len(req["messages"]), model=req.get("model"))

    def _finish(self, meta_extra: dict, line_extra: dict) -> None:
        c, t1 = self.cur, time.time()
        rel = f"transcript/turn_{c['turn']}"
        meta = dict(turn=c["turn"], attempt=c["attempt"], started=c["started"], finished=t1,
                    latency_s=round(t1 - c["started"], 3), **meta_extra)
        write_json_atomic(c["dir"] / "meta.json", meta)
        line = dict(turn=c["turn"], attempt=c["attempt"], dir=rel, request=f"{rel}/request.json",
                    response=f"{rel}/response.json" if (c["dir"] / "response.json").exists() else None,
                    meta=f"{rel}/meta.json", images=[f"{rel}/{i['file']}" for i in c["images"]],
                    image_sha256=[i["sha256"] for i in c["images"]], new_images=[f"{rel}/{f}" for f in c["new"]],
                    latency_s=meta["latency_s"], retries=meta["retries"], ts=t1, **line_extra)
        with open(self.run.dir / "transcript.jsonl", "a") as f:
            f.write(json.dumps(line, default=str) + "\n")
            f.flush()
        self.run.event("model_response", c["attempt"], turn=c["turn"], latency_s=meta["latency_s"],
                       retries=meta["retries"], n_images=len(c["images"]), **line_extra)

    def response(self, resp, usage: dict, errors: list) -> None:
        raw = _plain(resp)
        write_json_atomic(self.cur["dir"] / "response.json", raw)
        self._finish(dict(retries=len(errors), retry_errors=errors, model=_get(resp, "model"),
                          stop_reason=_get(resp, "stop_reason")),
                     dict(usage=usage, stop_reason=_get(resp, "stop_reason"), error=None))

    def failed(self, error: str, errors: list) -> None:
        self._finish(dict(retries=max(0, len(errors) - 1), retry_errors=errors, error=error),
                     dict(usage=None, stop_reason=None, error=error))


def save_observation(run_dir: Path, state: dict, images: dict[str, str]) -> dict:
    """observation/<view>.png (the exact bytes put in the first request) + state.json. Returns rel paths."""
    d = run_dir / "observation"
    d.mkdir(exist_ok=True)
    out = {}
    for view, b64 in images.items():
        (d / f"{view}.png").write_bytes(base64.b64decode(b64))
        out[view] = f"observation/{view}.png"
    write_json_atomic(d / "state.json", state)
    out["state"] = "observation/state.json"
    return out


def make_client():
    import anthropic
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY is not set: put ANTHROPIC_API_KEY=sk-ant-... in .env "
                           "(repo root) or export it.")
    # Keys not scoped to a workspace must name one per request (API error otherwise).
    ws = os.environ.get("ANTHROPIC_WORKSPACE_ID", "").strip()
    return anthropic.Anthropic(max_retries=2, default_headers={"anthropic-workspace-id": ws} if ws else None)


def is_scripted_client(client) -> bool:
    """True when the caller injected a client that is not a real anthropic.Anthropic (a scripted/fake model).
    None = run_agent_loop creates the real client itself (make_client) -> False."""
    if client is None:
        return False
    try:
        import anthropic
        return not isinstance(client, (anthropic.Anthropic, anthropic.AnthropicBedrock, anthropic.AnthropicVertex))
    except (ImportError, AttributeError):
        return True


def make_hints(example: bool, strategy: str | None, context: str | None, observation: str = "telemetry") -> dict:
    """Everything beyond the environment (task text, API_DOC, state, images) that reached the model.
    All default off; summary.json["hints"]."""
    return {"example": bool(example), "strategy": (strategy or "").strip() or None, "context": bool(context),
            "oracle_state": observation == "oracle"}


def _load_env() -> None:
    from dotenv import find_dotenv, load_dotenv
    load_dotenv(find_dotenv(usecwd=True))
    load_dotenv(REPO / ".env")


def _refresh_viewer(run_dir: Path, runs_dir: Path) -> None:
    try:
        importlib.import_module("robot_race.trace").write_trace(str(run_dir))
    except Exception as e:  # noqa: BLE001  (the trace page must never kill the run)
        print(f"  trace error: {type(e).__name__}: {e}")
    try:
        viewer = importlib.import_module("robot_race.viewer")
    except ImportError:
        return
    try:
        viewer.write_index(str(run_dir))
        viewer.update_manifest(str(runs_dir))
    except Exception as e:  # noqa: BLE001  (the page must never kill the run)
        print(f"  viewer error: {type(e).__name__}: {e}")


def _failed_result(task: str, seed: int, error: str) -> dict:
    from .interfaces import RESULT_KEYS
    r = {k: None for k in RESULT_KEYS}
    r.update(task=task, seed=seed, success=False, error=error, collisions=0, dropped=False, lifted=False,
             item_final_pos=None, calls=[], wall_s=0.0,
             media={"video": None, "poster": None, "keyframes": [], "trajectory": None, "live": None,
                    "calls_json": None, "provenance": None})
    return r


class _Run:
    """Owns runs/<run_id>/: summary.json (atomic) and events.jsonl (append + flush)."""

    def __init__(self, run_dir: Path, summary: dict):
        self.dir, self.summary = run_dir, summary
        self.events = run_dir / "events.jsonl"

    def event(self, type_: str, attempt: int | None = None, **data) -> None:
        line = {"ts": time.time(), "type": type_, "run_id": self.summary["run_id"], "attempt": attempt, **data}
        with open(self.events, "a") as f:
            f.write(json.dumps(line, default=str) + "\n")
            f.flush()

    def save(self) -> None:
        self.summary["updated_at"] = time.time()
        write_json_atomic(self.dir / "summary.json", self.summary)


def _make_run_dir(runs_dir: Path, run_id: str | None, task: str, seed: int, strategy: str | None) -> Path:
    if run_id:  # assigned by the backend: it may have created the dir already
        (runs_dir / run_id).mkdir(parents=True, exist_ok=True)
        return runs_dir / run_id
    base = make_run_id(task, seed, strategy)
    for i in range(1, 100):  # parallel racers started in the same second with the same slug
        d = runs_dir / (base if i == 1 else f"{base}-{i}")
        try:
            d.mkdir(parents=True)
            return d
        except FileExistsError:
            continue
    raise RuntimeError(f"could not create a run dir for {base}")


# ---------------------------------------------------------------- the loop

def run_agent_loop(task: str, seed: int, tries: int = 5, strategy: str | None = None, context: str | None = None,
                   example: bool = False, fast: bool = False, run_id: str | None = None, runs_dir: str = "runs",
                   client=None, timeout_s: float = 180, model: str | None = None, verbose: bool = True,
                   observation: str = "telemetry") -> dict:
    """Run the Claude retry loop; returns the final summary dict (also at runs/<run_id>/summary.json)."""
    if task not in TASKS:
        raise KeyError(f"unknown task {task!r}; choose from {list(TASKS)}")
    log = print if verbose else (lambda *a, **k: None)
    _load_env()
    model = model or os.environ.get("ANTHROPIC_MODEL") or DEFAULT_MODEL
    scripted = is_scripted_client(client)  # decided before we create the real client ourselves
    client = client or make_client()
    hints = make_hints(example, strategy, context, observation)
    runs_root = Path(runs_dir)
    run_dir = _make_run_dir(runs_root, run_id, task, seed, strategy)
    summary = dict(run_id=run_dir.name, task=task, seed=seed, model=model, strategy=strategy or "",
                   status="running", solved_at=None, created_at=time.time(), tries=tries, fast=fast,
                   example=example, context=bool(context), hints=hints, hinted=is_hinted(hints),
                   scripted=scripted, error=None, attempts=[],
                   transcript="transcript.jsonl", trace="trace.html", observation=None,
                   observation_mode=observation,
                   usage={"input_tokens": 0, "output_tokens": 0,
                          "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})
    run = _Run(run_dir, summary)
    transcript = Transcript(run)
    run.save()
    run.event("run_started", task=task, seed=seed, model=model, strategy=summary["strategy"], tries=tries,
              fast=fast, hints=hints, scripted=scripted, observation_mode=observation)
    _refresh_viewer(run_dir, runs_root)
    log(f"run {summary['run_id']}  model={model}  -> {run_dir}")
    if scripted:
        log("  SCRIPTED: injected client, not the Anthropic API (summary.scripted = true)")
    if summary["hinted"]:
        log(f"  HINTED: {hint_labels(hints)}")

    try:
        state, images = observe_scene(task, seed, observation=observation)
        summary["observation"] = save_observation(run_dir, state, images)
        run.save()
        system = system_prompt(strategy, observation)
        messages = [{"role": "user", "content": first_turn(task, state, images, context, example)}]
        for k in range(1, tries + 1):
            adir = run_dir / f"attempt_{k}"
            adir.mkdir(exist_ok=True)
            run.event("attempt_started", k)
            text, usage = call_model(client, model, system, messages, transcript, k)
            for key, v in usage.items():
                summary["usage"][key] += v
            messages.append({"role": "assistant", "content": text or "(empty reply)"})
            (adir / "response.md").write_text(text)
            code = extract_code(text)
            code_sha = sha256_hex(code) if code is not None else None
            entry = {"k": k, "code_path": None, "response_path": f"attempt_{k}/response.md", "code": code,
                     "code_sha256": code_sha, "transcript_turn": transcript.n, "result": None}
            if code is None:
                result = _failed_result(task, seed, "no python code block")
                write_json_atomic(adir / "result.json", result)
            else:
                entry["code_path"] = f"attempt_{k}/policy.py"
                run.event("code_generated", k, code=code, code_sha256=code_sha)
                result = _run_attempt(code, task, seed, adir, timeout_s, fast, code_sha, observation)
            entry["result"] = result
            summary["attempts"].append(entry)
            if result.get("success"):
                summary.update(status="solved", solved_at=k)
            run.save()
            run.event("attempt_finished", k, result=result)
            _refresh_viewer(run_dir, runs_root)
            err = (result.get("error") or "").strip().splitlines()
            log(f"[{k}/{tries}] {'SUCCESS' if result.get('success') else 'fail   '} "
                f"t={result.get('time_s')}s coll={result.get('collisions')} E={result.get('energy_j')}J "
                f"lifted={result.get('lifted')}" + (f"  err: {err[-1][:100]}" if err else ""))
            if result.get("success"):
                break
            if k < tries:
                messages.append({"role": "user", "content": feedback_turn(k, result, adir, no_code=code is None,
                                                                            observation=observation)})
        if summary["status"] == "running":
            summary["status"] = "failed"
    except Exception as e:  # noqa: BLE001  (API hard failure, scene build, ...): mark the run, re-raise nothing
        summary.update(status="error", error=f"{type(e).__name__}: {e}")
        log(f"run error: {summary['error']}")
    finally:
        if summary["status"] == "running":  # KeyboardInterrupt etc.
            summary.update(status="error", error=summary["error"] or "interrupted")
        run.save()
        run.event("run_finished", None, status=summary["status"], solved_at=summary["solved_at"],
                  error=summary["error"])
        _refresh_viewer(run_dir, runs_root)
    log(f"{summary['status'].upper()}" + (f" at attempt {summary['solved_at']}" if summary["solved_at"] else "")
        + f"  tokens in={summary['usage']['input_tokens']} out={summary['usage']['output_tokens']}")
    return summary


def _run_attempt(code: str, task: str, seed: int, adir: Path, timeout_s: float, fast: bool,
                 code_sha: str | None = None, observation: str = "telemetry") -> dict:
    try:
        executor = importlib.import_module("robot_race.executor")
        return executor.run_policy(code, task, seed, str(adir), timeout_s=timeout_s, fast=fast, live=True,
                                   response_code_sha256=code_sha, observation=observation)
    except Exception as e:  # noqa: BLE001  (executor bug: still a scored, failed attempt)
        (adir / "policy.py").write_text(code)
        result = _failed_result(task, seed, f"executor error: {type(e).__name__}: {e}")
        result["code_path"] = "policy.py"
        write_json_atomic(adir / "result.json", result)
        return result
