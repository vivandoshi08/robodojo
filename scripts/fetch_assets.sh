#!/usr/bin/env bash
# Fetches the Franka Panda model (meshes + MJCF) from MuJoCo Menagerie into assets/menagerie/.
set -euo pipefail
cd "$(dirname "$0")/.."
COMMIT=c96a32d28fb5da84da38c1da4d749e7a13212855   # tested revision
DEST=assets/menagerie
if [ -f "$DEST/franka_emika_panda/panda.xml" ]; then echo "Panda assets already present"; exit 0; fi
mkdir -p assets
git clone -q --filter=blob:none --sparse https://github.com/google-deepmind/mujoco_menagerie "$DEST"
git -C "$DEST" sparse-checkout set franka_emika_panda
git -C "$DEST" checkout -q "$COMMIT" 2>/dev/null || echo "note: pinned commit unavailable, using latest"
echo "Panda assets ready in $DEST/franka_emika_panda"
