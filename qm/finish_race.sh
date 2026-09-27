#!/usr/bin/env bash
# Host side, after a QM race: pull the sandboxes' runs, close the race if the root didn't, and distill memory (GBrain skill) from all finished runs.
#   bash qm/finish_race.sh <race_id>
set -euo pipefail
cd "$(dirname "$0")/.."
RACE="${1:?usage: bash qm/finish_race.sh <race_id>}"
bash qm/collect_runs.sh
mkdir -p races
# Workers record attempts live. If the tracker was down, backfill with (only this race's containers!):
#   (cd racetrack && uv run python ingest.py ../runs/qm/<container> --race <race_id>)
uv run python -m racetrack.client leaderboard --race-id "$RACE" | grep -q '"final": true' \
  || uv run python -m racetrack.client close --race-id "$RACE" > "races/$RACE.close.json" || true
uv run python -c "from robot_race.memory_hooks import post_race; import json; print(json.dumps(post_race('runs/qm')))" \
  || echo "memory distill skipped"
echo "done: race $RACE  (dashboard: \${RACETRACK_URL:-http://localhost:8000}/)"
