#!/usr/bin/env bash
# Start the whole stack (migrate, user, worker, beat, events, dashboard) on this
# machine against one Redis. Ctrl-C stops everything.
#
#   docker run --rm -p 6379:6379 redis:7-alpine        # in another terminal
#   CM_EXAMPLE_BROKER_URL=redis://127.0.0.1:6379/0 ./examples/full_stack/run_local.sh
set -euo pipefail
cd "$(dirname "$0")"
export CM_EXAMPLE_BROKER_URL="${CM_EXAMPLE_BROKER_URL:-redis://127.0.0.1:6379/0}"
export CM_EXAMPLE_DB_URL="${CM_EXAMPLE_DB_URL:-sqlite:///$PWD/full_stack.db}"
export PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}"
PORT="${CM_EXAMPLE_PORT:-8000}"
PASSWORD="${CM_EXAMPLE_ADMIN_PASSWORD:-admin-password}"

celery-manager migrate --db-url "$CM_EXAMPLE_DB_URL"
printf '%s\n' "$PASSWORD" | celery-manager create-user admin --role admin \
    --db-url "$CM_EXAMPLE_DB_URL" --password-stdin || true   # already exists on re-runs

trap 'kill 0' EXIT INT TERM
celery -A project worker --pool=threads -E -n demo@%h --loglevel=INFO &
celery-manager events --config project:config &
python -m uvicorn --factory project:make_dashboard --host 127.0.0.1 --port "$PORT" &
sleep 3   # let the schema and worker settle before beat reads the database
celery -A project beat -S celery_manager.beat:DatabaseScheduler --loglevel=INFO &
echo "Dashboard: http://127.0.0.1:$PORT/   sign in as admin / $PASSWORD"
wait
