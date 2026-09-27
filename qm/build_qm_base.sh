#!/usr/bin/env bash
# Builds QM's local sandbox image (qm-sandbox-local:latest) for THIS machine's arch.
# QM's own `npm run sandbox:local:build` forces linux/amd64; under emulation on Apple Silicon
# MuJoCo dies with SIGILL (exit 132, no AVX). Same steps as QM's script, native platform.
#   QM_DIR=../qm bash qm/build_qm_base.sh
set -euo pipefail
QM_DIR="$(cd "${QM_DIR:-$(dirname "$0")/../../qm}" && pwd)"
case "$(uname -m)" in arm64|aarch64) PLATFORM=linux/arm64 ;; *) PLATFORM=linux/amd64 ;; esac
cd "$QM_DIR"
FINGERPRINT="$(node --input-type=module -e '
const { computeSandboxImageFingerprint } = await import("./src/sandbox/local-sandbox.ts");
console.log(await computeSandboxImageFingerprint(process.cwd()));
')"
echo "==> qm-sandbox-base:dev ($PLATFORM) from $QM_DIR/fly/Dockerfile"
docker build --platform "$PLATFORM" -f fly/Dockerfile -t qm-sandbox-base:dev .
echo "==> qm-sandbox-local:latest (fingerprint $FINGERPRINT)"
docker build --platform "$PLATFORM" -f local/Dockerfile --build-arg BASE=qm-sandbox-base:dev \
  --label "qm.sandbox-fingerprint=$FINGERPRINT" -t qm-sandbox-local:latest .
