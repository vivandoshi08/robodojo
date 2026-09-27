// Memorable episode table → skills/<strategy>/skillopt-benchmark.jsonl.
//
// skillopt can't read a score table directly: it runs the skill on tasks and
// judges each answer. So every scenario (trash type + snapped bin position)
// with at least one throw in the bin becomes one task, and the judge checks
// whether the skill's answer lands inside the parameter range that actually
// scored in past races.

import { config } from "./config.ts";
import { describeBin, type Scenario, scenarioOf } from "./scenario.ts";
import {
  type BenchmarkTask,
  type Episode,
  isSuccess,
  PARAM_KEYS,
  type ParamKey,
  type RuleCheck,
} from "./types.ts";

export type JudgeMode = "rule" | "llm";

interface Range {
  lo: number;
  hi: number;
}

interface Group {
  scenario: Scenario;
  successes: Episode[];
  failures: Episode[];
}

function groupByScenario(episodes: Episode[]): Group[] {
  const groups = new Map<string, Group>();
  for (const e of episodes) {
    const scenario = scenarioOf(e);
    const g = groups.get(scenario.key) ?? { scenario, successes: [], failures: [] };
    (isSuccess(e) ? g.successes : g.failures).push(e);
    groups.set(scenario.key, g);
  }
  return [...groups.values()].sort((a, b) => a.scenario.key.localeCompare(b.scenario.key));
}

function successRange(successes: Episode[], key: ParamKey, margin: number): Range {
  const vals = successes.map((e) => e.params[key]);
  const lo = Math.min(...vals) - margin;
  return { lo: key === "grasp_angle_deg" ? lo : Math.max(0, lo), hi: Math.max(...vals) + margin };
}

const fmt = (key: ParamKey, v: number) => v.toFixed(config.precision[key]);

/** Every value in the range at the skill's required precision, e.g. 2.3|2.4|2.5. */
function valuesInRange(key: ParamKey, r: Range): string[] {
  const step = 10 ** -config.precision[key];
  const out: string[] = [];
  for (let i = Math.ceil(r.lo / step - 1e-9); i <= Math.floor(r.hi / step + 1e-9); i++) {
    out.push(fmt(key, i * step));
  }
  return out;
}

const escapeRe = (s: string) => s.replace(/[.*+?^${}()|[\]\\-]/g, "\\$&");
const numberPattern = (key: ParamKey) =>
  config.precision[key] === 0 ? "-?\\d+" : `-?\\d+\\.\\d{${config.precision[key]}}`;

/** The exact output lines SKILL.md tells the agent to end with. */
export const PARAMS_LINE_REGEX = `^PARAMS ${PARAM_KEYS.map((k) => `${k}=${numberPattern(k)}`).join(" ")}\\s*$`;
export const REASON_LINE_REGEX = "^REASON: \\S.*$";

function taskPrompt(g: Group): string {
  const misses = g.failures.slice(-config.maxFailuresInPrompt).map(
    (e) =>
      `- ${PARAM_KEYS.map((k) => `${k}=${fmt(k, e.params[k])}`).join(" ")} → ${e.outcome} ("${e.reason}")`,
  );
  return [
    "Race attempt.",
    `Trash: ${g.scenario.trash_type}`,
    `Bin: ${describeBin(g.scenario)}`,
    ...(misses.length ? ["Recent misses in this situation (recalled from Memorable):", ...misses] : []),
    "Choose the toss parameters for this attempt.",
  ].join("\n");
}

function ruleJudge(g: Group): { kind: "rule"; checks: RuleCheck[] } {
  const ranges = PARAM_KEYS.map((k): RuleCheck => {
    const values = valuesInRange(k, successRange(g.successes, k, config.margin[k]));
    return { op: "regex", arg: `${k}=(?:${values.map(escapeRe).join("|")})(?![\\d.])` };
  });
  return {
    kind: "rule",
    checks: [
      { op: "regex", arg: PARAMS_LINE_REGEX },
      ...ranges,
      { op: "regex", arg: REASON_LINE_REGEX },
      { op: "max_chars", arg: 800 },
    ],
  };
}

function llmJudge(g: Group): { kind: "llm"; rubric: string } {
  const ranges = PARAM_KEYS.map((k) => {
    const r = successRange(g.successes, k, 0);
    return `${k} ${fmt(k, r.lo)}–${fmt(k, r.hi)}`;
  }).join(", ");
  const misses = g.failures
    .map((e) => PARAM_KEYS.map((k) => `${k}=${fmt(k, e.params[k])}`).join(" ") + ` (${e.outcome})`)
    .join("; ");
  return {
    kind: "llm",
    rubric: [
      "Score the answer from 0 to 1.",
      `In past races, throws in this situation that landed in the bin used: ${ranges} (${g.successes.length} successful throws).`,
      misses ? `These throws missed: ${misses}.` : "",
      "1.0: ends with a valid PARAMS line and REASON line, all three parameters are inside the successful ranges, and the reason names the factor that matters for this trash type.",
      "0.5: valid PARAMS line and at least two parameters in range.",
      "0.0: no PARAMS line, or it repeats a combination that missed.",
    ]
      .filter(Boolean)
      .join("\n"),
  };
}

export interface BuildResult {
  tasks: BenchmarkTask[];
  /** Scenarios with no successful throw yet, so no answer key. */
  skipped: string[];
  warnings: string[];
}

/**
 * Successful throws from every agent are used as the answer key, not just the
 * winner's: the skill being tuned is the winner's, but any throw that landed
 * is evidence of what works.
 */
export function buildBenchmark(episodes: Episode[], mode: JudgeMode = "rule"): BuildResult {
  const tasks: BenchmarkTask[] = [];
  const skipped: string[] = [];
  for (const g of groupByScenario(episodes)) {
    if (g.successes.length === 0) {
      skipped.push(g.scenario.key);
      continue;
    }
    tasks.push({
      task_id: g.scenario.key,
      task: taskPrompt(g),
      judge: mode === "rule" ? ruleJudge(g) : llmJudge(g),
    });
  }
  const warnings =
    tasks.length < config.minTasks
      ? [
          `Only ${tasks.length} tasks; skillopt --split ${config.skillopt.split} needs at least ${config.minTasks}. Run more races, or shrink config.bucket so bin positions split into more scenarios.`,
        ]
      : [];
  return { tasks, skipped, warnings };
}

export async function writeBenchmark(skillDir: string, tasks: BenchmarkTask[]): Promise<string> {
  const path = `${skillDir}/skillopt-benchmark.jsonl`;
  await Bun.write(path, tasks.map((t) => JSON.stringify(t)).join("\n") + "\n");
  return path;
}
