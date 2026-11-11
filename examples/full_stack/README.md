# Full stack example

Every celery-manager piece running together on one machine: a Celery worker,
beat with the database scheduler, the events listener, the dashboard and an MCP
server for AI agents. `project.py` is the whole application: the Celery app,
the shared `CeleryManagerConfig` and five small tasks:

| Task | Shows |
|---|---|
| `demo.add` | a run that succeeds, with worker and runtime |
| `demo.explode` | a failure with traceback, failure groups, retry |
| `demo.import_rows` | live progress |
| `demo.summarize` | LLM usage (tokens and cost via `llm_prices`) |
| `demo.heartbeat` | a periodic schedule fired by beat (pause, resume, edit) |

This same project is what the end-to-end test (`tests/e2e/`) launches, so it is
checked on every release.

## Run it with one command

You need a Redis. The quickest is Docker:

```shell
docker run --rm -p 6379:6379 redis:7-alpine
```

Then, with celery-manager installed (`pip install --pre "celery-manager[standalone,redis]"`
or `pip install -e '.[dev]'` from a checkout):

```shell
./examples/full_stack/run_local.sh
```

Open <http://127.0.0.1:8000/> and sign in as `admin` / `admin-password`.
Ctrl-C stops everything.

## Run each process yourself

From `examples/full_stack/` with `CM_EXAMPLE_BROKER_URL` set if Redis is not on
`localhost:6379`:

```shell
export PYTHONPATH=$PWD
celery-manager migrate --db-url sqlite:///full_stack.db
celery-manager create-user admin --role admin --db-url sqlite:///full_stack.db
celery -A project worker --pool=threads -E
celery -A project beat -S celery_manager.beat:DatabaseScheduler
celery-manager events --config project:config
uvicorn --factory project:make_dashboard --port 8000
```

## What to look at

- **Tasks**: press Start and run `demo.add` with `[2, 3]`, `demo.explode`,
  `demo.import_rows`, `demo.summarize`. Open a run for its worker, runtime,
  result, progress or LLM calls; retry the failed one.
- **Insights**: failure groups and LLM usage totals.
- **Schedules**: `heartbeat` fires every `CM_EXAMPLE_TICK_SECONDS`; pause and
  resume it, or edit its interval (beat applies it within seconds).
- **Workers** and **Queues**: the worker and the `celery` queue.
- **Alerts**: add a webhook channel and a failure-rate rule.

## Connect an AI agent (MCP)

```json
{
  "mcpServers": {
    "celery-manager": {
      "command": "celery-manager",
      "args": ["mcp", "--config", "project:config", "--role", "viewer"],
      "env": {
        "PYTHONPATH": "/path/to/examples/full_stack",
        "CM_EXAMPLE_DB_URL": "sqlite:////path/to/examples/full_stack/full_stack.db"
      }
    }
  }
}
```

`viewer` is read-only; use `operator` to let the agent retry and start tasks
(bulk retry is a dry run unless the agent confirms the count).

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `CM_EXAMPLE_BROKER_URL` | `redis://127.0.0.1:6379/0` | Celery broker |
| `CM_EXAMPLE_DB_URL` | `sqlite:///full_stack.db` | celery-manager database |
| `CM_EXAMPLE_SECRET` | a local-only value | session signing key; set a private one anywhere else |
| `CM_EXAMPLE_TICK_SECONDS` | `30` | interval of the `heartbeat` schedule |
| `CM_EXAMPLE_PORT` | `8000` | dashboard port for `run_local.sh` |
| `CM_EXAMPLE_ADMIN_PASSWORD` | `admin-password` | admin password for `run_local.sh` |

The defaults are for local use only: plain-HTTP cookies and a known secret.
For production see [Docker Compose example](../docker/README.md) and the
production guide.
