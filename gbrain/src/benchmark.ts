// runs/ history → skills/trash-to-bin/skillopt-benchmark.jsonl.
//
// skillopt can't run the simulator: it gives the skill a task, gets code
// back, and an LLM judge grades that text. So each task + seed from past
// races becomes one benchmark task, judged against code that actually got
// the item into the bin and the reasons other attempts failed.

import { join } from "node:path";
import { config, skillDir } from "./config.ts";
import { failureReason } from "./runs.ts";
import type { Attempt, BenchmarkTask, Run } from "./types.ts";

const truncate = (code: string, lines = 40) => code.trimEnd().split("\n").slice(0, lines).join("\n");

/** Robot API doc and task texts, read from robot_race/ so prompts match what agents see. */
async function readRobotRace(): Promise<{ apiDoc: string; taskText: Record<string, string> }> {
  const iface = await Bun.file(join(config.repoRoot, "robot_race/interfaces.py")).text();
  const apiDoc = iface.match(/API_DOC = """([\s\S]*?)"""/)?.[1]?.trim();
  if (!apiDoc) throw new Error("API_DOC not found in robot_race/interfaces.py");
  const tasks = await Bun.file(join(config.repoRoot, "robot_race/tasks.py")).text();
  const taskText: Record<string, string> = {};
  for (const m of tasks.matchAll(/"(\w+)":\s*dict\([^)]*?text="([^"]+)"/g)) taskText[m[1]!] = m[2]!;
  return { apiDoc, taskText };
}

export async function buildBenchmark(runs: Run[]): Promise<{ tasks: BenchmarkTask[]; warning?: string }> {
  const { apiDoc, taskText } = await readRobotRace();
  const attempts = runs.flatMap((r) => r.attempts);
  const setups = [...new Set(attempts.map((a) => `${a.result.task}|${a.result.seed}`))].sort();

  const tasks: BenchmarkTask[] = [];
  for (const setup of setups) {
    const [task, seedStr] = setup.split("|") as [string, string];
    const seed = Number(seedStr);
    const here = attempts.filter((a) => a.result.task === task && a.result.seed === seed);
    const wins = here.filter((a) => a.result.success);
    // Prefer code that solved this exact setup; fall back to the same task.
    const examples: Attempt[] = (wins.length ? wins : attempts.filter((a) => a.result.task === task && a.result.success)).slice(0, 2);
    if (examples.length === 0) continue;
    const misses = [...new Set(here.map((a) => failureReason(a.result)).filter(Boolean))] as string[];

    tasks.push({
      task_id: `${task}-s${seed}`,
      task: [
        taskText[task] ?? task,
        `Task ${task}, seed ${seed}. Read the item and bin positions with robot.get_state().`,
        "",
        apiDoc,
        "",
        "Reply with one ```python block that defines run(robot). Only `robot`, `np` and `math` are available.",
      ].join("\n"),
      judge: {
        kind: "llm",
        rubric: [
          "Grade Python code for a simulated robot arm (you cannot run it): would it get the item into the bin?",
          "",
          `Code that succeeded on ${task} in past races:`,
          ...examples.map((a) => `\`\`\`python\n${truncate(a.code)}\n\`\`\``),
          ...(misses.length ? ["", `Past attempts at this setup failed because they: ${misses.join("; ")}.`] : []),
          "",
          "1.0: one run(robot) using only the documented API, doing what made the successful code work and avoiding the listed failures.",
          "0.5: valid and plausible, but misses one thing the successful code handles.",
          "0.0: no run(robot), invented API calls, or repeats a listed failure.",
        ].join("\n"),
      },
    });
  }

  const warning =
    tasks.length < 15
      ? `Only ${tasks.length} tasks; skillopt --split ${config.skillopt.split} needs at least 15. Race more seeds.`
      : undefined;
  return { tasks, warning };
}

export async function writeBenchmark(tasks: BenchmarkTask[]): Promise<string> {
  const path = join(skillDir, "skillopt-benchmark.jsonl");
  await Bun.write(path, tasks.map((t) => JSON.stringify(t)).join("\n") + "\n");
  return path;
}
