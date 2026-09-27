#!/usr/bin/env bun
// race-brain: the GBrain side of the race.
//
//   brief <strategy>                     what an agent loads before a race
//   verdict <race_id> [--results f.json] referee writes races/<id> to GBrain
//   benchmark <strategy> [--judge llm]   episode table → skillopt-benchmark.jsonl
//   optimize <strategy> [--preview-cost] [--no-mutate] [--max-cost N]
//   after-race <race_id> [--results f.json] [--judge llm]
//                                        verdict + benchmark + cost preview for the winner
//
// Add --print to any command to echo gbrain commands instead of running them.

import { buildBenchmark, type JudgeMode, writeBenchmark } from "./benchmark.ts";
import { buildBriefing, skillDirFor } from "./briefing.ts";
import { config } from "./config.ts";
import { GBrain } from "./gbrain.ts";
import { FileMemorable, type MemorableClient } from "./memorable.ts";
import { runSkillopt } from "./skillopt.ts";
import type { RaceResult } from "./types.ts";
import { publishVerdict, standingsFrom } from "./verdict.ts";

const argv = process.argv.slice(2);
const flag = (name: string) => argv.includes(`--${name}`);
const option = (name: string) => {
  const i = argv.indexOf(`--${name}`);
  return i >= 0 ? argv[i + 1] : undefined;
};
const [command, target] = argv;

const g = new GBrain(flag("print"));
const memorable: MemorableClient = new FileMemorable(config.memorableFixture);

async function loadResult(raceId: string): Promise<RaceResult> {
  const file = option("results");
  if (file) return (await Bun.file(file).json()) as RaceResult;
  // No referee file: derive standings from the full episode table for this race.
  const episodes = await memorable.episodeTable({ raceIds: [raceId] });
  if (episodes.length === 0) throw new Error(`No episodes for race ${raceId}.`);
  return {
    race_id: raceId,
    finished_at: new Date().toISOString(),
    agents: standingsFrom(episodes),
  };
}

async function verdict(raceId: string): Promise<RaceResult> {
  const result = await loadResult(raceId);
  const slug = await publishVerdict(g, result, await memorable.raceEpisodes(raceId));
  console.log(`Verdict written to ${slug}; winner: ${result.agents[0]?.strategy}`);
  return result;
}

async function benchmark(strategy: string): Promise<void> {
  const mode = (option("judge") ?? "rule") as JudgeMode;
  const { tasks, skipped, warnings } = buildBenchmark(await memorable.episodeTable(), mode);
  const path = await writeBenchmark(skillDirFor(strategy), tasks);
  console.log(`Wrote ${tasks.length} tasks (${mode} judges) to ${path}`);
  if (skipped.length) console.log(`Skipped ${skipped.length} scenarios with no successful throw yet.`);
  for (const w of warnings) console.warn(`warning: ${w}`);
}

async function optimize(strategy: string, previewCost = flag("preview-cost")): Promise<void> {
  const maxCost = option("max-cost");
  const out = await runSkillopt(g, strategy, {
    previewCost,
    noMutate: flag("no-mutate"),
    maxCostUsd: maxCost ? Number(maxCost) : undefined,
  });
  if (out) console.log(out);
  if (!previewCost && !g.print) {
    console.log(`Review with: git diff ${skillDirFor(strategy)}/SKILL.md`);
  }
}

async function main(): Promise<void> {
  if (!target) throw new Error("Usage: race-brain <brief|verdict|benchmark|optimize|after-race> <strategy|race_id> [--print]");
  switch (command) {
    case "brief":
      console.log(await buildBriefing(g, target));
      return;
    case "verdict":
      await verdict(target);
      return;
    case "benchmark":
      await benchmark(target);
      return;
    case "optimize":
      await optimize(target);
      return;
    case "after-race": {
      const result = await verdict(target);
      const winner = result.agents[0]!.strategy;
      await benchmark(winner);
      // Stop at a cost preview; running for real is a deliberate `optimize` call.
      await optimize(winner, true);
      console.log(`Next: race-brain optimize ${winner}`);
      return;
    }
    default:
      throw new Error(`Unknown command: ${command}`);
  }
}

main().catch((err) => {
  console.error(err instanceof Error ? err.message : err);
  process.exit(1);
});
