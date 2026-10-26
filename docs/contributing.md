# Working on celery-manager

Notes for changing celery-manager itself, rather than using it. The one that
catches people is the last section: **the dashboard bundle is committed, so a
frontend change is not done until you rebuild and commit it.**

## Set up

```bash
python -m venv venv
venv/bin/pip install -e ".[dev]"
```

The `dev` extra is `pytest`, `httpx` (needed by FastAPI's `TestClient`),
`ruff`, and `uvicorn`. That is enough to run the whole test suite and the
linter; nothing in `tests/` imports anything outside it.

Two more extras exist and are deliberately separate:

- `docs` — `mkdocs-material`, for building this site.
- `standalone` — `uvicorn`, for *serving* celery-manager rather than mounting
  it into an ASGI app you already run. Not a core dependency, because
  mounting needs no server of its own.

## Tests and linting

```bash
venv/bin/python -m pytest        # testpaths = ["tests"] is set in pyproject.toml
venv/bin/ruff check .
```

The tests run entirely against `sqlite:///:memory:` and an eager or stubbed
Celery app — no broker, no worker, no Redis. There is no `[tool.ruff]`
section in `pyproject.toml`, so ruff runs with whatever defaults (or
user-level config) are in play; pin one there if you want reproducible lint
results across machines.

## Documentation

```bash
venv/bin/pip install -e ".[docs]"
venv/bin/mkdocs serve            # live preview on http://127.0.0.1:8000
venv/bin/mkdocs build --strict   # writes ./site (gitignored)
```

`--strict` turns warnings into failures, but note that a **broken in-page
anchor is reported at `INFO`, not as a warning** — so a build can exit `0`
while a `#some-heading` link is dead. Read the output, don't just check the
exit code.

The house rule for this documentation is that every claim is checked against
real source or real behaviour before it is written. If you document an
endpoint, call it. If you document a UI behaviour, read the component. Prior
review has found stale prose more than once, which is why the pages are
explicit about limitations and failure modes rather than tidy.

## Rebuilding the dashboard bundle

The React dashboard is committed **prebuilt** into
`src/celery_manager/dashboard/static/`, which is what lets an installed celery-manager (the wheel or a git install)
serve a UI with no Node toolchain. `frontend/dist/` is
gitignored; the copy under `src/` is the one that ships.

So a change under `frontend/src/` has no effect on the installed package
until you rebuild and copy:

```bash
cd frontend
npm install                      # first time only
npm run build                    # tsc -b && vite build -> frontend/dist
rm -rf ../src/celery_manager/dashboard/static
cp -r dist ../src/celery_manager/dashboard/static
```

Then commit `src/celery_manager/dashboard/static/` alongside your
`frontend/src/` change. Vite content-hashes the asset filenames, so the diff
will show a new `index-<hash>.js` / `index-<hash>.css` pair and a rewritten
`index.html` — that is expected, and a `frontend/src/` change with no bundle
change in the same commit is the bug to look for.

## Building the distributions

```bash
venv/bin/python -m build         # wheel + sdist into ./dist
```

Check two things in the result:

- The wheel contains `celery_manager/dashboard/static/` with the same asset
  hashes as your working tree — otherwise the installed package serves a
  stale UI.
- The sdist contains only what `[tool.hatch.build.targets.sdist]` allows.
  That list is an explicit allowlist rather than a denylist, because
  hatchling's default ("everything VCS doesn't ignore") happily ships
  whatever working files a tool has dropped in the repo root.

## Schema changes

celery-manager's schema is versioned with Alembic. Revisions live in
`src/celery_manager/db/migrations/versions/` and ship in the wheel, and
`init_db()` applies them at startup (see
[Configuration: schema and upgrades](configuration.md#schema-and-upgrades)).
Every change to `celery_manager.db.models` needs a new revision.
`tests/unit/test_migrations.py::test_models_and_migrations_agree` fails until
it has one.

The repo-root `alembic.ini` is for contributors only. It points at the
in-package migrations and a scratch SQLite database (`alembic-scratch.db`,
gitignored), and puts `src/` first on `sys.path`, so it always uses your
working tree. To add a revision, from the repo root:

```bash
rm -f alembic-scratch.db
venv/bin/alembic upgrade head          # scratch DB at the current latest revision
# ...edit src/celery_manager/db/models.py...
venv/bin/alembic revision --autogenerate --rev-id 0006 -m "add my_column"
venv/bin/alembic upgrade head && venv/bin/alembic check   # applies cleanly, no diff left
```

That writes `versions/0006_add_my_column.py`. Then:

- **Read the generated file.** Autogenerate misses some changes (renames
  become drop + add, and server defaults and some constraint changes
  aren't detected), so fix it by hand where needed. Tidy the
  `# ### commands auto generated` markers.
- **Revision IDs are sequential, four digits** (`0001`, `0002`, ...),
  and `down_revision` is the previous one. If two branches both add the same ID,
  whichever merges second renumbers its revision to the next free ID (file name,
  `revision`, `down_revision`). `test_revision_history_is_linear` fails while
  there are two heads.
- **Keep upgrades safe on a live table.** A new column on an existing table
  should be nullable, or have a `server_default` if it must be `NOT NULL`.
  Leave the `batch_alter_table` blocks that autogenerate writes (`env.py`
  sets `render_as_batch=True`): on SQLite they recreate the table for changes
  `ALTER TABLE` can't make, and on other backends they are plain `ALTER`s.
- **Write a working `downgrade()`.**
- **Never edit a revision that has shipped in a release.** Add a new one.
  Revisions `0001` and `0002` are special: they tolerate databases created
  before migrations existed. Later revisions don't need to, because every
  database that reaches them is versioned.
- **Add a test** if the revision transforms data, and mention the schema
  change in `CHANGELOG.md`.

To try a revision against PostgreSQL, pass a URL:
`venv/bin/alembic -x db_url=postgresql+psycopg://... upgrade head`.
The races and migration tests also run against a server database when
`CM_TEST_DB_URL` is set (CI does this for PostgreSQL).
