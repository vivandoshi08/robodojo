#!/usr/bin/env bash
# Starts QM's local dev instance (web UI, Postgres in Docker, sandboxes from robodojo-qm-sandbox).
#   bash qm/start_qm.sh          # up (prints the web URL)
#   bash qm/start_qm.sh status   # or: down | doctor | logs
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
QM_DIR="$(cd "${QM_DIR:-$HERE/../../qm}" && pwd)"
export QM_DEV_ENV="$HERE/dev.env"
if ! grep -q '^ANTHROPIC_API_KEY=sk-' "$QM_DEV_ENV" 2>/dev/null; then
  echo "put your key in $QM_DEV_ENV (ANTHROPIC_API_KEY=sk-ant-...)" >&2
  exit 1
fi
docker image inspect robodojo-qm-sandbox:latest >/dev/null 2>&1 \
  || { echo "missing robodojo-qm-sandbox:latest: bash qm/build_qm_base.sh && bash qm/build_sandbox.sh" >&2; exit 1; }
cd "$QM_DIR"
case "${1:-up}" in
  up) exec npm run dev-instance:web ;;
  *)  exec npm run "dev-instance:$1" ;;
esac
