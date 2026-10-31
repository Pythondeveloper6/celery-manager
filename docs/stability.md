# Stability and upgrade policy

celery-manager is still released as alpha (`0.x` with `aN` suffixes), but the
surfaces below are now treated as stable. A change that breaks one of them
needs a deprecation period, a CHANGELOG entry under **Breaking**, and a
migration note. Anything not listed may still change between minor releases.

## What is stable

| Surface | Stable contract |
|---|---|
| Package root | Every name in `celery_manager.__all__`: `CeleryManagerConfig`, `setup`, `track`, `report_progress`, `RunObserver`, `current_run`, `install_publish_hook`, `task_url`, `control`, `__version__`. |
| Configuration | Existing `CeleryManagerConfig` field names, their types and their defaults. New fields are added with defaults that keep current behaviour. |
| REST API | Every route under `/api/v1`, its request fields and the fields in its response. New optional request fields and new response fields can appear at any time; clients must ignore fields they do not know. The unversioned `/api/...` aliases follow the same rules. |
| CLI | The `celery-manager` subcommands (`events`, `alerts`, `migrate`, `create-user`, `set-role`, `list-users`, `prune`, `import-pauses`, `doctor`, and the ones added later) and their flags. |
| Metrics | Metric names and label names on `/metrics`. |
| Database schema | The tables are reached only through the Alembic revisions in `celery_manager/db/migrations`. Revisions that shipped are never edited; every schema change is a new revision with a working `downgrade()`. |
| Task headers | The `cm_*` message headers stamped by the scheduler mixins and the publish hook. |

Not stable: anything under a module or attribute whose name starts with `_`,
the dashboard's HTML and internal JSON shapes used only by the bundled UI,
log message text, and the exact wording of `doctor` checks.

## Deprecation

A stable name or route that is going away keeps working for at least one
minor release (for example `0.4` to `0.5`) after the release that deprecates
it. During that window it emits a `DeprecationWarning` (Python) or a
`Deprecation` response header (HTTP), and the CHANGELOG names the
replacement.

## Upgrades

- Upgrading from the **previous release** is supported directly; older
  releases upgrade through each intermediate release's migrations, which
  `celery-manager migrate` applies in order.
- Back up the monitoring tables, run `celery-manager migrate --db-url ...`
  with a DDL-capable credential, then confirm with `--check`. Run with
  `auto_migrate=False` in application processes when they must not issue DDL.
- Every revision has a `downgrade()`. Downgrading is for rolling back a
  failed deployment, not for long-term use: data in columns added by the
  newer revision is dropped.

## How the policy is enforced

- `tests/unit/test_stability_contract.py` pins the hashes of shipped
  migration revisions, the package-root names and the CLI command set.
  Changing a pinned value is a policy decision and needs this page and the
  CHANGELOG updated in the same change.
- `tests/unit/test_migrations.py` fails when models and migrations disagree
  or the revision history has two heads.
- `tests/integration/test_release_upgrade.py` seeds data at the previous
  release's schema, upgrades to head, checks the data survived, downgrades and
  upgrades again. CI runs it against PostgreSQL 16, MySQL 8.4 and SQL Server
  2022 in the **Storage** job, through `tests.release_gate`, which fails if
  any selected check skipped.

## Towards 1.0

The `Development Status` classifier moves to Beta with the first beta release
and to Production/Stable at 1.0. The remaining blockers for 1.0 are tracked in
the [roadmap](roadmap.md): an independent security review, overhead numbers
from a production deployment, and a production trial.
