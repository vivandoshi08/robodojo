---
name: trash-to-bin
version: 0.1.0
description: Procedural memory for the robot race. Plain-language rules about which strategy works for which trash and bin position, distilled from every finished race in runs/. Future agents get this before a race so they start from proven skills. Improved by gbrain skillopt after races.
triggers:
  - "put trash in the bin"
  - "robot race"
mutating: false
---

# trash-to-bin — What Past Races Proved

## How to use this

Before writing `run(robot)`, find the rules for your item (and bin distance, if listed) below. Start from the approach that has proven reliable there, and avoid the ways other attempts failed. If your item isn't listed yet, no race has covered it.

## Output Format

One ```python block that defines `run(robot)`, using only `robot`, `np` and `math`.

## Proven rules

<!-- distilled:start -->
_Distilled from 14 finished runs in runs/ on 2026-09-27._

### can

**Use Your strategy in this race: Fused-vision settled release ...** for can: it solved 1 of 1.

- **Your strategy in this race: Fused-vision settled release ...**: solved 1 of 1 run, in 1.0 tries on average (16.8 s)
- **Your strategy in this race: Top-only low release (agent-3)**: solved 1 of 1 run, in 1.0 tries on average (16.3 s)
- **Your strategy in this race: Fused-vision mid grasp (agent-2)**: solved 1 of 1 run, in 2.0 tries on average (11.7 s). Failed by: crashed (robot_race.tasks.TimeLimit: episode exceeded 40 s of sim time)
- **Your strategy in this race: Top-cam rim drop (agent-1)**: solved 1 of 1 run, in 3.0 tries on average (25.8 s). Failed by: missed the bin or bounced out, never lifted the item
- **no-strategy**: solved 3 of 6 runs, in 2.0 tries on average (17.5 s). Failed by: missed the bin or bounced out (×7), never lifted the item (×4), crashed (robot_race.tasks.TimeLimit: episode exceeded 40 s of sim time) (×2), crashed (no python code block) (×2), dropped the item on the floor, crashed (RuntimeError: Could not find red can in top camera image), crashed (RuntimeError: Red can not found in top camera view)
- **Your strategy in this race: Refined deep-bin placement (a...**: solved 0 of 1 run. Failed by: dropped the item on the floor, crashed (robot_race.tasks.TimeLimit: episode exceeded 40 s of sim time), never lifted the item
- **Your strategy in this race: Rim drop settled (agent-1)**: solved 0 of 1 run. Failed by: never lifted the item (×3)
- **Your strategy in this race: Fused-vision fast mid-can (ag...**: solved 0 of 1 run. Failed by: never lifted the item (×3)
- **Your strategy in this race: Height-filtered depth fusion ...**: solved 0 of 1 run. Failed by: missed the bin or bounced out (×2), never lifted the item
<!-- distilled:end -->
