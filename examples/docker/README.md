# Docker Compose example

A complete deployment on one machine: the celery-manager dashboard (`web`), a
Celery `worker`, exactly one `beat` with the stamping scheduler, the
`events` listener (also evaluating alert rules), PostgreSQL for
celery-manager's tables and Redis as the broker. A one-shot `migrate` service
applies schema migrations before anything else starts, so the long-running
processes run with `auto_migrate=False`.

```shell
# from the repository root
docker compose -f examples/docker/compose.yaml up --build -d
docker compose -f examples/docker/compose.yaml run --rm migrate \
    sh -c 'celery-manager create-user admin --role admin --db-url "$CM_DB_URL"'
open http://localhost:8000/
```

`project.py` is the whole application: the Celery app (with the settings
`celery-manager doctor` recommends), two tracked periodic tasks, and the
`CeleryManagerConfig` every process shares. The image installs the
celery-manager source from this checkout; in your own project use
`pip install "celery-manager[redis,standalone]"` instead.

Before using it beyond your own machine: change `CM_SECRET_KEY` and the
database password (use Docker secrets or your platform's secret store), put the
dashboard behind HTTPS and remove `CM_COOKIE_SECURE: "false"`, and keep the web
port bound to localhost or a private network.

`docker compose ... down -v` removes everything, including the database volume.
