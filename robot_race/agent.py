"""Claude retry loop: see the scene, write run(robot), run it in the sim, read the score + key frames, revise.

Writes the web artifact contract (CLAUDE.md section 8) live: runs/<run_id>/summary.json (atomic) and
events.jsonl (append-only), plus attempt_<k>/response.md next to what the executor writes.
Importable: the website backend / race orchestrator call run_agent_loop() in-process.
"""
from __future__ import annotations

import base64
import importlib
import io
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

from .interfaces import API_DOC
from .tasks import TASKS

DEFAULT_MODEL = "claude-sonnet-5"  # pinned for reproducible races; override with ANTHROPIC_MODEL
MAX_TOKENS = 8192
API_RETRIES = 3               # our retries on top of the SDK's own (max_retries=2)
REPO = Path(__file__).resolve().parent.parent
REFERENCE_POLICY = REPO / "policies" / "reference_pick_and_drop.py"
FEEDBACK_KEYS = ["success", "error", "time_s", "collisions", "energy_j", "item_final_pos", "lifted", "dropped"]
_sleep = time.sleep           # patched in tests

SYSTEM_ROLE = ("You are a robotics engineer writing Python control code for a simulated Franka Panda arm. "
               "Your code runs in a MuJoCo physics simulation; you then see its result and key frames "
               "and can revise it.")
OUTPUT_RULES = """OUTPUT RULES
- Reply with a short plan, then exactly one ```python code block that defines run(robot).
- Only `robot`, `np` (numpy) and `math` are available. np and math are pre-injected: do not import anything.
- No file, network or other I/O, no threads or subprocesses. Don't call run(robot) yourself.
- The whole episode must finish within the 40 s sim-time limit."""


def system_prompt(strategy: str | None = None) -> str:
    parts = [SYSTEM_ROLE, API_DOC, OUTPUT_RULES]
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


def observe_scene(task: str, seed: int, size=(640, 480)) -> tuple[dict, dict[str, str]]:
    """Initial get_state() + front/top renders (base64 PNG) from a fresh, unrecorded episode."""
    from .robot import SimRobot
    from .tasks import Env
    env = Env(task, seed, record=False)
    try:
        state = SimRobot(env).get_state()
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


def feedback_turn(k: int, result: dict, attempt_dir: Path, no_code: bool = False) -> list:
    fb = {key: result.get(key) for key in FEEDBACK_KEYS}
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


def call_model(client, model: str, system: str, messages: list) -> tuple[str, dict]:
    """messages.create with prompt caching and a few retries on transient errors -> (text, usage)."""
    import anthropic
    fatal = (anthropic.AuthenticationError, anthropic.PermissionDeniedError, anthropic.BadRequestError,
             anthropic.NotFoundError)
    for i in range(API_RETRIES):
        try:
            resp = client.messages.create(
                model=model, max_tokens=MAX_TOKENS,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                messages=_with_cache_breakpoint(messages))
            break
        except fatal:
            raise
        except Exception as e:  # noqa: BLE001  (connection, rate limit, overloaded, 5xx)
            if i == API_RETRIES - 1:
                raise
            wait = 2.0 * 2 ** i
            print(f"  API error ({type(e).__name__}: {e}); retrying in {wait:.0f}s")
            _sleep(wait)
    text = "".join(_get(b, "text", "") for b in _get(resp, "content", []) if _get(b, "type") == "text")
    u = _get(resp, "usage")
    usage = {k: int(_get(u, k, 0) or 0) for k in
             ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")}
    return text, usage


def make_client():
    import anthropic
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("ANTHROPIC_API_KEY is not set: put ANTHROPIC_API_KEY=sk-ant-... in .env "
                           "(repo root) or export it.")
    return anthropic.Anthropic(max_retries=2)


def _load_env() -> None:
    from dotenv import find_dotenv, load_dotenv
    load_dotenv(find_dotenv(usecwd=True))
    load_dotenv(REPO / ".env")


def _refresh_viewer(run_dir: Path, runs_dir: Path) -> None:
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
             media={"video": None, "poster": None, "keyframes": [], "trajectory": None, "live": None})
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
                   client=None, timeout_s: float = 180, model: str | None = None, verbose: bool = True) -> dict:
    """Run the Claude retry loop; returns the final summary dict (also at runs/<run_id>/summary.json)."""
    if task not in TASKS:
        raise KeyError(f"unknown task {task!r}; choose from {list(TASKS)}")
    log = print if verbose else (lambda *a, **k: None)
    _load_env()
    model = model or os.environ.get("ANTHROPIC_MODEL") or DEFAULT_MODEL
    client = client or make_client()
    runs_root = Path(runs_dir)
    run_dir = _make_run_dir(runs_root, run_id, task, seed, strategy)
    summary = dict(run_id=run_dir.name, task=task, seed=seed, model=model, strategy=strategy or "",
                   status="running", solved_at=None, created_at=time.time(), tries=tries, fast=fast,
                   example=example, context=bool(context), error=None, attempts=[],
                   usage={"input_tokens": 0, "output_tokens": 0,
                          "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0})
    run = _Run(run_dir, summary)
    run.save()
    run.event("run_started", task=task, seed=seed, model=model, strategy=summary["strategy"], tries=tries,
              fast=fast)
    _refresh_viewer(run_dir, runs_root)
    log(f"run {summary['run_id']}  model={model}  -> {run_dir}")

    try:
        state, images = observe_scene(task, seed)
        system = system_prompt(strategy)
        messages = [{"role": "user", "content": first_turn(task, state, images, context, example)}]
        for k in range(1, tries + 1):
            adir = run_dir / f"attempt_{k}"
            adir.mkdir(exist_ok=True)
            run.event("attempt_started", k)
            text, usage = call_model(client, model, system, messages)
            for key, v in usage.items():
                summary["usage"][key] += v
            messages.append({"role": "assistant", "content": text or "(empty reply)"})
            (adir / "response.md").write_text(text)
            code = extract_code(text)
            entry = {"k": k, "code_path": None, "response_path": f"attempt_{k}/response.md", "code": code,
                     "result": None}
            if code is None:
                result = _failed_result(task, seed, "no python code block")
                write_json_atomic(adir / "result.json", result)
            else:
                entry["code_path"] = f"attempt_{k}/policy.py"
                run.event("code_generated", k, code=code)
                result = _run_attempt(code, task, seed, adir, timeout_s, fast)
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
                messages.append({"role": "user", "content": feedback_turn(k, result, adir, no_code=code is None)})
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


def _run_attempt(code: str, task: str, seed: int, adir: Path, timeout_s: float, fast: bool) -> dict:
    try:
        executor = importlib.import_module("robot_race.executor")
        return executor.run_policy(code, task, seed, str(adir), timeout_s=timeout_s, fast=fast, live=True)
    except Exception as e:  # noqa: BLE001  (executor bug: still a scored, failed attempt)
        (adir / "policy.py").write_text(code)
        result = _failed_result(task, seed, f"executor error: {type(e).__name__}: {e}")
        result["code_path"] = "policy.py"
        write_json_atomic(adir / "result.json", result)
        return result
