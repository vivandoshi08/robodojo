// Writes a FAKE runs/ tree in the CLAUDE.md §8 layout (summary.json,
// attempt_<k>/result.json + policy.py) to try the GBrain side before real
// races exist. Strategy names and success rates are made up. Output: fixtures/runs/.
//
//   bun run fixture && RACE_RUNS_DIR=fixtures/runs bun src/cli.ts distill --print

import { mkdir, rm } from "node:fs/promises";
import { join } from "node:path";

const OUT = "fixtures/runs";
let seed = 7;
const rand = () => (seed = (seed * 1664525 + 1013904223) % 2 ** 32) / 2 ** 32;

const tasks = ["paper_to_bin", "bottle_to_bin", "can_to_bin"];
const strategies = ["pick-and-place", "drop", "toss", "push-off-edge"];
// Made-up chance each strategy solves an attempt, per item.
const skill: Record<string, Record<string, number>> = {
  paper_to_bin: { "pick-and-place": 0.4, drop: 0.3, toss: 0.7, "push-off-edge": 0.2 },
  bottle_to_bin: { "pick-and-place": 0.6, drop: 0.7, toss: 0.2, "push-off-edge": 0.3 },
  can_to_bin: { "pick-and-place": 0.7, drop: 0.6, toss: 0.4, "push-off-edge": 0.5 },
};
const code = (s: string) => `def run(robot):\n    s = robot.get_state()\n    # (fake ${s} policy)\n    robot.open_gripper()\n`;
const json = (path: string, data: unknown) => Bun.write(path, JSON.stringify(data, null, 2) + "\n");

await rm(OUT, { recursive: true, force: true });
let n = 0;
for (const task of tasks) {
  for (const s of [0, 1, 2, 3, 4]) {
    const bin_center = [0.3 + rand() * 0.3, 0.2 + rand() * 0.4, 0]; // randomized bin, per the pitch
    const far = Math.hypot(bin_center[0]!, bin_center[1]!) >= 0.5;
    const stamp = `20260927-15${String(n++).padStart(2, "0")}00`;
    for (const strategy of strategies) {
      const run_id = `${stamp}-${task}-s${s}-${strategy}`;
      let p = skill[task]![strategy]!;
      if (strategy === "toss") p += far ? 0.2 : -0.3; // toss better far away
      if (strategy === "pick-and-place") p += far ? -0.3 : 0.2; // place better close
      let solved_at: number | null = null;
      for (let k = 1; k <= 5 && solved_at === null; k++) {
        const success = rand() < p;
        const lifted = success || rand() < 0.7;
        const result = {
          task, seed: s, success, time_s: Number((5 + rand() * 8).toFixed(2)),
          collisions: Math.floor(rand() * 3), energy_j: Number((20 + rand() * 30).toFixed(2)),
          dropped: !success && lifted && rand() < 0.4, lifted, error: null,
          bin_center, bin_knocked_over: !success && strategy !== "pick-and-place" && rand() < 0.15,
        };
        await mkdir(join(OUT, run_id, `attempt_${k}`), { recursive: true });
        await json(join(OUT, run_id, `attempt_${k}`, "result.json"), result);
        await Bun.write(join(OUT, run_id, `attempt_${k}`, "policy.py"), code(strategy));
        if (success) solved_at = k;
      }
      await json(join(OUT, run_id, "summary.json"), {
        task, seed: s, model: "fake", strategy: `(fake ${strategy} card)`,
        status: solved_at ? "solved" : "failed", solved_at,
      });
    }
  }
}
console.log(`Wrote ${n * strategies.length} fake runs to ${OUT}/`);

export {};
