"""Run a policy file on N seeds (and tasks) in parallel, print a score table, write the run artifacts.

    python run_policy.py policies/reference_pick_and_drop.py --task can_to_bin --seeds 0-9 --jobs 4 [--fast]

Writes runs/<run_id>/{summary.json, events.jsonl, index.html, seed_<n>/...} (CLAUDE.md section 8);
with --task all the per-seed dirs are <task>__seed_<n>. Exit code 0 iff every episode succeeded.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

COLS = ["task", "seed", "success", "time_s", "collisions", "energy_j", "lifted", "dropped", "wall_s", "error"]


def parse_seeds(s: str) -> list[int]:
    """'0-9' -> 0..9, '0,2,5' -> [0, 2, 5], '3' -> [3]; parts can be mixed ('0-2,7')."""
    out: list[int] = []
    for part in str(s).replace(" ", "").split(","):
        if not part:
            continue
        if "-" in part:
            a, b = (int(x) for x in part.split("-", 1))
            if b < a:
                raise ValueError(f"bad seed range {part!r}")
            out += range(a, b + 1)
        else:
            out.append(int(part))
    if not out:
        raise ValueError(f"no seeds in {s!r}")
    return list(dict.fromkeys(out))


def parse_tasks(s: str) -> list[str]:
    from robot_race.tasks import TASKS
    tasks = list(TASKS) if s == "all" else [t for t in s.split(",") if t]
    for t in tasks:
        if t not in TASKS:
            raise ValueError(f"unknown task {t!r}; choose from {list(TASKS)} or 'all'")
    return tasks


def _write_json(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2, default=str)
    os.replace(tmp, path)


def _short_err(err) -> str:
    if not err:
        return ""
    lines = [l.strip() for l in str(err).strip().splitlines() if l.strip()]
    s = lines[-1] if lines else str(err)
    return s if len(s) <= 60 else s[:57] + "..."


def format_table(results: list[dict]) -> str:
    def cell(r, c):
        v = r.get(c)
        if c == "error":
            return _short_err(v)
        if isinstance(v, bool):
            return "yes" if v else "no"
        if isinstance(v, float):
            return f"{v:.2f}"
        return "" if v is None else str(v)
    rows = [COLS] + [[cell(r, c) for c in COLS] for r in results]
    w = [max(len(row[i]) for row in rows) for i in range(len(COLS))]
    lines = ["  ".join(x.ljust(w[i]) for i, x in enumerate(row)).rstrip() for row in rows]
    lines.insert(1, "  ".join("-" * n for n in w))
    return "\n".join(lines)


def _rates(results: list[dict], tasks: list[str]) -> dict:
    out = {}
    for t in tasks:
        rs = [r for r in results if r.get("task") == t]
        out[t] = round(sum(bool(r.get("success")) for r in rs) / len(rs), 4) if rs else None
    return out


def run_batch(policy_path: str, tasks, seeds, jobs: int = 4, fast: bool = False, timeout_s: float = 180,
              run_id: str | None = None, runs_dir: str = "runs", quiet: bool = False) -> dict:
    """Run policy_path on every (task, seed) with `jobs` parallel executor subprocesses. Returns the summary."""
    from robot_race import executor  # lazy: step A

    tasks = [tasks] if isinstance(tasks, str) else list(tasks)
    seeds = parse_seeds(seeds) if isinstance(seeds, str) else [int(s) for s in seeds]
    code = open(policy_path).read()
    policy = os.path.splitext(os.path.basename(policy_path))[0]
    task_label = tasks[0] if len(tasks) == 1 else "all"
    run_id = run_id or f"{datetime.now():%Y%m%d-%H%M%S}-policy-{policy}-{task_label}"
    run_dir = os.path.join(runs_dir, run_id)
    os.makedirs(run_dir, exist_ok=True)
    say = (lambda *a: None) if quiet else (lambda *a: print(*a, flush=True))

    lock = threading.Lock()
    events_path = os.path.join(run_dir, "events.jsonl")

    def emit(type_, attempt=None, **kw):
        ev = dict(ts=time.time(), type=type_, run_id=run_id, attempt=attempt, **kw)
        with lock, open(events_path, "a") as f:
            f.write(json.dumps(ev, default=str) + "\n")

    t0 = time.time()
    summary = dict(run_id=run_id, kind="policy", policy=policy, policy_path=policy_path, code=code,
                   task=task_label, tasks=tasks, seeds=seeds, seed=seeds[0] if len(seeds) == 1 else None,
                   model=None, strategy=None, jobs=jobs, fast=fast, timeout_s=timeout_s,
                   status="running", success_rate=None, success_rate_by_task={}, solved_at=None,
                   created_at=datetime.now().isoformat(timespec="seconds"), finished_at=None, wall_s=None,
                   results=[])
    _write_json(os.path.join(run_dir, "summary.json"), summary)
    emit("run_started", kind="policy", policy=policy, tasks=tasks, seeds=seeds, fast=fast)

    jobs_list = [(t, s) for t in tasks for s in seeds]
    sub = (lambda t, s: f"seed_{s}") if len(tasks) == 1 else (lambda t, s: f"{t}__seed_{s}")

    def one(task, seed):
        out_dir = os.path.join(run_dir, sub(task, seed))
        os.makedirs(out_dir, exist_ok=True)
        t1 = time.time()
        try:
            res = executor.run_policy(code, task, seed, out_dir, timeout_s=timeout_s, fast=fast)
        except Exception:  # the executor itself blew up: record it as a failed episode
            res = dict(success=False, error="run_policy crashed:\n" + "".join(traceback.format_exc().splitlines(True)[-6:]))
        res = dict(res or {})
        res.setdefault("wall_s", round(time.time() - t1, 2))
        res.update(task=task, seed=seed, dir=os.path.basename(out_dir))
        return res

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        futs = {pool.submit(one, t, s): (t, s) for t, s in jobs_list}
        for fut in as_completed(futs):
            r = fut.result()
            results.append(r)
            emit("attempt_finished", attempt=r["seed"], task=r["task"], seed=r["seed"], result=r)
            say(f"[{len(results)}/{len(jobs_list)}] {r['task']} seed {r['seed']}: "
                f"{'OK  ' if r.get('success') else 'FAIL'} sim {r.get('time_s')} s, wall {r.get('wall_s')} s"
                + (f"  ({_short_err(r.get('error'))})" if r.get("error") else ""))
            with lock:
                summary["results"] = sorted(results, key=lambda x: (tasks.index(x["task"]), x["seed"]))
                _write_json(os.path.join(run_dir, "summary.json"), summary)

    results = summary["results"]
    n_ok = sum(bool(r.get("success")) for r in results)
    rate = round(n_ok / len(results), 4) if results else 0.0
    summary.update(status="solved" if results and n_ok == len(results) else "failed", success_rate=rate,
                   success_rate_by_task=_rates(results, tasks), wall_s=round(time.time() - t0, 2),
                   finished_at=datetime.now().isoformat(timespec="seconds"))
    _write_json(os.path.join(run_dir, "summary.json"), summary)
    emit("run_finished", status=summary["status"], solved_at=None, success_rate=rate)

    say("\n" + format_table(results) + "\n")
    for t, v in summary["success_rate_by_task"].items():
        k = sum(bool(r.get("success")) for r in results if r["task"] == t)
        say(f"{t:16s} {k}/{len([r for r in results if r['task'] == t])}  ({v:.0%})")
    say(f"overall {n_ok}/{len(results)} ({rate:.0%}) in {summary['wall_s']:.1f} s wall, jobs={jobs} -> {run_dir}")

    try:
        from robot_race import viewer  # lazy: step C
    except ImportError:
        viewer = None
    if viewer is not None:
        for fn, arg in ((getattr(viewer, "write_index", None), run_dir), (getattr(viewer, "update_manifest", None), runs_dir)):
            if fn is None:
                continue
            try:
                p = fn(arg)
                say(f"viewer: {p}")
            except Exception as e:  # never lose a batch over the gallery
                say(f"viewer {fn.__name__} failed: {e!r}")
    summary["run_dir"] = run_dir
    return summary


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("policy", help="policy file defining run(robot)")
    ap.add_argument("--task", default="can_to_bin", help="task name, comma list, or 'all'")
    ap.add_argument("--seeds", default="0", help="'0-9', '0,2,5' or '3'")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--fast", action="store_true", help="key frames + trajectory only, no mp4")
    ap.add_argument("--timeout", type=float, default=180, help="wall-clock seconds per episode")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--runs-dir", default="runs")
    a = ap.parse_args(argv)
    s = run_batch(a.policy, parse_tasks(a.task), parse_seeds(a.seeds), jobs=a.jobs, fast=a.fast,
                  timeout_s=a.timeout, run_id=a.run_id, runs_dir=a.runs_dir)
    return 0 if s["status"] == "solved" else 1


if __name__ == "__main__":
    sys.exit(main())
