# gbrain/: GBrain integration

From the pitch: *the winning strategy is distilled into procedural memory in GBrain, so future agents start from proven skills instead of blank prompts, and skillopt keeps improving that skill with every race.*

```
runs/ (from the simulations) ──distill──▶ skills/trash-to-bin/SKILL.md ──brief──▶ next race's agents (--context-file)
                                            (also saved to GBrain)           ▲
                                                  └──benchmark + optimize────┘  skillopt improves the skill
```

| Command (run from `gbrain/`) | What it does |
|---|---|
| `bun src/cli.ts distill` | Reads every finished run in `runs/`. Writes plain-language rules per trash type (and bin distance, when recorded) into `skills/trash-to-bin/SKILL.md`, e.g. *"Use toss for paper: it solved 5 of 5. drop: failed by bouncing out (×3)"*. Saves the skill to GBrain as `procedures/trash-to-bin`. |
| `bun src/cli.ts brief --out context.md` | Writes the skill for `run_agent.py --context-file context.md`. |
| `bun src/cli.ts benchmark` | Turns `runs/` history into the skillopt benchmark: each task + seed becomes a task, judged against code that succeeded there. |
| `bun src/cli.ts optimize [--preview-cost]` | Runs `gbrain skillopt` on the skill. `git diff` shows what improved. Commit `SKILL.md` first. |

Add `--print` to see `gbrain` commands without running them.

**What this doesn't do:** it never writes to `runs/` (that belongs to the simulations and the website), doesn't score races, and doesn't write strategies. It only reads results and turns them into GBrain's skill.

`distill` regenerates only the section between `<!-- distilled:start -->` and `<!-- distilled:end -->`. The rest of `SKILL.md`, including skillopt's edits, is kept.

## Setup

```bash
bun install -g github:garrytan/gbrain          # not the npm package named gbrain
gbrain init --pglite --no-embedding && gbrain doctor
cd gbrain && bun install
export ANTHROPIC_API_KEY=...                   # only for optimize (skillopt)
```

## Try it with fake data

```bash
bun run fixture                                # FAKE runs in fixtures/runs
RACE_RUNS_DIR=fixtures/runs bun src/cli.ts distill --print
git checkout skills/trash-to-bin/SKILL.md      # discard the fake rules
```

## Needed from the simulation side

For distance rules ("toss works past 50 cm") and the knocked-over score, `result.json` needs two more fields. Both are used automatically once present:
- `bin_center`: `[x, y, z]`, already available from `robot.get_state()["bin"]["center"]`
- `bin_knocked_over`: `true` / `false`

Strategy names come from the first line of the strategy card in `summary.json`, so every racer using the same card is grouped together.
