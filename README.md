# robodojo
## Setup

Requires [uv](https://docs.astral.sh/uv/).

```sh
uv sync                        # install mujoco + deps
uv run pytest                  # run MuJoCo smoke test
uv run python sim_check.py     # drop a box, print its height
```
