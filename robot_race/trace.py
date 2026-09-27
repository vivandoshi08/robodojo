"""Audit trace for one agent run: runs/<run_id>/trace.html (self-contained, relative paths only).

Per model turn, from the files written at the time (never re-rendered): the exact images + text the model
received (transcript/turn_<n>/request.json + img_<i>.png, sha256 re-checked here), its raw reply
(response.json), then the attempt that ran: provenance (sha256 of policy.py vs. the code block in the
reply), attempt.mp4 next to the sim-timed robot calls (calls.json; click a call to seek: video time ==
sim time), the result, and the feedback that went back (= the next turn's newest user message).

write_trace(run_dir) -> path;  python -m robot_race.trace runs/<run_id>
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys

from .viewer import _atomic_write, _e, _load_json, _read_text, _url

CSS = """
:root{color-scheme:light dark;--bg:#f6f7f9;--card:#fff;--fg:#1b1f24;--mut:#667085;--bd:#e3e6ea;
--ok:#12805c;--bad:#c4320a;--run:#b07500;--code:#f0f2f5;--hl:#fff4cc;--acc:#2f6fde}
@media (prefers-color-scheme:dark){:root{--bg:#0f1115;--card:#181b21;--fg:#e6e8eb;--mut:#98a2b3;
--bd:#2a2f38;--ok:#3ccb8f;--bad:#ff7a59;--run:#f5b93a;--code:#11141a;--hl:#3a3212;--acc:#7aa7ff}}
*{box-sizing:border-box}body{margin:0 auto;max-width:1180px;padding:16px;background:var(--bg);color:var(--fg);
font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
h1{font-size:20px;margin:0 0 4px}h2{font-size:16px;margin:0 0 8px}h3{font-size:14px;margin:12px 0 6px}
a{color:var(--acc)}.mut{color:var(--mut)}.meta span{margin-right:14px;white-space:nowrap}
.tl{border-left:3px solid var(--bd);margin-left:8px;padding-left:18px}
.step{position:relative;background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:12px 14px;
margin:0 0 14px;min-width:0}
.step:before{content:"";position:absolute;left:-27px;top:16px;width:12px;height:12px;border-radius:50%;
background:var(--dot,var(--mut));border:2px solid var(--bg)}
.s-in{--dot:var(--acc)}.s-out{--dot:var(--run)}.s-ex{--dot:var(--ok)}.s-ex.fail{--dot:var(--bad)}
.badge{display:inline-block;font-size:12px;font-weight:600;padding:2px 8px;border-radius:99px;color:#fff;
white-space:nowrap}.ok{background:var(--ok)}.bad{background:var(--bad)}.run{background:var(--run)}
.pill{display:inline-block;font:12px ui-monospace,Menlo,monospace;padding:1px 6px;border:1px solid var(--bd);
border-radius:6px;color:var(--mut)}
pre{background:var(--code);border:1px solid var(--bd);border-radius:6px;padding:8px;overflow:auto;font-size:12px;
white-space:pre-wrap;word-break:break-word;margin:6px 0;max-height:520px}
pre.code{font-family:ui-monospace,Menlo,monospace}pre.ran{border-color:var(--ok);border-width:2px}
pre.err{border-color:var(--bad)}details{margin:6px 0}summary{cursor:pointer;color:var(--mut)}
.imgs{display:flex;flex-wrap:wrap;gap:8px;margin:6px 0}.imgs figure{margin:0;width:200px}
.imgs img{width:100%;display:block;border:1px solid var(--bd);border-radius:4px;background:#000;cursor:zoom-in}
figcaption{font-size:11px;color:var(--mut);word-break:break-all}
.ex{display:grid;grid-template-columns:minmax(0,3fr) minmax(0,2fr);gap:12px}
@media (max-width:820px){.ex{grid-template-columns:1fr}}
video,.ex .still{width:100%;background:#000;border-radius:6px;display:block}
.calls{max-height:420px;overflow:auto;border:1px solid var(--bd);border-radius:6px}
.call{display:grid;grid-template-columns:92px 1fr;gap:6px;width:100%;text-align:left;border:0;
border-bottom:1px solid var(--bd);background:none;color:inherit;padding:4px 8px;font:12px ui-monospace,Menlo,monospace;
cursor:pointer}.call:hover{background:var(--code)}.call.on{background:var(--hl)}.call .t{color:var(--mut)}
.call.err{color:var(--bad)}.call small{grid-column:2;color:var(--mut)}
.stats{display:grid;grid-template-columns:repeat(auto-fill,minmax(110px,1fr));gap:4px 10px;margin:8px 0}
.stats b{display:block;font-size:11px;font-weight:500;color:var(--mut)}
.clock{font:12px ui-monospace,Menlo,monospace;color:var(--mut);margin-top:4px}
dialog{padding:0;border:0;background:transparent;max-width:96vw}dialog img{max-width:96vw;max-height:92vh;display:block}
dialog::backdrop{background:rgba(0,0,0,.8)}
"""

JS = r"""<script>
const lb=document.getElementById('lb');
document.addEventListener('click',ev=>{const a=ev.target.closest('a.zoom');if(!a)return;ev.preventDefault();
 lb.querySelector('img').src=a.getAttribute('href');lb.showModal();});
lb.addEventListener('click',()=>lb.close());
// Video time == sim time: executor frame i shows sim time i/fps (replay.video_times). Seek to the middle of
// frame round(t*fps) so the browser shows exactly that frame.
function seek(v,t){const fps=+v.dataset.fps;const go=()=>{const f=Math.round(t*fps);
 v.pause();v.currentTime=Math.min((f+0.5)/fps,v.duration-0.5/fps);};
 v.readyState>=1?go():v.addEventListener('loadedmetadata',go,{once:true});}
document.querySelectorAll('button.call[data-v]').forEach(b=>b.addEventListener('click',()=>
 seek(document.getElementById(b.dataset.v),+b.dataset.t)));
document.querySelectorAll('video[data-fps]').forEach(v=>{const fps=+v.dataset.fps,end=+v.dataset.end;
 const clock=document.getElementById(v.id+'-clock');
 const calls=[...document.querySelectorAll('button.call[data-v="'+v.id+'"]')];
 const upd=()=>{const t=Math.min(Math.floor(v.currentTime*fps+1e-6)/fps,end);
  if(clock)clock.textContent='sim t = '+t.toFixed(2)+' s  (frame '+Math.floor(v.currentTime*fps+1e-6)+')';
  calls.forEach(c=>c.classList.toggle('on',t>=+c.dataset.t&&t<Math.max(+c.dataset.t1,+c.dataset.t+1/fps)));};
 v.addEventListener('timeupdate',upd);v.addEventListener('seeked',upd);});
</script>"""


def _sha_file(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except OSError:
        return None


def _read_jsonl(path: str) -> list[dict]:
    out = []
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f:
                try:
                    if ln.strip():
                        out.append(json.loads(ln))
                except ValueError:
                    pass  # partial last line while the run is writing
    except OSError:
        pass
    return out


def _short(h: str | None) -> str:
    return (h or "?")[:12]


def _badge(text: str, ok: bool | None) -> str:
    return f'<span class="badge {"ok" if ok else ("run" if ok is None else "bad")}">{_e(text)}</span>'


def _images_html(run_dir: str, rel_dir: str, blocks: list) -> str:
    figs = []
    for b in blocks:
        rel = f"{rel_dir}/{b.get('file')}"
        disk = _sha_file(os.path.join(run_dir, rel))
        ok = disk is not None and disk == b.get("sha256")
        figs.append(f'<figure><a class="zoom" href="{_e(_url(*rel.split("/")))}"><img src="{_e(_url(*rel.split("/")))}" '
                    f'alt="{_e(b.get("file"))}" loading="lazy"></a><figcaption>{_e(b.get("file"))} · sha256 '
                    f'{_e(_short(b.get("sha256")))} {"== file" if ok else "!= FILE"}</figcaption></figure>')
    return f'<div class="imgs">{"".join(figs)}</div>' if figs else ""


def _content_html(run_dir: str, rel_dir: str, content) -> str:
    """A user message's blocks in order: text as <pre>, consecutive images as one thumbnail row."""
    if isinstance(content, str):
        return f"<pre>{_e(content)}</pre>"
    out, imgs = [], []
    for b in content or []:
        if isinstance(b, dict) and b.get("type") == "image":
            imgs.append(b)
            continue
        if imgs:
            out.append(_images_html(run_dir, rel_dir, imgs))
            imgs = []
        if isinstance(b, dict) and b.get("type") == "text":
            out.append(f"<pre>{_e(b.get('text'))}</pre>")
        else:
            out.append(f"<pre>{_e(json.dumps(b))}</pre>")
    if imgs:
        out.append(_images_html(run_dir, rel_dir, imgs))
    return "\n".join(out)


def _reply_html(text: str, code: str | None) -> str:
    """Reply text; fenced blocks as <pre class=code>, the executed (extracted) one outlined."""
    parts = re.split(r"(```[^\n]*\n.*?```)", text or "", flags=re.S)
    fences = [i for i, p in enumerate(parts) if p.startswith("```")]
    ran = None
    if code is not None:
        for i in reversed(fences):
            body = re.sub(r"^```[^\n]*\n", "", parts[i])[:-3]
            if body.strip() + "\n" == code:
                ran = i
                break
    out = []
    for i, p in enumerate(parts):
        if i in fences:
            tag = ' <span class="pill">this block was executed</span>' if i == ran else ""
            out.append(f'<pre class="code{" ran" if i == ran else ""}">{_e(p)}</pre>{tag}')
        elif p.strip():
            out.append(f"<pre>{_e(p.strip())}</pre>")
    return "\n".join(out) or '<p class="mut">(empty reply)</p>'


def _stats(pairs) -> str:
    return '<div class="stats">' + "".join(
        f"<div><b>{_e(k)}</b>{_e('-' if v is None else v)}</div>" for k, v in pairs) + "</div>"


def _provenance_html(adir: str, prov: dict | None, reply_code: str | None) -> str:
    from .agent import sha256_hex
    if not prov:
        return _badge("no provenance.json", False)
    disk = _sha_file(os.path.join(adir, prov.get("policy_file") or "policy.py"))
    reply = sha256_hex(reply_code) if reply_code is not None else None
    checks = [("policy.py on disk == sha256 recorded when it ran", disk == prov.get("policy_sha256")),
              ("code block re-extracted from response.json == sha256 passed to the executor",
               reply is not None and reply == prov.get("response_code_sha256")),
              ("executor: code executed == code in reply", prov.get("code_matches_response") is True)]
    ok = all(c for _, c in checks)
    head = _badge(f"code executed == code in reply (sha256 {_short(prov.get('policy_sha256'))}…)" if ok
                  else "PROVENANCE MISMATCH", ok)
    rows = "".join(f"<li>{'✓' if c else '✗'} {_e(t)}</li>" for t, c in checks)
    keys = ["policy_sha256", "response_code_sha256", "mujoco_version", "python_version", "numpy_version",
            "sim_timestep", "control_dt", "episode_limit_s", "trajectory_samples", "trajectory_t_range",
            "keyframe_times", "wall_s"]
    return (f"{head}<details><summary>provenance</summary><ul>{rows}</ul><pre>"
            + _e(json.dumps({k: prov.get(k) for k in keys if k in prov}, indent=1)) + "</pre></details>")


def _calls_html(calls: dict | None, vid: str | None, has_video: bool) -> str:
    if not calls:
        return '<p class="mut">no calls.json</p>'
    rows = []
    for c in calls.get("calls") or []:
        args = ", ".join(f"{k}={json.dumps(v)}" for k, v in (c.get("args") or {}).items())
        t0, t1 = c.get("t_start"), c.get("t_end")
        attrs = f' data-v="{_e(vid)}" data-t="{_e(t0)}" data-t1="{_e(t1)}"' if has_video else ""
        extra = f"tcp → {c.get('tcp_after')}  width {c.get('gripper_width_after')}  holding {c.get('holding_after')}"
        if c.get("error"):
            extra = f"raised {c['error']}"
        rows.append(f'<button class="call{" err" if c.get("error") else ""}"{attrs} title="seek video to sim t={_e(t0)}">'
                    f'<span class="t">{t0:6.2f}–{t1:.2f}s</span><span>{_e(c.get("call"))}({_e(args)})</span>'
                    f"<small>{_e(extra)}</small></button>" if isinstance(t0, (int, float)) and isinstance(t1, (int, float))
                    else f'<div class="call">{_e(json.dumps(c))}</div>')
    ex = calls.get("exception")
    if ex:
        rows.append(f'<div class="call err"><span class="t">{_e(ex.get("sim_time"))}s</span>'
                    f'<span>policy raised {_e(ex.get("type"))}: {_e(ex.get("message"))}</span></div>')
    return (f'<div class="calls">{"".join(rows) or "<p class=mut>no robot calls</p>"}</div>'
            f'<div class="mut">policy ended at sim t={_e(calls.get("policy_t_end"))} s; +1 s settle; '
            f'episode ends t={_e(calls.get("episode_t_end"))} s. Times are sim seconds (== video time).</div>')


def _execution_html(run_dir: str, k: int, entry: dict | None, reply_code: str | None) -> str:
    name = f"attempt_{k}"
    adir = os.path.join(run_dir, name)
    result = _load_json(os.path.join(adir, "result.json")) or (entry or {}).get("result") or {}
    prov = _load_json(os.path.join(adir, "provenance.json"))
    calls = _load_json(os.path.join(adir, "calls.json"))
    ok = bool(result.get("success"))
    media = result.get("media") or {}
    vid = f"v{k}"
    video = (media.get("video") if isinstance(media.get("video"), str) else None)
    video = video if video and os.path.isfile(os.path.join(adir, video)) else None
    fps = ((prov or {}).get("video") or {}).get("fps") or 30
    end = ((prov or {}).get("trajectory_t_range") or [0, 0])[-1]
    if video:
        poster = media.get("poster")
        pa = f' poster="{_e(_url(name, poster))}"' if poster else ""
        left = (f'<video id="{vid}" controls muted playsinline preload="metadata" data-fps="{_e(fps)}" '
                f'data-end="{_e(end)}"{pa} src="{_e(_url(name, video))}"></video>'
                f'<div class="clock" id="{vid}-clock">sim t = 0.00 s</div>')
    else:
        still = media.get("poster") or next(iter(media.get("keyframes") or []), None)
        left = (f'<img class="still" src="{_e(_url(name, still))}" alt="final frame">' if still
                else '<p class="mut">no media</p>') + '<p class="mut">no attempt.mp4 (fast mode): calls can\'t seek</p>'
    if result.get("code_path") or os.path.isfile(os.path.join(adir, "policy.py")):
        left += f'<p><a href="{_e(_url(name, "policy.py"))}">policy.py</a> (the file that ran)</p>'
    stats = _stats([("success", "yes" if ok else "no"), ("sim time", f"{result.get('time_s')} s"),
                    ("collisions", result.get("collisions")), ("energy", f"{result.get('energy_j')} J"),
                    ("lifted", result.get("lifted")), ("dropped", result.get("dropped")),
                    ("item final pos", result.get("item_final_pos")), ("wall", f"{result.get('wall_s')} s")])
    err = f'<pre class="err">{_e(result["error"])}</pre>' if result.get("error") else ""
    return (f'<div class="step s-ex{"" if ok else " fail"}" id="exec-{k}"><h2>Execution: attempt {k} '
            f'{_badge("success" if ok else "fail", ok)}</h2>'
            f"<div>{_provenance_html(adir, prov, reply_code) if reply_code is not None else ''}</div>"
            f'<div class="ex"><div>{left}</div><div><h3>robot calls (click to seek)</h3>'
            f"{_calls_html(calls, vid, bool(video))}</div></div>{stats}{err}"
            f'<div class="mut"><a href="{_e(_url(name, "result.json"))}">result.json</a> · '
            f'<a href="{_e(_url(name, "calls.json"))}">calls.json</a> · '
            f'<a href="{_e(_url(name, "provenance.json"))}">provenance.json</a></div></div>')


def write_trace(run_dir: str) -> str:
    from .agent import extract_code
    run_dir = os.path.normpath(run_dir)
    run_id = os.path.basename(run_dir)
    s = _load_json(os.path.join(run_dir, "summary.json")) or {}
    turns = _read_jsonl(os.path.join(run_dir, "transcript.jsonl"))
    entries = {a.get("k"): a for a in s.get("attempts") or [] if isinstance(a, dict)}
    status = s.get("status") or "unknown"
    meta = [("task", s.get("task")), ("seed", s.get("seed")), ("model", s.get("model")),
            ("strategy", s.get("strategy")), ("solved at", s.get("solved_at"))]
    parts = [f'<header><h1>{_e(run_id)} {_badge(status, status == "solved" if status != "running" else None)}</h1>'
             '<div class="meta">' + "".join(f'<span><span class="mut">{_e(k)}:</span> {_e(v)}</span>'
                                            for k, v in meta if v not in (None, "")) + "</div>"
             '<p class="mut">Everything below is read from files written at the time: the requests exactly as sent '
             '(images are the decoded bytes of the base64 blocks, sha256 re-checked on load), the raw API '
             'responses, the code that ran (sha256-matched to the reply), sim-timed robot calls and the '
             'resulting video. <a href="index.html">gallery</a> · <a href="summary.json">summary.json</a> · '
             '<a href="transcript.jsonl">transcript.jsonl</a> · <a href="events.jsonl">events.jsonl</a></p></header>']
    first_req = _load_json(os.path.join(run_dir, turns[0]["request"])) if turns else None
    if first_req:
        sys_text = "\n\n".join(b.get("text", "") for b in first_req.get("system") or [] if isinstance(b, dict))
        parts.append(f"<details><summary>system prompt ({len(sys_text)} chars, model {_e(first_req.get('model'))}, "
                     f"max_tokens {_e(first_req.get('max_tokens'))})</summary><pre>{_e(sys_text)}</pre></details>")
    obs = s.get("observation") or {}
    parts.append('<div class="tl">')
    for i, t in enumerate(turns):
        n, k = t.get("turn"), t.get("attempt")
        rel = t.get("dir") or f"transcript/turn_{n}"
        req = _load_json(os.path.join(run_dir, t.get("request") or "")) or {}
        msgs = req.get("messages") or []
        last = msgs[-1]["content"] if msgs and isinstance(msgs[-1], dict) else []
        title = "Initial prompt (observation)" if n == 1 else f"Feedback on attempt {n - 1} → model"
        note = ""
        if n == 1 and obs:
            checks = []
            for view in ("front", "top"):
                osha = _sha_file(os.path.join(run_dir, obs.get(view) or ""))
                checks.append(f"observation/{view}.png sha256 {_short(osha)} "
                              + ("is in this request" if osha and osha in (t.get("image_sha256") or []) else "NOT in request"))
            note = f'<div class="mut">{_e("; ".join(checks))}</div>'
        hist = len(msgs) - 1
        parts.append(f'<div class="step s-in" id="turn-{n}"><h2>Turn {n} · {_e(title)}</h2>'
                     f'<div class="mut">newest user message of the request, block by block · '
                     f'{len(t.get("images") or [])} images in the whole request'
                     + (f" (incl. {hist} earlier messages of history)" if hist > 0 else "")
                     + f' · <a href="{_e(_url(*rel.split("/"), "request.json"))}">request.json</a></div>{note}'
                     + _content_html(run_dir, rel, last) + "</div>")
        resp = _load_json(os.path.join(run_dir, t.get("response") or "")) if t.get("response") else None
        if resp is None:
            parts.append(f'<div class="step s-out"><h2>Model reply (turn {n})</h2>'
                         f'<pre class="err">{_e(t.get("error") or "no response yet")}</pre></div>')
            continue
        text = "".join(b.get("text", "") for b in resp.get("content") or []
                       if isinstance(b, dict) and b.get("type") == "text")
        code = extract_code(text)
        u = t.get("usage") or {}
        parts.append(f'<div class="step s-out" id="reply-{n}"><h2>Model reply (turn {n})</h2>'
                     + _stats([("model", resp.get("model")), ("stop", resp.get("stop_reason")),
                               ("in tokens", u.get("input_tokens")), ("out tokens", u.get("output_tokens")),
                               ("cache read", u.get("cache_read_input_tokens")),
                               ("latency", f"{t.get('latency_s')} s"), ("retries", t.get("retries")),
                               ("response id", resp.get("id"))])
                     + _reply_html(text, code)
                     + f'<div class="mut"><a href="{_e(_url(*rel.split("/"), "response.json"))}">response.json</a>'
                     f' (raw API response)</div></div>')
        if k is not None and os.path.isfile(os.path.join(run_dir, f"attempt_{k}", "result.json")):
            if code is None:
                parts.append(f'<div class="step s-ex fail"><h2>Execution: attempt {k}</h2>'
                             '<p>No ```python block in the reply: nothing ran.</p></div>')
            else:
                parts.append(_execution_html(run_dir, k, entries.get(k), code))
            if i == len(turns) - 1 and status != "running":
                why = "solved: nothing more sent" if status == "solved" else "run ended: no feedback sent"
                parts.append(f'<div class="step"><span class="mut">{_e(why)}</span></div>')
    parts.append("</div>")
    if s.get("error"):
        parts.append(f'<pre class="err">{_e(s["error"])}</pre>')
    refresh = '<meta http-equiv="refresh" content="5">\n' if status == "running" else ""
    page = (f'<!doctype html>\n<html lang="en"><head><meta charset="utf-8">\n'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">\n{refresh}'
            f"<title>Robot Race trace</title><style>{CSS}</style></head>\n<body>\n" + "\n".join(parts)
            + '\n<dialog id="lb"><img alt="enlarged"></dialog>\n' + JS + "\n</body></html>\n")
    return _atomic_write(os.path.join(run_dir, "trace.html"), page)


if __name__ == "__main__":
    print(write_trace(sys.argv[1]))
