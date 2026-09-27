// Shapes of the files the simulations write under runs/ (CLAUDE.md §3 and §8).
// GBrain only reads them.

/** runs/<run_id>/attempt_<k>/result.json */
export interface AttemptResult {
  task: string;
  seed: number;
  success: boolean;
  time_s: number;
  collisions: number;
  energy_j: number;
  dropped: boolean;
  lifted: boolean;
  error: string | null;
  /** Not in the contract yet: needed for distance rules ("toss works past 50 cm"). */
  bin_center?: number[];
  /** Not in the contract yet: the pitch scores on it. */
  bin_knocked_over?: boolean;
}

/** runs/<run_id>/summary.json (only the fields we use). */
export interface RunSummary {
  task: string;
  seed: number;
  strategy: string | null;
  status: "running" | "solved" | "failed" | "error";
  solved_at: number | null;
}

export interface Attempt {
  k: number;
  result: AttemptResult;
  code: string;
}

/** One racer = one run_agent.py process. */
export interface Run {
  run_id: string;
  summary: RunSummary;
  strategy: string;
  attempts: Attempt[];
}

// SkillOpt benchmark row, matching gbrain src/core/skillopt/types.ts.
export interface BenchmarkTask {
  task_id: string;
  task: string;
  judge: { kind: "llm"; rubric: string };
}
