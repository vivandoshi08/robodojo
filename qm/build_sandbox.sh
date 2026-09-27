#!/usr/bin/env bash
# Builds robodojo-qm-sandbox:latest (QM's local sandbox image + MuJoCo + this repo).
# Prereq: qm-sandbox-local:latest exists (in the QM checkout: npm run sandbox:local:build).
set -euo pipefail
cd "$(dirname "$0")/.."
TAG="${ROBODOJO_SANDBOX_IMAGE:-robodojo-qm-sandbox:latest}"
if ! docker image inspect qm-sandbox-local:latest >/dev/null 2>&1; then
  echo "missing qm-sandbox-local:latest: run 'npm run sandbox:local:build' in the QM checkout first" >&2
  exit 1
fi
# QM's base image is linux/amd64; stay on it so the fingerprint label and agent binary match.
DOCKER_BUILDKIT=1 docker build --platform linux/amd64 -f qm/sandbox/Dockerfile -t "$TAG" .
echo "built $TAG  ->  set LOCAL_SANDBOX_IMAGE=$TAG in QM's dev env (see qm/dev.env.example)"
