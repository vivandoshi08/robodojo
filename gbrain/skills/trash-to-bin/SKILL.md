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
_No races distilled yet. Run `bun src/cli.ts distill`._
<!-- distilled:end -->
