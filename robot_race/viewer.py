"""Local gallery + web manifest for runs/ (CLAUDE.md section 8 is the contract).

write_index(run_dir)      -> runs/<run_id>/index.html  (one card per attempt, auto-refresh while running)
update_manifest(runs_root) -> runs/index.json + runs/index.html (all runs, newest first)
python -m robot_race.viewer [runs_root_or_run_dir]

Readers here must tolerate writers that are mid-write: corrupt/partial JSON is skipped, never fatal.
"""
from __future__ import annotations

import html
import json
import os
import re
import sys
import threading
import time
from datetime import datetime
from urllib.parse import quote

REFRESH_S = 2


# ---------------------------------------------------------------- helpers

def _natkey(s: str):
    return [int(p) if p.isdigit() else p for p in re.split(r"(\d+)", s)]


def _load_json(path: str):
    """Parsed JSON or None (missing, partial or corrupt)."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _read_text(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except OSError:
        return None


def _atomic_write(path: str, text: str) -> str:
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"  # unique per thread: parallel runs share a process
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)
    return path


def _e(x) -> str:
    return html.escape("" if x is None else str(x))


def _url(*parts: str) -> str:
    return "/".join(quote(p) for p in parts if p)


def attempt_dirs(run_dir: str) -> tuple[list[str], list[str]]:
    """(finished, in_progress) subdir names, natural-sorted. finished = has result.json,
    in_progress = has live.jpg but no result.json yet."""
    done, live = [], []
    try:
        names = sorted(os.listdir(run_dir), key=_natkey)
    except OSError:
        return done, live
    for n in names:
        d = os.path.join(run_dir, n)
        if n.startswith(".") or not os.path.isdir(d):
            continue
        if os.path.isfile(os.path.join(d, "result.json")):
            done.append(n)
        elif os.path.isfile(os.path.join(d, "live.jpg")):
            live.append(n)
    return done, live


def _media_file(adir: str, result: dict | None, key: str, default: str) -> str | None:
    """Relative media path inside an attempt dir, if the file exists."""
    cands = []
    m = (result or {}).get("media")
    if isinstance(m, dict) and isinstance(m.get(key), str) and not os.path.isabs(m[key]):
        cands.append(m[key])
    cands.append(default)
    for c in cands:
        if os.path.isfile(os.path.join(adir, c)):
            return c
    return None


def attempt_media(adir: str, result: dict | None = None) -> dict:
    keys = sorted((f for f in _listdir(adir) if re.fullmatch(r"key_\d+\.png", f)), key=_natkey)
    return dict(
        video=_media_file(adir, result, "video", "attempt.mp4"),
        gif=_media_file(adir, result, "gif", "attempt.gif"),
        poster=_media_file(adir, result, "poster", "poster.jpg"),
        keyframes=keys,
        live=_media_file(adir, result, "live", "live.jpg"),
    )


def _listdir(d: str) -> list[str]:
    try:
        return os.listdir(d)
    except OSError:
        return []


def _still(m: dict) -> str | None:
    """Best single image for an attempt: poster, else last key frame, else live frame."""
    return m["poster"] or (m["keyframes"][-1] if m["keyframes"] else None) or m["live"]


def _created_at(run_dir: str, summary: dict | None) -> str:
    v = (summary or {}).get("created_at")
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v).isoformat(timespec="seconds")
    if isinstance(v, str) and v:
        return v
    m = re.match(r"(\d{8}-\d{6})", os.path.basename(os.path.normpath(run_dir)))
    if m:
        try:
            return datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").isoformat(timespec="seconds")
        except ValueError:
            pass
    try:
        return datetime.fromtimestamp(os.path.getmtime(run_dir)).isoformat(timespec="seconds")
    except OSError:
        return ""


def run_info(run_dir: str) -> dict | None:
    """Everything the manifest/home page need about one run, or None if it isn't a (readable) run.
    Agent runs have summary.json; run_policy runs only have <subdir>/result.json."""
    spath = os.path.join(run_dir, "summary.json")
    summary = None
    if os.path.exists(spath):
        summary = _load_json(spath)
        if not isinstance(summary, dict):
            return None  # corrupt or mid-write: skip this run for now
    done, live = attempt_dirs(run_dir)
    if summary is None and not done:
        return None
    results = {}
    for n in done:
        r = _load_json(os.path.join(run_dir, n, "result.json"))
        results[n] = r if isinstance(r, dict) else None
    ok = [n for n in done if (results[n] or {}).get("success")]
    best = ok[0] if ok else (done[-1] if done else (live[-1] if live else None))
    first = next((r for r in results.values() if r), {})
    s = summary or {}
    if summary is not None:
        status = s.get("status") or ("solved" if ok else ("running" if live else "failed"))
    else:
        status = "solved" if ok else "failed"
    seeds = sorted({r.get("seed") for r in results.values() if r and r.get("seed") is not None},
                   key=lambda x: (str(type(x)), x))
    n_att = max(len(done) + len(live), len(s.get("attempts") or []) if isinstance(s.get("attempts"), list) else 0)
    return dict(
        run_id=os.path.basename(os.path.normpath(run_dir)),
        task=s.get("task", first.get("task")),
        seed=s.get("seed", seeds[0] if len(seeds) == 1 else None),
        model=s.get("model"),
        strategy=s.get("strategy"),
        status=status,
        solved_at=s.get("solved_at"),
        n_attempts=n_att,
        created_at=_created_at(run_dir, summary),
        kind=(summary or {}).get("kind") or ("agent" if summary is not None else "policy"),
        n_success=len(ok),
        seeds=seeds,
        best=best,
        summary=summary,
        results=results,
        done=done,
        live=live,
    )


# ---------------------------------------------------------------- HTML

CSS = """
:root{color-scheme:light dark;--bg:#f6f7f9;--card:#fff;--fg:#1b1f24;--mut:#667085;--bd:#e3e6ea;
--ok:#12805c;--bad:#c4320a;--run:#b07500;--code:#f0f2f5}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#181b21;--fg:#e6e8eb;--mut:#98a2b3;
--bd:#2a2f38;--ok:#3ccb8f;--bad:#ff7a59;--run:#f5b93a;--code:#11141a}}
*{box-sizing:border-box}body{margin:0;padding:16px;background:var(--bg);color:var(--fg);
font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
h1{font-size:20px;margin:0 0 4px}a{color:inherit}.mut{color:var(--mut)}
header{margin-bottom:16px}.meta span{margin-right:14px;white-space:nowrap}
.grid{display:grid;gap:14px;grid-template-columns:repeat(auto-fill,minmax(300px,1fr))}
.card{background:var(--card);border:1px solid var(--bd);border-radius:10px;overflow:hidden;min-width:0}
.card.win{border-color:var(--ok)}.card>.body{padding:10px 12px}
.media{background:#000;aspect-ratio:4/3;display:flex;align-items:center;justify-content:center}
.media video,.media img{width:100%;height:100%;object-fit:contain;display:block}
.strip{display:grid;grid-template-columns:repeat(4,1fr);gap:2px;background:#000}
.strip img{width:100%;display:block}.none{color:#888;font-size:12px}
.top{display:flex;justify-content:space-between;align-items:center;gap:8px;margin-bottom:6px}
.badge{font-size:12px;font-weight:600;padding:2px 8px;border-radius:99px;color:#fff;white-space:nowrap}
.b-ok,.b-solved{background:var(--ok)}.b-fail,.b-failed,.b-error{background:var(--bad)}
.b-running{background:var(--run)}
.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:4px 8px;font-size:13px}
.stats b{display:block;font-size:11px;font-weight:500;color:var(--mut)}
pre{background:var(--code);border:1px solid var(--bd);border-radius:6px;padding:8px;overflow:auto;
font-size:12px;max-height:360px;white-space:pre-wrap;word-break:break-word}
pre.err{border-color:var(--bad)}details{margin-top:6px}summary{cursor:pointer;color:var(--mut)}
"""


def _page(title: str, body: str, refresh: bool = False) -> str:
    meta = f'<meta http-equiv="refresh" content="{REFRESH_S}">\n' if refresh else ""
    return (f'<!doctype html>\n<html lang="en"><head><meta charset="utf-8">\n'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">\n{meta}'
            f"<title>{_e(title)}</title><style>{CSS}</style></head>\n<body>\n{body}\n</body></html>\n")


def _badge(text: str, cls: str) -> str:
    return f'<span class="badge b-{_e(cls)}">{_e(text)}</span>'


def _media_html(name: str, m: dict, live: bool = False) -> str:
    if live and m["live"]:
        src = _url(name, m["live"]) + f"?t={int(time.time() * 1000)}"
        return (f'<div class="media"><img class="live" data-src="{_e(_url(name, m["live"]))}" '
                f'src="{_e(src)}" alt="live frame"></div>')
    if m["video"]:
        poster = f' poster="{_e(_url(name, m["poster"]))}"' if m["poster"] else ""
        return (f'<div class="media"><video controls muted loop playsinline preload="metadata"{poster} '
                f'src="{_e(_url(name, m["video"]))}"></video></div>')
    if m["gif"]:
        return f'<div class="media"><img src="{_e(_url(name, m["gif"]))}" alt="attempt"></div>'
    if len(m["keyframes"]) > 1:
        imgs = "".join(f'<img src="{_e(_url(name, k))}" alt="{_e(k)}" loading="lazy">' for k in m["keyframes"])
        top = (f'<div class="media"><img src="{_e(_url(name, m["poster"]))}" alt="poster"></div>'
               if m["poster"] else "")
        return f'{top}<div class="strip">{imgs}</div>'
    still = _still(m)
    if still:
        return f'<div class="media"><img src="{_e(_url(name, still))}" alt="frame"></div>'
    return '<div class="media"><span class="none">no media</span></div>'


def _fmt(v, unit: str = "") -> str:
    if v is None:
        return "-"
    if isinstance(v, bool):
        return "yes" if v else "no"
    return f"{v}{unit}"


def _card(run_dir: str, name: str, result: dict | None, code_fallback: str | None, live: bool = False) -> str:
    adir = os.path.join(run_dir, name)
    m = attempt_media(adir, result)
    r = result or {}
    if live:
        badge = _badge("running", "running")
    elif result is None:
        badge = _badge("unreadable", "fail")
    else:
        badge = _badge("success", "ok") if r.get("success") else _badge("fail", "fail")
    parts = [f'<div class="card{" win" if r.get("success") else ""}" id="{_e(name)}">',
             _media_html(name, m, live=live), '<div class="body">',
             f'<div class="top"><strong>{_e(name)}</strong>{badge}</div>']
    if not live:
        stats = [("time", _fmt(r.get("time_s"), " s")), ("collisions", _fmt(r.get("collisions"))),
                 ("energy", _fmt(r.get("energy_j"), " J")), ("lifted", _fmt(r.get("lifted"))),
                 ("dropped", _fmt(r.get("dropped"))), ("seed", _fmt(r.get("seed")))]
        parts.append('<div class="stats">' + "".join(f"<div><b>{k}</b>{_e(v)}</div>" for k, v in stats) + "</div>")
    if r.get("error"):
        parts.append(f'<pre class="err">{_e(r["error"])}</pre>')
    code = _read_text(os.path.join(adir, "policy.py")) or code_fallback
    if code:
        parts.append(f"<details><summary>policy code</summary><pre>{_e(code)}</pre></details>")
    resp = _read_text(os.path.join(adir, "response.md"))
    if resp:
        parts.append(f"<details><summary>model response</summary><pre>{_e(resp)}</pre></details>")
    parts.append("</div></div>")
    return "\n".join(parts)


LIVE_JS = """<script>for(const i of document.querySelectorAll('img.live'))
i.src=i.dataset.src+'?t='+Date.now();</script>"""


def write_index(run_dir: str) -> str:
    """Write <run_dir>/index.html: one card per attempt (and per in-progress attempt). Returns the path."""
    info = run_info(run_dir)
    run_id = os.path.basename(os.path.normpath(run_dir))
    done, live = attempt_dirs(run_dir)
    s = (info or {}).get("summary") or {}
    results = (info or {}).get("results") or {}
    codes = {}
    for i, a in enumerate(s.get("attempts") or [], 1):
        if isinstance(a, dict) and a.get("code"):
            codes[f"attempt_{a.get('attempt', a.get('k', i))}"] = a["code"]
    running = s.get("status") == "running" or bool(live)
    status = (info or {}).get("status") or ("running" if running else "unknown")
    if running:
        status = "running"
    meta = [("task", (info or {}).get("task")), ("seed", (info or {}).get("seed")),
            ("model", s.get("model")), ("strategy", s.get("strategy")), ("solved at", s.get("solved_at"))]
    if info and info["kind"] == "policy":
        meta = [("task", info["task"]), ("seeds", ", ".join(map(str, info["seeds"])) or None),
                ("success", f'{info["n_success"]}/{len(done)}')]
    meta_html = "".join(f'<span><span class="mut">{_e(k)}:</span> {_e(v)}</span>' for k, v in meta if v not in (None, ""))
    cards = [_card(run_dir, n, results.get(n), codes.get(n)) for n in done]
    cards += [_card(run_dir, n, None, codes.get(n), live=True) for n in live]
    body = (f'<header><div class="top"><h1>{_e(run_id)}</h1>{_badge(status, status)}</div>'
            f'<div class="meta">{meta_html}</div>'
            f'<div class="mut"><a href="../index.html">all runs</a>'
            + (' · <a href="trace.html">audit trace</a> (exact model inputs/outputs + provenance)'
               if os.path.isfile(os.path.join(run_dir, "trace.html")) else "")
            + '</div></header>\n'
            f'<div class="grid">\n' + ("\n".join(cards) or '<p class="mut">no attempts yet</p>') + "\n</div>")
    if running:
        body += "\n" + LIVE_JS
    return _atomic_write(os.path.join(run_dir, "index.html"), _page(f"Robot Race {run_id}", body, refresh=running))


# ---------------------------------------------------------------- manifest + home

def list_runs(runs_root: str = "runs") -> list[dict]:
    out = []
    for n in _listdir(runs_root):
        d = os.path.join(runs_root, n)
        if n.startswith(".") or not os.path.isdir(d):
            continue
        try:
            info = run_info(d)
        except Exception:  # never let one odd run dir break the manifest
            info = None
        if info:
            out.append(info)
    out.sort(key=lambda i: (i["created_at"] or "", i["run_id"]), reverse=True)
    return out


def _manifest_entry(runs_root: str, info: dict) -> dict:
    poster = video = None
    if info["best"]:
        adir = os.path.join(runs_root, info["run_id"], info["best"])
        m = attempt_media(adir, info["results"].get(info["best"]))
        still, vid = _still(m), m["video"]
        poster = f'{info["run_id"]}/{info["best"]}/{still}' if still else None
        video = f'{info["run_id"]}/{info["best"]}/{vid}' if vid else None
    keys = ["run_id", "task", "seed", "model", "strategy", "status", "solved_at", "n_attempts", "created_at",
            "kind", "n_success", "seeds", "best"]
    e = {k: info[k] for k in keys}
    e["best_attempt"] = e.pop("best")
    e.update(poster=poster, video=video)
    return e


def build_manifest(runs_root: str = "runs") -> list[dict]:
    return [_manifest_entry(runs_root, i) for i in list_runs(runs_root)]


def update_manifest(runs_root: str = "runs") -> str:
    """Write runs/index.json (newest first) and runs/index.html. Returns the index.json path."""
    os.makedirs(runs_root, exist_ok=True)
    manifest = build_manifest(runs_root)
    path = _atomic_write(os.path.join(runs_root, "index.json"), json.dumps(manifest, indent=1))
    write_runs_home(runs_root, manifest)
    return path


def write_runs_home(runs_root: str = "runs", manifest: list[dict] | None = None) -> str:
    """Write runs/index.html: one card per run linking to <run_id>/index.html."""
    manifest = build_manifest(runs_root) if manifest is None else manifest
    cards = []
    for e in manifest:
        img = (f'<img src="{_e(quote(e["poster"]))}" alt="" loading="lazy">' if e["poster"]
               else '<span class="none">no media</span>')
        sub = [e["task"], f'seed {e["seed"]}' if e["seed"] is not None else None, e["model"],
               f'{e["n_success"]}/{e["n_attempts"]} ok' if e["kind"] == "policy" else f'{e["n_attempts"]} attempts',
               f'solved at {e["solved_at"]}' if e["solved_at"] is not None else None]
        cards.append(
            f'<a class="card" href="{_e(_url(e["run_id"], "index.html"))}" style="text-decoration:none">'
            f'<div class="media">{img}</div><div class="body">'
            f'<div class="top"><strong>{_e(e["run_id"])}</strong>{_badge(e["status"], e["status"])}</div>'
            f'<div class="mut">{_e(" · ".join(str(x) for x in sub if x not in (None, "")))}</div>'
            + (f'<div class="mut">{_e(e["strategy"])}</div>' if e["strategy"] else "")
            + "</div></a>")
    running = any(e["status"] == "running" for e in manifest)
    body = ('<header><h1>Robot Race runs</h1></header>\n<div class="grid">\n'
            + ("\n".join(cards) or '<p class="mut">no runs yet</p>') + "\n</div>")
    return _atomic_write(os.path.join(runs_root, "index.html"), _page("Robot Race runs", body, refresh=running))


# ---------------------------------------------------------------- CLI

def _is_run_dir(d: str) -> bool:
    if os.path.exists(os.path.join(d, "summary.json")):
        return True
    done, live = attempt_dirs(d)
    return bool(done or live)


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    target = argv[0] if argv else "runs"
    if not os.path.isdir(target):
        sys.exit(f"not a directory: {target}")
    if _is_run_dir(target):
        print(write_index(target))
        print(update_manifest(os.path.dirname(os.path.abspath(target))))
        return
    for n in sorted(_listdir(target)):
        d = os.path.join(target, n)
        if not n.startswith(".") and os.path.isdir(d) and _is_run_dir(d):
            try:
                print(write_index(d))
            except OSError as ex:
                print(f"skip {d}: {ex}", file=sys.stderr)
    print(update_manifest(target))


if __name__ == "__main__":
    main()
