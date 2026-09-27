// Generates fixtures/episodes.json: fake Memorable episodes so the GBrain
// side can be built and demoed before the real race runs. Deterministic.

let seed = 42;
const rand = () => ((seed = (seed * 1664525 + 1013904223) % 2 ** 32) / 2 ** 32);
const pick = <T>(xs: readonly T[]) => xs[Math.floor(rand() * xs.length)]!;
const round = (v: number, p: number) => Number(v.toFixed(p));

const trash = ["can", "plastic bottle", "paper ball", "glass jar", "foam cup"] as const;
const distances = [1.0, 1.5, 2.0, 2.5];
const angles = [-30, -15, 0, 15, 30];
const agents = [
  { agent_id: "agent-1", strategy: "arc-toss", skill: 0.75 },
  { agent_id: "agent-2", strategy: "flat-throw", skill: 0.5 },
  { agent_id: "agent-3", strategy: "flat-throw", skill: 0.4 },
];
const missReasons = {
  short: "Underestimated drag; throw fell short.",
  long: "Too much velocity for a dense item; overshot.",
  wide: "Grasp angle not rotated enough toward the bin.",
  rim_out: "Flat trajectory bounced off the rim.",
} as const;

const rows = [];
let id = 0;
for (const race_id of ["race-001", "race-002", "race-003"]) {
  for (const a of agents) {
    for (let attempt = 1; attempt <= 12; attempt++) {
      const trash_type = pick(trash);
      const bin = { distance_m: pick(distances), angle_deg: pick(angles) };
      // "Ideal" throw for this scenario, plus agent-dependent noise.
      const ideal = {
        release_height_m: 0.7 + bin.distance_m * 0.1,
        toss_velocity_mps: 1.4 + bin.distance_m * 0.7 + (trash_type === "paper ball" || trash_type === "foam cup" ? 0.4 : 0),
        grasp_angle_deg: bin.angle_deg * 1.1,
      };
      const noise = 1 - a.skill;
      const params = {
        release_height_m: round(ideal.release_height_m + (rand() - 0.5) * 0.3 * noise, 2),
        toss_velocity_mps: round(ideal.toss_velocity_mps + (rand() - 0.5) * 1.2 * noise, 1),
        grasp_angle_deg: Math.round(ideal.grasp_angle_deg + (rand() - 0.5) * 16 * noise),
      };
      const inBin = rand() < a.skill;
      const outcome = inBin ? "in_bin" : pick(Object.keys(missReasons) as (keyof typeof missReasons)[]);
      rows.push({
        episode_id: `ep-${++id}`,
        race_id,
        agent_id: a.agent_id,
        strategy: a.strategy,
        attempt,
        trash_type,
        bin_distance_m: bin.distance_m,
        bin_angle_deg: bin.angle_deg,
        ...params,
        outcome,
        score: inBin ? 10 : 0,
        reason: inBin
          ? pick(["High arc dropped it straight in.", "Matched velocity to distance.", "Adjusted after last miss."])
          : missReasons[outcome as keyof typeof missReasons],
      });
    }
  }
}

await Bun.write("fixtures/episodes.json", JSON.stringify(rows, null, 2) + "\n");
console.log(`Wrote ${rows.length} episodes to fixtures/episodes.json`);

export {};
