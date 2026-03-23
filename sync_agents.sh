#!/usr/bin/env bash
# Rebuild agent Docker images and restart the Celery worker to pick up changes.
# Run this after adding, removing, or editing files in agents/ or skills/.
#
# Usage:
#   ./sync_agents.sh              # incremental (only changed agents)
#   ./sync_agents.sh --force      # force rebuild all images

set -euo pipefail

FORCE_FLAG=""
if [[ "${1:-}" == "--force" ]]; then
    FORCE_FLAG="--force-all"
    echo "Force rebuilding all agent images..."
else
    echo "Rebuilding changed agent images..."
fi

# Build/rebuild agent Docker images
docker compose exec web python manage.py sync_images $FORCE_FLAG

# Restart Celery worker so it picks up any code changes
docker compose restart celery

echo "Done. Agents are ready."
