// Distill every finished race in runs/ into plain-language rules
// ("paper: toss solved 4/4, drop 1/4") inside GBrain's skill, so future
// agents start from proven skills. Only the section between the markers is
// regenerated; the rest of SKILL.md (including skillopt's edits) is kept.

import { config, skillPath } from "./config.ts";
import { failureReason, itemOf } from "./runs.ts";
import type { Run } from "./types.ts";

const START = "<!-- distilled:start -->";
const END = "<!-- distilled:end -->";

interface Tally {
  runs: number;
  solved: number;
  tries: number[];
  times: number[];
  failures: Map<string, number>;
}

const avg = (xs: number[]) => xs.reduce((a, b) => a + b, 0) / xs.length;

/** Bin distance from the robot base, if the run recorded the bin position. */
function binDistance(run: Run): number | undefined {
  const c = run.attempts[0]?.result.bin_center;
  return c ? Math.hypot(c[0]!, c[1]!) : undefined;
}

function groupLabel(run: Run): string {
  const item = itemOf(run.summary.task);
  const d = binDistance(run);
  if (d === undefined) return item;
  const cm = config.distanceSplitM * 100;
  return `${item}, bin ${d < config.distanceSplitM ? `closer than ${cm} cm` : `${cm} cm or farther`}`;
}

function tally(runs: Run[]): Map<string, Map<string, Tally>> {
  const groups = new Map<string, Map<string, Tally>>();
  for (const run of runs) {
    const label = groupLabel(run);
    const byStrategy = groups.get(label) ?? new Map<string, Tally>();
    const t: Tally = byStrategy.get(run.strategy) ?? { runs: 0, solved: 0, tries: [], times: [], failures: new Map() };
    t.runs += 1;
    const win = run.attempts.find((a) => a.result.success);
    if (win) {
      t.solved += 1;
      t.tries.push(win.k);
      t.times.push(win.result.time_s);
    }
    for (const a of run.attempts) {
      const why = failureReason(a.result);
      if (why) t.failures.set(why, (t.failures.get(why) ?? 0) + 1);
    }
    byStrategy.set(run.strategy, t);
    groups.set(label, byStrategy);
  }
  return groups;
}

function strategyLine(name: string, t: Tally): string {
  const solved = `solved ${t.solved} of ${t.runs} run${t.runs === 1 ? "" : "s"}`;
  const speed = t.solved ? `, in ${avg(t.tries).toFixed(1)} tries on average (${avg(t.times).toFixed(1)} s)` : "";
  const fails = [...t.failures].sort((a, b) => b[1] - a[1]).map(([why, n]) => (n > 1 ? `${why} (×${n})` : why));
  return `- **${name}**: ${solved}${speed}${fails.length ? `. Failed by: ${fails.join(", ")}` : ""}`;
}

export function renderRules(runs: Run[]): string {
  if (runs.length === 0) return "_No finished races in runs/ yet._";
  const sections: string[] = [];
  for (const [label, byStrategy] of [...tally(runs)].sort((a, b) => a[0].localeCompare(b[0]))) {
    // Most reliable first: solve rate, then fewer tries.
    const ordered = [...byStrategy].sort(
      (a, b) =>
        b[1].solved / b[1].runs - a[1].solved / a[1].runs ||
        (a[1].tries.length ? avg(a[1].tries) : 99) - (b[1].tries.length ? avg(b[1].tries) : 99),
    );
    const [bestName, best] = ordered[0]!;
    const headline = best.solved
      ? `**Use ${bestName}** for ${label}: it solved ${best.solved} of ${best.runs}.`
      : `Nothing has solved ${label} yet.`;
    sections.push(`### ${label}\n\n${headline}\n\n${ordered.map(([n, t]) => strategyLine(n, t)).join("\n")}`);
  }
  return `_Distilled from ${runs.length} finished runs in runs/ on ${new Date().toISOString().slice(0, 10)}._\n\n${sections.join("\n\n")}`;
}

/** Replace the distilled section of SKILL.md and return the new file. */
export async function updateSkill(runs: Run[]): Promise<string> {
  const text = await Bun.file(skillPath).text();
  const block = `${START}\n${renderRules(runs)}\n${END}`;
  const next =
    text.includes(START) && text.includes(END)
      ? text.slice(0, text.indexOf(START)) + block + text.slice(text.indexOf(END) + END.length)
      : `${text.trimEnd()}\n\n## Proven rules\n\n${block}\n`;
  await Bun.write(skillPath, next);
  return next;
}
