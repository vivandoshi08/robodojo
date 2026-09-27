# race-brain: the GBrain side of the race

Robot agents race to toss trash into bins. **Memorable** records what happens *during* a race, attempt by attempt. **GBrain** holds what agents know *before* a race and what the team learns *after* it. This folder is the GBrain half.

```
            before race                     during race                    after race
  GBrain ───────────────▶ agents ─────────▶ Memorable ─────────▶ referee ───────────▶ GBrain
  skills/<strategy>/SKILL.md        episodes per attempt        races/<id> verdict page
  + strategies/<name> record        (recall mid-race)           + skillopt on the winner's SKILL.md
                                                                          │
                          next race loads the improved skill ◀────────────┘
```

## What it does

| Command | When | What happens in GBrain |
|---|---|---|
| `race-brain brief <strategy>` | before a race | Returns the strategy's `SKILL.md` plus its race record from the `strategies/<name>` page. This is what the agent loads. |
| `race-brain verdict <race_id>` | race ends | Writes `races/<id>` with standings, why the winner won, and Memorable episodes as evidence. Links it to each strategy page (`won_by` / `lost_by`), tags the winner, and adds a timeline entry. |
| `race-brain benchmark <strategy>` | between races | Turns Memorable's episode table into `skills/<strategy>/skillopt-benchmark.jsonl`. |
| `race-brain optimize <strategy>` | between races | Runs `gbrain skillopt` on the winner's `SKILL.md`. It keeps an edit only if it scores better on held-out scenarios. |
| `race-brain after-race <race_id>` | race ends | Runs verdict, then benchmark, then a skillopt cost preview for the winner, in that order. |

Add `--print` to any command to see the `gbrain` commands without running them.

## How race scores feed skillopt

`gbrain skillopt` doesn't read score tables. It runs the skill on **tasks** and uses a **judge** to score each answer. `src/benchmark.ts` bridges the two:

- Each **scenario** (trash type + bin position, snapped to 0.5 m / 15°) with at least one throw in the bin becomes a **task**. The task prompt includes recent misses, the way Memorable recall works mid-race.
- The **judge** checks the skill's `PARAMS` line against the parameter ranges that actually landed in the bin in past races, with a small margin. It gets partial credit per parameter.
- `--judge rule` (the default) is free and deterministic. `--judge llm` writes a rubric that includes the misses, and it costs API credits.
- Throws that landed from *any* agent form the answer key. The skill being tuned is the winner's.

SkillOpt then rewrites `SKILL.md`, re-tests it and keeps the change only if it wins. `git diff skills/<strategy>/SKILL.md` shows the improvement.

## Setup

Requires [Bun](https://bun.sh) 1.3.11+.

```bash
# 1. GBrain (install from GitHub; the npm package named gbrain is unrelated)
bun install -g github:garrytan/gbrain
gbrain init --pglite --no-embedding
gbrain doctor

# 2. This project (run from robodojo/gbrain)
cd gbrain
bun install
bun run typecheck

# 3. Only needed for skillopt (optimize / after-race): a model API key
export ANTHROPIC_API_KEY=...     # then check: gbrain models doctor
```

Optional: `.mcp.json` (repo root) registers GBrain as an MCP server, so Claude Code sessions in this repo can read and write the brain.

## Try it without the real race

```bash
bun run fixture                              # fake Memorable episodes → fixtures/episodes.json
bun src/cli.ts after-race race-002 --print   # the full post-race flow, printed
bun src/cli.ts benchmark arc-toss            # writes a real benchmark file
```

## Wiring to the real system

- **From Python** (referee, sim): shell out, e.g. `subprocess.run(["bun", "src/cli.ts", "verdict", race_id, "--results", path], cwd="gbrain", check=True)`.

- **Memorable**: implement `MemorableClient` in `src/memorable.ts`, using `race_episodes(race_id)` and the episode table. If the column names differ, update `fromRow`.
- **Referee**: pass final standings as JSON with `--results result.json` (shape: `RaceResult` in `src/types.ts`). Without it, standings are computed from the episodes.
- **New strategy**: add `skills/<strategy>/SKILL.md`. It must end with the `PARAMS` / `REASON` output format, because the judges match it.
- **Tuning**: set bucket sizes, number precision, margins and the cost cap in `src/config.ts`.

skillopt refuses to run if `SKILL.md` has uncommitted changes, so commit before you optimize.

## Files

```
src/cli.ts          commands
src/briefing.ts     pre-race: skill + record
src/verdict.ts      post-race: verdict page, links, timeline
src/benchmark.ts    episode table → skillopt benchmark
src/skillopt.ts     gbrain skillopt invocation
src/memorable.ts    Memorable boundary (fixture client for now)
src/gbrain.ts       gbrain CLI wrapper (--print mode)
src/config.ts       tunables
skills/arc-toss/    example strategy skill
scripts/make-fixture.ts
```
