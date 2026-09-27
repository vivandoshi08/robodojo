// Shared shapes. Episode mirrors what Memorable writes after every attempt;
// if the real table uses different column names, change the mapping in
// memorable.ts (fromRow), not these types.

export const PARAM_KEYS = ["release_height_m", "toss_velocity_mps", "grasp_angle_deg"] as const;
export type ParamKey = (typeof PARAM_KEYS)[number];
export type TossParams = Record<ParamKey, number>;

export interface BinPosition {
  distance_m: number;
  /** Negative = left of the robot, positive = right. */
  angle_deg: number;
}

export type Outcome = "in_bin" | "rim_out" | "short" | "long" | "wide" | "dropped";

export interface Episode {
  episode_id: string;
  race_id: string;
  agent_id: string;
  strategy: string;
  attempt: number;
  trash_type: string;
  bin_position: BinPosition;
  params: TossParams;
  outcome: Outcome;
  score: number;
  /** One-line reason the agent gave for the result. */
  reason: string;
}

export const isSuccess = (e: Episode) => e.outcome === "in_bin";

export interface AgentResult {
  agent_id: string;
  strategy: string;
  total_score: number;
  attempts: number;
  successes: number;
}

/** What the referee hands us when a race ends. */
export interface RaceResult {
  race_id: string;
  /** ISO timestamp. */
  finished_at: string;
  agents: AgentResult[];
  /** Optional free-text summary from the referee, shown at the top of the page. */
  summary?: string;
}

// SkillOpt benchmark row, matching gbrain src/core/skillopt/types.ts.
export type RuleCheck = { op: "contains" | "regex" | "section_present" | "max_chars"; arg: string | number };
export type Judge = { kind: "rule"; checks: RuleCheck[] } | { kind: "llm"; rubric: string };
export interface BenchmarkTask {
  task_id: string;
  task: string;
  judge: Judge;
}
