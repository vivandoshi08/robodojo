"""Memory for the race: GBrain (distilled skill) + Memorable (episode memory), wired in automatically.

    before a race   pre_race_context(task)             -> markdown for run_agent.py --context-file
    each attempt    on_attempt_memorable(k, result, adir, ...)  -> one Episode in the local store
                    (+ Memorable ingest of successes when MEMORABLE_API_KEY is set)
    after a race    post_race(runs_dir)                -> `gbrain distill` (SKILL.md + GBrain page)

Everything fails open: a missing `bun`, `gbrain` or `memorable` binary, or a missing key, only shrinks
what gets recalled or stored; it never stops a race. What reaches the model is recorded as the
`context` hint (docs/INTEGRITY.md).

Env: ROBODOJO_MEMORY_ROOT (episode store; default races/_memory), MEMORABLE_API_KEY (cloud ingest +
recall), RACE_RUNS_DIR (read by gbrain; set here from runs_dir).
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
from datetime import datetime
from collections import Counter
from pathlib import Path

from robot_race.tasks import TASKS

REPO = Path(__file__).resolve().parent.parent
GBRAIN_DIR = REPO / "gbrain"
SKILL_PATH = GBRAIN_DIR / "skills" / "trash-to-bin" / "SKILL.md"
DEFAULT_BIN = (0.45, 0.35)


def _log(msg: str) -> None:
    print(f"[memory] {msg}", flush=True)


def _env() -> dict[str, str]:
    env = dict(os.environ)
    dotenv = REPO / ".env"  # ANTHROPIC_API_KEY for `gbrain think`, MEMORABLE_API_KEY
    if dotenv.exists():
        for line in dotenv.read_text().splitlines():
            k, sep, v = line.strip().partition("=")
            if sep and not k.startswith("#") and k.strip() not in env:
                env[k.strip()] = v.strip().strip('"').strip("'")
    env["PATH"] = f"{Path.home() / '.bun' / 'bin'}{os.pathsep}{env.get('PATH', '')}"
    env.setdefault("ROBODOJO_MEMORY_ROOT", str(REPO / "races" / "_memory"))
    return env


def _bun() -> str | None:
    return shutil.which("bun", path=_env()["PATH"])


def _gbrain() -> str | None:
    return shutil.which("gbrain", path=_env()["PATH"])


def gbrain_put(slug: str, markdown: str) -> bool:
    """Create or replace a GBrain page (stdin content, --force = overwrite)."""
    exe = _gbrain()
    if not exe:
        return False
    try:
        p = subprocess.run([exe, "put", slug, "--force"], input=markdown, env=_env(), capture_output=True,
                           text=True, timeout=120)
    except Exception as e:  # noqa: BLE001
        _log(f"gbrain put {slug} failed: {type(e).__name__}: {e}")
        return False
    if p.returncode != 0:
        _log(f"gbrain put {slug} failed: {p.stderr.strip()[-200:]}")
    return p.returncode == 0


def recorder():
    """AttemptRecorder on the shared episode store (ROBODOJO_MEMORY_ROOT)."""
    from memorable_layer.config import Settings
    from memorable_layer.recorder import AttemptRecorder
    return AttemptRecorder(settings=Settings.from_env(_env()))


def bin_geometry(task: str) -> tuple[float, float]:
    """(distance_cm, bearing_deg) of the bin from the robot base; bearing < 0 = left (+y)."""
    x, y = TASKS.get(task, {}).get("bin_center", DEFAULT_BIN)
    return round(math.hypot(x, y) * 100, 1), round(-math.degrees(math.atan2(y, x)), 1)


# ---------------------------------------------------------------- result.json -> Episode

def failure_reason(r: dict) -> str | None:
    """Same wording as gbrain/src/runs.ts failureReason (null if it succeeded)."""
    if r.get("success"):
        return None
    if r.get("bin_knocked_over"):
        return "knocked the bin over"
    if r.get("error"):
        return f"crashed ({r['error'].strip().splitlines()[-1][:160]})"
    if not r.get("lifted"):
        return "never lifted the item"
    if r.get("dropped"):
        return "dropped the item on the floor"
    return "missed the bin or bounced out"


def outcome(r: dict, task: str) -> str:
    if r.get("success"):
        return "in"
    if r.get("error") or not r.get("lifted"):
        return "no_grasp"
    if r.get("dropped"):
        return "dropped_early"
    pos = r.get("item_final_pos") or []
    if len(pos) >= 2:
        bx, by = TASKS.get(task, {}).get("bin_center", DEFAULT_BIN)
        if math.hypot(pos[0] - bx, pos[1] - by) < 0.2:
            return "rim_out"
    return "miss"


def episode_from_result(k: int, result: dict, adir: Path, *, task: str, seed: int | None = None,
                        strategy: str | None = None, race_id: str | None = None,
                        agent_id: str | None = None, run_id: str | None = None):
    from memorable_layer.episode import Episode
    dist, bearing = bin_geometry(task)
    code = Path(adir) / "policy.py"
    code_sha = hashlib.sha256(code.read_bytes()).hexdigest() if code.exists() else None
    run_id = run_id or Path(adir).parent.name
    return Episode(
        # scope = run: unique per (agent, seed), so episode ids never collide inside one race
        race_id=race_id or run_id, scope=run_id, strategy=strategy or "no-strategy",
        attempt=int(k), trash_type=TASKS.get(task, {}).get("item", task), bin_distance_cm=dist,
        bin_bearing_deg=bearing, params={}, outcome=outcome(result, task),
        reason=failure_reason(result) or "landed in the bin", agent_id=agent_id,
        bin_knocked_over=bool(result.get("bin_knocked_over")), duration_s=result.get("time_s"), seed=seed,
        extra={"task": task, "run_id": run_id, "code_sha256": code_sha,
               "attempt_dir": f"{run_id}/{Path(adir).name}",
               **{m: result.get(m) for m in ("time_s", "collisions", "energy_j", "lifted", "dropped",
                                             "item_final_pos")}},
    )


def on_attempt_memorable(k: int, result: dict, adir: Path, *, task: str, seed: int | None = None,
                         strategy: str | None = None, race_id: str | None = None,
                         agent_id: str | None = None) -> dict:
    """Record one attempt. Local store always; Memorable ingest only for successes with a key."""
    ep = episode_from_result(k, result, adir, task=task, seed=seed, strategy=strategy,
                             race_id=race_id, agent_id=agent_id)
    ingest = bool(_env().get("MEMORABLE_API_KEY"))
    out = recorder().record(ep, ingest=ingest)
    _log(f"stored episode {out['episode_id']} ({ep.outcome.value})"
         + (f", memorable: {out.get('slug') or out.get('refusal')}" if ingest and ep.success else ""))
    return out


def chain(*hooks):
    """One on_attempt callback that runs every hook; one failing never skips the others."""
    hooks = [h for h in hooks if h is not None]
    if not hooks:
        return None

    def run(k, result, adir):
        for h in hooks:
            try:
                h(k, result, adir)
            except Exception as e:  # noqa: BLE001
                _log(f"hook {getattr(h, '__name__', h)} failed: {type(e).__name__}: {e}")
    return run


# ---------------------------------------------------------------- recall

def gbrain_skill() -> str:
    """The distilled skill body (`bun gbrain/src/cli.ts brief`), frontmatter stripped."""
    bun = _bun()
    if bun:
        out = GBRAIN_DIR / ".brief.md"
        try:
            subprocess.run([bun, "src/cli.ts", "brief", "--out", str(out)], cwd=GBRAIN_DIR, env=_env(),
                           capture_output=True, timeout=60, check=True)
            text = out.read_text().strip()
            out.unlink(missing_ok=True)
            return text
        except Exception as e:  # noqa: BLE001
            _log(f"gbrain brief failed ({type(e).__name__}); reading SKILL.md directly")
    if not SKILL_PATH.exists():
        return ""
    text = SKILL_PATH.read_text()
    if text.startswith("---\n"):
        text = text.split("\n---\n", 1)[-1]
    return text.strip()


def task_episodes(task: str) -> list:
    """Episodes for this task from the JSONL log (the SQLite mirror drops `extra`, where task lives)."""
    from memorable_layer.episode import Episode
    path = recorder().store.jsonl_path
    if not path.exists():
        return []
    eps = [Episode.from_dict(json.loads(line)) for line in path.read_text().splitlines() if line.strip()]
    return [e for e in eps if (e.extra or {}).get("task") == task]


def episode_summary(task: str) -> tuple[str, dict]:
    eps = task_episodes(task)
    if not eps:
        return "", {"episodes": 0}
    wins = [e for e in eps if e.success]
    runs = {(e.extra or {}).get("run_id") for e in eps}
    by_strat: dict[str, list] = {}
    for e in eps:
        by_strat.setdefault(e.strategy, []).append(e)
    reasons = Counter(e.reason for e in eps if not e.success)
    lines = [f"{len(eps)} past attempts across {len(runs)} runs on `{task}`; {len(wins)} landed in the bin."]
    for s, es in sorted(by_strat.items(), key=lambda kv: -sum(x.success for x in kv[1])):
        w = [x for x in es if x.success]
        avg_t = sum(x.duration_s or 0 for x in w) / len(w) if w else None
        lines.append(f"- **{s}**: {len(w)}/{len(es)} attempts succeeded"
                     + (f", avg episode {avg_t:.1f} s" if avg_t else ""))
    if reasons:
        lines.append("- Most common failures: " + "; ".join(f"{r} (x{n})" for r, n in reasons.most_common(4)))
    if wins:
        last = max(wins, key=lambda e: e.recorded_at)
        lines.append(f"- Latest success: strategy **{last.strategy}**, attempt {last.attempt}, "
                     f"{last.duration_s} s, {(last.extra or {}).get('collisions')} collisions.")
    stats = {"episodes": len(eps), "successes": len(wins), "runs": len(runs),
             "top_failures": reasons.most_common(4)}
    return "\n".join(lines), stats


def memorable_recall(task: str) -> tuple[str, list]:
    """Cloud recall through the memorable CLI (only with a key; empty otherwise)."""
    if not _env().get("MEMORABLE_API_KEY"):
        return "", []
    try:
        from memorable_layer.trace import recall_query
        rec = recorder()
        dist, bearing = bin_geometry(task)
        item = TASKS.get(task, {}).get("item", task)
        hits = []
        for e in task_episodes(task):
            if e.strategy not in {h.get("strategy") for h in hits}:
                for h in rec.client.recall(recall_query(e.strategy, item, dist, bearing), limit=1):
                    hits.append({**h.to_dict(), "strategy": e.strategy})
        hits = sorted(hits, key=lambda h: -h["score"])[:2]
        shown = [rec.client.show(h["slug"]) for h in hits]
        return "\n\n".join(s for s in shown if s), hits
    except Exception as e:  # noqa: BLE001
        _log(f"memorable recall failed: {type(e).__name__}: {e}")
        return "", []


THINK_TIMEOUT_S = 180


def gbrain_think(task: str, race_id: str | None = None) -> dict:
    """`gbrain think` over past races, anchored on the distilled skill; the answer is saved as a page.

    (`think --save` needs gbrain's persistence coordinator, which the local CLI refuses, so the answer
    is written with `gbrain put thinks/<race_id>` instead.)"""
    exe = _gbrain()
    if not exe:
        return {"used": False, "error": "gbrain not on PATH"}
    text = TASKS.get(task, {}).get("text", task)
    q = (f"{text} ({task}): what worked and failed in past races, which strategies to reuse vs avoid, "
         "what to try next")
    try:
        p = subprocess.run([exe, "think", q, "--anchor", "procedures/trash-to-bin", "--json"], env=_env(),
                           capture_output=True, text=True, timeout=THINK_TIMEOUT_S)
        data = json.loads(p.stdout) if p.returncode == 0 and p.stdout.strip() else None
    except Exception as e:  # noqa: BLE001
        return {"used": False, "error": f"{type(e).__name__}: {e}"}
    if not data or not (data.get("answer") or "").strip():
        return {"used": False, "error": (p.stderr.strip()[-300:] or "empty answer")}
    cited = sorted({c.get("page_slug") for c in data.get("citations") or [] if c.get("page_slug")})
    slug = f"thinks/{race_id or datetime.now().strftime('%Y%m%d-%H%M%S')}-{task}"
    page = (f"---\ntitle: \"Pre-race think: {task}\"\ntype: think\ntask: {task}\nrace_id: {race_id or ''}\n---\n\n"
            f"# Pre-race think: {task}\n\n**Question:** {q}\n\n{data['answer'].strip()}\n\n"
            + ("## Gaps\n" + "\n".join(f"- {g}" for g in data.get("gaps") or []) + "\n\n" if data.get("gaps") else "")
            + "Cites: " + ", ".join(f"[[{c}]]" for c in cited) + "\n")
    saved = gbrain_put(slug, page)
    return {"used": True, "answer": data["answer"].strip(), "gaps": data.get("gaps") or [],
            "cited_pages": cited, "saved_slug": slug if saved else None, "cost_usd": data.get("cost_usd"),
            "model": data.get("modelUsed")}


def recall(task: str, race_id: str | None = None, think: bool = True) -> tuple[str, dict]:
    """(markdown context for racers, memory.json-ready summary of what was recalled)."""
    skill = gbrain_skill()
    episodes, ep_stats = episode_summary(task)
    recalled, hits = memorable_recall(task)
    th = gbrain_think(task, race_id) if think and "Distilled from" in skill else {"used": False}
    parts = ["# Memory from past races (recalled automatically)",
             "Facts from earlier attempts in this same environment. Use them or ignore them."]
    if "Distilled from" in skill:
        parts += ["## Distilled skill (GBrain: procedures/trash-to-bin)", skill]
    if th.get("used"):
        parts += ["## Past races vs now (gbrain think)", th["answer"]
                  + ("\n\nCited: " + ", ".join(th["cited_pages"]) if th["cited_pages"] else "")]
    if episodes:
        parts += ["## Episode memory (Memorable episode store)", episodes]
    if recalled:
        parts += ["## Recalled procedures (Memorable)", recalled]
    has_memory = len(parts) > 2
    info = {"task": task, "race_id": race_id, "gbrain_skill": "Distilled from" in skill,
            "think": th, "episodes": ep_stats,
            "memorable_hits": hits, "has_memory": has_memory}
    return ("\n\n".join(parts) + "\n") if has_memory else "", info


def pre_race_context(task: str, race_id: str | None = None, races_dir: str | Path | None = None) -> str:
    """Context markdown for every racer. With a race_id, also writes races/<id>/context.md + memory.json."""
    text, info = recall(task, race_id)
    if race_id:
        d = Path(races_dir or REPO / "races") / race_id
        d.mkdir(parents=True, exist_ok=True)
        if text:
            (d / "context.md").write_text(text)
        _merge_json(d / "memory.json", {"recalled": info, "context_file": "context.md" if text else None})
    return text


def think_to_lessons(info: dict | None) -> list[str]:
    """The `gbrain think` answer from recall() info as planner lessons (its bullet lines)."""
    ans = ((info or {}).get("think") or {}).get("answer") or ""
    return [f"[gbrain think] {line.strip()[2:]}" for line in ans.splitlines()
            if line.strip().startswith(("- ", "* "))][:15]


def _merge_json(path: Path, patch: dict) -> None:
    cur = json.loads(path.read_text()) if path.exists() else {}
    cur.update(patch)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(cur, indent=2, default=str))
    os.replace(tmp, path)


# ---------------------------------------------------------------- after the race

def distill(runs_dir: str | Path = "runs") -> dict:
    """Re-distill every finished run into the GBrain skill (SKILL.md + GBrain page)."""
    bun = _bun()
    if not bun:
        _log("bun not found: skipping gbrain distill")
        return {"distilled": False, "error": "bun not found"}
    env = {**_env(), "RACE_RUNS_DIR": str(Path(runs_dir).resolve())}
    has_gbrain = _gbrain() is not None
    cmd = [bun, "src/cli.ts", "distill"] + ([] if has_gbrain else ["--print"])
    try:
        p = subprocess.run(cmd, cwd=GBRAIN_DIR, env=env, capture_output=True, text=True, timeout=300)
    except Exception as e:  # noqa: BLE001
        return {"distilled": False, "error": f"{type(e).__name__}: {e}"}
    msg = (p.stdout.strip().splitlines() or [""])[0] if p.returncode == 0 else p.stderr.strip()[-300:]
    _log(f"gbrain distill: {msg}" + ("" if has_gbrain else " (gbrain not on PATH: SKILL.md only)"))
    return {"distilled": p.returncode == 0, "gbrain_page": has_gbrain and p.returncode == 0,
            "message": msg, "skill_path": str(SKILL_PATH.relative_to(REPO))}


def _race_material(race_id: str, races_dir: Path) -> dict:
    """plan / race / close from races/<id>/, else from the tracker (QM races have no local dir)."""
    d = races_dir / race_id
    rd = lambda n: json.loads((d / n).read_text()) if (d / n).exists() else None  # noqa: E731
    m = {"plan": rd("plan.json"), "race": rd("race.json"), "close": rd("close.json"), "source": "files"}
    if not d.exists():
        m["source"] = "none"
    if m["plan"] is None or m["close"] is None:
        try:
            from racetrack.client import Tracker
            t = Tracker(timeout=10)
            board = t.leaderboard(race_id)
            agents = t._req("GET", f"/races/{race_id}/agents")
            m["close"] = m["close"] or {"winner": board.get("winner"), "lessons": t.lessons(race_id),
                                        "scores": board.get("scores")}
            m["plan"] = m["plan"] or {"strategies": [{"agent_id": a.get("agent_id"),
                                                      "name": a.get("persona") or a.get("name") or a.get("agent_id"),
                                                      **(a.get("info") or {})} for a in agents or []]}
            m["race"] = m["race"] or t._req("GET", f"/races/{race_id}")
            m["source"] = "tracker" if m["source"] == "none" else "files+tracker"
        except Exception as e:  # noqa: BLE001
            m["tracker_error"] = f"{type(e).__name__}: {e}"
    return m


def race_page(race_id: str, m: dict) -> str:
    plan, race, close = m.get("plan") or {}, m.get("race") or {}, m.get("close") or {}
    task = plan.get("task_id") or race.get("task") or ""
    winner = close.get("winner") or {}
    lessons = close.get("lessons") or {}
    skill = lessons.get("skill") or close.get("skill") or {}
    out = [f"---\ntitle: \"Race {race_id}\"\ntype: race\ntask: {task}\n"
           f"winner: {winner.get('agent_id') or ''}\n---\n",
           f"# Race {race_id}", f"Task: `{task}`. Skill: [[procedures/trash-to-bin]].", "", "## Strategies"]
    for s in plan.get("strategies") or []:
        ch = "; ".join(f"{k}={v}" for k, v in (s.get("choices") or {}).items())
        out.append(f"- **{s.get('agent_id')}: {s.get('name')}** [{s.get('mode', 'explore')}]"
                   + (f" ({ch})" if ch else "") + (f": {s['approach']}" if s.get("approach") else ""))
    scores = close.get("scores") or (m.get("leaderboard") or {}).get("scores") or []
    if scores:
        out += ["", "## Leaderboard", "| rank | agent | score | solved | first-try |", "|---|---|---|---|---|"]
        for a in scores:
            out.append(f"| {a.get('rank')} | {a.get('agent_id')} {a.get('persona') or ''} | "
                       f"{a.get('score', 0):.1f} | {a.get('seeds_solved')}/{a.get('seeds_target')} | "
                       f"{a.get('first_try_rate', 0):.0%} |")
    elif race.get("local_ranking"):
        out += ["", "## Ranking (local)"] + [f"{b['rank']}. {b['agent_id']} {b['strategy']}: solved "
                                             f"{b['solved']}/{b['seeds']}" for b in race["local_ranking"]]
    out += ["", f"## Winner: {winner.get('agent_id') or 'none'} {winner.get('persona') or ''}".rstrip()]
    fails = Counter()
    for r in race.get("runs") or []:
        fails[r.get("status")] += 1
    if lessons.get("lines"):
        out += ["", "## Lessons"] + [f"- {x}" for x in lessons["lines"]]
    if fails:
        out += ["", "## Run outcomes", ", ".join(f"{k}: {v}" for k, v in fails.items())]
    if skill.get("code"):
        out += ["", f"## Winning code ({skill.get('agent_id')}, seed {skill.get('seed')}, "
                    f"attempt {skill.get('attempt')})", "```python", skill["code"].rstrip(), "```"]
    return "\n".join(out) + "\n"


def post_race(race_id: str | None = None, runs_dir: str | Path = "runs",
              races_dir: str | Path | None = None) -> dict:
    """After a race: distill all runs into the skill, then a GBrain page races/<race_id>.

    Works from race_id alone: races/<id>/ files if present, else the tracker (QM races)."""
    races_dir = Path(races_dir or REPO / "races")
    out = {"distill": distill(runs_dir)}
    if race_id:
        m = _race_material(race_id, races_dir)
        if m["source"] == "none":
            out["race_page"] = {"slug": None, "saved": False, "source": "none",
                                "tracker_error": m.get("tracker_error")}
            return out
        page = race_page(race_id, m)
        out["race_page"] = {"slug": f"races/{race_id}", "saved": gbrain_put(f"races/{race_id}", page),
                            "source": m["source"], "tracker_error": m.get("tracker_error")}
        d = races_dir / race_id
        if d.exists():
            (d / "race_page.md").write_text(page)
            _merge_json(d / "memory.json", {"post_race": out})
    return out
