# QM sandbox image with the robodojo sim baked in.
# Every QM scope (and every swarm worker) gets its own container from this image, so each
# racing agent has an isolated MuJoCo + robot API with nothing shared.
#
# Build from the repo root:  bash qm/build_sandbox.sh
# Needs qm-sandbox-local:latest first (in the QM checkout: npm run sandbox:local:build).
ARG BASE=qm-sandbox-local:latest
FROM ${BASE}

# Headless MuJoCo rendering (EGL via Mesa llvmpipe; no GPU in the container).
RUN apt-get update && apt-get install -y --no-install-recommends \
      libegl1 libgl1 libgles2 libgl1-mesa-dri git \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /uvx /usr/local/bin/

ENV MUJOCO_GL=egl \
    PYOPENGL_PLATFORM=egl \
    UV_PROJECT_ENVIRONMENT=/opt/robodojo/.venv \
    RR_VIDEO_FPS=12

WORKDIR /opt/robodojo
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY scripts/fetch_assets.sh scripts/
RUN bash scripts/fetch_assets.sh
COPY robot_race robot_race
COPY policies policies
COPY scripts scripts
COPY run_agent.py run_policy.py ./
COPY qm/skills qm/skills
COPY qm/strategies.json qm/strategies.json

# Smoke check at build time: the reference policy lands the can with no video.
RUN uv run --no-sync python -c "\
from robot_race.tasks import Env; from robot_race.robot import SimRobot; import numpy as np, math; \
env = Env('can_to_bin', 0, record=False); r = SimRobot(env, observation='oracle'); g = {'robot': r, 'np': np, 'math': math}; \
exec(open('policies/reference_pick_and_drop.py').read(), g); g['run'](r); env.settle(1.0); \
res = env.result(); print(res); assert res['success']"

# QM's agent server expects HOME=/root; the per-scope volume mounts there.
WORKDIR /root
