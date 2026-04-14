#!/bin/bash
# docker_prod.sh - Production Docker management for FuzzyClaw
# Usage: ./docker_prod.sh <command> [options]
#
# Commands:
#   deploy [--no-cache]  - Full deployment (build, up, migrate, static, sync agents)
#   logs <service>       - Follow logs for a service
#   celery-logs          - Shortcut for celery logs
#   web-logs             - Shortcut for web logs
#   fuzzy-logs           - Shortcut for fuzzy logs
#   shell                - Django shell in web container
#   bash                 - Bash shell in web container
#   sync-agents          - Rebuild agent images + restart celery
#   <any>                - Pass through to docker compose

set -e

COMPOSE_FILE="docker-compose.prod.yml"
ENV_FILE=".env"

COMPOSE="docker compose -f ${COMPOSE_FILE} --env-file ${ENV_FILE}"

case "$1" in
    deploy)
        echo "=== Deploying FuzzyClaw ==="

        if [ "$2" == "--no-cache" ]; then
            echo "Building with --no-cache..."
            $COMPOSE build --no-cache
        else
            echo "Building containers..."
            $COMPOSE build
        fi

        echo "Starting containers..."
        $COMPOSE up -d

        echo "Waiting for database to be ready..."
        sleep 5

        echo "Running migrations..."
        $COMPOSE exec web python manage.py migrate

        echo "Building agent images..."
        $COMPOSE exec web python manage.py sync_images

        echo "Verifying agents and skills..."
        $COMPOSE exec web python manage.py check_agents || true
        $COMPOSE exec web python manage.py check_skills || true

        echo ""
        echo "=== Deployment complete! ==="
        echo "Check status: ./docker_prod.sh ps"
        echo "View logs:    ./docker_prod.sh logs web"
        ;;

    celery-logs)
        $COMPOSE logs -f celery
        ;;

    web-logs)
        $COMPOSE logs -f web
        ;;

    fuzzy-logs)
        $COMPOSE logs -f fuzzy
        ;;

    logs)
        if [ -z "$2" ]; then
            $COMPOSE logs -f
        else
            $COMPOSE logs -f "$2"
        fi
        ;;

    shell)
        $COMPOSE exec web python manage.py shell
        ;;

    bash)
        $COMPOSE exec web bash
        ;;

    sync-agents)
        echo "=== Rebuilding agent images ==="
        $COMPOSE exec web python manage.py sync_images
        echo "Restarting celery..."
        $COMPOSE restart celery
        echo "=== Done ==="
        ;;

    restart)
        echo "Restarting ${2:-all services}..."
        if [ -z "$2" ]; then
            $COMPOSE restart
        else
            $COMPOSE restart "$2"
        fi
        ;;

    status)
        $COMPOSE ps
        echo ""
        echo "=== Resource Usage ==="
        docker stats --no-stream $($COMPOSE ps -q) 2>/dev/null || true
        ;;

    *)
        $COMPOSE "$@"
        ;;
esac
