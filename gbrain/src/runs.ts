// Read the runs/ tree. GBrain never writes there: runs/ belongs to the
// simulations and the website (CLAUDE.md §8).

import { readdir } from "node:fs/promises";
import { join } from "node:path";
import { config } from "./config.ts";
import type { Attempt, AttemptResult, Run, RunSummary } from "./types.ts";

/**
 * Strategy name: the run_id's optional slug ("<stamp>-<task>-s<seed>-<slug>",
 * CLAUDE.md §8), else the first line of the strategy card.
 */
function strategyName(card: string | null, runId: string): string {
  const fromId = runId.match(/-s\d+-(.+)$/)?.[1];
  if (fromId) return fromId;
  return card?.trim().split("\n")[0]?.slice(0, 40) || "no-strategy";
}

/** "paper_to_bin" → "paper", "can_to_far_bin" → "can". */
export const itemOf = (task: string) => task.split("_to_")[0] ?? task;

async function loadRun(runsDir: string, runId: string): Promise<Run> {
  const runDir = join(runsDir, runId);
  const summary = (await Bun.file(join(runDir, "summary.json")).json()) as RunSummary;
  const attempts: Attempt[] = [];
  for (const d of await readdir(runDir, { withFileTypes: true })) {
    if (!d.isDirectory() || !/^attempt_\d+$/.test(d.name)) continue;
    const resultFile = Bun.file(join(runDir, d.name, "result.json"));
    if (!(await resultFile.exists())) continue; // still running
    const codeFile = Bun.file(join(runDir, d.name, "policy.py"));
    attempts.push({
      k: Number(d.name.slice(8)),
      result: (await resultFile.json()) as AttemptResult,
      code: (await codeFile.exists()) ? await codeFile.text() : "",
    });
  }
  attempts.sort((a, b) => a.k - b.k);
  return { run_id: runId, summary, strategy: strategyName(summary.strategy, runId), attempts };
}

/** Every finished run (status other than "running"). */
export async function loadRuns(runsDir = config.runsDir): Promise<Run[]> {
  const entries = await readdir(runsDir, { withFileTypes: true }).catch(() => []);
  const runs: Run[] = [];
  for (const d of entries.filter((e) => e.isDirectory()).sort((a, b) => a.name.localeCompare(b.name))) {
    if (!(await Bun.file(join(runsDir, d.name, "summary.json")).exists())) continue;
    const run = await loadRun(runsDir, d.name);
    if (run.summary.status !== "running" && run.attempts.length) runs.push(run);
  }
  return runs;
}

/** Why an attempt failed, in plain words (null if it succeeded). */
export function failureReason(r: AttemptResult): string | null {
  if (r.success) return null;
  if (r.bin_knocked_over) return "knocked the bin over";
  if (r.error) return `crashed (${r.error.trim().split("\n").at(-1)})`;
  if (!r.lifted) return "never lifted the item";
  if (r.dropped) return "dropped the item on the floor";
  return "missed the bin or bounced out";
}
