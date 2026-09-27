// Referee → GBrain: turn a finished race into a verdict page, with the
// Memorable episodes linked as evidence, and connect it to strategy pages.

import { config } from "./config.ts";
import type { GBrain } from "./gbrain.ts";
import { describeBin, scenarioOf, slugify } from "./scenario.ts";
import { type AgentResult, type Episode, isSuccess, PARAM_KEYS, type RaceResult } from "./types.ts";

export const raceSlug = (raceId: string) => `races/${slugify(raceId)}`;
export const strategySlug = (strategy: string) => `strategies/${slugify(strategy)}`;

/** Standings from a full set of race episodes (used when the referee sends none). */
export function standingsFrom(episodes: Episode[]): AgentResult[] {
  const byAgent = new Map<string, AgentResult>();
  for (const e of episodes) {
    const a = byAgent.get(e.agent_id) ?? {
      agent_id: e.agent_id,
      strategy: e.strategy,
      total_score: 0,
      attempts: 0,
      successes: 0,
    };
    a.total_score += e.score;
    a.attempts += 1;
    if (isSuccess(e)) a.successes += 1;
    byAgent.set(e.agent_id, a);
  }
  return sortStandings([...byAgent.values()]);
}

const sortStandings = (agents: AgentResult[]) =>
  [...agents].sort((a, b) => b.total_score - a.total_score || b.successes - a.successes);

const paramCell = (e: Episode) =>
  PARAM_KEYS.map((k) => e.params[k].toFixed(config.precision[k])).join(" / ");

function episodeRow(e: Episode): string {
  const s = scenarioOf(e);
  return `| ${e.agent_id} #${e.attempt} | ${e.trash_type} | ${describeBin(s)} | ${paramCell(e)} | ${e.outcome} | ${e.reason} | \`memorable://${e.race_id}/${e.episode_id}\` |`;
}

/** Most common one-line reasons, so the page explains the result in the agents' own words. */
function topReasons(episodes: Episode[], n = 3): string[] {
  const counts = new Map<string, number>();
  for (const e of episodes) if (e.reason) counts.set(e.reason, (counts.get(e.reason) ?? 0) + 1);
  return [...counts.entries()]
    .sort((a, b) => b[1] - a[1])
    .slice(0, n)
    .map(([r, c]) => (c > 1 ? `${r} (×${c})` : r));
}

export function renderVerdict(result: RaceResult, evidence: Episode[]): string {
  const standings = sortStandings(result.agents);
  const winner = standings[0];
  if (!winner) throw new Error(`Race ${result.race_id} has no agents.`);

  const wins = evidence.filter((e) => e.agent_id === winner.agent_id && isSuccess(e));
  const losses = evidence.filter((e) => e.agent_id !== winner.agent_id && !isSuccess(e));
  const lossesShown = standings.slice(1).flatMap((a) =>
    losses.filter((e) => e.agent_id === a.agent_id).slice(0, config.evidencePerSide),
  );
  const header = "| Attempt | Trash | Bin | Height m / Velocity m/s / Grasp ° | Outcome | Agent's reason | Episode |\n|---|---|---|---|---|---|---|";

  return `---
type: race
race_id: ${result.race_id}
date: ${result.finished_at}
winner_agent: ${winner.agent_id}
winner_strategy: ${winner.strategy}
---

# ${result.race_id}: ${winner.strategy} wins

${result.summary ?? `**${winner.agent_id}** won with ${winner.total_score} points (${winner.successes}/${winner.attempts} in the bin), running [[${strategySlug(winner.strategy)}]].`}

## Standings

| # | Agent | Strategy | Score | In bin |
|---|---|---|---|---|
${standings.map((a, i) => `| ${i + 1} | ${a.agent_id} | [[${strategySlug(a.strategy)}]] | ${a.total_score} | ${a.successes}/${a.attempts} |`).join("\n")}

## Why it won

What worked for the winner:
${topReasons(wins).map((r) => `- ${r}`).join("\n") || "- (no reasons recorded)"}

What went wrong for the others:
${topReasons(losses).map((r) => `- ${r}`).join("\n") || "- (no reasons recorded)"}

## Evidence: winner's successes

${header}
${wins.slice(0, config.evidencePerSide).map(episodeRow).join("\n")}

## Evidence: other agents' failures

${header}
${lossesShown.map(episodeRow).join("\n")}

Evidence comes from Memorable \`race_episodes(${result.race_id})\`. The full episode table feeds \`gbrain skillopt ${slugify(winner.strategy)}\`.
`;
}

function strategyStub(strategy: string): string {
  return `---
type: strategy
name: ${strategy}
skill: skills/${slugify(strategy)}/SKILL.md
---

# Strategy: ${strategy}

Race results are linked here from the referee's verdict pages. The playbook agents load before a race is \`skills/${slugify(strategy)}/SKILL.md\`.
`;
}

/** Write races/<id>, then link it to every strategy that raced and log the win. */
export async function publishVerdict(g: GBrain, result: RaceResult, evidence: Episode[]): Promise<string> {
  const slug = raceSlug(result.race_id);
  const standings = sortStandings(result.agents);
  const winner = standings[0]!;

  await g.put(slug, renderVerdict(result, evidence));

  const strategies = [...new Set(standings.map((a) => a.strategy))];
  for (const s of strategies) {
    await g.ensurePage(strategySlug(s), strategyStub(s));
    await g.link(slug, strategySlug(s), s === winner.strategy ? "won_by" : "lost_by");
  }
  await g.tag(slug, `winner-${slugify(winner.strategy)}`);
  await g.timelineAdd(
    strategySlug(winner.strategy),
    result.finished_at.slice(0, 10),
    `Won ${result.race_id} with ${winner.total_score} points (${winner.successes}/${winner.attempts} in the bin)`,
  );
  return slug;
}
