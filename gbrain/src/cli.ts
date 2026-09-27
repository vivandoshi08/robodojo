#!/usr/bin/env bun
// GBrain integration for the robot race (run from gbrain/):
//
//   distill [--print]          runs/ → plain-language rules in skills/trash-to-bin/SKILL.md, saved to GBrain
//   brief [--out context.md]   the skill, for run_agent.py --context-file (agents start from proven skills)
//   benchmark                  runs/ → skillopt benchmark for the skill
//   optimize [--preview-cost]  gbrain skillopt improves the skill
//
// GBrain only reads runs/. Scoring and strategies stay with their owners.

import { buildBenchmark, writeBenchmark } from "./benchmark.ts";
import { config, skillPath } from "./config.ts";
import { updateSkill } from "./distill.ts";
import { GBrain } from "./gbrain.ts";
import { loadRuns } from "./runs.ts";

const argv = process.argv.slice(2);
const flag = (name: string) => argv.includes(`--${name}`);
const option = (name: string) => argv[argv.indexOf(`--${name}`) + 1];
const g = new GBrain(flag("print"));

async function main(): Promise<void> {
  switch (argv[0]) {
    case "distill": {
      const runs = await loadRuns();
      const skill = await updateSkill(runs);
      await g.put(config.page, skill);
      console.log(`Distilled ${runs.length} runs into ${skillPath} and GBrain page ${config.page}.`);
      console.log("Commit SKILL.md before running optimize.");
      return;
    }
    case "brief": {
      const out = flag("out") ? option("out")! : "context.md";
      const body = (await Bun.file(skillPath).text()).replace(/^---\n[\s\S]*?\n---\n/, "").trim();
      await Bun.write(out, `${body}\n`);
      console.log(`Wrote ${out}. Pass it to racers with --context-file ${out}`);
      return;
    }
    case "benchmark": {
      const { tasks, warning } = await buildBenchmark(await loadRuns());
      console.log(`Wrote ${tasks.length} tasks to ${await writeBenchmark(tasks)}`);
      if (warning) console.warn(`warning: ${warning}`);
      return;
    }
    case "optimize": {
      const out = await g.run([
        "skillopt", config.skill,
        "--skills-dir", config.skillsDir,
        "--split", config.skillopt.split,
        "--max-cost-usd", String(config.skillopt.maxCostUsd),
        ...(flag("preview-cost") ? ["--dry-run"] : []),
      ]);
      if (out) console.log(out);
      if (!flag("preview-cost") && !g.print) console.log(`See what improved: git diff ${skillPath}`);
      return;
    }
    default:
      throw new Error("Usage: bun src/cli.ts <distill|brief|benchmark|optimize> [--print]");
  }
}

main().catch((err) => {
  console.error(err instanceof Error ? err.message : err);
  process.exit(1);
});
