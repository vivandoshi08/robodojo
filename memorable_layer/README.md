# `memorable_layer` — Memorable integration

**Memory between attempts, not just between races.**

Every attempt an agent makes is recorded with its parameters, its outcome and one
line on why it ended that way. Before the next attempt the agent recalls similar
episodes and adjusts — so it improves *during* a race, not only after it.

Roles, kept deliberately distinct so there are two integrations rather than one
duplicated:

| | owns |
|---|---|
| **Memorable** (this package) | what happens *during* a race, attempt by attempt |
| **GBrain** (referee side) | what agents know *before* a race and what is learned *after*: verdict page, policy, `skillopt` |

Pure standard library. No third-party dependencies, nothing added to the repo
root, no npm package in a Python project — the `memorable` CLI is installed in
the sandbox image and called as a subprocess.

## The two calls an agent makes

```python
from memorable_layer import AttemptRecorder, Episode

recorder = AttemptRecorder()          # reads scope/race/namespace from env

# before the throw
brief = recorder.brief("toss", "bottle", bin_distance_cm=50, bin_bearing_deg=-15)
post_to_qm_channel(brief.channel_line)
#   [attempt 3] recalled 2 overshoot episode(s), dropping velocity 15%
params = brief.suggested_params

# after the throw
recorder.record(Episode(
    race_id=..., scope=..., strategy="toss", attempt=3, trash_type="bottle",
    bin_distance_cm=50, bin_bearing_deg=-15, params=params,
    outcome="in", reason="landed clean inside the bin",
    bin_knocked_over=False, duration_s=4.2, seed=1234,
))
```

Or through `execute`, from inside the sandbox — every subcommand prints JSON:

```sh
python -m memorable_layer brief  --strategy toss --trash bottle --bin-cm 50 --line
echo "$EPISODE_JSON" | python -m memorable_layer record -
python -m memorable_layer status
```

## What GBrain consumes (the seam)

```sh
python -m memorable_layer episodes       --race race-1   # every attempt, winners and losers
python -m memorable_layer evidence       --race race-1   # the whole verdict bundle
python -m memorable_layer answer-key     --race race-1   # param ranges from throws that went in
python -m memorable_layer skillopt-rows  --race race-1   # flat numbers for gbrain skillopt
python -m memorable_layer strategy-record                # "won 2 of 3 races"
python -m memorable_layer policy-hint                    # observed best strategy per distance
```

Same functions are importable: `race_episodes`, `race_evidence`,
`answer_key_rows`, `skillopt_rows`, `strategy_record`, `distance_policy_hint`.

`answer-key` is the answer key behind a skillopt practice question — for
`"can, bin 1 m away, 15° left: what height, speed and angle?"` it returns the
min/median/max of each parameter over throws that **actually landed**, with the
episode ids as evidence:

```json
{
  "question": "toss a can into a bin 100 cm away 15 degrees to the left",
  "bin_distance_m": 1.0,
  "successes": 4,
  "units": "metres",
  "params": {"release_height_m": {"min": 0.79, "median": 0.82, "max": 0.85, "n": 4}},
  "evidence_episode_ids": ["6f6ced39c8ac", "..."]
}
```

**Units.** Parameter names carry their units (`release_height_cm`,
`toss_velocity_mps`). Everything GBrain reads also gets a metre mirror —
`bin_distance_m`, and `params_si` with `*_cm` keys renamed `*_m` — because the
skillopt questions are phrased in metres and a silent 100× is the easiest bug to
ship here.

An empty answer key for a situation means memory genuinely does not know it yet.
That is a real signal; do not paper over it.

### Building before the sim exists

```sh
python -m memorable_layer fixture --races 3 --attempts 4
```

Writes real-shaped episodes (marked `"fixture": true` in `extra`, so they are
never mistaken for measured data) so the GBrain side can be developed and tested
against the real schema today.

## Isolation

One QM scope is one Memorable namespace, pinned with `MEMORABLE_HOME`: the CLI
resolves its store to `$MEMORABLE_HOME/.memorable/procedures.jsonl`, and the
episode log rides along at `$MEMORABLE_HOME/.robodojo`. Competitors cannot read
each other's attempts mid-race — `similar()` is scope-filtered as well, so even a
shared store would not leak.

After the verdict, the winner's experience is meant to be shared:

```sh
python -m memorable_layer collect  /path/to/scope-*/.robodojo     # referee sees the whole race
python -m memorable_layer promote  --race race-1 --winner toss --into ~/shared-memorable
```

## Environment

| variable | meaning |
|---|---|
| `MEMORABLE_API_KEY` | `mk_...`, **required in every sandbox** — extraction is a cloud call. Headless sign-in: `echo "$MEMORABLE_API_KEY" \| memorable login --paste` (bare `memorable login` waits for a browser and will hang a tool call) |
| `MEMORABLE_HOME` | one per QM scope — this is the isolation |
| `MEMORABLE_AUTO_INGEST` | forced to `0`; traces are handed over explicitly |
| `ROBODOJO_SCOPE` | scope id; falls back to `QM_SCOPE`, then hostname |
| `ROBODOJO_RACE_ID` | race id |
| `ROBODOJO_MEMORY_ROOT` | episode log location (default: `$MEMORABLE_HOME/.robodojo`) |
| `ROBODOJO_MEMORABLE_BIN` | override the binary, e.g. `npx memorable-cli@latest` |
| `ROBODOJO_MEMORABLE_READONLY` | `1` makes the layer read-only |

Sandbox image needs, for whoever owns the deployment directory:

```dockerfile
RUN npm install -g memorable-cli@latest
ENV MEMORABLE_AUTO_INGEST=0
# per-scope at launch: MEMORABLE_HOME=/memorable/<scope>, MEMORABLE_API_KEY from a QM secret
```

## Two constraints that shaped this

1. **The extraction service refuses a trace with no verified postcondition**
   (`no_postcondition, prefilter` — hit live while building this). A stored
   procedure needs a step that *changes* something plus a command that verified
   it and passed. So `build_trace` reads the skill file, replays the misses as
   dead ends, writes the tuned `params.json`, and ends on the winning sim command
   — and only a **successful** attempt is ingested. Misses are still recorded in
   the episode log and carried into the trace of whichever attempt finally lands,
   which is where "what missed and why" survives.
2. **The episode log is the source of truth for numbers.** Memorable holds the
   procedure; scoring, filtering and `skillopt` need every attempt, including the
   refused ones. Hence JSONL (append-only, never loses a row) plus a SQLite
   mirror for grouping.

Recall surfaces the winning numbers because they are on the verifying command
line — extraction keeps commands verbatim:

```
Verified last time by: python -m robodojo.sim --strategy toss --trash bottle \
  --bin-cm 50 --bin-bearing-deg 0 --release-height-cm 30 --toss-velocity-mps 1.45
```

## Failure behaviour

Reads **fail open**: a missing CLI, a cold store, a timeout or a refusal returns
empty and the race continues. Writes fail closed and report the reason in the
return value. The memory layer can never break a sim run or the demo.

## Tests

```sh
python -m unittest discover -s memorable_layer/tests -t .
```

42 tests, no network: the CLI is replaced by a fake binary, so the suite proves
the trace shape and the fail-open paths without spending extraction allowance.

## Situations, not attempts

`task_description` describes the *situation* — "toss a bottle into a bin 50 cm
away" — with distance bucketed to 10 cm and bearing to 15°. Memorable keeps one
procedure per task with revisions beside it, so every race on the same situation
accumulates on one intent, and `memorable list --json` reports which revision
recall prefers plus how often things went well afterwards. That is a free
cross-race scoreboard.
