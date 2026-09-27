# Robo Dojo

Race robot strategies against each other, keep what wins.

Four agents spar on the same trash-into-bin task, each locked to a style
(place, drop, toss, push). A judge agent scores every match, Memorable
records every attempt and why it missed, and GBrain distills the winner
into a readable SKILL.md.

## Run it (live)

The page is served by `robot_race/serve.py`, which also exposes the race API:

```bash
cd racetrack && uv run uvicorn server:app --port 8000 &   # tracker (optional: scorecard, failure modes, history)
uv run python -m robot_race.serve --port 8080              # dojo + API over runs/ and races/
# open http://127.0.0.1:8080/        (?nogate skips the intro, ?demo=1 = offline simulation)
```

`--no-launch` makes it a read-only viewer (Start is refused). `RACETRACK_URL` points at the tracker.

## Inputs and outputs

| UI element | Source |
|---|---|
| Start race (task, agents, seeds, tries, label, Local sim / QM sandboxes) | `POST /api/race` -> `run_race.py --fast` (local) or `qm.launch.launch_qm_race` (QM) |
| Launch progress / QM instructions | `GET /api/launch/<id>` (log tail, race_id once planned, QM `message` to send to the root) |
| Race picker, Race history tab | `GET /api/races` (tracker `/races` summaries + local `races/<id>/`) |
| Mats (one per planned racer: name, approach, explore/exploit) | `races/<id>/plan.json` via `GET /api/races/<id>` |
| Mat screen | `runs/<run_id>/attempt_<k>/live.jpg` while running, then `attempt.mp4`, else key frames, else poster |
| Mat caption | `GET /api/runs/<run_id>/events?since=` (model request, code written, attempt result) |
| Mat tag (QM) | `GET /api/races/<id>/qm` -> `qm.launch.qm_status` workers |
| Scorecard, winner crown | tracker `/races/<id>/leaderboard` (fallback: local ranking from `summary.json`) |
| Verdict + lessons | `races/<id>/close.json` (tracker close: winner, lessons) |
| Training journal | every attempt in `summary.json` + tracker `failure_mode`; QM attempts from tracker only |
| "Recalled before the race" | `races/<id>/context.md`, `races/<id>/memory.json` (GBrain brief + Memorable) |
| The scroll | `GET /api/brain` -> `gbrain/skills/trash-to-bin/SKILL.md` |
| Memorable episodes | `GET /api/memory?task=` (local EpisodeStore) |

Runs join a race through `summary.json["race"] = {race_id, agent_id, strategy_name}` (written by
`run_agent.py --plan`), so the live view needs no race.json until the race ends.

## Demo mode (`?demo=1`)

The original simulation, kept for offline pitching: results come from `judge()`, a fake probability model.

## Where to edit (demo mode)

- Colors and type: CSS variables at the top of `<style>` (`--riso`, `--bp`, `--paper`).
- Strategies: `STRATS` array.
- Failure reasons and scroll lessons: `REASONS`.
- Success odds per style and item: `BASE`.
- Scoring formula: `score()`.
- Scroll distillation: `distill()`.
