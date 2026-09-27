# Robo Dojo

Race robot strategies against each other, keep what wins.

Four agents spar on the same trash-into-bin task, each locked to a style
(place, drop, toss, push). A judge agent scores every match, Memorable
records every attempt and why it missed, and GBrain distills the winner
into a readable SKILL.md.

## Run it

It's a single static file. Open `index.html` in a browser, or serve it:

```bash
python3 -m http.server 8000
# then visit http://localhost:8000
```

## What's in the page

| Section | Dojo name | Real system |
|---|---|---|
| Four blueprint panels | The mats | One QM sandbox per strategy |
| Sensei's scorecard | Sensei / referee | Judge agent |
| Training journal | Training journal | Memorable |
| The scroll | The scroll | GBrain SKILL.md (skillopt) |
| Belt ranks | Belt rank | Wins across races |

## Current state: simulated

All race results come from `judge()` in the script, a fake probability
model. To go live, replace it with events from the backend
(WebSocket or Server-Sent Events):

```json
{"type":"attempt","round":2,"strategy":"toss","trash":"bottle","dist_cm":62,
 "ok":false,"reason":"Bottle landed inside but bounced back out","time_s":3.4,"bin_knocked":false}
{"type":"verdict","round":2,"winner":"drop","scores":{"toss":41,"drop":78,"place":35,"push":52}}
{"type":"skill_update","version":3,"markdown":"# Throwing trash into a bin\n..."}
```

## Where to edit

- Colors and type: CSS variables at the top of `<style>` (`--riso`, `--bp`, `--paper`).
- Strategies: `STRATS` array.
- Failure reasons and scroll lessons: `REASONS`.
- Success odds per style and item: `BASE`.
- Scoring formula: `score()`.
- Scroll distillation: `distill()`.
