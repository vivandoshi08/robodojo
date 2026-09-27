# Robot Race: Phase 1 (read this first, Claude Code)

We race LLM agents that write Python to control a simulated robot arm doing a physical task
(put trash into a trash bin). Phase 1 goal: **one command runs a Claude agent that sees the scene,
writes `run(robot)` against our robot API, runs it in MuJoCo, reads back the score + key frames,
and retries until the can lands in the bin (≤5 tries).** GBrain / QM / Memorable are handled by
teammates; this repo exposes hooks for them (see "Phase 2 hooks").

All code must be written during the hackathon. Use libraries freely, do not fork existing projects.

## 1. Setup workflow (every teammate, ~1 min)

```bash
uv sync                                    # pinned deps from pyproject.toml / uv.lock
bash scripts/fetch_assets.sh               # sparse-clones the Franka Panda from MuJoCo Menagerie (~36 MB, ~5 s)
uv run python scripts/check_reference.py   # expect: can/box/far-bin 10/10, bottle ~7/10, paper ~6/10
echo "ANTHROPIC_API_KEY=sk-ant-..." >> .env
```

- Linux without a display: `export MUJOCO_GL=egl`. Mac/Windows: don't set it.
- Interactive viewer on Mac needs `mjpython` instead of `python`.
- Do NOT add `gymnasium-robotics` or `robosuite`: both crash (AssertionError) on current MuJoCo.

## 2. Repo layout

```
robot_race/
  interfaces.py   DONE   Robot protocol, RESULT_KEYS, API_DOC (single source of truth for the prompt)
  scene.py        DONE   MjSpec: Panda + tcp site + table + bin + one trash item + cameras (front/side/top)
  tasks.py        DONE   TASKS, Env (step, metrics, video frames, success check, result())
  robot.py        DONE   SimRobot: API_DOC implemented with damped-least-squares IK on the 7 arm joints
  fake_robot.py   DONE   FakeRobot stub (same methods, canned state, logs calls)
  executor.py     DONE   run one attempt in a subprocess -> result.json + trajectory + MP4 + key frames
  replay.py       DONE   re-render any attempt from its trajectory (HD, any camera)
  agent.py        DONE   Claude retry loop
  viewer.py       DONE   runs/<id>/index.html gallery + runs/index.json manifest
  serve.py        DONE   dev HTTP server + /api/runs for the website (Range, CORS)
policies/reference_pick_and_drop.py  DONE  scripted top-down grasp -> carry -> release
scripts/fetch_assets.sh              DONE
scripts/check_reference.py           DONE  reference policy x 5 tasks x 10 seeds, no video (~1 min)
run_policy.py     DONE   CLI: run a policy file on N seeds in parallel
run_agent.py      DONE   CLI: run the agent loop
```

## 3. Contracts (don't change without the team)

- **Robot API** = `interfaces.API_DOC`. Agent code gets `robot`, `np`, `math`; must define `run(robot)`.
- **World frame**: meters, +x away from the base, +y left, +z up. Panda base at origin. Table top z=0.20.
  Bin on the floor, center (0.45, 0.35), inner half-size 0.11, rim 0.22 m.
- **Gripper yaw**: at yaw `a` the fingers close along direction `a+90°`. Item `yaw_deg` = its long axis,
  so `rotate_gripper(item["yaw_deg"])` grasps across the narrow side. Yaw wraps to [-90, 90).
- **result.json keys**: `task, seed, success, time_s, collisions, energy_j, dropped, lifted, error,
  frames, video, code_path` (+ `item_final_pos`, `calls`, `wall_s`). Env.result() already returns the
  physics part; the executor adds `error`, media paths, `calls`, `wall_s`.
- **Tasks**: `can_to_bin`, `bottle_to_bin`, `paper_to_bin`, `box_to_bin`, `can_to_far_bin` (bin moved;
  for the memory demo). Seed randomizes item position (and yaw for box/bottle).

## 4. Remaining build steps

Steps A-D can run in parallel (different files). E is last.

**A. executor.py** (parallel)
(As built; supersedes the original plan of in-sim GIF recording.)
- `run_policy(code: str, task, seed, out_dir, timeout_s=180, fast=False, live=True) -> dict`
- Writes `out_dir/policy.py`, spawns `python -m robot_race.executor --task --seed --code --out [--fast] [--no-live]`
  with `subprocess.run(timeout=...)`, reads `out_dir/result.json`. On timeout or crash, writes a
  result with `success=False` and the error/stderr tail. Never wipes `out_dir`.
- Worker: `Env(task, seed, record=False, live_path=...)`, `SimRobot(env)`, exec the code, call `run(robot)`,
  catch `TimeLimit` and all exceptions, `env.settle(1.0)`, `env.result()`, save `trajectory.npz`, then
  render key frames, poster and (unless fast) `attempt.mp4` from the trajectory via `replay.py`. No GIF.

**B. agent.py + run_agent.py** (parallel; build against FakeRobot / a canned result until A lands)
- Model: `ANTHROPIC_MODEL` env var, else `claude-sonnet-5` (pinned so races compare strategies, not models).
- System prompt: short role line + `API_DOC` + output rules (one ```python block defining
  `run(robot)`, only `robot`/`np`/`math`, no imports of other modules, no I/O).
- First user turn: task text (`TASKS[task]["text"]`) + `get_state()` JSON + front and top images
  (render via `Env(...).render()`; base64 PNG image blocks).
- Loop up to `--tries`: call model -> extract last ```python block -> `executor.run_policy` into
  `runs/<run_id>/attempt_<k>/` -> if success stop, else send back result JSON (success, error, metrics,
  item_final_pos, lifted, dropped) + key frames as images + "Revise run(robot)".
- Save `response.md` per attempt and `runs/<run_id>/summary.json` (task, seed, model, strategy,
  per-attempt results + code, solved_at).
- CLI flags: `--task --seed --tries 5 --strategy "<text>" --example --context-file <path> --fast`
  (`--example` adds the reference policy to the prompt: Gate 3 fallback;
  `--fast` = no mp4: key frames, poster and trajectory only).
- Done when: `python run_agent.py --task can_to_bin --seed 0` succeeds within 5 tries.

**C. viewer.py** (parallel)
- `write_index(run_dir)`: scan subdirs with `result.json`; one card each: mp4 video, success badge, time,
  collisions, energy, error, code in `<details>`. Plain HTML + inline CSS. Call it at the end of
  run_policy.py and run_agent.py.

**D. run_policy.py + fake_robot.py** (parallel)
- `python run_policy.py policies/reference_pick_and_drop.py --task can_to_bin --seeds 0-9 --jobs 4 [--fast]`
  -> threads calling `executor.run_policy`, prints a table + success rate, writes the viewer.
- FakeRobot: same methods as SimRobot, returns canned state (copy one real `get_state()`), zeros image.

**E. Integration** (sequential, after A-D)
1. Gate 2: `run_policy.py` on the reference policy, 5 seeds, videos look right. (PASSED: 5/5)
2. Gate 3: `run_agent.py --task can_to_bin` on seeds 0-2. If it fails, tighten the prompt
   (not the API) or use `--example`.
3. Then try `bottle_to_bin`, `box_to_bin` (needs yaw), `paper_to_bin` (hard: bounces).

## 5. Performance facts (measured on a 2-CPU cloud box, no GPU)

- Physics + IK: ~20x real time (a 12 s episode runs in ~0.5 s without video).
- Rendering is the bottleneck on CPU: ~80 ms/frame with shadows off (default), ~290 ms with shadows.
  Env vars: `RR_VIDEO_FPS` (default 12), `RR_SHADOWS=1` for demo-quality video on a laptop GPU.
- For races, use `--fast` (no mp4) and re-render the winner with `python -m robot_race.replay`.

## 6. Tuning knobs if grasps fail

- `scene.ITEMS`: mass, size, friction (items use friction 1.5, condim 4).
- `robot.py`: `CONTROL_DT`, IK damping `lam`, null-space gain 0.05, settle tolerance 4 mm.
- Paper ball bounces out of the bin ~40% of the time with the reference drop height: that's a feature
  (agents should learn to lower it into the bin first).

## 7. Phase 2 hooks (for GBrain / Memorable / QM teammates)

- `--strategy "<text>"`: strategy card appended to the system prompt, used to force diverse racers.
- `--context-file <path>`: recalled memory (skills, past failures) prepended to the first user turn.
- `runs/<run_id>/summary.json`: full trace (prompts' results, code per attempt), ready to send to
  Memorable (`POST /v1/extract`) and to distill into a GBrain skill page for the winner.
- A race = N `run_agent.py` processes with different `--strategy`, same task + seed; rank by
  success, then time, collisions, energy (weights set by the user).

## 8. Web artifact contract (public website UI reads these files; don't change without the team)

Everything the website shows comes from files under `runs/`. Writers write atomically
(write to `*.tmp`, then `os.replace`) so a polling UI never reads half a file.

```
runs/
  index.json                      manifest of all runs, newest first (viewer.update_manifest)
  <run_id>/                       run_id = "<YYYYmmdd-HHMMSS>-<task>-s<seed>[-<slug>]"
    summary.json                  task, seed, model, strategy, status, solved_at, attempts[...]
    events.jsonl                  append-only live feed, one JSON object per line
    index.html                    local gallery (viewer.write_index)
    attempt_<k>/                  k starts at 1
      policy.py                   code that ran
      response.md                 raw model reply (agent runs only)
      result.json                 RESULT_KEYS + item_final_pos, calls, wall_s, media{...}
      trajectory.npz              t [T], qpos [T, nq] at 50 Hz: re-render any camera/res later
      live.jpg                    latest frame while the attempt runs (~2 Hz, overwritten)
      poster.jpg                  final frame, 640x480
      key_0..3.png                0, 1/3, 2/3, end (320x240): these go back to the model
      attempt.mp4                 H.264, yuv420p, +faststart, 640x480 @ 30 fps (browser-playable)
```

- `result.json["media"]` = relative paths: `{"video", "poster", "keyframes": [...], "trajectory", "live"}`
  (missing entries = null). `frames`/`video`/`code_path` are also relative to the attempt dir (never absolute: this JSON is public).
- `events.jsonl` types: `run_started`, `attempt_started`, `code_generated`, `attempt_finished`,
  `run_finished`. Every event has `ts` (unix float), `type`, `run_id`, `attempt` (null for run-level).
  `attempt_finished` embeds the result dict; `run_finished` has `status` ("solved"|"failed"|"error"), `solved_at`.
- `summary.json["status"]`: `running` | `solved` | `failed` | `error`.
- `runs/index.json`: `[{run_id, task, seed, model, strategy, status, solved_at, n_attempts, created_at,
  poster, video}]` (poster/video = best attempt, relative to `runs/`).
- HD re-render for the demo: `python -m robot_race.replay runs/<id>/attempt_<k> --width 1920 --height 1080
  --fps 60 --shadows --camera front` -> `attempt_hd.mp4`. On a Mac GPU this renders faster than real time
  (measured: 960x720 + shadows = 81 fps).
- `--fast` (races): trajectory + key frames + poster only, no mp4; re-render the winner later with replay.
