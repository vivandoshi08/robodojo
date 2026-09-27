# racetrack: strategy planning, scoring, leaderboard and race-over-race tracking

This folder adds race planning and tracking on top of the Phase 1 loop. It never touches `interfaces.py`, the sim, `check_success()`, the executor or the agent loop. It reads the `result.json` the executor already writes, exactly as the plan freezes it:

```
{"success", "time_s", "collisions", "energy", "dropped", "error", "frames", "video", "code"}
```

It adds the identity that `run_agent.py` already knows: which agent, which seed, which try.

## Merging

1. Drop this `racetrack/` folder into the repo. Nothing else in the repo changes.
2. Only the machine that shows the dashboard needs `pip install fastapi uvicorn pydantic`. `client.py` uses only the standard library, so it adds no dependencies to the sim or agent environment.
3. Start the tracker: `cd racetrack && uvicorn server:app --port 8000`, then open http://localhost:8000.

## Phase 1: hooking in `run_agent.py`

**Option A, one line (recommended).** Add this right after the executor writes `result.json`:

```python
from racetrack.client import Tracker      # or: sys.path.append("racetrack"); from client import Tracker
tracker = Tracker()                       # RACETRACK_URL env var overrides http://localhost:8000

tracker.record_attempt("phase1", "agent-1", seed, attempt, result_json_path)
```

- **Result:** pass the path to `result.json`, or the dict you already have. With the path, the dashboard can also play the GIF and show key frames, since they're resolved next to the file.
- **Race and agent:** `phase1` and `agent-1` are created automatically on the first call.
- **Failures never stop the loop:** if the tracker is down, the attempt goes to `runs_backup.jsonl` and `tracker.replay_backup()` sends it later.
- **Retries are safe:** re-sending the same agent, seed and try replaces the earlier record.

**Option B, zero code changes.** Point the ingester at the folder where attempts are saved:

```bash
python ingest.py path/to/runs --race phase1 --watch
```

It reads the seed and try from the folder names (`seed0/attempt2`, `seed_0/try_2`, and `agent-1/...` if present), or from `seed`/`attempt`/`agent_id` fields inside `result.json`. It also backfills attempts that already ran.

## Phase 2: LLM-planned strategies, then the race

No strategies are hardcoded. Before each race, `planner.py` makes one model call with:

- the task description and success criteria;
- the robot API (the sim's own `interfaces.py`);
- optionally the scene (`get_state()` JSON and a camera image);
- memory from earlier races.

The model names the 2 to 5 decisions that matter most for *that* task and API, and assigns each agent a different combination. For the can task, that might be grasp style, transport speed, and dropping vs lowering. The code checks that no two agents share every choice; if they do, it asks the model to fix the plan once.

A different simulation needs only a new task file and its API. Nothing in the planner is specific to the trash can.

```bash
# needs ANTHROPIC_API_KEY; RACE_PLANNER_MODEL overrides the default claude-opus-5-5
python planner.py --task-file tasks/can_to_bin.json --api ../interfaces.py --agents 4 \
                  --state state.json --image scene.png --race cold --out plan.json
```

- **`--race cold`:** starts a tracker race and registers each agent's strategy, so the dashboard shows it.
- **Memory:** `--memory tracker` (the default) pulls lessons and the winning code from the latest closed race on the same task. `--memory file.json` takes the same shape from Memorable or GBrain instead, and `--memory none` turns it off.
- **Exploit and explore:** when memory includes a proven skill, about half the agents refine it and the rest explore new combinations. `--exploit N` overrides the split.

**The one change the agent loop needs:** add the agent's strategy to its prompt.

```python
from racetrack.planner import load_strategy_prompt
prompt += load_strategy_prompt("plan.json", agent_id)    # the agent's approach, fixed choices, starting params, main risk
```

**The race runner** then runs one agent loop per strategy in the plan and records every try:

```python
plan = json.load(open("plan.json"))
for s in plan["strategies"]:                     # run these in parallel
    ...his loop with load_strategy_prompt(...) for s["agent_id"]...
    tracker.record_attempt(plan["race_id"], s["agent_id"], seed, attempt, result_json_path)
out = tracker.close_race(plan["race_id"])
```

Run the planner again with `--race warm` and it plans from what the cold race learned.

`close_race` returns what the memory layer needs:

- **`out["lessons"]["lines"]`:** plain-text lessons per strategy, including failures, for **Memorable**.
  - Example: *"Gentle top-down: solved 3/3 seeds in 3.0 tries on average (1 on the first try); failed tries: 4 dropped, 2 code error; most common error: NameError: name 'np' is not defined"*
- **`out["lessons"]["skill"]`:** the winning policy's `code` with its agent, seed, try and stats, for the **GBrain** skill library (the Voyager-style design in the plan). Save it only if `out["lessons"]["skill_eligible"]` is true.

## Scoring

A seed ends when it's solved or after `max_attempts` tries (5, per Gate 3). Per agent:

```
score = 100 * solve_rate
      -  10 * mean(tries used - 1)            every retry costs 10
      -   1 * mean time_s of the winning tries
      -  10 * collisions per seed (final try)
      - 0.1 * mean energy of the winning tries
```

- **Defaults:** 3 seeds per agent and 5 tries, matching Gate 3.
- **Changing it:** pass `scoring={...}` to `start_race`. Fields: `seeds_per_agent`, `max_attempts`, `w_success`, `w_extra_attempt`, `w_time_s`, `w_collisions`, `w_energy`, `min_solve_rate_for_skill`.
- **Units:** `time_s` and `energy` units come from the executor. Check the weights once real numbers exist, so solving still dominates.
- **Leader:** the best score right now. The live view uses this.
- **Winner:** picked at close, from agents that finished all their seeds. Ties go to higher solve rate, then fewer tries, then faster time.
- **Skill bar:** a winner with a solve rate below 60% becomes lessons only, not a skill.
- **Headline memory metric:** first-try rate, the share of seeds solved on try 1. The race-over-race panel compares it between the cold and warm races.

### Failure modes

Each failed try gets a failure mode, derived from the `result.json` fields, in this order:

| Mode | When |
|---|---|
| `timeout` | `error` mentions a timeout or kill |
| `code_error` | any other `error` |
| `dropped` | `dropped` is true |
| `collision` | failed with collisions |
| `missed` | anything else |

The tracker also keeps the last line of each traceback, which is the useful part for lessons.

A throwing variant can optionally send `raw: {bin_xy, crossing_xy}` to get short/long/left/right. Nothing needs it today.

## API

| Method | Path | What |
|---|---|---|
| POST | `/races` | Start a race: `{label, task, scoring?}` |
| GET | `/races` | All races with summaries |
| POST | `/races/{id}/agents` | Register one agent or a list (optional) |
| POST | `/races/{id}/attempts` | Record one attempt or a list: `result.json` + `agent_id`, `seed`, `attempt` |
| GET | `/races/{id}/leaderboard` | Ranked agents, `leader`, `winner` (once closed), summary |
| GET | `/races/{id}/attempts/{agent}/{seed}/{try}` | One attempt, including its code |
| GET | `/races/{id}/attempts/{agent}/{seed}/{try}/media/video` | The GIF or MP4 (also `frame0`, `frame1`, …) |
| GET | `/races/{id}/lessons` | Lessons so far, plus the leading policy |
| POST | `/races/{id}/close` | Freeze the race, pick the winner, return lessons and the skill |
| GET | `/compare?race_ids=a,b` | Race-over-race comparison (all races if omitted) |

## Files

- **Core:**
  - `models.py`: data contracts
  - `metrics.py`: failure classification
  - `scoring.py`: scores, winner, lessons, comparison
- **Service:**
  - `store.py`: SQLite storage (`racetrack.db`; delete it to reset)
  - `server.py`: the API
  - `dashboard.html`: the live board, including the winning policy's GIF and code
- **Planning:**
  - `planner.py`: the LLM strategy planner, and the strategy block each agent adds to its prompt
  - `tasks/can_to_bin.json`: the example task file; copy it for a new simulation
- **Hooks:**
  - `client.py`: what `run_agent.py` imports
  - `ingest.py`: folder ingest
- **Testing:**
  - `fake_runs.py`: a stand-in for the agent loop. It fakes tries for the strategies in a plan file and writes Phase 1-format `result.json` files. Delete it once the real loop runs.
  - `test_scoring.py`, `test_api.py` and `test_planner.py`: run with `python -m pytest -q`. The planner tests use a mocked model.
