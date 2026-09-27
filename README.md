# Robo Dojo

Robo Dojo races LLM agents that write Python to control a simulated robot arm. The task is a physical one: put the trash in the bin.

Give it a task. A planner picks four distinct strategies from what past races taught it, and the racers run in parallel in MuJoCo. Each racer writes `run(robot)`, watches the result, and retries. A tracker scores every attempt, and the winning technique goes into memory (GBrain + Memorable), so the next race starts smarter.

```
task ─▶ recall ─▶ plan ─▶ race ─▶ score ─▶ remember ─┐
          ▲                                           │
          └───────────────────────────────────────────┘
```

## The pipeline

| Step | What happens | Where |
|---|---|---|
| 1. Start | Pick task, racers (default 4), seeds, tries, and where they run: **Local sim** or **QM sandboxes**. Optionally write your own strategies ("sweep the can off the table into the bin"). | `ui/index.html` → `POST /api/race` |
| 2. Recall | GBrain brief (the distilled `SKILL.md`) + `gbrain think` (cited synthesis of past races) + Memorable episodes and procedures for this task. | `robot_race/memory_hooks.py` → `races/<id>/context.md`, `memory.json` |
| 3. Plan | Claude names the axes that matter (localization, grasp, release, speed…) and picks one distinct combination per racer: **explore** new ones, **exploit** past winners, or **custom** (yours, verbatim). | `racetrack/planner.py` → `races/<id>/plan.json` |
| 4. Race | Each racer is a Claude agent loop: see the scene → write `run(robot)` → run it in MuJoCo → read score + key frames → revise (up to N tries). Same task and seed for every racer. | `run_race.py`, `run_agent.py`, `robot_race/agent.py`, `robot_race/executor.py` |
| 5. Score | Every attempt posts to the tracker. Score is in (0, 100]: solving dominates, and fewer tries, less time, fewer collisions and less energy rank higher. | `racetrack/server.py`, `racetrack/scoring.py` |
| 6. Remember | On close: winner + lessons (`close.json`), `gbrain distill` updates `SKILL.md`, a `races/<id>` page is written to GBrain, each attempt is recorded as a Memorable episode, and the winner's video is rendered. | `memory_hooks.post_race` |
| 7. Watch | Live mats per strategy, scorecard, training journal, the scroll (`SKILL.md`). Click a robot for the full run: video, metrics, code, model reasoning, call timeline synced to the video, audit trace. | `robot_race/serve.py` + `ui/` |

### Memory in action (`can_to_bin`, seed 0)

| Race | Memory | Winner | Solved at |
|---|---|---|---|
| race-001 | cold | Top-cam rim drop | try 3 |
| race-002 | warm (cites race-001) | Fused-vision mid grasp | try 2 |
| race-006 | warm, 4 strategies | Top-only low release (+1 more) | **try 1** |

## Quick start

```bash
uv sync
bash scripts/fetch_assets.sh                      # Franka Panda from MuJoCo Menagerie
echo "ANTHROPIC_API_KEY=sk-ant-..." >> .env
echo "MEMORABLE_API_KEY=mk_..."     >> .env         # optional: Memorable procedures

# memory tools
bun install -g github:garrytan/gbrain && gbrain init --pglite --no-embedding
npm i -g memorable-cli && memorable login && memorable enable

# services
(cd racetrack && uv run uvicorn server:app --host 0.0.0.0 --port 8000)   # tracker
uv run python -m robot_race.serve --port 8080                             # UI + API
```

Then open http://127.0.0.1:8080/. `?nogate` skips the intro, `?race=<id>` opens a race, and `?demo=1` runs the offline simulation.

### CLI equivalents

```bash
uv run python run_race.py --task can_to_bin --agents 4 --seeds 0 --tries 3 --fast --label demo
uv run python run_race.py ... --custom "Sweep::push the can off the table edge into the bin"
uv run python -m racetrack.planner preview --task can_to_bin --agents 4 --out plan.json
uv run python run_race.py --plan plan.json ...                  # race a reviewed plan
uv run python run_agent.py --task can_to_bin --seed 0           # one agent, no race
uv run python run_policy.py policies/reference_pick_and_drop.py --task can_to_bin --seeds 0-9
uv run python -m robot_race.replay runs/<id>/attempt_<k> --width 1920 --height 1080 --fps 60 --shadows
```

## Components

- **Simulator** (`robot_race/`): the MuJoCo scene (Franka Panda, table, bin, trash item, cameras), `SimRobot` with IK, and an executor that runs each attempt in a subprocess. It writes `result.json`, trajectory, key frames, video, `calls.json` and `provenance.json`. Tasks: `can_to_bin`, `bottle_to_bin`, `box_to_bin`, `paper_to_bin`, `can_to_far_bin`.
- **Agent loop** (`robot_race/agent.py`): Claude writes `run(robot)` against `interfaces.API_DOC`, then retries with result JSON and key frames. Every model request and response is saved verbatim (`transcript/`, `trace.html`).
- **Racetrack** (`racetrack/`): the strategy planner, plus a tracker (FastAPI + SQLite) for races, agents, attempts, leaderboard, lessons and media. See [racetrack/README.md](racetrack/README.md).
- **GBrain** (`gbrain/`): `distill` turns finished runs into `skills/trash-to-bin/SKILL.md` and GBrain pages. `brief` and `think` feed the next race. `skillopt` improves the skill. See [gbrain/README.md](gbrain/README.md).
- **Memorable** (`memorable_layer/`): per-attempt episode memory (outcome, failure reason, metrics, code hash), recalled before each race, with winning procedures stored via the Memorable CLI. See [memorable_layer/README.md](memorable_layer/README.md).
- **QM** (`qm/`): runs racers as a QM swarm, one isolated Docker sandbox per strategy. `qm/launch.py` starts a race from the web UI over QM's signed API. Plan, memory context and results go through the tracker, so QM racers share the same memory. See [qm/README.md](qm/README.md).
- **UI** (`ui/index.html`, served by `robot_race/serve.py`): the dojo. Endpoints: `POST /api/race`, `POST /api/plan`, `GET /api/races[/<id>]`, `/api/runs/<id>[/events|/transcript]`, `POST /api/runs/<id>/attempt_<k>/render`, `/api/brain`, `/api/memory`.

## Integrity

Everything that reached the model beyond the environment is recorded as `hints` in `summary.json` (memory context, strategy text, reference example) and shown as a banner. The executor refuses to run code whose hash doesn't match the model's reply. See [docs/INTEGRITY.md](docs/INTEGRITY.md) and `CLAUDE.md` for the file contract under `runs/`.

## Tests

```bash
uv run pytest -q tests racetrack memorable_layer/tests
```
