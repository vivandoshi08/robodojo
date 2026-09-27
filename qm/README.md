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
| "race multiple agents, each locked to a strategy" | root session calls `POST /v1/swarm` `spawn` with one context per strategy card (`qm/strategies.json`) |
| "winner picked automatically" | workers send a `race_result` swarm message; root ranks (success, attempts, time, collisions, energy) |
| race is started by a human, in Slack or the web UI | the `robodojo-race` skill (`qm/skills/robodojo-race/SKILL.md`), imported as a skill pack |
| GBrain / Memorable carry knowledge across races | QM memory providers (`MEMORY_PROVIDER_CONFIG`, MCP) can route org-scope memory to them (phase 2) |

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

5. Import the skill: Admin UI → Skill packs → Register (repo is public, no credential needed):
   - URL `https://github.com/vivandoshi08/robodojo`
   - Ref: a pinned commit on the branch that has `qm/skills/` (`git rev-parse HEAD`)
   - Config `{"skillGlobs": ["qm/skills/*"]}` (keeps out `gbrain/skills/arc-toss`, which isn't a QM skill)
   - Browse → select `robodojo-race` → Import. Re-import with a new ref after editing the skill.
6. In the web UI: "Race all strategies on can_to_bin seed 0". Afterwards `bash qm/collect_runs.sh`.

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
```
