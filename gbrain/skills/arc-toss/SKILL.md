---
name: arc-toss
version: 0.1.0
description: Pick release height, toss velocity and grasp angle for one robot trash toss using a high-arc strategy. Loaded by an agent before each race; tuned between races by gbrain skillopt using Memorable episode data.
triggers:
  - "choose toss parameters"
  - "arc toss"
mutating: false
---

# arc-toss — High-Arc Trash Toss

## Contract

Input: one attempt — the trash type, where the bin is (distance and angle), and any recent misses recalled from Memorable for this situation.

Output: exactly one set of toss parameters and a one-line reason, in the format below. Nothing else is executed, so the numbers must be final.

## Phases

1. **Classify the trash.** Light and draggy items (paper, plastic bags, foam) lose speed fast and drift; dense items (cans, glass, bottles with liquid) fly true but bounce off the rim.
2. **Start from distance.** Farther bins need more velocity; the high arc means height matters more than velocity for dense items.
3. **Correct for angle.** Rotate grasp toward the bin; wider angles need a few extra degrees because the release is off-axis.
4. **Learn from misses.** If a recalled miss was short, raise velocity; long, lower it; rim-out, raise height for a steeper drop; wide, adjust grasp angle toward the bin. Never repeat a combination that already missed.
5. **Commit.** Output the parameters at the stated precision.

## Output Format

End the answer with exactly these two lines:

```
PARAMS release_height_m=<2 decimals> toss_velocity_mps=<1 decimal> grasp_angle_deg=<integer>
REASON: <one line naming the factor that decided the throw>
```

Example:

```
PARAMS release_height_m=0.95 toss_velocity_mps=2.8 grasp_angle_deg=-18
REASON: Can at 2.0 m left; last try rimmed out, so higher release for a steeper drop.
```

## Anti-Patterns

- Giving ranges ("2.5–3.0") instead of one number per parameter.
- Extra decimals or units inside the PARAMS line.
- Ignoring recalled misses and repeating the same throw.
- Long explanations: keep the whole answer under 800 characters.
