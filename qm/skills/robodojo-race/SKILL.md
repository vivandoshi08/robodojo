---
name: robodojo-race
description: Race several robot-arm strategies against each other on a RoboDojo trash-to-bin task. The root agent spawns one QM swarm worker per strategy; each worker writes run(robot) code in its own sandbox, runs it in MuJoCo, reads the score and key frames, and retries. Use when asked to "race", "spar", or find the best strategy for a RoboDojo task.
scope: org
---

# robodojo-race

RoboDojo is baked into every sandbox at `/opt/robodojo` (MuJoCo, Franka Panda, robot API).
Tasks: `can_to_bin`, `bottle_to_bin`, `paper_to_bin`, `box_to_bin`, `can_to_far_bin`.
Strategy cards: `/opt/robodojo/qm/strategies.json`.

## If you are the root (the human asked for a race)

1. Pick the task and seed from the request (defaults: `can_to_bin`, seed 0). Read the strategy cards.
2. Spawn one worker per strategy with `POST /v1/swarm` (see `/v1/apis`):
   `{"action":"spawn","requestId":"race-<task>-s<seed>","contexts":[{"role":"racer","task":"<task>","seed":<seed>,"strategy":<card object>}, ...],
     "text":"You are a RoboDojo racer. Follow the robodojo-race skill, 'If you are a racer' section."}`
3. Tail messages (`GET /v1/swarm?read=1&after=<seq>&waitMs=10000`) until every worker has sent a
   `race_result` or failed. Do not solve the task yourself.
4. Rank: success first, then fewer attempts, then lower `time_s`, `collisions`, `energy_j`.
   Reply with a standings table, the winning strategy, and the winner's final `run(robot)` code.
   Say why the losers lost, using their `error` / `item_final_pos` / `dropped` fields.

## If you are a racer (your swarm context has role "racer")

Your context gives `task`, `seed`, and `strategy`. Stay within your strategy card, even if another
approach looks easier: the race compares strategies.

1. Read the robot API: `cd /opt/robodojo && uv run --no-sync python -c "from robot_race.interfaces import API_DOC; print(API_DOC)"`.
   Read the task text and initial state:
   `uv run --no-sync python -c "from robot_race.tasks import TASKS, Env; from robot_race.robot import SimRobot; import json; e=Env('<task>', <seed>, record=False); print(TASKS['<task>']['text']); print(json.dumps(SimRobot(e).get_state(), default=str))"`
2. Up to 5 attempts, k = 1..5:
   - Write `/root/race/attempt_<k>.py` defining `run(robot)`. Only `robot`, `np`, `math` are available;
     no other imports, no file or network I/O.
   - Run it: `cd /opt/robodojo && uv run --no-sync python run_policy.py /root/race/attempt_<k>.py --task <task> --seeds <seed> --fast --runs-dir /root/runs --run-id <strategy_id>-a<k>`
   - Read `/root/runs/<strategy_id>-a<k>/seed_<seed>/result.json` and look at the key frames
     `key_0.png .. key_3.png` next to it. Stop on `success: true`; otherwise revise.
3. Send one message to the root (`POST /v1/swarm`, `action: "send"`, audience = the root's id):
   text = a JSON object
   `{"type":"race_result","strategy":"<id>","success":bool,"attempts":k,"time_s":..,"collisions":..,"energy_j":..,"error":..,"item_final_pos":..,"lesson":"<one sentence on what worked or why it failed>","code":"<final run(robot) source>"}`
