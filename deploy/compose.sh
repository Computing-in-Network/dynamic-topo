#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/deploy/docker-compose.prod.yml"
ENV_FILE="$ROOT_DIR/deploy/.env"

action="${1:-}"

if [[ -z "$action" ]]; then
  echo "Usage: deploy/compose.sh {up|down|restart|ps|logs|pull}"
  exit 1
fi

# Workaround for docker-compose v1 Python site-packages conflict.
export PYTHONNOUSERSITE=1

case "$action" in
  up)
    docker-compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" up -d
    ;;
  down)
    docker-compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" down
    ;;
  restart)
    docker-compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" restart
    ;;
  ps)
    docker-compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" ps
    ;;
  logs)
    docker-compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" logs -f --tail=200
    ;;
  pull)
    docker-compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" pull
    ;;
  *)
    echo "Unknown action: $action"
    echo "Usage: deploy/compose.sh {up|down|restart|ps|logs|pull}"
    exit 1
    ;;
esac
