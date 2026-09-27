import type { ParamKey } from "./types.ts";

export const config = {
  /** Where strategy skills live: skills/<strategy>/SKILL.md. Must be inside a git repo. */
  skillsDir: process.env.RACE_BRAIN_SKILLS_DIR ?? "skills",

  /** Until Memorable's API is wired up, episodes are read from this JSON file. */
  memorableFixture: process.env.MEMORABLE_FIXTURE ?? "fixtures/episodes.json",

  /** Bin positions are snapped to these steps so nearby throws count as one scenario. */
  bucket: { distanceM: 0.5, angleDeg: 15 },

  /** Decimal places the skill must use for each parameter (the rule judge matches these exactly). */
  precision: { release_height_m: 2, toss_velocity_mps: 1, grasp_angle_deg: 0 } satisfies Record<ParamKey, number>,

  /** Slack added around the successful range before judging, so near-misses of the range still pass. */
  margin: { release_height_m: 0.03, toss_velocity_mps: 0.1, grasp_angle_deg: 3 } satisfies Record<ParamKey, number>,

  /** --split 1:1:1 needs >= 5 tasks in the selection slice, so >= 15 total. */
  minTasks: 15,

  /** Past misses shown in each benchmark task, like Memorable's mid-race recall. */
  maxFailuresInPrompt: 3,

  /** Episodes quoted as evidence on a verdict page, per side. */
  evidencePerSide: 5,

  skillopt: { split: "1:1:1", maxCostUsd: 5 },
};
