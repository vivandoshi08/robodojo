# robodojo
## Setup

Requires [uv](https://docs.astral.sh/uv/).

```sh
uv sync                        # install mujoco + deps
uv run pytest                  # run MuJoCo smoke test
uv run python sim_check.py     # drop a box, print its height
```

## GBrain (agent knowledge + skill improvement)

`gbrain/` holds the GBrain side: pre-race briefings, the referee's verdict pages, and `gbrain skillopt` on the winning strategy's `SKILL.md`, fed by Memorable episode data. Requires [Bun](https://bun.sh). See [gbrain/README.md](gbrain/README.md).

```sh
cd gbrain && bun install
bun run fixture && bun src/cli.ts after-race race-002 --print   # demo on fake data
```
