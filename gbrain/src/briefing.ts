// Before a race: what an agent knows going in = its strategy's skill
// (possibly improved by skillopt) + that strategy's race record in GBrain.

import { config } from "./config.ts";
import type { GBrain } from "./gbrain.ts";
import { slugify } from "./scenario.ts";
import { strategySlug } from "./verdict.ts";

export const skillDirFor = (strategy: string) => `${config.skillsDir}/${slugify(strategy)}`;

export async function buildBriefing(g: GBrain, strategy: string): Promise<string> {
  const skillPath = `${skillDirFor(strategy)}/SKILL.md`;
  const skill = Bun.file(skillPath);
  if (!(await skill.exists())) throw new Error(`No skill for "${strategy}" at ${skillPath}.`);

  let record: string;
  try {
    record = (await g.get(strategySlug(strategy))).trim() || "(print mode: record not fetched)";
  } catch {
    record = "(no races recorded yet)";
  }

  return `${await skill.text()}

---

## Race record for ${strategy} (from GBrain page ${strategySlug(strategy)})

${record}
`;
}
