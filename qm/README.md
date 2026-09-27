# QM infra for RoboDojo

[QM](https://github.com/yc-software/qm) ("Quartermaster") is YC's open-source multiplayer agent
harness. A central Node/TypeScript core (Fastify + Postgres) drives an agent loop (Claude Code, Pi,
OpenCode or Codex). Every person, room and **swarm worker** gets its own scope: memory, files,
keychain view, permissions, and a durable **sandbox** (a Docker container locally; Fly/AWS/E2B/Modal
in the cloud). The agent's main tool is `execute`, which runs shell commands in that sandbox.

## How RoboDojo uses it

| Pitch line | QM feature |
|---|---|
| "each racer runs in its own isolated QM sandbox" | swarm workers each get a blank container from our image (`qm/sandbox/Dockerfile`: QM base + MuJoCo + this repo) |
| "race multiple agents, each locked to a strategy" | root session calls `POST /v1/swarm` `spawn` with one context per planned strategy (racetrack planner; fallback `qm/strategies.json`) |
| "winner picked automatically" | workers record every attempt on the racetrack tracker; root closes the race (tracker ranks, stores lessons) |
| race is started by a human, in Slack or the web UI | the `robodojo-race` skill (`qm/skills/robodojo-race/SKILL.md`), imported as a skill pack |
| GBrain / Memorable carry knowledge across races | host-side `robot_race.memory_hooks`, served to sandboxes by the tracker (`GET /races/{id}/context`), refreshed on race close |

```
human (web/Slack) ─▶ QM root session ──spawn──▶ worker "place" ─▶ sandbox: write run(robot) → run_policy.py → result.json + key frames ─┐
                          ▲                 ├──▶ worker "drop"  ─▶ sandbox ...                                                       │
                          │                 ├──▶ worker "toss"  ─▶ sandbox ...                                                       │
                          │                 └──▶ worker "push"  ─▶ sandbox ...                                                       │
                          └──────────────────────────── race_result messages ◀──────────────────────────────────────────────────────┘
bash qm/collect_runs.sh  → copies /root/runs out of the containers into runs/qm/ for the website
```

The racer *is* the QM agent: it reads `API_DOC`, writes the policy, runs it, looks at key frames and
retries. No API key lives in the sandbox.

## Setup (local, Mac)

Prereqs: Docker Desktop running, Node >= 24.15, an Anthropic API key.

```bash
# 1. QM itself (kept outside this repo; we depend on it, we don't fork it)
git clone https://github.com/yc-software/qm ../qm && (cd ../qm && npm ci)
bash qm/build_qm_base.sh                            # qm-sandbox-local:latest, native arch (QM's own script forces amd64 -> MuJoCo SIGILL on Apple Silicon)

# 2. Our sandbox image on top of it
bash qm/build_sandbox.sh                            # robodojo-qm-sandbox:latest, runs a reference-policy smoke test

# 3. Config: copy the template, add your key, point QM's dev instance at it
cp qm/dev.env.example qm/dev.env                    # edit ANTHROPIC_API_KEY

# 4. Start QM (Postgres in Docker + web UI), then open the printed URL
bash qm/start_qm.sh                                 # QM_DEV_ENV=qm/dev.env + npm run dev-instance:web
bash qm/start_qm.sh doctor                          # if anything looks off (also: status, down)
```

5. Start the race tracker on the host (sandboxes reach it at `http://host.docker.internal:8000`;
   QM runs every sandbox with `--add-host=host.docker.internal:host-gateway`):
   `cd racetrack && uv run uvicorn server:app --host 0.0.0.0 --port 8000`
6. Import the skill: Admin UI → Skill packs → Register (repo is public, no credential needed):
   - URL `https://github.com/vivandoshi08/robodojo`
   - Ref: a pinned commit on the branch that has `qm/skills/` (`git rev-parse HEAD`)
   - Config `{"skillGlobs": ["qm/skills/*"]}` (keeps out `gbrain/skills/arc-toss`, which isn't a QM skill)
   - Browse → select `robodojo-race` → Import. Re-import with a new ref after editing the skill.
7. Rebuild the sandbox image after pulling (it bakes `racetrack/` + `qm/plan_to_contexts.py` in):
   `bash qm/build_sandbox.sh`, then restart QM (`bash qm/start_qm.sh down && bash qm/start_qm.sh`) so it picks
   up `LOCAL_SANDBOX_IMAGE` from `qm/dev.env`.

## Running a race (task -> strategies -> live QM racers -> best -> memory)

```bash
# host: plan (LLM planner + lessons of past races), open the race on the tracker, attach recalled memory
uv run python qm/plan_to_contexts.py prepare --task can_to_bin --seed 0 --agents 4 --label qm
#   -> {"race_id": "...", "source": "planner"|"strategies.json", "memory": true|false, ...}
#   (--no-planner = the static cards in qm/strategies.json; --no-memory = cold race)
```

In the QM web UI (the dev instance's web port, `bash qm/start_qm.sh status`; the API port answers 401
to anything but QM's own sources): **"Race <race_id> on can_to_bin seed 0"**. The root reads the planned
strategies + memory back from the tracker (`plan_to_contexts.py contexts`), spawns one swarm worker per
strategy, and each worker records every attempt live (`python -m racetrack.client record`), so the
tracker dashboard (`http://localhost:8000/`) fills in while the race runs. Without a race id the root
prepares one itself (cards, since sandboxes carry no API key).

The root closes the race (`python -m racetrack.client close`): winner + lessons are stored (next race's
planner reads them) and the tracker distills memory in the background (`memory_hooks.post_race`:
GBrain skill page + Memorable). Then on the host:

```bash
bash qm/finish_race.sh <race_id>    # pull sandbox runs into runs/qm/, close if needed, re-distill memory
```

Memory never runs inside a sandbox: GBrain's brain DB and the Memorable CLI live on the host, and
sandboxes only see them through `GET /races/{id}/context` on the tracker.

## Files

```
qm/sandbox/Dockerfile        QM local sandbox + MuJoCo (EGL) + uv env + Panda assets + smoke test
qm/build_qm_base.sh          builds QM's base sandbox image for the host arch
qm/build_sandbox.sh          builds robodojo-qm-sandbox:latest
qm/dev.env.example           dev-instance settings (local sandbox image, Postgres stores, swarms)
qm/start_qm.sh               starts QM with those settings
qm/strategies.json           strategy cards, one swarm worker each
qm/skills/robodojo-race/     the skill root + racers follow
qm/collect_runs.sh           pull race artifacts out of sandbox containers into runs/qm/
qm/plan_to_contexts.py       prepare (plan + tracker race + memory) / contexts (swarm spawn contexts)
qm/finish_race.sh            host: collect runs, close the race, distill memory
```
