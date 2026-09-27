#!/usr/bin/env bash
# Copies /root/runs out of every local QM sandbox container into ./runs/qm/<container>/
# so the website and replay.py can see race attempts. Local (Docker) backend only.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p runs/qm
n=0
for c in $(docker ps -a --filter "label=qm.sandbox=1" --format '{{.Names}}'); do
  mkdir -p "runs/qm/$c"
  if docker cp "$c:/root/runs/." "runs/qm/$c/" 2>/dev/null; then
    n=$((n + 1)); echo "collected $c"
  else
    rmdir "runs/qm/$c" 2>/dev/null || true
  fi
done
echo "$n sandbox(es) collected into runs/qm/"
