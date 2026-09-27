# robodojo
## Setup

Requires [uv](https://docs.astral.sh/uv/).

```sh
uv sync                        # install mujoco + deps
uv run pytest                  # run MuJoCo smoke test
uv run python sim_check.py     # drop a box, print its height
```

## GBrain (agent knowledge + skill improvement)

`gbrain/` distills finished races in `runs/` into a plain-language skill in GBrain that future agents start from, and improves it with `gbrain skillopt`. Requires [Bun](https://bun.sh). See [gbrain/README.md](gbrain/README.md).

```sh
cd gbrain && bun install
bun run fixture && RACE_RUNS_DIR=fixtures/runs bun src/cli.ts distill --print   # fake data
```
