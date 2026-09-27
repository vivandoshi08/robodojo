import { resolve } from "node:path";

const repoRoot = resolve(import.meta.dir, "../..");

export const config = {
  repoRoot,

  /** The runs/ tree the simulations write (CLAUDE.md §8). */
  runsDir: process.env.RACE_RUNS_DIR ?? resolve(repoRoot, "runs"),

  /** GBrain's skill: the distilled procedural memory future agents start from. */
  skill: "trash-to-bin",
  skillsDir: resolve(import.meta.dir, "../skills"),

  /** GBrain page the skill is also saved to. */
  page: "procedures/trash-to-bin",

  /** Bin distances (from the robot base) that split the rules, in meters. */
  distanceSplitM: 0.5,

  skillopt: { split: "1:1:1", maxCostUsd: 5 },
};

export const skillDir = resolve(config.skillsDir, config.skill);
export const skillPath = resolve(skillDir, "SKILL.md");
