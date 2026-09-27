// Between races: run gbrain skillopt on the winning strategy's SKILL.md.

import { config } from "./config.ts";
import type { GBrain } from "./gbrain.ts";
import { slugify } from "./scenario.ts";

export interface SkilloptOptions {
  /** gbrain's own --dry-run: estimate cost, make no model calls. */
  previewCost?: boolean;
  /** Write proposed.md instead of editing SKILL.md. */
  noMutate?: boolean;
  maxCostUsd?: number;
}

export function skilloptArgs(strategy: string, o: SkilloptOptions = {}): string[] {
  return [
    "skillopt",
    slugify(strategy),
    "--skills-dir",
    config.skillsDir,
    "--split",
    config.skillopt.split,
    "--max-cost-usd",
    String(o.maxCostUsd ?? config.skillopt.maxCostUsd),
    ...(o.previewCost ? ["--dry-run"] : []),
    ...(o.noMutate ? ["--no-mutate"] : []),
  ];
}

/**
 * Exit codes from gbrain: 0 improved (or proposal written), 1 no improvement,
 * 2 aborted by a gate (budget, dirty SKILL.md, small benchmark...). GBrain.run
 * throws on any non-zero code, so 1 and 2 surface as errors with gbrain's message.
 */
export const runSkillopt = (g: GBrain, strategy: string, o: SkilloptOptions = {}) =>
  g.run(skilloptArgs(strategy, o));
