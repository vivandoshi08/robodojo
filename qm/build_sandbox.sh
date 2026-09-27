#!/usr/bin/env bash
# Builds robodojo-qm-sandbox:latest (QM's local sandbox image + MuJoCo + this repo).
# Prereq: qm-sandbox-local:latest exists (bash qm/build_qm_base.sh).
set -euo pipefail
cd "$(dirname "$0")/.."
TAG="${ROBODOJO_SANDBOX_IMAGE:-robodojo-qm-sandbox:latest}"
if ! docker image inspect qm-sandbox-local:latest >/dev/null 2>&1; then
  echo "missing qm-sandbox-local:latest: run 'bash qm/build_qm_base.sh' first" >&2
  exit 1
fi
# Match the base image arch (build it natively with qm/build_qm_base.sh on Apple Silicon).
PLATFORM="$(docker image inspect -f "{{.Os}}/{{.Architecture}}" qm-sandbox-local:latest)"
DOCKER_BUILDKIT=1 docker build --platform "$PLATFORM" -f qm/sandbox/Dockerfile -t "$TAG" .
echo "built $TAG  ->  set LOCAL_SANDBOX_IMAGE=$TAG in QM's dev env (see qm/dev.env.example)"
