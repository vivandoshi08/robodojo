# Integrity: what is fixed, what the model controls, how to check

Claim: an LLM looks at the simulated scene and writes code, and that code alone moves the robot.
The only fixed parts are the environment. Nothing about how to solve a task is built into the agent path.

## Fixed environment (allowed)

| Part | Where |
|---|---|
| Scene: Panda, table, bin, item, cameras | `robot_race/scene.py` |
| Tasks: item, task text, bin position, seed randomization | `robot_race/tasks.py` `TASKS`, `Env.__init__` |
| Physics, episode limit, scoring (`success`, `lifted`, `dropped`, metrics) | `robot_race/tasks.py` `Env.step/settle/result` |
| Robot API and how it is implemented (IK, speed clamp, workspace clamp, gripper timing) | `robot_race/interfaces.py` `API_DOC`, `robot_race/robot.py` `SimRobot` |
| Reset: home pose, gripper open, item settles before t=0 | `Env.__init__` |
| After `run()` returns: 1 s of passive physics with the last command held, then scoring | `executor.worker` (`env.settle(1.0)`) |
| Prompt frame: role line, `API_DOC` (facts: frame, units, what each call does, gripper-yaw convention), output-format rules | `agent.SYSTEM_ROLE`, `API_DOC`, `agent.OUTPUT_RULES` |
| What the model sees: task text, `get_state()`, front and top images, and after each attempt the measured result JSON, its own last 12 calls, and 4 key frames | `agent.first_turn`, `agent.feedback_turn` |

## What the model controls

Everything that moves the robot. The executor (`robot_race/executor.py` `worker`) compiles `policy.py` with
only `robot`, `np` and `math` in scope, then calls `run(robot)`. It adds no other calls: no fallback policy,
no retries that edit code, and no default motion. If the reply contains no code block, nothing runs
(`error: "no python code block"`). API retries resend the same request unchanged.

## Opt-in hints (off by default, always recorded)

| Flag | Effect | Recorded |
|---|---|---|
| `--example` | hand-written `policies/reference_pick_and_drop.py` goes into the first user turn | `summary.json` `hints.example` |
| `--strategy "<text>"` | strategy card added to the end of the system prompt | `hints.strategy` |
| `--context-file` | recalled memory added before the task in the first turn | `hints.context` |

When any hint is used, `hinted: true` appears in `summary.json` and in `runs/index.json`, and a banner
("Hint given: reference example in prompt", and so on) appears on `trace.html`, `<run>/index.html` and the
home card.

## How each claim is verified

| Claim | File and field |
|---|---|
| A real model answered (not a scripted client) | `summary.json` `scripted == false`; `transcript/turn_<n>/response.json` `id`, `model`, `usage` |
| No solution hints reached the model | `summary.json` `hints` all off, `hinted == false`; `transcript/turn_<n>/request.json` has the full text sent |
| The code that ran is the code the model wrote | `attempt_<k>/provenance.json` `code_matches_response == true` (`policy_sha256 == response_code_sha256`); the executor refuses a mismatch (`refused: true`, `error: "code does not match model reply..."`) |
| The images the model saw are the ones on disk | `transcript.jsonl` `image_sha256` vs `transcript/turn_<n>/img_<i>.png`; `observation/*.png` |
| The robot did what the code says | `attempt_<k>/calls.json` (every API call, with sim times and TCP before and after) vs `policy.py`; `trajectory.npz`; `attempt.mp4` |
| The feedback held measured results only | turn n+1's newest user message in `request.json`: result JSON (keys = `agent.FEEDBACK_KEYS` + `calls_tail`), key frames, "Revise run(robot)." |
| Not a hand-written policy run | `runs/index.json` `kind == "agent"` (`run_policy.py` runs are `kind: "policy"` and get a "HAND-WRITTEN POLICY" banner) |

A run can be publicly described as "the model did it" only when all three hold:
`kind == "agent"`, `scripted == false`, and `hinted == false`.

## Auditing a run by hand

```bash
R=runs/<run_id>
jq '{model, scripted, hinted, hints, status, solved_at}' $R/summary.json
# 1. what was sent: system prompt + first turn (no reference code, no strategy card unless hinted)
jq -r '.system[0].text' $R/transcript/turn_1/request.json
jq -r '.messages[0].content[] | select(.type=="text") | .text' $R/transcript/turn_1/request.json
grep -c "descend so fingertips" $R/transcript/*/request.json   # reference-policy line: expect 0 unless hints.example
# 2. what came back, and whether the code that ran matches it byte for byte
jq -r '.content[0].text' $R/transcript/turn_1/response.json
for a in $R/attempt_*; do jq '{code_matches_response, refused, policy_sha256}' $a/provenance.json;
  shasum -a 256 $a/policy.py; done
# 3. what the robot did: calls.json against policy.py, then watch attempt.mp4 (video time == sim time)
jq -c '.calls[] | {i, call, args, t_start}' $R/attempt_1/calls.json | head
```

`python scripts/trace_demo.py` runs the whole chain with a scripted client. It writes to `runs_demo/`
(never `runs/`), sets `scripted: true`, and its pages carry the "SCRIPTED DEMO, NOT A MODEL" banner.
`tests/test_integrity.py` checks every item on this page.

## Known limit

Policy code receives the `SimRobot` object, so it could reach internals such as `robot.env` or `robot.d`
instead of using the API. That code would still be written by the model, but it would bypass the API.
`calls.json` records only API calls. Review `policy.py` for any attribute access beyond the calls listed in
`API_DOC`.
