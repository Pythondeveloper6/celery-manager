# celery-manager Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.
>
> **This is a living document** (per user request): update task/step checkboxes as work proceeds, and append any new request made mid-implementation to the **Backlog** section at the bottom (as a new v1 task if in scope, otherwise as a Phase 2/3 backlog item) rather than handling it as an untracked side-note.

**Goal:** Build `celery-manager` v1 — a pip-installable package giving any Django/Flask/FastAPI project using Celery a dashboard + API to see, control, and schedule background tasks, including pausing a single future occurrence of a periodic task.

**Architecture:** Layered: SQLAlchemy storage → decorator (skip-check + log capture) + events listener (visibility) → core Python API → REST API (FastAPI, mountable, + SSE) → dashboard SPA (React, prebuilt static bundle). See spec §4 for the full diagram.

**Tech Stack:** Python 3.10+, Celery 5.x, SQLAlchemy 2.0 (sync), FastAPI/Starlette, bcrypt + itsdangerous (auth), pytest, React + TypeScript + Vite + Tailwind (frontend, built to static assets), MkDocs Material (docs).

**Spec:** `docs/superpowers/specs/2026-08-15-celery-manager-design.md`

## Global Constraints

- Python version floor: 3.10+.
- Target Celery version: current stable 5.x line.
- Storage defaults to SQLite (zero-config); must be swappable to any SQLAlchemy-supported DB via a config `db_url` — no code depending on SQLite-only features.
- End users of the package need **no Node/JS toolchain** — the dashboard SPA is built once by us and its static output is committed/shipped inside the Python package.
- The dashboard UI gets a dedicated design pass (frontend-design skill) for every screen — no default/generic component styling, real empty/loading/error states, light+dark handling.
- Git identity for this repo is `pythondeveloper6@gmail.com` (already configured locally). **No GitHub repo is created and nothing is pushed from this machine** — commit locally only, every task ends with a commit.
- One commit per task (or per step group where a task has multiple natural checkpoints) — small, reviewable diffs.
- No placeholder code (`TODO`, `pass  # implement later`, etc.) — every step ships real, runnable content.

---

## Milestone map

| # | Task | Depends on |
|---|------|-----------|
| 1 | Project scaffold & tooling | — |
| 2 | Storage layer (models + session/config) | 1 |
| 3 | Decorator — skip-check + log capture | 2 |
| 4 | Events listener | 2 |
| 5 | Core API — task control (list/start/stop/cancel) | 2 |
| 6 | Core API — schedule occurrence control | 2, 3 |
| 7 | Core API — retry + audit log | 5, 6 |
| 8 | Auth (users, hashing, session cookies) | 2 |
| 9 | REST API skeleton (FastAPI app, mount helper, auth dep) | 8 |
| 10 | REST API — task & schedule endpoints | 5, 6, 7, 9 |
| 11 | REST API — SSE live updates | 4, 9 |
| 12 | Dashboard SPA scaffold + design system + login | 9 |
| 13 | Dashboard — task list & detail | 10, 12 |
| 14 | Dashboard — future-schedule view + retry/bulk retry | 10, 12 |
| 15 | Dashboard — audit log + static bundling wire-up | 10, 12 |
| 16 | Django demo project | 1–15 |
| 17 | Docs site (MkDocs Material) | 1–15 |
| 18 | Dashboard — periodic schedules list screen (added mid-run, closes a spec gap) | 10, 12, 15 |

---

## Progress (living — updated after every task)

Full detail and rulings live in the SDD ledger at
`.superpowers/sdd/plan/progress.md`; this section is the at-a-glance status
plus anything worth surfacing outside that ledger.

- [x] **Task 1: Project scaffold & tooling** — commit `25c7f69`. Review: Approved (1 Important finding ruled non-blocking — `[project.scripts]` entry point points at `celery_manager.cli:main`, which doesn't exist until Task 16; intentional, documented sequencing, nothing between Task 1 and 16 invokes the CLI). 1 deferred minor (unused import).
- [x] **Task 2: Storage layer** — commit `18b9e9d`. Review: Approved, clean. One accepted deviation: fixed a genuine bug in this plan (`ScheduleOverride.created_at` was non-nullable with no default, but the plan's own test never supplied it) by adding a Python-side default. 1 deferred minor (`AuditLog.created_at` has the same latent shape, untested so far, already safe because every planned call site sets it explicitly).
- [x] **Task 3: Decorator — skip-check + log capture** — commit `926a5e0`. Review: Approved, clean. One accepted deviation: fixed a second genuine plan bug — SQLite drops tzinfo on `DateTime(timezone=True)` round-trip through a fresh session, which broke the plan's own literal skip-check comparison. **This turned out to be systemic**, not local to this task: it also would have silently broken Task 6's future-occurrence/override merge (a dict-key comparison, which fails silently rather than raising — worse than this task's `TypeError`). Fixed proactively in the plan text for Task 6 (`_as_aware_utc` helper) and Task 10 (added a cross-session end-to-end test) before either was dispatched — see commit `c5eaec6`.
- [x] **Task 4: Events listener** — commit `4f2f96c`. Review: Approved, clean. `EventListener`'s Celery API usage (`app.events.Receiver`/`.capture()`/`State`) independently verified against installed Celery 5.6.3 by both implementer and reviewer.
- [x] **Task 5: Core API — task control** — commit `43def07`. Review: Approved, clean.
- [x] **Task 6: Core API — schedule occurrence control** — commit `291b088` + fix `1f6df0c`. Review found a real thread-safety issue in the `nowfun`-pinning mechanism (shared schedule object, no lock, FastAPI threadpool would race); fixed by switching to a private `copy.copy()` — re-review confirmed addressed, race structurally eliminated.
- [x] **Task 7: Core API — retry + audit log** — commit `a74a8e0` (dispatched in parallel with Task 9). Review: Approved, clean. Fixed a doc-only bug found by review: the Interfaces bullets for `retry_task`/`bulk_retry` above were missing `username` (the Step 4 code and tests always had it).
- [x] **Task 8: Auth** — commit `ad76df4` + fix `8e86197` (dispatched in parallel with Task 6 — independent files, no overlap). Review found a real gap: `verify_session` only caught `BadSignature`, not the broader `BadData`, so a corrupted (not tampered) token could raise uncaught instead of returning `None`. Fixed by broadening the catch; re-review confirmed addressed with a new regression test reproducing the exact scenario.
- [x] **Task 9: REST API skeleton** — commit `e2569ba` (dispatched in parallel with Task 7). Review: Approved, clean. Also fixed (separate commit `fd233af`, independently reviewed and approved) a real cross-thread SQLite bug in Task 2's `init_db()` found while testing — `sqlite:///:memory:` defaults to `SingletonThreadPool`, giving each thread its own private DB; FastAPI's TestClient dispatches on a worker thread, so test data was invisible to route handlers. Fixed with `poolclass=StaticPool`, scoped strictly to `:memory:` URLs, verified not to affect file-sqlite/Postgres/MySQL paths.
- [x] **Task 10: REST API — task & schedule endpoints** — commit `aa1fa6d` + 2 fix rounds (`9d67826`, `e842d0e`). Endpoints themselves: clean on first review. The occurrence-matching design underneath them went through real hardening across 2 rounds: exact-equality matching → tolerance-window matching (fixes wall-clock drift breaking "paused occurrence stays visibly paused") → write-side dedup + read-side claim-based matching (fixes a double-match and a stale-override-wins edge case tolerance-window matching introduced). This is the core of the product's headline feature and got the most scrutiny of any task so far.
- [x] **Task 11: REST API — SSE live updates** — commit `da124e2`. Review: Approved. Two framework-driven fixes (TestClient hangs on infinite streams, untyped route param misread as query string) — neither changes production behavior beyond making the route reachable/testable.
- [x] **Task 12: Dashboard SPA scaffold + design system + login** — commit `7f426b7` + fix `b971f39`. "Signal panel" ops-console design (slate + phosphor-cyan, self-hosted IBM Plex, WCAG-AA contrast-checked, light+dark, real states) — independently reviewed as genuinely meeting the "high attention to UI design" bar, verified end-to-end with real Playwright/Chromium. Fixed one implementer gap (inputs not disabled during loading) and one plan-inherited gap (blank screen forever on network failure during initial auth check).
- [x] **Task 13: Dashboard — task list & detail** — commit `c1da6b8` (a git-index race during parallel dispatch briefly misattributed this to a docs commit — split back into a clean commit once resolved, no data lost). Found and fixed a real bug: the backend's static-file serving has no SPA fallback route, so deep links 404'd — switched `BrowserRouter`→`HashRouter`, verified with a genuine hard-reload test. Extended the design system with contrast-checked status-color tokens, verified as a real continuation of Task 12's system, not a bolt-on. Review: Approved, clean.
- [x] **Task 14: Dashboard — future-schedule view + retry/bulk retry** — commit `54283bc` + fix `19bdb09`. Verified end-to-end against a real stack (Docker Redis, real Celery worker, real browser). Found and fixed a headline-feature-breaking bug in the plan's own illustrative code (paused occurrences would never render as paused — `consumed` is always false for a matched override, need `action != null` instead) plus a real CSS specificity bug (pressed button invisible on hover). One fix round for a bulk-selection staleness gap under live SSE updates.
- [ ] **Task 15: Dashboard — audit log + static bundling wire-up** — not started.
- [x] **Task 16: Django demo project** — commit `24ed13e` (dispatched in parallel with Task 13; Redis not available in this sandbox, so verification used `task_always_eager` + REST API checks instead of a live worker/beat — README still documents the full real flow). Review: Approved, clean. Fixed a real bug in the plan's own `settings.py` example (bare-int schedule value breaks the occurrences endpoint — needs a real `celery.schedules.schedule()` instance).
- [x] **Task 17: Docs site (MkDocs Material)** — commit `6d45fb2` (dispatched in parallel with Task 15). Documented from real source, not the original plan text — every spot-checked claim across all 6 pages verified accurate against actual code, including non-obvious deviations from earlier fix rounds. Review: Approved, clean.
- [ ] **Task 18: Dashboard — periodic schedules list screen** — not started. **Added mid-run**: spec §4.6 requires a "periodic schedules list" v1 screen; the original Task 12-15 breakdown never planned one, only the per-schedule occurrence view (Task 14). Discovered by Task 15's implementer while wiring the nav bar — there was nothing to link "Schedules" to. Without this, the fully-correct pause/cancel headline feature is unreachable from the UI without hand-typing a URL.

---

### Task 1: Project scaffold & tooling

**Files:**
- Create: `pyproject.toml`
- Create: `src/celery_manager/__init__.py`
- Create: `src/celery_manager/config.py`
- Create: `tests/__init__.py`
- Create: `tests/unit/__init__.py`
- Create: `.gitignore`

**Interfaces:**
- Produces: `celery_manager.config.CeleryManagerConfig` — dataclass with fields `celery_app: Celery`, `db_url: str = "sqlite:///celery_manager.db"`, `secret_key: str`, `eta_tolerance_seconds: int = 60`.
- Produces: `celery_manager.__version__: str`.

- [ ] **Step 1: Write `pyproject.toml`**

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "celery-manager"
version = "0.1.0"
description = "A dashboard and control API for Celery background tasks."
requires-python = ">=3.10"
dependencies = [
    "celery>=5.3,<6.0",
    "sqlalchemy>=2.0,<3.0",
    "fastapi>=0.110,<1.0",
    "uvicorn>=0.29",
    "bcrypt>=4.1",
    "itsdangerous>=2.1",
]

[project.optional-dependencies]
dev = ["pytest>=8.0", "httpx>=0.27", "ruff>=0.4"]

[project.scripts]
celery-manager = "celery_manager.cli:main"

[tool.hatch.build.targets.wheel]
packages = ["src/celery_manager"]

[tool.pytest.ini_options]
testpaths = ["tests"]
```

- [ ] **Step 2: Write `src/celery_manager/__init__.py`**

```python
__version__ = "0.1.0"
```

- [ ] **Step 3: Write `src/celery_manager/config.py`**

```python
from __future__ import annotations

from dataclasses import dataclass, field

from celery import Celery


@dataclass
class CeleryManagerConfig:
    """Runtime configuration for celery-manager.

    celery_app: the host application's Celery instance. Used for task
        introspection (registered task names) and control calls
        (revoke/terminate).
    db_url: SQLAlchemy connection URL for celery-manager's own storage.
        Defaults to a local SQLite file; swap to any SQLAlchemy-supported
        DB (Postgres, MySQL, ...) for production use.
    secret_key: used to sign session cookies for the built-in auth.
    eta_tolerance_seconds: how close "now" must be to a schedule
        override's target time for the decorator to treat it as a match.
    """

    celery_app: Celery
    db_url: str = "sqlite:///celery_manager.db"
    secret_key: str = "change-me"
    eta_tolerance_seconds: int = 60
```

- [ ] **Step 4: Write `.gitignore`**

```
__pycache__/
*.pyc
.venv/
*.egg-info/
dist/
build/
celery_manager.db
frontend/node_modules/
frontend/dist/
.pytest_cache/
```

- [ ] **Step 5: Create empty test package markers**

`tests/__init__.py` and `tests/unit/__init__.py` — both empty files.

- [ ] **Step 6: Verify the package installs**

Run: `pip install -e ".[dev]"`
Expected: installs cleanly, `python -c "import celery_manager; print(celery_manager.__version__)"` prints `0.1.0`.

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml .gitignore src/celery_manager/__init__.py src/celery_manager/config.py tests/__init__.py tests/unit/__init__.py
git commit -m "chore: project scaffold and config dataclass"
```

---

### Task 2: Storage layer

**Files:**
- Create: `src/celery_manager/db/__init__.py`
- Create: `src/celery_manager/db/models.py`
- Create: `src/celery_manager/db/session.py`
- Test: `tests/unit/test_models.py`

**Interfaces:**
- Consumes: `celery_manager.config.CeleryManagerConfig` (Task 1).
- Produces: SQLAlchemy `Base` and ORM classes `TaskRun`, `PeriodicSchedule`, `ScheduleOverride`, `User`, `AuditLog` (all in `celery_manager.db.models`).
- Produces: `celery_manager.db.session.init_db(config: CeleryManagerConfig) -> Engine` and `get_session_factory(engine: Engine) -> sessionmaker`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_models.py
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from celery_manager.db.models import Base, TaskRun, PeriodicSchedule, ScheduleOverride, User, AuditLog


def test_task_run_round_trip():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    with Session() as session:
        run = TaskRun(
            task_id="abc-123",
            task_name="demo.send_report",
            status="succeeded",
            queue="default",
            args="[]",
            kwargs="{}",
            eta=None,
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
        )
        session.add(run)
        session.commit()

        fetched = session.query(TaskRun).filter_by(task_id="abc-123").one()
        assert fetched.task_name == "demo.send_report"
        assert fetched.status == "succeeded"


def test_schedule_override_round_trip():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)

    with Session() as session:
        sched = PeriodicSchedule(
            name="demo-every-2h",
            task_name="demo.send_report",
            schedule_spec="2h",
            enabled=True,
        )
        session.add(sched)
        session.commit()

        override = ScheduleOverride(
            schedule_name="demo-every-2h",
            target_time=datetime(2026, 8, 15, 20, 0, tzinfo=timezone.utc),
            action="skip",
            created_by="alice",
        )
        session.add(override)
        session.commit()

        fetched = (
            session.query(ScheduleOverride)
            .filter_by(schedule_name="demo-every-2h")
            .one()
        )
        assert fetched.action == "skip"
        assert fetched.consumed is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_models.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'celery_manager.db'`

- [ ] **Step 3: Write `src/celery_manager/db/__init__.py`**

```python
```

(empty — package marker)

- [ ] **Step 4: Write `src/celery_manager/db/models.py`**

```python
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TaskRun(Base):
    __tablename__ = "task_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    task_name: Mapped[str] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)  # queued/started/succeeded/failed/retried/skipped/revoked
    queue: Mapped[str | None] = mapped_column(String(255), nullable=True)
    worker: Mapped[str | None] = mapped_column(String(255), nullable=True)
    args: Mapped[str] = mapped_column(Text, default="[]")
    kwargs: Mapped[str] = mapped_column(Text, default="{}")
    eta: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    runtime_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    exception: Mapped[str | None] = mapped_column(Text, nullable=True)
    traceback: Mapped[str | None] = mapped_column(Text, nullable=True)
    log_output: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    schedule_name: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)


class PeriodicSchedule(Base):
    __tablename__ = "periodic_schedules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    task_name: Mapped[str] = mapped_column(String(255))
    schedule_spec: Mapped[str] = mapped_column(String(255))  # human-readable, e.g. "2h" or "crontab(minute=0,hour=7)"
    args: Mapped[str] = mapped_column(Text, default="[]")
    kwargs: Mapped[str] = mapped_column(Text, default="{}")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class ScheduleOverride(Base):
    __tablename__ = "schedule_overrides"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    schedule_name: Mapped[str] = mapped_column(String(255), ForeignKey("periodic_schedules.name"), index=True)
    target_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    action: Mapped[str] = mapped_column(String(32))  # skip | cancel | reschedule
    reschedule_to: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_by: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(64))
    target_type: Mapped[str] = mapped_column(String(64))
    target_id: Mapped[str] = mapped_column(String(255))
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
```

- [ ] **Step 5: Write `src/celery_manager/db/session.py`**

```python
from __future__ import annotations

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import sessionmaker

from celery_manager.config import CeleryManagerConfig
from celery_manager.db.models import Base


def init_db(config: CeleryManagerConfig) -> Engine:
    """Create the engine and ensure all tables exist."""
    connect_args = {"check_same_thread": False} if config.db_url.startswith("sqlite") else {}
    engine = create_engine(config.db_url, connect_args=connect_args)
    Base.metadata.create_all(engine)
    return engine


def get_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, expire_on_commit=False)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/unit/test_models.py -v`
Expected: PASS (2 tests)

- [ ] **Step 7: Commit**

```bash
git add src/celery_manager/db tests/unit/test_models.py
git commit -m "feat: storage layer with SQLAlchemy models"
```

---

### Task 3: Decorator — skip-check + log capture

**Files:**
- Create: `src/celery_manager/decorator.py`
- Create: `src/celery_manager/logcapture.py`
- Test: `tests/unit/test_decorator.py`

**Interfaces:**
- Consumes: `TaskRun`, `ScheduleOverride` (Task 2); `CeleryManagerConfig` (Task 1).
- Produces: `celery_manager.decorator.track(schedule_name: str | None = None)` — decorator factory; applied as `@track()` or `@track(schedule_name="...")` **above** `@app.task`.
- Produces: `celery_manager.logcapture.capture_logs() -> contextlib.AbstractContextManager[io.StringIO]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_decorator.py
from datetime import datetime, timedelta, timezone

import pytest
from celery import Celery
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from celery_manager.db.models import Base, ScheduleOverride, TaskRun
from celery_manager.decorator import track, configure


@pytest.fixture
def env():
    app = Celery("test_app", broker="memory://", backend="cache+memory://")
    app.conf.task_always_eager = True

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    configure(session_factory=Session, eta_tolerance_seconds=60)

    calls = []

    @app.task(name="demo.echo", bind=True)
    @track(schedule_name="demo-every-2h")
    def echo(self, value):
        calls.append(value)
        return value

    return app, Session, echo, calls


def test_runs_normally_when_no_override(env):
    app, Session, echo, calls = env
    echo.apply(args=["hi"])
    assert calls == ["hi"]


def test_skips_when_override_matches_now(env):
    app, Session, echo, calls = env
    with Session() as session:
        session.add(ScheduleOverride(
            schedule_name="demo-every-2h",
            target_time=datetime.now(timezone.utc),
            action="skip",
            consumed=False,
            created_by="alice",
            created_at=datetime.now(timezone.utc),
        ))
        session.commit()

    echo.apply(args=["hi"])
    assert calls == []  # task body did not run

    with Session() as session:
        run = session.query(TaskRun).filter_by(task_name="demo.echo").one()
        assert run.status == "skipped"

        override = session.query(ScheduleOverride).filter_by(schedule_name="demo-every-2h").one()
        assert override.consumed is True


def test_ignores_override_outside_tolerance_window(env):
    app, Session, echo, calls = env
    with Session() as session:
        session.add(ScheduleOverride(
            schedule_name="demo-every-2h",
            target_time=datetime.now(timezone.utc) - timedelta(hours=1),
            action="skip",
            consumed=False,
            created_by="alice",
            created_at=datetime.now(timezone.utc),
        ))
        session.commit()

    echo.apply(args=["hi"])
    assert calls == ["hi"]  # override is stale, task ran
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_decorator.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'celery_manager.decorator'`

- [ ] **Step 3: Write `src/celery_manager/logcapture.py`**

```python
from __future__ import annotations

import contextlib
import io
import logging


@contextlib.contextmanager
def capture_logs():
    """Capture root-logger output (and anything a task logs) into a buffer."""
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        yield buffer
    finally:
        root.removeHandler(handler)
```

- [ ] **Step 4: Write `src/celery_manager/decorator.py`**

```python
from __future__ import annotations

import functools
import json
import traceback as tb_module
from datetime import datetime, timedelta, timezone

from celery_manager.logcapture import capture_logs

_session_factory = None
_eta_tolerance_seconds = 60


def configure(session_factory, eta_tolerance_seconds: int = 60) -> None:
    """Wire the decorator to a session factory. Called once at app setup
    (celery_manager.setup(config) does this for you in the core API)."""
    global _session_factory, _eta_tolerance_seconds
    _session_factory = session_factory
    _eta_tolerance_seconds = eta_tolerance_seconds


def _find_matching_override(session, schedule_name: str):
    from celery_manager.db.models import ScheduleOverride

    now = datetime.now(timezone.utc)
    window = timedelta(seconds=_eta_tolerance_seconds)
    candidates = (
        session.query(ScheduleOverride)
        .filter_by(schedule_name=schedule_name, consumed=False)
        .all()
    )
    for override in candidates:
        if abs(override.target_time - now) <= window:
            return override
    return None


def track(schedule_name: str | None = None):
    """Decorator for Celery tasks. Apply above @app.task:

        @app.task(name="demo.echo", bind=True)
        @track(schedule_name="demo-every-2h")
        def echo(self, value):
            ...

    Checks for a matching schedule_overrides entry before running; if found
    and action is skip/cancel, records the run as 'skipped' and does not
    call the wrapped function. Otherwise runs normally, capturing logs and
    persisting a TaskRun row with the outcome.
    """

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            from celery_manager.db.models import TaskRun

            task_self = args[0] if args else None
            task_id = getattr(task_self.request, "id", None) if task_self else None
            task_name = getattr(task_self, "name", func.__name__) if task_self else func.__name__
            effective_schedule_name = schedule_name or task_name

            if _session_factory is None:
                # Not configured (e.g. task called outside a configured app) — run normally.
                return func(*args, **kwargs)

            with _session_factory() as session:
                override = _find_matching_override(session, effective_schedule_name)
                if override is not None and override.action in ("skip", "cancel"):
                    override.consumed = True
                    session.add(TaskRun(
                        task_id=task_id or f"skipped-{datetime.now(timezone.utc).isoformat()}",
                        task_name=task_name,
                        status="skipped",
                        args=json.dumps(args[1:] if task_self else args),
                        kwargs=json.dumps(kwargs),
                        schedule_name=effective_schedule_name,
                        started_at=datetime.now(timezone.utc),
                        finished_at=datetime.now(timezone.utc),
                    ))
                    session.commit()
                    return None

            started_at = datetime.now(timezone.utc)
            with capture_logs() as buffer:
                try:
                    result = func(*args, **kwargs)
                except Exception as exc:
                    finished_at = datetime.now(timezone.utc)
                    with _session_factory() as session:
                        session.add(TaskRun(
                            task_id=task_id or f"run-{started_at.isoformat()}",
                            task_name=task_name,
                            status="failed",
                            args=json.dumps(args[1:] if task_self else args),
                            kwargs=json.dumps(kwargs),
                            schedule_name=effective_schedule_name,
                            started_at=started_at,
                            finished_at=finished_at,
                            runtime_seconds=(finished_at - started_at).total_seconds(),
                            exception=str(exc),
                            traceback=tb_module.format_exc(),
                            log_output=buffer.getvalue(),
                        ))
                        session.commit()
                    raise
                else:
                    finished_at = datetime.now(timezone.utc)
                    with _session_factory() as session:
                        session.add(TaskRun(
                            task_id=task_id or f"run-{started_at.isoformat()}",
                            task_name=task_name,
                            status="succeeded",
                            args=json.dumps(args[1:] if task_self else args),
                            kwargs=json.dumps(kwargs),
                            schedule_name=effective_schedule_name,
                            started_at=started_at,
                            finished_at=finished_at,
                            runtime_seconds=(finished_at - started_at).total_seconds(),
                            log_output=buffer.getvalue(),
                        ))
                        session.commit()
                    return result

        return wrapper

    return decorator
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/test_decorator.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add src/celery_manager/decorator.py src/celery_manager/logcapture.py tests/unit/test_decorator.py
git commit -m "feat: task decorator with schedule skip-check and log capture"
```

---

### Task 4: Events listener

**Files:**
- Create: `src/celery_manager/events/__init__.py`
- Create: `src/celery_manager/events/listener.py`
- Test: `tests/unit/test_events_listener.py`

**Interfaces:**
- Consumes: `TaskRun` (Task 2); host `Celery` app (Task 1's config).
- Produces: `celery_manager.events.listener.upsert_task_run(session, event: dict) -> None` (pure function, unit-testable without a real broker).
- Produces: `celery_manager.events.listener.EventListener(config).run()` — blocking call wired to `app.events.Receiver`; not exercised in unit tests (thin wrapper, covered by the integration suite per spec §7 test strategy, not built in this task).

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_events_listener.py
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from celery_manager.db.models import Base, TaskRun
from celery_manager.events.listener import upsert_task_run


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def test_task_sent_creates_row():
    session = _session()
    upsert_task_run(session, {
        "type": "task-sent",
        "uuid": "task-1",
        "name": "demo.echo",
        "queue": "default",
        "args": "['hi']",
        "kwargs": "{}",
    })
    session.commit()

    run = session.query(TaskRun).filter_by(task_id="task-1").one()
    assert run.task_name == "demo.echo"
    assert run.status == "queued"


def test_task_started_then_succeeded_updates_same_row():
    session = _session()
    upsert_task_run(session, {"type": "task-sent", "uuid": "task-2", "name": "demo.echo"})
    upsert_task_run(session, {"type": "task-started", "uuid": "task-2", "hostname": "worker-1"})
    upsert_task_run(session, {"type": "task-succeeded", "uuid": "task-2", "result": "'hi'", "runtime": 0.02})
    session.commit()

    rows = session.query(TaskRun).filter_by(task_id="task-2").all()
    assert len(rows) == 1
    assert rows[0].status == "succeeded"
    assert rows[0].worker == "worker-1"
    assert rows[0].runtime_seconds == 0.02


def test_task_failed_records_exception():
    session = _session()
    upsert_task_run(session, {"type": "task-sent", "uuid": "task-3", "name": "demo.echo"})
    upsert_task_run(session, {
        "type": "task-failed",
        "uuid": "task-3",
        "exception": "ValueError('boom')",
        "traceback": "Traceback ...",
    })
    session.commit()

    run = session.query(TaskRun).filter_by(task_id="task-3").one()
    assert run.status == "failed"
    assert "boom" in run.exception
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_events_listener.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'celery_manager.events'`

- [ ] **Step 3: Write `src/celery_manager/events/__init__.py`**

```python
```

(empty — package marker)

- [ ] **Step 4: Write `src/celery_manager/events/listener.py`**

```python
from __future__ import annotations

from datetime import datetime, timezone

from celery_manager.config import CeleryManagerConfig
from celery_manager.db.models import TaskRun

_STATUS_BY_EVENT = {
    "task-sent": "queued",
    "task-received": "queued",
    "task-started": "started",
    "task-succeeded": "succeeded",
    "task-failed": "failed",
    "task-retried": "retried",
    "task-revoked": "revoked",
}


def upsert_task_run(session, event: dict) -> None:
    """Apply a single Celery event to the task_runs table.

    Pure function over a SQLAlchemy session so it's testable without a
    real broker/receiver — EventListener.run() below just feeds it events.
    """
    event_type = event["type"]
    status = _STATUS_BY_EVENT.get(event_type)
    if status is None:
        return  # worker-* events etc. are not task_runs concerns here

    task_id = event["uuid"]
    run = session.query(TaskRun).filter_by(task_id=task_id).one_or_none()
    if run is None:
        run = TaskRun(task_id=task_id, task_name=event.get("name", "unknown"), status=status)
        session.add(run)
    else:
        run.status = status

    if "name" in event:
        run.task_name = event["name"]
    if "queue" in event:
        run.queue = event["queue"]
    if "hostname" in event:
        run.worker = event["hostname"]
    if "args" in event:
        run.args = str(event["args"])
    if "kwargs" in event:
        run.kwargs = str(event["kwargs"])
    if event_type == "task-started":
        run.started_at = datetime.now(timezone.utc)
    if event_type in ("task-succeeded", "task-failed"):
        run.finished_at = datetime.now(timezone.utc)
    if "runtime" in event:
        run.runtime_seconds = event["runtime"]
    if "exception" in event:
        run.exception = event["exception"]
    if "traceback" in event:
        run.traceback = event["traceback"]


class EventListener:
    """Long-running consumer of the Celery event bus. Run via the
    `celery-manager events` CLI command (Task 16 wires this up for the
    Django demo; see cli.py)."""

    def __init__(self, config: CeleryManagerConfig, session_factory):
        self._config = config
        self._session_factory = session_factory

    def run(self) -> None:
        app = self._config.celery_app
        state = app.events.State()

        def on_event(event):
            state.event(event)
            with self._session_factory() as session:
                upsert_task_run(session, event)
                session.commit()

        with app.connection() as connection:
            receiver = app.events.Receiver(connection, handlers={"*": on_event})
            receiver.capture(limit=None, timeout=None, wakeup=True)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/test_events_listener.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add src/celery_manager/events tests/unit/test_events_listener.py
git commit -m "feat: events listener upserting task_runs from the Celery event bus"
```

---

### Task 5: Core API — task control

**Files:**
- Create: `src/celery_manager/control/__init__.py`
- Create: `src/celery_manager/control/tasks.py`
- Test: `tests/unit/test_control_tasks.py`

**Interfaces:**
- Consumes: `TaskRun` (Task 2), host `Celery` app.
- Produces: `list_task_runs(session, *, status=None, task_name=None, limit=50, offset=0) -> list[TaskRun]`
- Produces: `get_task_run(session, task_id: str) -> TaskRun | None`
- Produces: `start_task(celery_app, task_name: str, args: list, kwargs: dict) -> str` (returns new task_id)
- Produces: `cancel_task(celery_app, task_id: str, terminate: bool = False) -> None`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_control_tasks.py
from datetime import datetime, timezone
from unittest.mock import MagicMock

from celery import Celery
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from celery_manager.db.models import Base, TaskRun
from celery_manager.control.tasks import list_task_runs, get_task_run, start_task, cancel_task


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as session:
        session.add_all([
            TaskRun(task_id="t1", task_name="demo.echo", status="succeeded"),
            TaskRun(task_id="t2", task_name="demo.echo", status="failed"),
            TaskRun(task_id="t3", task_name="demo.other", status="succeeded"),
        ])
        session.commit()
    return Session()


def test_list_task_runs_filters_by_status():
    session = _session()
    results = list_task_runs(session, status="failed")
    assert [r.task_id for r in results] == ["t2"]


def test_list_task_runs_filters_by_name():
    session = _session()
    results = list_task_runs(session, task_name="demo.other")
    assert [r.task_id for r in results] == ["t3"]


def test_get_task_run_returns_none_when_missing():
    session = _session()
    assert get_task_run(session, "does-not-exist") is None


def test_start_task_calls_apply_async():
    app = MagicMock(spec=Celery)
    app.send_task.return_value = MagicMock(id="new-task-id")

    task_id = start_task(app, "demo.echo", args=["hi"], kwargs={})

    app.send_task.assert_called_once_with("demo.echo", args=["hi"], kwargs={})
    assert task_id == "new-task-id"


def test_cancel_task_revokes_by_default():
    app = MagicMock(spec=Celery)
    cancel_task(app, "t1")
    app.control.revoke.assert_called_once_with("t1", terminate=False)


def test_cancel_task_terminates_when_requested():
    app = MagicMock(spec=Celery)
    cancel_task(app, "t1", terminate=True)
    app.control.revoke.assert_called_once_with("t1", terminate=True)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_control_tasks.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'celery_manager.control'`

- [ ] **Step 3: Write `src/celery_manager/control/__init__.py`**

```python
```

(empty — package marker)

- [ ] **Step 4: Write `src/celery_manager/control/tasks.py`**

```python
from __future__ import annotations

from celery import Celery

from celery_manager.db.models import TaskRun


def list_task_runs(session, *, status: str | None = None, task_name: str | None = None, limit: int = 50, offset: int = 0) -> list[TaskRun]:
    query = session.query(TaskRun)
    if status is not None:
        query = query.filter_by(status=status)
    if task_name is not None:
        query = query.filter_by(task_name=task_name)
    return query.order_by(TaskRun.id.desc()).offset(offset).limit(limit).all()


def get_task_run(session, task_id: str) -> TaskRun | None:
    return session.query(TaskRun).filter_by(task_id=task_id).one_or_none()


def start_task(celery_app: Celery, task_name: str, args: list, kwargs: dict) -> str:
    """Dispatch a task on demand (e.g. from the dashboard's 'run now' action)."""
    result = celery_app.send_task(task_name, args=args, kwargs=kwargs)
    return result.id


def cancel_task(celery_app: Celery, task_id: str, terminate: bool = False) -> None:
    """Revoke a queued task, or terminate it if already running (terminate=True)."""
    celery_app.control.revoke(task_id, terminate=terminate)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/test_control_tasks.py -v`
Expected: PASS (6 tests)

- [ ] **Step 6: Commit**

```bash
git add src/celery_manager/control tests/unit/test_control_tasks.py
git commit -m "feat: core API for listing, starting, and cancelling tasks"
```

---

### Task 6: Core API — schedule occurrence control

**Files:**
- Create: `src/celery_manager/control/schedules.py`
- Test: `tests/unit/test_control_schedules.py`

**Interfaces:**
- Consumes: `PeriodicSchedule`, `ScheduleOverride` (Task 2); host `Celery` app.
- Produces: `upcoming_occurrences(schedule: PeriodicSchedule, celery_schedule, count: int = 10, from_time: datetime | None = None) -> list[datetime]` — takes a `celery.schedules.schedule`/`crontab` instance (the actual schedule object, since `schedule_spec` on our model is just the display string) and returns the next N run times.
- Produces: `pause_occurrence(session, schedule_name: str, target_time: datetime, action: str, created_by: str) -> ScheduleOverride`
- Produces: `list_overrides(session, schedule_name: str) -> list[ScheduleOverride]`
- Produces: `sync_periodic_schedules(session, celery_app) -> list[PeriodicSchedule]` — reads the host app's `celery_app.conf.beat_schedule` and upserts a `PeriodicSchedule` row per entry, keyed by the beat_schedule dict key as `name`. Without this, `periodic_schedules` stays empty and the dashboard has nothing to show — called once at REST API startup (Task 10 wires this into `create_app`).
- Produces: `list_upcoming_with_overrides(session, celery_app, schedule_name: str, count: int = 10, from_time: datetime | None = None) -> list[dict]` — the actual data the dashboard's future-schedule view needs: every upcoming occurrence (computed via `upcoming_occurrences`, using the live schedule object from `celery_app.conf.beat_schedule`), each annotated with `action`/`consumed` from any matching override. **Not just already-overridden rows** — `list_overrides` alone (used directly in Task 10's first draft below) only returns occurrences someone already acted on, which would leave the dashboard with nothing to pause. This function is what the REST endpoint in Task 10 actually calls.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_control_schedules.py
from datetime import datetime, timedelta, timezone

from celery.schedules import crontab, schedule as interval_schedule
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from celery_manager.db.models import Base, PeriodicSchedule, ScheduleOverride
from celery_manager.control.schedules import (
    upcoming_occurrences,
    pause_occurrence,
    list_overrides,
    sync_periodic_schedules,
    list_upcoming_with_overrides,
)


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def test_upcoming_occurrences_for_interval_schedule():
    sched = PeriodicSchedule(name="demo-every-2h", task_name="demo.echo", schedule_spec="2h")
    start = datetime(2026, 8, 15, 6, 0, tzinfo=timezone.utc)

    occurrences = upcoming_occurrences(sched, interval_schedule(run_every=timedelta(hours=2)), count=3, from_time=start)

    assert occurrences == [
        datetime(2026, 8, 15, 8, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 15, 10, 0, tzinfo=timezone.utc),
        datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc),
    ]


def test_upcoming_occurrences_for_crontab_schedule():
    sched = PeriodicSchedule(name="daily-report", task_name="demo.echo", schedule_spec="crontab(hour=7,minute=30)")
    start = datetime(2026, 8, 15, 6, 0, tzinfo=timezone.utc)

    occurrences = upcoming_occurrences(sched, crontab(hour=7, minute=30), count=2, from_time=start)

    assert occurrences == [
        datetime(2026, 8, 15, 7, 30, tzinfo=timezone.utc),
        datetime(2026, 8, 16, 7, 30, tzinfo=timezone.utc),
    ]


def test_pause_occurrence_creates_override():
    session = _session()
    session.add(PeriodicSchedule(name="demo-every-2h", task_name="demo.echo", schedule_spec="2h"))
    session.commit()

    target = datetime(2026, 8, 15, 20, 0, tzinfo=timezone.utc)
    override = pause_occurrence(session, "demo-every-2h", target, action="skip", created_by="alice")

    assert override.id is not None
    assert list_overrides(session, "demo-every-2h") == [override]


def test_pause_occurrence_is_idempotent_for_same_target():
    session = _session()
    session.add(PeriodicSchedule(name="demo-every-2h", task_name="demo.echo", schedule_spec="2h"))
    session.commit()

    target = datetime(2026, 8, 15, 20, 0, tzinfo=timezone.utc)
    pause_occurrence(session, "demo-every-2h", target, action="skip", created_by="alice")
    pause_occurrence(session, "demo-every-2h", target, action="skip", created_by="alice")

    assert len(list_overrides(session, "demo-every-2h")) == 1


def test_sync_periodic_schedules_upserts_from_beat_schedule():
    from unittest.mock import MagicMock
    from celery import Celery

    session = _session()
    app = MagicMock(spec=Celery)
    app.conf.beat_schedule = {
        "demo-every-2h": {"task": "demo.echo", "schedule": interval_schedule(run_every=timedelta(hours=2)), "args": [], "kwargs": {}},
    }

    synced = sync_periodic_schedules(session, app)

    assert len(synced) == 1
    assert synced[0].name == "demo-every-2h"
    assert synced[0].task_name == "demo.echo"

    # calling again does not duplicate the row
    sync_periodic_schedules(session, app)
    assert session.query(PeriodicSchedule).count() == 1


def test_list_upcoming_with_overrides_merges_computed_occurrences_and_overrides():
    from unittest.mock import MagicMock
    from celery import Celery

    # Two independent Session objects on the same engine — mirrors two
    # separate HTTP requests each opening their own session via
    # session_factory() (Task 10's routes). This is deliberate: it's what
    # actually exercises SQLite's tzinfo-dropping round-trip (a session
    # that only ever reads back objects it inserted itself, via SQLAlchemy's
    # identity map, never re-parses the stored value and would hide the bug
    # _as_aware_utc fixes).
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)

    app = MagicMock(spec=Celery)
    app.conf.beat_schedule = {
        "demo-every-2h": {"task": "demo.echo", "schedule": interval_schedule(run_every=timedelta(hours=2)), "args": [], "kwargs": {}},
    }
    with Session() as write_session:
        sync_periodic_schedules(write_session, app)
        first_occurrence = datetime(2026, 8, 15, 8, 0, tzinfo=timezone.utc)
        pause_occurrence(write_session, "demo-every-2h", first_occurrence, action="skip", created_by="alice")

    start = datetime(2026, 8, 15, 6, 0, tzinfo=timezone.utc)
    read_session = Session()
    result = list_upcoming_with_overrides(read_session, app, "demo-every-2h", count=3, from_time=start)

    assert len(result) == 3
    assert result[0] == {"target_time": first_occurrence, "action": "skip", "consumed": False}
    assert result[1]["action"] is None
    assert result[1]["consumed"] is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_control_schedules.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'celery_manager.control.schedules'`

- [ ] **Step 3: Write `src/celery_manager/control/schedules.py`**

```python
from __future__ import annotations

from datetime import datetime, timezone

from celery_manager.db.models import PeriodicSchedule, ScheduleOverride


def upcoming_occurrences(schedule: PeriodicSchedule, celery_schedule, count: int = 10, from_time: datetime | None = None) -> list[datetime]:
    """Compute the next `count` run times for a periodic schedule.

    `celery_schedule` is the actual celery.schedules.schedule/crontab
    instance the host app configured for this entry (not derivable from
    our stored display string alone — callers look it up from the host
    app's beat_schedule by `schedule.name`).
    """
    from_time = from_time or datetime.now(timezone.utc)
    occurrences: list[datetime] = []
    last_run_at = from_time
    for _ in range(count):
        remaining = celery_schedule.remaining_estimate(last_run_at)
        next_run = last_run_at + remaining
        occurrences.append(next_run)
        last_run_at = next_run
    return occurrences


def pause_occurrence(session, schedule_name: str, target_time: datetime, action: str, created_by: str) -> ScheduleOverride:
    """Record a skip/cancel/reschedule override for one future occurrence.
    Idempotent: re-calling for the same (schedule_name, target_time) that
    already has an unconsumed override returns the existing row."""
    existing = (
        session.query(ScheduleOverride)
        .filter_by(schedule_name=schedule_name, target_time=target_time, consumed=False)
        .one_or_none()
    )
    if existing is not None:
        existing.action = action
        session.commit()
        return existing

    override = ScheduleOverride(
        schedule_name=schedule_name,
        target_time=target_time,
        action=action,
        created_by=created_by,
        created_at=datetime.now(timezone.utc),
    )
    session.add(override)
    session.commit()
    return override


def list_overrides(session, schedule_name: str) -> list[ScheduleOverride]:
    return (
        session.query(ScheduleOverride)
        .filter_by(schedule_name=schedule_name)
        .order_by(ScheduleOverride.target_time)
        .all()
    )


def sync_periodic_schedules(session, celery_app) -> list[PeriodicSchedule]:
    """Upsert periodic_schedules from the host app's beat_schedule config.
    Call once at REST API startup (see Task 10) so the dashboard's list of
    schedules always reflects what's actually configured to run."""
    synced = []
    for name, entry in celery_app.conf.beat_schedule.items():
        row = session.query(PeriodicSchedule).filter_by(name=name).one_or_none()
        if row is None:
            row = PeriodicSchedule(name=name)
            session.add(row)
        row.task_name = entry["task"]
        row.schedule_spec = str(entry["schedule"])
        row.enabled = True
        synced.append(row)
    session.commit()
    return synced


def _as_aware_utc(dt: datetime) -> datetime:
    """SQLite drops tzinfo on DateTime(timezone=True) round-trip through a
    fresh session (confirmed in Task 3's decorator work) — every write in
    this codebase uses datetime.now(timezone.utc) / tz-aware values, so a
    naive read-back is always UTC. Normalize before using a DB-sourced
    datetime as a dict key or in a Python-level comparison; SQL-level
    filter_by comparisons (e.g. in pause_occurrence above) don't need this,
    only post-fetch Python equality/hashing does."""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def list_upcoming_with_overrides(session, celery_app, schedule_name: str, count: int = 10, from_time: datetime | None = None) -> list[dict]:
    """The data the dashboard's future-schedule view renders: every
    upcoming occurrence, each annotated with any existing override."""
    periodic = session.query(PeriodicSchedule).filter_by(name=schedule_name).one_or_none()
    entry = celery_app.conf.beat_schedule.get(schedule_name)
    if periodic is None or entry is None:
        return []

    occurrences = upcoming_occurrences(periodic, entry["schedule"], count=count, from_time=from_time)
    overrides_by_time = {_as_aware_utc(o.target_time): o for o in list_overrides(session, schedule_name)}

    return [
        {
            "target_time": t,
            "action": overrides_by_time[t].action if t in overrides_by_time else None,
            "consumed": overrides_by_time[t].consumed if t in overrides_by_time else False,
        }
        for t in occurrences
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/unit/test_control_schedules.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add src/celery_manager/control/schedules.py tests/unit/test_control_schedules.py
git commit -m "feat: core API for computing and pausing future schedule occurrences"
```

---

### Task 7: Core API — retry + audit log

**Files:**
- Create: `src/celery_manager/control/audit.py`
- Modify: `src/celery_manager/control/tasks.py` — add `retry_task`, `bulk_retry`
- Test: `tests/unit/test_control_retry_audit.py`

**Interfaces:**
- Consumes: `AuditLog` (Task 2), `TaskRun`, `start_task` (Task 5).
- Produces: `celery_manager.control.audit.record(session, username: str, action: str, target_type: str, target_id: str, details: dict | None = None) -> AuditLog`
- Produces: `celery_manager.control.tasks.retry_task(celery_app, session, task_id: str, username: str, args: list | None = None, kwargs: dict | None = None) -> str`
- Produces: `celery_manager.control.tasks.bulk_retry(celery_app, session, task_ids: list[str], username: str) -> list[str]`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_control_retry_audit.py
from unittest.mock import MagicMock

from celery import Celery
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from celery_manager.db.models import Base, TaskRun, AuditLog
from celery_manager.control.audit import record
from celery_manager.control.tasks import retry_task, bulk_retry


def _session_with_runs():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as session:
        session.add_all([
            TaskRun(task_id="t1", task_name="demo.echo", status="failed", args="[1]", kwargs="{}"),
            TaskRun(task_id="t2", task_name="demo.echo", status="failed", args="[2]", kwargs="{}"),
        ])
        session.commit()
    return Session()


def test_record_writes_audit_row():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine, expire_on_commit=False)()

    record(session, "alice", "cancel_occurrence", "schedule", "demo-every-2h", {"target_time": "2026-08-15T20:00:00Z"})

    row = session.query(AuditLog).one()
    assert row.username == "alice"
    assert row.action == "cancel_occurrence"


def test_retry_task_resends_original_args_and_logs_audit():
    session = _session_with_runs()
    app = MagicMock(spec=Celery)
    app.send_task.return_value = MagicMock(id="t1-retry")

    new_id = retry_task(app, session, "t1", username="alice")

    app.send_task.assert_called_once_with("demo.echo", args=[1], kwargs={})
    assert new_id == "t1-retry"
    audit_rows = session.query(AuditLog).all()
    assert len(audit_rows) == 1
    assert audit_rows[0].action == "retry"


def test_bulk_retry_returns_all_new_ids():
    session = _session_with_runs()
    app = MagicMock(spec=Celery)
    app.send_task.side_effect = [MagicMock(id="t1-retry"), MagicMock(id="t2-retry")]

    new_ids = bulk_retry(app, session, ["t1", "t2"], username="alice")

    assert new_ids == ["t1-retry", "t2-retry"]
    assert session.query(AuditLog).count() == 2
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_control_retry_audit.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'celery_manager.control.audit'`

- [ ] **Step 3: Write `src/celery_manager/control/audit.py`**

```python
from __future__ import annotations

import json
from datetime import datetime, timezone

from celery_manager.db.models import AuditLog


def record(session, username: str, action: str, target_type: str, target_id: str, details: dict | None = None) -> AuditLog:
    row = AuditLog(
        username=username,
        action=action,
        target_type=target_type,
        target_id=target_id,
        details=json.dumps(details) if details is not None else None,
        created_at=datetime.now(timezone.utc),
    )
    session.add(row)
    session.commit()
    return row
```

- [ ] **Step 4: Extend `src/celery_manager/control/tasks.py`**

Add to the bottom of the file:

```python
import json as _json

from celery_manager.control.audit import record as _record_audit


def retry_task(celery_app: Celery, session, task_id: str, username: str, args: list | None = None, kwargs: dict | None = None) -> str:
    """Re-run a task, by default with its original args/kwargs; pass
    args/kwargs to override them."""
    run = get_task_run(session, task_id)
    if run is None:
        raise ValueError(f"no task run found for {task_id}")

    final_args = args if args is not None else _json.loads(run.args or "[]")
    final_kwargs = kwargs if kwargs is not None else _json.loads(run.kwargs or "{}")

    new_id = start_task(celery_app, run.task_name, final_args, final_kwargs)
    _record_audit(session, username, "retry", "task", task_id, {"new_task_id": new_id})
    return new_id


def bulk_retry(celery_app: Celery, session, task_ids: list[str], username: str) -> list[str]:
    return [retry_task(celery_app, session, task_id, username) for task_id in task_ids]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/unit/test_control_retry_audit.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add src/celery_manager/control/audit.py src/celery_manager/control/tasks.py tests/unit/test_control_retry_audit.py
git commit -m "feat: retry (single + bulk) and audit logging"
```

---

### Task 8: Auth (users, password hashing, session cookies)

**Files:**
- Create: `src/celery_manager/auth/__init__.py`
- Create: `src/celery_manager/auth/users.py`
- Create: `src/celery_manager/auth/sessions.py`
- Test: `tests/unit/test_auth.py`

**Interfaces:**
- Consumes: `User` (Task 2), `secret_key` from config (Task 1).
- Produces: `create_user(session, username: str, password: str) -> User`
- Produces: `verify_password(session, username: str, password: str) -> User | None`
- Produces: `sign_session(secret_key: str, username: str) -> str` / `verify_session(secret_key: str, token: str) -> str | None`

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_auth.py
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from celery_manager.db.models import Base
from celery_manager.auth.users import create_user, verify_password
from celery_manager.auth.sessions import sign_session, verify_session


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def test_create_and_verify_password():
    session = _session()
    create_user(session, "alice", "correct-horse-battery-staple")

    assert verify_password(session, "alice", "correct-horse-battery-staple") is not None
    assert verify_password(session, "alice", "wrong-password") is None
    assert verify_password(session, "bob", "anything") is None


def test_password_is_hashed_not_stored_plain():
    session = _session()
    user = create_user(session, "alice", "correct-horse-battery-staple")
    assert user.password_hash != "correct-horse-battery-staple"


def test_session_round_trip():
    token = sign_session("test-secret", "alice")
    assert verify_session("test-secret", token) == "alice"


def test_session_rejects_wrong_secret():
    token = sign_session("test-secret", "alice")
    assert verify_session("different-secret", token) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_auth.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'celery_manager.auth'`

- [ ] **Step 3: Write `src/celery_manager/auth/__init__.py`**

```python
```

(empty — package marker)

- [ ] **Step 4: Write `src/celery_manager/auth/users.py`**

```python
from __future__ import annotations

import bcrypt

from celery_manager.db.models import User


def create_user(session, username: str, password: str) -> User:
    password_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    user = User(username=username, password_hash=password_hash)
    session.add(user)
    session.commit()
    return user


def verify_password(session, username: str, password: str) -> User | None:
    user = session.query(User).filter_by(username=username).one_or_none()
    if user is None:
        return None
    if bcrypt.checkpw(password.encode("utf-8"), user.password_hash.encode("utf-8")):
        return user
    return None
```

- [ ] **Step 5: Write `src/celery_manager/auth/sessions.py`**

```python
from __future__ import annotations

from itsdangerous import BadSignature, URLSafeTimedSerializer

_SALT = "celery-manager-session"


def sign_session(secret_key: str, username: str) -> str:
    serializer = URLSafeTimedSerializer(secret_key, salt=_SALT)
    return serializer.dumps({"username": username})


def verify_session(secret_key: str, token: str, max_age_seconds: int = 60 * 60 * 24 * 7) -> str | None:
    serializer = URLSafeTimedSerializer(secret_key, salt=_SALT)
    try:
        data = serializer.loads(token, max_age=max_age_seconds)
    except BadSignature:
        return None
    return data.get("username")
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/unit/test_auth.py -v`
Expected: PASS (4 tests)

- [ ] **Step 7: Commit**

```bash
git add src/celery_manager/auth tests/unit/test_auth.py
git commit -m "feat: built-in auth with bcrypt password hashing and signed session cookies"
```

---

### Task 9: REST API skeleton

**Files:**
- Create: `src/celery_manager/api/__init__.py`
- Create: `src/celery_manager/api/app.py`
- Create: `src/celery_manager/api/deps.py`
- Create: `src/celery_manager/api/routes/__init__.py`
- Create: `src/celery_manager/api/routes/auth.py`
- Test: `tests/unit/test_api_auth.py`

**Interfaces:**
- Consumes: `CeleryManagerConfig` (Task 1); `verify_password`, `sign_session`, `verify_session` (Task 8).
- Produces: `celery_manager.api.app.create_app(config: CeleryManagerConfig) -> FastAPI`
- Produces: `celery_manager.api.deps.get_current_user` (FastAPI dependency; raises 401 if no/invalid session cookie)
- Produces: `POST /api/auth/login`, `POST /api/auth/logout` on the app returned by `create_app`.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_api_auth.py
from fastapi.testclient import TestClient
from celery import Celery

from celery_manager.config import CeleryManagerConfig
from celery_manager.api.app import create_app
from celery_manager.auth.users import create_user
from celery_manager.db.session import get_session_factory


def _client():
    config = CeleryManagerConfig(celery_app=Celery("test"), db_url="sqlite:///:memory:", secret_key="test-secret")
    app, session_factory = create_app(config)
    with session_factory() as session:
        create_user(session, "alice", "correct-horse-battery-staple")
    return TestClient(app)


def test_login_succeeds_with_correct_credentials():
    client = _client()
    response = client.post("/api/auth/login", json={"username": "alice", "password": "correct-horse-battery-staple"})
    assert response.status_code == 200
    assert "session" in response.cookies


def test_login_fails_with_wrong_password():
    client = _client()
    response = client.post("/api/auth/login", json={"username": "alice", "password": "wrong"})
    assert response.status_code == 401


def test_protected_route_requires_session():
    client = _client()
    response = client.get("/api/whoami")
    assert response.status_code == 401


def test_protected_route_succeeds_after_login():
    client = _client()
    client.post("/api/auth/login", json={"username": "alice", "password": "correct-horse-battery-staple"})
    response = client.get("/api/whoami")
    assert response.status_code == 200
    assert response.json() == {"username": "alice"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_api_auth.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'celery_manager.api'`

- [ ] **Step 3: Write `src/celery_manager/api/__init__.py`** and **`src/celery_manager/api/routes/__init__.py`**

```python
```

(both empty — package markers)

- [ ] **Step 4: Write `src/celery_manager/api/deps.py`**

```python
from __future__ import annotations

from fastapi import Cookie, HTTPException, Request

from celery_manager.auth.sessions import verify_session


def get_current_user(request: Request, session: str | None = Cookie(default=None)) -> str:
    config = request.app.state.cm_config
    if session is None:
        raise HTTPException(status_code=401, detail="not authenticated")
    username = verify_session(config.secret_key, session)
    if username is None:
        raise HTTPException(status_code=401, detail="invalid or expired session")
    return username
```

- [ ] **Step 5: Write `src/celery_manager/api/routes/auth.py`**

```python
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel

from celery_manager.api.deps import get_current_user
from celery_manager.auth.sessions import sign_session
from celery_manager.auth.users import verify_password

router = APIRouter(prefix="/api")


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/auth/login")
def login(payload: LoginRequest, request: Request, response: Response):
    config = request.app.state.cm_config
    session_factory = request.app.state.cm_session_factory
    with session_factory() as db_session:
        user = verify_password(db_session, payload.username, payload.password)
    if user is None:
        raise HTTPException(status_code=401, detail="invalid credentials")
    token = sign_session(config.secret_key, user.username)
    response.set_cookie("session", token, httponly=True, samesite="lax")
    return {"username": user.username}


@router.post("/auth/logout")
def logout(response: Response):
    response.delete_cookie("session")
    return {"ok": True}


@router.get("/whoami")
def whoami(username: str = Depends(get_current_user)):
    return {"username": username}
```

- [ ] **Step 6: Write `src/celery_manager/api/app.py`**

```python
from __future__ import annotations

from fastapi import FastAPI

from celery_manager.config import CeleryManagerConfig
from celery_manager.db.session import init_db, get_session_factory
from celery_manager.decorator import configure as configure_decorator
from celery_manager.api.routes import auth as auth_routes


def create_app(config: CeleryManagerConfig):
    """Build the celery-manager FastAPI app. Returns (app, session_factory)
    — callers that need direct DB access (tests, the events listener CLI)
    use the session_factory; HTTP consumers just use the app."""
    engine = init_db(config)
    session_factory = get_session_factory(engine)
    configure_decorator(session_factory, eta_tolerance_seconds=config.eta_tolerance_seconds)

    app = FastAPI(title="celery-manager")
    app.state.cm_config = config
    app.state.cm_session_factory = session_factory

    app.include_router(auth_routes.router)

    return app, session_factory
```

- [ ] **Step 7: Run tests to verify they pass**

Run: `pytest tests/unit/test_api_auth.py -v`
Expected: PASS (4 tests)

- [ ] **Step 8: Commit**

```bash
git add src/celery_manager/api tests/unit/test_api_auth.py
git commit -m "feat: REST API skeleton with FastAPI app factory and cookie-based auth"
```

---

### Task 10: REST API — task & schedule endpoints

**Files:**
- Create: `src/celery_manager/api/routes/tasks.py`
- Create: `src/celery_manager/api/routes/schedules.py`
- Modify: `src/celery_manager/api/app.py` — register new routers
- Test: `tests/unit/test_api_tasks.py`
- Test: `tests/unit/test_api_schedules.py`

**Interfaces:**
- Consumes: everything from Tasks 5–9.
- Produces: `GET /api/tasks`, `GET /api/tasks/{task_id}`, `POST /api/tasks/{task_id}/retry`, `POST /api/tasks/retry-bulk`, `POST /api/tasks/{task_id}/cancel`, `POST /api/tasks/start`
- Produces: `GET /api/schedules/{name}/occurrences`, `POST /api/schedules/{name}/occurrences/pause`

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/test_api_tasks.py
from unittest.mock import patch

from fastapi.testclient import TestClient
from celery import Celery

from celery_manager.config import CeleryManagerConfig
from celery_manager.api.app import create_app
from celery_manager.auth.users import create_user
from celery_manager.db.models import TaskRun


def _client_and_session():
    config = CeleryManagerConfig(celery_app=Celery("test"), db_url="sqlite:///:memory:", secret_key="test-secret")
    app, session_factory = create_app(config)
    with session_factory() as session:
        create_user(session, "alice", "pw")
        session.add(TaskRun(task_id="t1", task_name="demo.echo", status="failed", args="[]", kwargs="{}"))
        session.commit()
    client = TestClient(app)
    client.post("/api/auth/login", json={"username": "alice", "password": "pw"})
    return client, session_factory


def test_list_tasks_requires_auth():
    config = CeleryManagerConfig(celery_app=Celery("test"), db_url="sqlite:///:memory:", secret_key="test-secret")
    app, _ = create_app(config)
    response = TestClient(app).get("/api/tasks")
    assert response.status_code == 401


def test_list_tasks_returns_seeded_run():
    client, _ = _client_and_session()
    response = client.get("/api/tasks")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["task_id"] == "t1"


def test_get_task_detail():
    client, _ = _client_and_session()
    response = client.get("/api/tasks/t1")
    assert response.status_code == 200
    assert response.json()["task_name"] == "demo.echo"


def test_get_task_detail_404_when_missing():
    client, _ = _client_and_session()
    response = client.get("/api/tasks/does-not-exist")
    assert response.status_code == 404


def test_retry_task_endpoint():
    client, _ = _client_and_session()
    with patch("celery_manager.api.routes.tasks.retry_task", return_value="t1-retry") as mock_retry:
        response = client.post("/api/tasks/t1/retry")
    assert response.status_code == 200
    assert response.json() == {"new_task_id": "t1-retry"}
    mock_retry.assert_called_once()
```

```python
# tests/unit/test_api_schedules.py
from datetime import timedelta

from fastapi.testclient import TestClient
from celery import Celery
from celery.schedules import schedule as interval_schedule

from celery_manager.config import CeleryManagerConfig
from celery_manager.api.app import create_app
from celery_manager.auth.users import create_user


def _client_and_session():
    celery_app = Celery("test")
    celery_app.conf.beat_schedule = {
        "demo-every-2h": {"task": "demo.echo", "schedule": interval_schedule(run_every=timedelta(hours=2))},
    }
    config = CeleryManagerConfig(celery_app=celery_app, db_url="sqlite:///:memory:", secret_key="test-secret")
    # create_app() calls sync_periodic_schedules() at startup (Task 10 Step 5),
    # which is what populates periodic_schedules from beat_schedule above.
    app, session_factory = create_app(config)
    with session_factory() as session:
        create_user(session, "alice", "pw")
    client = TestClient(app)
    client.post("/api/auth/login", json={"username": "alice", "password": "pw"})
    return client


def test_pause_occurrence_endpoint_creates_override():
    client = _client_and_session()
    target = "2026-08-15T20:00:00+00:00"
    response = client.post(
        "/api/schedules/demo-every-2h/occurrences/pause",
        json={"target_time": target, "action": "skip"},
    )
    assert response.status_code == 200
    assert response.json()["action"] == "skip"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/unit/test_api_tasks.py tests/unit/test_api_schedules.py -v`
Expected: FAIL — routes not registered / modules not found

- [ ] **Step 3: Write `src/celery_manager/api/routes/tasks.py`**

```python
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from celery_manager.api.deps import get_current_user
from celery_manager.control.tasks import (
    list_task_runs,
    get_task_run,
    start_task,
    cancel_task,
    retry_task,
    bulk_retry,
)

router = APIRouter(prefix="/api/tasks")


@router.get("")
def list_tasks(request: Request, status: str | None = None, task_name: str | None = None, username: str = Depends(get_current_user)):
    session_factory = request.app.state.cm_session_factory
    with session_factory() as session:
        runs = list_task_runs(session, status=status, task_name=task_name)
        return [
            {"task_id": r.task_id, "task_name": r.task_name, "status": r.status, "started_at": r.started_at, "finished_at": r.finished_at}
            for r in runs
        ]


@router.get("/{task_id}")
def get_task(task_id: str, request: Request, username: str = Depends(get_current_user)):
    session_factory = request.app.state.cm_session_factory
    with session_factory() as session:
        run = get_task_run(session, task_id)
        if run is None:
            raise HTTPException(status_code=404, detail="not found")
        return {
            "task_id": run.task_id,
            "task_name": run.task_name,
            "status": run.status,
            "args": run.args,
            "kwargs": run.kwargs,
            "exception": run.exception,
            "traceback": run.traceback,
            "log_output": run.log_output,
        }


class StartRequest(BaseModel):
    task_name: str
    args: list = []
    kwargs: dict = {}


@router.post("/start")
def start(payload: StartRequest, request: Request, username: str = Depends(get_current_user)):
    celery_app = request.app.state.cm_config.celery_app
    task_id = start_task(celery_app, payload.task_name, payload.args, payload.kwargs)
    return {"task_id": task_id}


@router.post("/{task_id}/cancel")
def cancel(task_id: str, request: Request, terminate: bool = False, username: str = Depends(get_current_user)):
    celery_app = request.app.state.cm_config.celery_app
    cancel_task(celery_app, task_id, terminate=terminate)
    return {"ok": True}


@router.post("/{task_id}/retry")
def retry(task_id: str, request: Request, username: str = Depends(get_current_user)):
    celery_app = request.app.state.cm_config.celery_app
    session_factory = request.app.state.cm_session_factory
    with session_factory() as session:
        new_id = retry_task(celery_app, session, task_id, username=username)
    return {"new_task_id": new_id}


class BulkRetryRequest(BaseModel):
    task_ids: list[str]


@router.post("/retry-bulk")
def retry_bulk(payload: BulkRetryRequest, request: Request, username: str = Depends(get_current_user)):
    celery_app = request.app.state.cm_config.celery_app
    session_factory = request.app.state.cm_session_factory
    with session_factory() as session:
        new_ids = bulk_retry(celery_app, session, payload.task_ids, username=username)
    return {"new_task_ids": new_ids}
```

- [ ] **Step 4: Write `src/celery_manager/api/routes/schedules.py`**

```python
from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from celery_manager.api.deps import get_current_user
from celery_manager.control.schedules import pause_occurrence, list_upcoming_with_overrides

router = APIRouter(prefix="/api/schedules")


class PauseRequest(BaseModel):
    target_time: datetime
    action: str  # skip | cancel | reschedule


@router.post("/{name}/occurrences/pause")
def pause(name: str, payload: PauseRequest, request: Request, username: str = Depends(get_current_user)):
    session_factory = request.app.state.cm_session_factory
    with session_factory() as session:
        override = pause_occurrence(session, name, payload.target_time, payload.action, created_by=username)
    return {"id": override.id, "action": override.action, "target_time": override.target_time}


@router.get("/{name}/occurrences")
def occurrences(name: str, request: Request, username: str = Depends(get_current_user)):
    """Every upcoming occurrence for this schedule (not just already-paused
    ones) so the dashboard has something to act on — see
    list_upcoming_with_overrides in Task 6."""
    celery_app = request.app.state.cm_config.celery_app
    session_factory = request.app.state.cm_session_factory
    with session_factory() as session:
        return list_upcoming_with_overrides(session, celery_app, name)
```

- [ ] **Step 5: Wire the new routers into `create_app`, and sync schedules on startup**

In `src/celery_manager/api/app.py`, add the imports:

```python
from celery_manager.api.routes import auth as auth_routes, tasks as task_routes, schedules as schedule_routes
from celery_manager.control.schedules import sync_periodic_schedules
```

After `configure_decorator(session_factory, eta_tolerance_seconds=config.eta_tolerance_seconds)`, add:

```python
    with session_factory() as session:
        sync_periodic_schedules(session, config.celery_app)
```

(This is what populates `periodic_schedules` from the host app's `beat_schedule` — without it, `/api/schedules/{name}/occurrences` has no row to look up. See Task 6.)

and after `app.include_router(auth_routes.router)`:

```python
    app.include_router(task_routes.router)
    app.include_router(schedule_routes.router)
```

- [ ] **Step 6: Add an occurrences-listing test to `tests/unit/test_api_schedules.py`**

Append:

```python
def test_list_occurrences_endpoint_shows_computed_upcoming_runs():
    client = _client_and_session()
    response = client.get("/api/schedules/demo-every-2h/occurrences")
    assert response.status_code == 200
    body = response.json()
    assert len(body) > 0
    assert "target_time" in body[0]
    assert body[0]["action"] is None


def test_paused_occurrence_shows_as_paused_on_next_request():
    # Each TestClient request opens its own session via session_factory()
    # (see Task 10 Step 5's create_app wiring) — this deliberately checks
    # the pause takes effect across two separate requests/sessions, not
    # just within one, since that's what a real browser does and it's the
    # exact scenario Task 6's _as_aware_utc normalization exists for.
    client = _client_and_session()
    occurrences = client.get("/api/schedules/demo-every-2h/occurrences").json()
    target_time = occurrences[0]["target_time"]

    pause_response = client.post(
        "/api/schedules/demo-every-2h/occurrences/pause",
        json={"target_time": target_time, "action": "skip"},
    )
    assert pause_response.status_code == 200

    refreshed = client.get("/api/schedules/demo-every-2h/occurrences").json()
    assert refreshed[0]["action"] == "skip"
    assert refreshed[0]["consumed"] is False
```

This exercises `sync_periodic_schedules` (called at `create_app` startup in Step 5) together with `list_upcoming_with_overrides` (Task 6) — both driven by the `beat_schedule` the fixture already sets on `celery_app.conf` above.

- [ ] **Step 7: Run tests to verify they pass**

Run: `pytest tests/unit/test_api_tasks.py tests/unit/test_api_schedules.py -v`
Expected: PASS (8 tests)

- [ ] **Step 7: Commit**

```bash
git add src/celery_manager/api tests/unit/test_api_tasks.py tests/unit/test_api_schedules.py
git commit -m "feat: REST endpoints for task control and schedule occurrence pausing"
```

---

### Task 11: REST API — SSE live updates

**Files:**
- Create: `src/celery_manager/api/sse.py`
- Modify: `src/celery_manager/api/app.py` — register SSE route, add an in-process pub/sub hook
- Modify: `src/celery_manager/events/listener.py` — publish to the pub/sub hook after each upsert
- Test: `tests/unit/test_api_sse.py`

**Interfaces:**
- Produces: `celery_manager.api.sse.EventBus` — simple in-process pub/sub (asyncio.Queue-based) other modules publish task-status-change dicts to.
- Produces: `GET /api/stream` — SSE endpoint yielding `data: {...}\n\n` lines as events are published.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_api_sse.py
import json

from fastapi.testclient import TestClient
from celery import Celery

from celery_manager.config import CeleryManagerConfig
from celery_manager.api.app import create_app
from celery_manager.auth.users import create_user


def test_stream_emits_published_event():
    config = CeleryManagerConfig(celery_app=Celery("test"), db_url="sqlite:///:memory:", secret_key="test-secret")
    app, session_factory = create_app(config)
    with session_factory() as session:
        create_user(session, "alice", "pw")

    client = TestClient(app)
    client.post("/api/auth/login", json={"username": "alice", "password": "pw"})

    app.state.cm_event_bus.publish({"task_id": "t1", "status": "succeeded"})

    with client.stream("GET", "/api/stream") as response:
        assert response.status_code == 200
        line = next(response.iter_lines())
        assert line.startswith("data: ")
        payload = json.loads(line[len("data: "):])
        assert payload == {"task_id": "t1", "status": "succeeded"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_api_sse.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'celery_manager.api.sse'`

- [ ] **Step 3: Write `src/celery_manager/api/sse.py`**

```python
from __future__ import annotations

import asyncio
import json


class EventBus:
    """In-process pub/sub for task-status-change events. The events
    listener (Task 4) publishes here; the /api/stream endpoint below
    subscribes and forwards to connected browsers as SSE."""

    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue] = []
        self._pending: list[dict] = []  # buffer for publishes before any subscriber connects (used in tests)

    def publish(self, event: dict) -> None:
        if not self._subscribers:
            self._pending.append(event)
            return
        for queue in self._subscribers:
            queue.put_nowait(event)

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        for event in self._pending:
            queue.put_nowait(event)
        self._pending.clear()
        self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        if queue in self._subscribers:
            self._subscribers.remove(queue)


async def sse_stream(bus: EventBus):
    queue = bus.subscribe()
    try:
        while True:
            event = await queue.get()
            yield f"data: {json.dumps(event, default=str)}\n\n"
    finally:
        bus.unsubscribe(queue)
```

- [ ] **Step 4: Register the SSE route in `src/celery_manager/api/app.py`**

Add the import:

```python
from fastapi.responses import StreamingResponse

from celery_manager.api.sse import EventBus, sse_stream
```

Inside `create_app`, after setting `app.state.cm_session_factory`:

```python
    app.state.cm_event_bus = EventBus()
```

After the router registrations, add the route directly:

```python
    @app.get("/api/stream")
    def stream(request):
        return StreamingResponse(sse_stream(app.state.cm_event_bus), media_type="text/event-stream")
```

- [ ] **Step 5: Publish from the events listener**

In `src/celery_manager/events/listener.py`, extend `EventListener.__init__` to optionally accept a bus, and publish after each upsert:

```python
class EventListener:
    def __init__(self, config: CeleryManagerConfig, session_factory, event_bus=None):
        self._config = config
        self._session_factory = session_factory
        self._event_bus = event_bus

    def run(self) -> None:
        app = self._config.celery_app
        state = app.events.State()

        def on_event(event):
            state.event(event)
            with self._session_factory() as session:
                upsert_task_run(session, event)
                session.commit()
            if self._event_bus is not None:
                self._event_bus.publish({"task_id": event.get("uuid"), "type": event.get("type")})

        with app.connection() as connection:
            receiver = app.events.Receiver(connection, handlers={"*": on_event})
            receiver.capture(limit=None, timeout=None, wakeup=True)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/unit/test_api_sse.py -v`
Expected: PASS

- [ ] **Step 7: Run the full test suite so far**

Run: `pytest tests/ -v`
Expected: PASS (all tests from Tasks 2–11)

- [ ] **Step 8: Commit**

```bash
git add src/celery_manager/api src/celery_manager/events/listener.py tests/unit/test_api_sse.py
git commit -m "feat: SSE live-update stream wired to the events listener"
```

---

### Task 12: Dashboard SPA scaffold + design system + login

**Files:**
- Create: `frontend/package.json`, `frontend/vite.config.ts`, `frontend/tailwind.config.ts`, `frontend/tsconfig.json`, `frontend/index.html`
- Create: `frontend/src/main.tsx`, `frontend/src/App.tsx`, `frontend/src/api/client.ts`
- Create: `frontend/src/pages/Login.tsx`
- Create: `frontend/src/styles/tokens.css` (design tokens: color, spacing, type scale — light + dark)
- Create: `src/celery_manager/dashboard/__init__.py`
- Modify: `src/celery_manager/api/app.py` — mount `StaticFiles` for the built SPA

**Interfaces:**
- Produces: a Vite-buildable React+TS app under `frontend/`, building to `frontend/dist/`.
- Produces: `src/celery_manager/dashboard/static/` — the *committed* copy of the latest `frontend/dist/` build, served by FastAPI's `StaticFiles` at `/`.
- Consumes: `POST /api/auth/login` (Task 9).

- [ ] **Step 1: Scaffold the Vite project**

Run: `npm create vite@latest frontend -- --template react-ts` (accept defaults), then `cd frontend && npm install && npm install -D tailwindcss postcss autoprefixer && npx tailwindcss init -p`

- [ ] **Step 2: Invoke the frontend-design skill before writing any screen**

Per spec §9, before building the Login screen (or any dashboard screen), load `frontend-design` for typography/color/layout direction rather than default component styling. Apply its output to `frontend/src/styles/tokens.css` (CSS custom properties for light + dark) and reference those tokens from every component — no ad hoc hex values in component files.

- [ ] **Step 3: Write `frontend/src/api/client.ts`**

```typescript
const BASE = "/api";

export async function login(username: string, password: string): Promise<{ username: string }> {
  const response = await fetch(`${BASE}/auth/login`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ username, password }),
  });
  if (!response.ok) {
    throw new Error(response.status === 401 ? "Invalid username or password" : "Login failed");
  }
  return response.json();
}

export async function whoami(): Promise<{ username: string } | null> {
  const response = await fetch(`${BASE}/whoami`, { credentials: "include" });
  if (!response.ok) return null;
  return response.json();
}
```

- [ ] **Step 4: Write `frontend/src/pages/Login.tsx`**

```tsx
import { useState } from "react";
import { login } from "../api/client";

export function Login({ onSuccess }: { onSuccess: (username: string) => void }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    setLoading(true);
    try {
      const user = await login(username, password);
      onSuccess(user.username);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Login failed");
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="cm-login-shell">
      <form className="cm-login-card" onSubmit={handleSubmit}>
        <h1>celery-manager</h1>
        <label>
          Username
          <input value={username} onChange={(e) => setUsername(e.target.value)} autoFocus required />
        </label>
        <label>
          Password
          <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} required />
        </label>
        {error && <p role="alert" className="cm-error">{error}</p>}
        <button type="submit" disabled={loading}>{loading ? "Signing in…" : "Sign in"}</button>
      </form>
    </div>
  );
}
```

(Class names above are hooks for the design-system CSS written in Step 2 — the frontend-design pass determines actual visual treatment, spacing, and states including a disabled/loading button style and error styling; this step defines structure and behavior, not final visuals.)

- [ ] **Step 5: Write `frontend/src/App.tsx`**

```tsx
import { useEffect, useState } from "react";
import { whoami } from "./api/client";
import { Login } from "./pages/Login";

export function App() {
  const [username, setUsername] = useState<string | null>(null);
  const [checked, setChecked] = useState(false);

  useEffect(() => {
    whoami().then((user) => {
      setUsername(user?.username ?? null);
      setChecked(true);
    });
  }, []);

  if (!checked) return null;
  if (!username) return <Login onSuccess={setUsername} />;

  return <div>Signed in as {username} — dashboard screens land in Tasks 13-15.</div>;
}
```

- [ ] **Step 6: Write `frontend/src/main.tsx`**

```tsx
import React from "react";
import ReactDOM from "react-dom/client";
import { App } from "./App";
import "./styles/tokens.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
```

- [ ] **Step 7: Build and copy the output into the package**

Run: `cd frontend && npm run build && rm -rf ../src/celery_manager/dashboard/static && cp -r dist ../src/celery_manager/dashboard/static`

- [ ] **Step 8: Mount static files in `src/celery_manager/api/app.py`**

Add the import:

```python
from pathlib import Path

from fastapi.staticfiles import StaticFiles
```

At the end of `create_app`, before `return app, session_factory`:

```python
    static_dir = Path(__file__).parent.parent / "dashboard" / "static"
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=static_dir, html=True), name="dashboard")
```

- [ ] **Step 9: Manual verification**

Run: `uvicorn` against a small throwaway script that calls `create_app` with a test config, open `http://localhost:8000/` in a browser, confirm the login screen renders and a correct/incorrect login both behave as expected. (A scripted smoke test replaces this once the Django demo exists — Task 16.)

- [ ] **Step 10: Commit**

```bash
git add frontend src/celery_manager/dashboard src/celery_manager/api/app.py
git commit -m "feat: dashboard SPA scaffold with design tokens and login screen"
```

---

### Task 13: Dashboard — task list & detail

**Files:**
- Create: `frontend/src/pages/TaskList.tsx`, `frontend/src/pages/TaskDetail.tsx`
- Create: `frontend/src/api/tasks.ts`
- Modify: `frontend/src/App.tsx` — add routing (react-router) between list/detail
- Modify: `frontend/src/styles/tokens.css` — extend design system for table/detail states

**Interfaces:**
- Consumes: `GET /api/tasks`, `GET /api/tasks/{task_id}` (Task 10); SSE stream (Task 11) for live status updates.
- Produces: `frontend/src/api/tasks.ts` exports `listTasks()`, `getTask(id)`, `subscribeToUpdates(onEvent)`.

- [ ] **Step 1: Add `react-router-dom`**

Run: `cd frontend && npm install react-router-dom`

- [ ] **Step 2: Write `frontend/src/api/tasks.ts`**

```typescript
const BASE = "/api";

export interface TaskSummary {
  task_id: string;
  task_name: string;
  status: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface TaskDetail extends TaskSummary {
  args: string;
  kwargs: string;
  exception: string | null;
  traceback: string | null;
  log_output: string | null;
}

export async function listTasks(params: { status?: string; task_name?: string } = {}): Promise<TaskSummary[]> {
  const query = new URLSearchParams(params as Record<string, string>).toString();
  const response = await fetch(`${BASE}/tasks${query ? `?${query}` : ""}`, { credentials: "include" });
  if (!response.ok) throw new Error("failed to load tasks");
  return response.json();
}

export async function getTask(taskId: string): Promise<TaskDetail> {
  const response = await fetch(`${BASE}/tasks/${taskId}`, { credentials: "include" });
  if (!response.ok) throw new Error("task not found");
  return response.json();
}

export function subscribeToUpdates(onEvent: (event: { task_id: string; type: string }) => void): () => void {
  const source = new EventSource(`${BASE}/stream`, { withCredentials: true });
  source.onmessage = (message) => onEvent(JSON.parse(message.data));
  return () => source.close();
}
```

- [ ] **Step 3: Write `frontend/src/pages/TaskList.tsx`**

```tsx
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { listTasks, subscribeToUpdates, TaskSummary } from "../api/tasks";

const STATUS_FILTERS = ["all", "queued", "started", "succeeded", "failed", "retried", "skipped"];

export function TaskList() {
  const [tasks, setTasks] = useState<TaskSummary[]>([]);
  const [status, setStatus] = useState("all");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  async function refresh() {
    try {
      const results = await listTasks(status === "all" ? {} : { status });
      setTasks(results);
      setError(null);
    } catch {
      setError("Couldn't load tasks.");
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    refresh();
  }, [status]);

  useEffect(() => subscribeToUpdates(() => refresh()), [status]);

  if (loading) return <p className="cm-loading">Loading tasks…</p>;
  if (error) return <p className="cm-error" role="alert">{error}</p>;

  return (
    <div className="cm-task-list">
      <div className="cm-filter-bar">
        {STATUS_FILTERS.map((s) => (
          <button key={s} className={s === status ? "cm-filter-active" : ""} onClick={() => setStatus(s)}>
            {s}
          </button>
        ))}
      </div>
      {tasks.length === 0 ? (
        <p className="cm-empty-state">No tasks match this filter yet.</p>
      ) : (
        <table>
          <thead>
            <tr><th>Task</th><th>Status</th><th>Started</th></tr>
          </thead>
          <tbody>
            {tasks.map((t) => (
              <tr key={t.task_id}>
                <td><Link to={`/tasks/${t.task_id}`}>{t.task_name}</Link></td>
                <td><span className={`cm-status cm-status-${t.status}`}>{t.status}</span></td>
                <td>{t.started_at ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
```

- [ ] **Step 4: Write `frontend/src/pages/TaskDetail.tsx`**

```tsx
import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { getTask, TaskDetail as TaskDetailType } from "../api/tasks";

export function TaskDetail() {
  const { taskId } = useParams<{ taskId: string }>();
  const [task, setTask] = useState<TaskDetailType | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!taskId) return;
    getTask(taskId).then(setTask).catch(() => setError("Task not found."));
  }, [taskId]);

  if (error) return <p className="cm-error" role="alert">{error}</p>;
  if (!task) return <p className="cm-loading">Loading…</p>;

  return (
    <div className="cm-task-detail">
      <h1>{task.task_name}</h1>
      <span className={`cm-status cm-status-${task.status}`}>{task.status}</span>
      <dl>
        <dt>Args</dt><dd><code>{task.args}</code></dd>
        <dt>Kwargs</dt><dd><code>{task.kwargs}</code></dd>
      </dl>
      {task.exception && (
        <section className="cm-failure">
          <h2>Why it failed</h2>
          <p>{task.exception}</p>
          <pre>{task.traceback}</pre>
        </section>
      )}
      <section>
        <h2>Log output</h2>
        {task.log_output ? <pre className="cm-log">{task.log_output}</pre> : <p className="cm-empty-state">No log output captured for this run.</p>}
      </section>
    </div>
  );
}
```

- [ ] **Step 5: Wire routing into `frontend/src/App.tsx`**

Replace the placeholder signed-in branch with:

```tsx
import { BrowserRouter, Routes, Route } from "react-router-dom";
import { TaskList } from "./pages/TaskList";
import { TaskDetail } from "./pages/TaskDetail";

// ...inside App(), replacing the placeholder return when signed in:
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/" element={<TaskList />} />
        <Route path="/tasks/:taskId" element={<TaskDetail />} />
      </Routes>
    </BrowserRouter>
  );
```

- [ ] **Step 6: Rebuild and copy static output**

Run: `cd frontend && npm run build && rm -rf ../src/celery_manager/dashboard/static && cp -r dist ../src/celery_manager/dashboard/static`

- [ ] **Step 7: Manual verification**

Serve the app (as in Task 12 Step 9), seed a couple of `TaskRun` rows via the core API in a throwaway script, confirm the list renders, filters work, and clicking through shows detail with log output / traceback sections in their correct states (present vs. empty).

- [ ] **Step 8: Commit**

```bash
git add frontend src/celery_manager/dashboard
git commit -m "feat: dashboard task list and detail screens with live updates"
```

---

### Task 14: Dashboard — future-schedule view + retry/bulk retry

**Files:**
- Create: `frontend/src/pages/ScheduleOccurrences.tsx`
- Create: `frontend/src/api/schedules.ts`
- Modify: `frontend/src/pages/TaskList.tsx` — add bulk-select + bulk retry
- Modify: `frontend/src/pages/TaskDetail.tsx` — add retry button
- Modify: `frontend/src/App.tsx` — add `/schedules/:name` route

**Interfaces:**
- Consumes: `GET /api/schedules/{name}/occurrences`, `POST /api/schedules/{name}/occurrences/pause`, `POST /api/tasks/{id}/retry`, `POST /api/tasks/retry-bulk` (Task 10).

- [ ] **Step 1: Write `frontend/src/api/schedules.ts`**

```typescript
const BASE = "/api";

export interface Occurrence {
  target_time: string;
  action: string;
  consumed: boolean;
}

export async function listOccurrences(name: string): Promise<Occurrence[]> {
  const response = await fetch(`${BASE}/schedules/${name}/occurrences`, { credentials: "include" });
  if (!response.ok) throw new Error("failed to load occurrences");
  return response.json();
}

export async function pauseOccurrence(name: string, targetTime: string, action: "skip" | "cancel"): Promise<void> {
  const response = await fetch(`${BASE}/schedules/${name}/occurrences/pause`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ target_time: targetTime, action }),
  });
  if (!response.ok) throw new Error("failed to pause occurrence");
}
```

- [ ] **Step 2: Write `frontend/src/pages/ScheduleOccurrences.tsx`**

```tsx
import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { listOccurrences, pauseOccurrence, Occurrence } from "../api/schedules";

export function ScheduleOccurrences() {
  const { name } = useParams<{ name: string }>();
  const [occurrences, setOccurrences] = useState<Occurrence[]>([]);
  const [error, setError] = useState<string | null>(null);

  async function refresh() {
    if (!name) return;
    try {
      setOccurrences(await listOccurrences(name));
      setError(null);
    } catch {
      setError("Couldn't load upcoming occurrences.");
    }
  }

  useEffect(() => { refresh(); }, [name]);

  async function handlePause(targetTime: string, action: "skip" | "cancel") {
    if (!name) return;
    await pauseOccurrence(name, targetTime, action);
    refresh();
  }

  if (error) return <p className="cm-error" role="alert">{error}</p>;

  return (
    <div className="cm-schedule-occurrences">
      <h1>{name} — upcoming runs</h1>
      {occurrences.length === 0 ? (
        <p className="cm-empty-state">No upcoming occurrences to show yet.</p>
      ) : (
        <ul>
          {occurrences.map((o) => (
            <li key={o.target_time} className={o.consumed ? "cm-occurrence-paused" : ""}>
              <span>{o.target_time}</span>
              {o.consumed ? (
                <span className="cm-badge">paused</span>
              ) : (
                <div className="cm-occurrence-actions">
                  <button onClick={() => handlePause(o.target_time, "skip")}>Skip this run</button>
                  <button onClick={() => handlePause(o.target_time, "cancel")}>Cancel this run</button>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
```

- [ ] **Step 3: Add retry button to `frontend/src/pages/TaskDetail.tsx`**

Add near the status badge:

```tsx
import { retryTask } from "../api/tasks";

// ...
  const [retrying, setRetrying] = useState(false);

  async function handleRetry() {
    if (!task) return;
    setRetrying(true);
    try {
      await retryTask(task.task_id);
    } finally {
      setRetrying(false);
    }
  }

// in the JSX, next to the status badge:
      {task.status === "failed" && (
        <button onClick={handleRetry} disabled={retrying}>{retrying ? "Retrying…" : "Retry"}</button>
      )}
```

Add the corresponding function to `frontend/src/api/tasks.ts`:

```typescript
export async function retryTask(taskId: string): Promise<{ new_task_id: string }> {
  const response = await fetch(`${BASE}/tasks/${taskId}/retry`, { method: "POST", credentials: "include" });
  if (!response.ok) throw new Error("retry failed");
  return response.json();
}

export async function bulkRetryTasks(taskIds: string[]): Promise<{ new_task_ids: string[] }> {
  const response = await fetch(`${BASE}/tasks/retry-bulk`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    credentials: "include",
    body: JSON.stringify({ task_ids: taskIds }),
  });
  if (!response.ok) throw new Error("bulk retry failed");
  return response.json();
}
```

- [ ] **Step 4: Add bulk-select and bulk retry to `frontend/src/pages/TaskList.tsx`**

Add selection state and a bulk-action bar:

```tsx
import { bulkRetryTasks } from "../api/tasks";

// ...
  const [selected, setSelected] = useState<Set<string>>(new Set());

  function toggle(taskId: string) {
    setSelected((prev) => {
      const next = new Set(prev);
      next.has(taskId) ? next.delete(taskId) : next.add(taskId);
      return next;
    });
  }

  async function handleBulkRetry() {
    await bulkRetryTasks([...selected]);
    setSelected(new Set());
    refresh();
  }

// in the JSX, above the table, when status === "failed" and selected.size > 0:
      {status === "failed" && selected.size > 0 && (
        <button onClick={handleBulkRetry}>Retry {selected.size} selected</button>
      )}

// each row gets a checkbox cell:
                <td><input type="checkbox" checked={selected.has(t.task_id)} onChange={() => toggle(t.task_id)} /></td>
```

- [ ] **Step 5: Add the route in `frontend/src/App.tsx`**

```tsx
import { ScheduleOccurrences } from "./pages/ScheduleOccurrences";

// add inside <Routes>:
        <Route path="/schedules/:name" element={<ScheduleOccurrences />} />
```

- [ ] **Step 6: Rebuild and copy static output**

Run: `cd frontend && npm run build && rm -rf ../src/celery_manager/dashboard/static && cp -r dist ../src/celery_manager/dashboard/static`

- [ ] **Step 7: Manual verification**

Seed a `PeriodicSchedule` row, navigate to `/schedules/<name>`, confirm occurrences list, "skip"/"cancel" work and reflect as paused; confirm single and bulk retry from the task list/detail screens produce new task runs.

- [ ] **Step 8: Commit**

```bash
git add frontend src/celery_manager/dashboard
git commit -m "feat: future-occurrence pause/cancel view, single and bulk retry"
```

---

### Task 15: Dashboard — audit log + static bundling wire-up

**Files:**
- Create: `frontend/src/pages/AuditLog.tsx`
- Create: `frontend/src/api/audit.ts`
- Create: `src/celery_manager/api/routes/audit.py`
- Modify: `src/celery_manager/api/app.py` — register audit router
- Modify: `frontend/src/App.tsx` — add `/audit` route + nav
- Test: `tests/unit/test_api_audit.py`

**Interfaces:**
- Produces: `GET /api/audit` — paginated audit log entries.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/test_api_audit.py
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from celery import Celery

from celery_manager.config import CeleryManagerConfig
from celery_manager.api.app import create_app
from celery_manager.auth.users import create_user
from celery_manager.db.models import AuditLog


def test_list_audit_log():
    config = CeleryManagerConfig(celery_app=Celery("test"), db_url="sqlite:///:memory:", secret_key="test-secret")
    app, session_factory = create_app(config)
    with session_factory() as session:
        create_user(session, "alice", "pw")
        session.add(AuditLog(username="alice", action="retry", target_type="task", target_id="t1", created_at=datetime.now(timezone.utc)))
        session.commit()

    client = TestClient(app)
    client.post("/api/auth/login", json={"username": "alice", "password": "pw"})
    response = client.get("/api/audit")

    assert response.status_code == 200
    assert response.json()[0]["action"] == "retry"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_api_audit.py -v`
Expected: FAIL — `404 Not Found` (no `/api/audit` route yet)

- [ ] **Step 3: Write `src/celery_manager/api/routes/audit.py`**

```python
from __future__ import annotations

from fastapi import APIRouter, Depends, Request

from celery_manager.api.deps import get_current_user
from celery_manager.db.models import AuditLog

router = APIRouter(prefix="/api/audit")


@router.get("")
def list_audit(request: Request, limit: int = 50, username: str = Depends(get_current_user)):
    session_factory = request.app.state.cm_session_factory
    with session_factory() as session:
        rows = session.query(AuditLog).order_by(AuditLog.id.desc()).limit(limit).all()
        return [
            {"username": r.username, "action": r.action, "target_type": r.target_type, "target_id": r.target_id, "details": r.details, "created_at": r.created_at}
            for r in rows
        ]
```

- [ ] **Step 4: Register the router in `src/celery_manager/api/app.py`**

Add `audit as audit_routes` to the existing routes import, and `app.include_router(audit_routes.router)` alongside the others.

- [ ] **Step 5: Run test to verify it passes**

Run: `pytest tests/unit/test_api_audit.py -v`
Expected: PASS

- [ ] **Step 6: Write `frontend/src/api/audit.ts`**

```typescript
const BASE = "/api";

export interface AuditEntry {
  username: string;
  action: string;
  target_type: string;
  target_id: string;
  details: string | null;
  created_at: string;
}

export async function listAuditLog(): Promise<AuditEntry[]> {
  const response = await fetch(`${BASE}/audit`, { credentials: "include" });
  if (!response.ok) throw new Error("failed to load audit log");
  return response.json();
}
```

- [ ] **Step 7: Write `frontend/src/pages/AuditLog.tsx`**

```tsx
import { useEffect, useState } from "react";
import { listAuditLog, AuditEntry } from "../api/audit";

export function AuditLog() {
  const [entries, setEntries] = useState<AuditEntry[]>([]);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listAuditLog().then(setEntries).catch(() => setError("Couldn't load the audit log."));
  }, []);

  if (error) return <p className="cm-error" role="alert">{error}</p>;
  if (entries.length === 0) return <p className="cm-empty-state">No manual actions recorded yet.</p>;

  return (
    <table className="cm-audit-log">
      <thead><tr><th>When</th><th>User</th><th>Action</th><th>Target</th></tr></thead>
      <tbody>
        {entries.map((e, i) => (
          <tr key={i}>
            <td>{e.created_at}</td>
            <td>{e.username}</td>
            <td>{e.action}</td>
            <td>{e.target_type}:{e.target_id}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
```

- [ ] **Step 8: Add the route and nav in `frontend/src/App.tsx`**

```tsx
import { AuditLog } from "./pages/AuditLog";

// add inside <Routes>:
        <Route path="/audit" element={<AuditLog />} />
```

Add a simple nav bar (design per the frontend-design tokens from Task 12) linking Tasks / Audit Log.

- [ ] **Step 9: Rebuild and copy static output**

Run: `cd frontend && npm run build && rm -rf ../src/celery_manager/dashboard/static && cp -r dist ../src/celery_manager/dashboard/static`

- [ ] **Step 10: Run the full test suite**

Run: `pytest tests/ -v`
Expected: PASS (all tests from every prior task)

- [ ] **Step 11: Commit**

```bash
git add frontend src/celery_manager/api src/celery_manager/dashboard tests/unit/test_api_audit.py
git commit -m "feat: audit log screen and endpoint; v1 dashboard feature-complete"
```

---

### Task 16: Django demo project

**Files:**
- Create: `examples/django_demo/manage.py`
- Create: `examples/django_demo/demo/settings.py`, `demo/urls.py`, `demo/celery.py`, `demo/wsgi.py`, `demo/__init__.py`
- Create: `examples/django_demo/tasks_app/__init__.py`, `tasks_app/tasks.py`, `tasks_app/apps.py`
- Create: `examples/django_demo/requirements.txt`
- Create: `examples/django_demo/README.md`

**Interfaces:**
- Consumes: the full `celery_manager` package (installed as `pip install -e ../..` inside the demo's own virtualenv, per its `requirements.txt`).

- [ ] **Step 1: Scaffold Django project**

Run (from `examples/django_demo/`): `django-admin startproject demo .` then `python manage.py startapp tasks_app`

- [ ] **Step 2: Write `examples/django_demo/requirements.txt`**

```
django>=5.0,<6.0
celery>=5.3,<6.0
redis>=5.0
-e ../..
```

- [ ] **Step 3: Write `examples/django_demo/demo/celery.py`**

```python
import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "demo.settings")

app = Celery("demo")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
```

- [ ] **Step 4: Update `examples/django_demo/demo/__init__.py`**

```python
from .celery import app as celery_app

__all__ = ("celery_app",)
```

- [ ] **Step 5: Add Celery + celery-manager settings to `examples/django_demo/demo/settings.py`**

Append:

```python
CELERY_BROKER_URL = "redis://localhost:6379/0"
CELERY_RESULT_BACKEND = "redis://localhost:6379/0"
CELERY_BEAT_SCHEDULE = {
    "send-demo-report-every-2-hours": {
        "task": "tasks_app.tasks.send_report",
        "schedule": 2 * 60 * 60,
        "args": (),
    },
}

CELERY_MANAGER_DB_URL = "sqlite:///celery_manager_demo.db"
CELERY_MANAGER_SECRET_KEY = "demo-secret-change-me"
```

Add `"tasks_app"` to `INSTALLED_APPS`.

- [ ] **Step 6: Write `examples/django_demo/tasks_app/tasks.py`**

```python
import logging
import random
import time

from celery import shared_task

from celery_manager.decorator import track
from demo.celery import app as celery_app
from celery_manager.config import CeleryManagerConfig
from celery_manager.decorator import configure
from celery_manager.db.session import init_db, get_session_factory
from django.conf import settings

_config = CeleryManagerConfig(
    celery_app=celery_app,
    db_url=settings.CELERY_MANAGER_DB_URL,
    secret_key=settings.CELERY_MANAGER_SECRET_KEY,
)
_engine = init_db(_config)
configure(get_session_factory(_engine), eta_tolerance_seconds=_config.eta_tolerance_seconds)

logger = logging.getLogger(__name__)


@shared_task(bind=True)
@track()
def send_welcome_email(self, user_email: str):
    logger.info("sending welcome email to %s", user_email)
    time.sleep(1)
    return f"sent to {user_email}"


@shared_task(bind=True)
@track(schedule_name="send-demo-report-every-2-hours")
def send_report(self):
    logger.info("generating demo report")
    if random.random() < 0.3:
        raise RuntimeError("report generator flaked (demo failure)")
    time.sleep(2)
    return "report sent"
```

- [ ] **Step 7: Mount celery-manager into the Django app's URLs**

Write `examples/django_demo/demo/urls.py`:

```python
from django.conf import settings
from django.urls import path
from django.http import HttpResponse

from celery_manager.api.app import create_app
from celery_manager.config import CeleryManagerConfig
from demo.celery import app as celery_app

_cm_config = CeleryManagerConfig(
    celery_app=celery_app,
    db_url=settings.CELERY_MANAGER_DB_URL,
    secret_key=settings.CELERY_MANAGER_SECRET_KEY,
)
cm_app, _ = create_app(_cm_config)

urlpatterns = [
    path("", lambda request: HttpResponse("Django demo app. Dashboard mounted at /dashboard/ (run via ASGI).")),
]
```

Note: `celery-manager`'s dashboard is a separate ASGI app (per the "framework-agnostic ASGI" architecture decision) — it is not served through Django's WSGI `urlpatterns`. Document in the README (Step 9) that it's run as its own `uvicorn` process alongside `manage.py runserver`, which is the intended v1 mounting story; a same-process ASGI-in-WSGI mount is a documented Phase 2 nicety, not required for v1.

- [ ] **Step 8: Write a small runner script for the dashboard process**

Create `examples/django_demo/run_dashboard.py`:

```python
import os

import django
import uvicorn

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "demo.settings")
django.setup()

from django.conf import settings

from celery_manager.api.app import create_app
from celery_manager.config import CeleryManagerConfig
from demo.celery import app as celery_app

config = CeleryManagerConfig(
    celery_app=celery_app,
    db_url=settings.CELERY_MANAGER_DB_URL,
    secret_key=settings.CELERY_MANAGER_SECRET_KEY,
)
app, _ = create_app(config)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8001)
```

- [ ] **Step 9: Write `examples/django_demo/README.md`**

```markdown
# celery-manager Django demo

1. `python -m venv .venv && source .venv/bin/activate`
2. `pip install -r requirements.txt`
3. Start Redis locally (e.g. `redis-server`).
4. `python manage.py migrate`
5. In separate terminals:
   - `python manage.py runserver` (the Django app)
   - `celery -A demo worker -l info` (the worker)
   - `celery -A demo beat -l info` (the periodic scheduler)
   - `celery-manager events --db-url sqlite:///celery_manager_demo.db` (the events listener)
   - `python run_dashboard.py` (the celery-manager dashboard, at http://localhost:8001)
6. Create a dashboard user: `python -c "from celery_manager.auth.users import create_user; from celery_manager.db.session import init_db, get_session_factory; from celery_manager.config import CeleryManagerConfig; from demo.celery import app; cfg = CeleryManagerConfig(celery_app=app, db_url='sqlite:///celery_manager_demo.db', secret_key='demo-secret-change-me'); Session = get_session_factory(init_db(cfg)); create_user(Session(), 'admin', 'admin')"`
7. Visit http://localhost:8001, sign in as `admin` / `admin`, trigger `send_welcome_email` from the "start task" action, and watch `send_report` fail ~30% of the time on its 2-hour schedule (pause an occurrence from the schedule view to see the skip take effect).
```

- [ ] **Step 10: Write `src/celery_manager/cli.py`** (referenced by the README's `celery-manager events` command; not yet created in earlier tasks)

```python
from __future__ import annotations

import argparse
import importlib

from celery_manager.config import CeleryManagerConfig
from celery_manager.db.session import init_db, get_session_factory
from celery_manager.events.listener import EventListener


def main() -> None:
    parser = argparse.ArgumentParser(prog="celery-manager")
    subparsers = parser.add_subparsers(dest="command", required=True)

    events_parser = subparsers.add_parser("events", help="run the events listener")
    events_parser.add_argument("--celery-app", required=True, help="import path, e.g. demo.celery:app")
    events_parser.add_argument("--db-url", required=True)
    events_parser.add_argument("--secret-key", default="change-me")

    args = parser.parse_args()

    if args.command == "events":
        module_path, attr = args.celery_app.split(":")
        module = importlib.import_module(module_path)
        celery_app = getattr(module, attr)

        config = CeleryManagerConfig(celery_app=celery_app, db_url=args.db_url, secret_key=args.secret_key)
        engine = init_db(config)
        session_factory = get_session_factory(engine)
        EventListener(config, session_factory).run()


if __name__ == "__main__":
    main()
```

Update the demo README's events command to `celery-manager events --celery-app demo.celery:app --db-url sqlite:///celery_manager_demo.db`.

- [ ] **Step 11: Manual end-to-end verification**

Follow the README from a clean clone: confirm the worker/beat/dashboard/events-listener all start, a manually-started task shows up live in the dashboard, the periodic `send_report` task's occasional failures show a traceback + log output, and pausing an upcoming `send-demo-report-every-2-hours` occurrence causes that run to be recorded as `skipped` rather than executed.

- [ ] **Step 12: Commit**

```bash
git add examples src/celery_manager/cli.py
git commit -m "feat: Django demo project and celery-manager CLI events command"
```

---

### Task 17: Docs site (MkDocs Material)

**Files:**
- Create: `mkdocs.yml`
- Create: `docs/index.md`, `docs/quickstart.md`, `docs/decorator.md`, `docs/rest-api.md`, `docs/dashboard.md`, `docs/configuration.md`
- Create: `docs/superpowers/` stays as-is (specs/plans; excluded from the published site nav)

**Interfaces:**
- Consumes: nothing code-level — documents the public interfaces built in Tasks 1–16.

- [ ] **Step 1: Write `mkdocs.yml`**

```yaml
site_name: celery-manager
theme:
  name: material
  palette:
    - scheme: default
      toggle:
        icon: material/brightness-7
        name: Switch to dark mode
    - scheme: slate
      toggle:
        icon: material/brightness-4
        name: Switch to light mode
nav:
  - Home: index.md
  - Quickstart: quickstart.md
  - The decorator: decorator.md
  - REST API: rest-api.md
  - Dashboard: dashboard.md
  - Configuration: configuration.md
```

- [ ] **Step 2: Write `docs/index.md`**

```markdown
# celery-manager

A dashboard and control API for Celery background tasks: see every task's
status and logs, start/stop/cancel/retry, and pause a single future
occurrence of a periodic task without touching its schedule.

- [Quickstart](quickstart.md)
- [The `@track` decorator](decorator.md)
- [REST API reference](rest-api.md)
- [Dashboard walkthrough](dashboard.md)
- [Configuration reference](configuration.md)

See the [Django demo](https://github.com/pythondeveloper6/celery-manager/tree/main/examples/django_demo)
for a complete working example.
```

- [ ] **Step 3: Write `docs/quickstart.md`**

```markdown
# Quickstart

## Install

```bash
pip install celery-manager
```

## Wire it into your app

```python
from celery_manager.config import CeleryManagerConfig
from celery_manager.api.app import create_app

config = CeleryManagerConfig(
    celery_app=your_celery_app,
    db_url="sqlite:///celery_manager.db",  # or any SQLAlchemy URL
    secret_key="a-real-secret-in-production",
)
app, _ = create_app(config)
```

Run it with any ASGI server, e.g. `uvicorn your_module:app`.

## Run the events listener

```bash
celery-manager events --celery-app your_module:your_celery_app --db-url sqlite:///celery_manager.db
```

This is a separate long-running process (alongside your worker and beat) — it's what gives the dashboard visibility into every task, decorated or not.

## Create your first dashboard user

```python
from celery_manager.auth.users import create_user
from celery_manager.db.session import init_db, get_session_factory

Session = get_session_factory(init_db(config))
create_user(Session(), "admin", "a-real-password")
```

Then sign in at the dashboard URL.
```

- [ ] **Step 4: Write `docs/decorator.md`**

```markdown
# The `@track` decorator

Apply `celery_manager.decorator.track()` above `@app.task` to opt a task
into two things:

1. **Per-occurrence schedule control** — if someone pauses/cancels a
   specific future run from the dashboard, this decorator is what makes
   the task actually skip its body at that run instead of executing.
2. **Full log capture** — stdout/stderr and `logging` calls made during
   the task are captured and shown on the task's detail page, not just the
   traceback on failure.

```python
from celery_manager.decorator import track

@app.task(bind=True)
@track(schedule_name="send-report-every-2-hours")
def send_report(self):
    ...
```

`schedule_name` should match the name you use for this schedule in the
dashboard's periodic schedules. If omitted, it defaults to the task's
registered name — fine as long as you only have one schedule per task.

Tasks *without* `@track` are still visible in the dashboard (the events
listener picks up every task automatically) but don't support
per-occurrence pause/cancel or full log capture — only the traceback
Celery already provides on failure.
```

- [ ] **Step 5: Write `docs/rest-api.md`, `docs/dashboard.md`, `docs/configuration.md`**

`docs/rest-api.md` — table of every endpoint built in Tasks 9–11 and 15 (method, path, auth requirement, request/response shape), generated by hand from the route files (`src/celery_manager/api/routes/*.py`).

`docs/dashboard.md` — walkthrough of each screen built in Tasks 12–15 (task list/filter, task detail, schedule occurrences, retry/bulk retry, audit log) with what each control does.

`docs/configuration.md` — every `CeleryManagerConfig` field (Task 1) with its default and what changing it affects (`db_url` backend swap, `eta_tolerance_seconds` trade-offs, `secret_key` production requirement).

- [ ] **Step 6: Add the docs build to dev dependencies**

In `pyproject.toml`, add to `[project.optional-dependencies]`:

```toml
docs = ["mkdocs-material>=9.5"]
```

- [ ] **Step 7: Build and verify**

Run: `pip install -e ".[docs]" && mkdocs build --strict`
Expected: builds with no warnings/errors.

- [ ] **Step 8: Commit**

```bash
git add mkdocs.yml docs pyproject.toml
git commit -m "docs: MkDocs Material site covering quickstart, decorator, REST API, dashboard, config"
```

---

### Task 18: Dashboard — periodic schedules list screen (closes a spec gap found during Task 15)

**Why this task exists:** spec §4.6 lists "periodic schedules list" as a required v1 screen, but the original Tasks 12–15 breakdown only ever planned the *per-schedule* occurrence view (`/schedules/:name`, Task 14) — never a screen to discover schedule names in the first place. Task 15's implementer found this while building the nav bar (no page to link "Schedules" to) and correctly declined to silently paper over it. Without this, the headline pause/cancel-a-future-occurrence feature is fully built and fully correct but effectively unreachable from the UI — a user would have to already know a schedule's exact name and hand-type the URL. This closes that gap.

**Files:**
- Modify: `src/celery_manager/control/schedules.py` — add `list_periodic_schedules`
- Modify: `src/celery_manager/api/routes/schedules.py` — add `GET /api/schedules`
- Create: `frontend/src/pages/ScheduleList.tsx`
- Modify: `frontend/src/api/schedules.ts` — add `listSchedules`
- Modify: `frontend/src/App.tsx` — add `/schedules` route
- Modify: `frontend/src/App.tsx`'s nav (added in Task 15) — add the "Schedules" link
- Test: `tests/unit/test_control_schedules.py`
- Test: `tests/unit/test_api_schedules.py`

**Interfaces:**
- Consumes: `PeriodicSchedule` (Task 2); `sync_periodic_schedules` (Task 6, already populates this table from `beat_schedule` at `create_app` startup — this task only adds a read path, no new sync logic).
- Produces: `list_periodic_schedules(session) -> list[PeriodicSchedule]`
- Produces: `GET /api/schedules` → `[{"name", "task_name", "schedule_spec", "enabled"}, ...]`, ordered by name.
- Produces: `frontend/src/api/schedules.ts` export `listSchedules(): Promise<ScheduleSummary[]>`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/test_control_schedules.py
def test_list_periodic_schedules_returns_all_ordered_by_name():
    session = _session()
    session.add(PeriodicSchedule(name="zzz-last", task_name="demo.echo", schedule_spec="1h"))
    session.add(PeriodicSchedule(name="aaa-first", task_name="demo.echo", schedule_spec="2h"))
    session.commit()

    result = list_periodic_schedules(session)

    assert [s.name for s in result] == ["aaa-first", "zzz-last"]


def test_list_periodic_schedules_empty_when_none_configured():
    session = _session()
    assert list_periodic_schedules(session) == []
```

Add `list_periodic_schedules` to the existing import line at the top of the file.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/unit/test_control_schedules.py -v -k list_periodic_schedules`
Expected: FAIL — `ImportError` (name not yet exported)

- [ ] **Step 3: Add `list_periodic_schedules` to `src/celery_manager/control/schedules.py`**

Append to the file:

```python
def list_periodic_schedules(session) -> list[PeriodicSchedule]:
    """Every configured periodic schedule, for the dashboard's schedule-list
    screen — populated by sync_periodic_schedules() at create_app startup,
    this is a pure read."""
    return session.query(PeriodicSchedule).order_by(PeriodicSchedule.name).all()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/unit/test_control_schedules.py -v -k list_periodic_schedules`
Expected: PASS (2 tests)

- [ ] **Step 5: Write the failing API test**

```python
# append to tests/unit/test_api_schedules.py
def test_list_schedules_endpoint_returns_synced_schedules():
    client = _client_and_session()
    response = client.get("/api/schedules")
    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["name"] == "demo-every-2h"
    assert body[0]["task_name"] == "demo.echo"
    assert body[0]["enabled"] is True


def test_list_schedules_endpoint_requires_auth():
    from celery import Celery
    from celery_manager.api.app import create_app
    from celery_manager.config import CeleryManagerConfig
    from fastapi.testclient import TestClient

    config = CeleryManagerConfig(celery_app=Celery("test"), db_url="sqlite:///:memory:", secret_key="test-secret")
    app, _ = create_app(config)
    response = TestClient(app).get("/api/schedules")
    assert response.status_code == 401
```

- [ ] **Step 6: Run test to verify it fails**

Run: `pytest tests/unit/test_api_schedules.py -v -k list_schedules`
Expected: FAIL — `404 Not Found` (route doesn't exist yet)

- [ ] **Step 7: Add the route to `src/celery_manager/api/routes/schedules.py`**

Add `list_periodic_schedules` to the existing `control.schedules` import, then append:

```python
@router.get("")
def list_schedules(request: Request, username: str = Depends(get_current_user)):
    session_factory = request.app.state.cm_session_factory
    with session_factory() as session:
        schedules = list_periodic_schedules(session)
        return [
            {"name": s.name, "task_name": s.task_name, "schedule_spec": s.schedule_spec, "enabled": s.enabled}
            for s in schedules
        ]
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `pytest tests/unit/test_api_schedules.py -v`
Expected: PASS (all tests in the file, including the 2 new ones)

- [ ] **Step 9: Add `listSchedules` to `frontend/src/api/schedules.ts`**

Append:

```typescript
export interface ScheduleSummary {
  name: string;
  task_name: string;
  schedule_spec: string;
  enabled: boolean;
}

export async function listSchedules(): Promise<ScheduleSummary[]> {
  const response = await fetch(`${BASE}/schedules`, { credentials: "include" });
  if (!response.ok) throw new Error("failed to load schedules");
  return response.json();
}
```

- [ ] **Step 10: Write `frontend/src/pages/ScheduleList.tsx`**

```tsx
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { listSchedules, ScheduleSummary } from "../api/schedules";

export function ScheduleList() {
  const [schedules, setSchedules] = useState<ScheduleSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    listSchedules()
      .then(setSchedules)
      .catch(() => setError("Couldn't load schedules."));
  }, []);

  if (error) return <p className="cm-error" role="alert">{error}</p>;
  if (schedules === null) return <p className="cm-loading">Loading…</p>;

  return (
    <div className="cm-page">
      <header className="cm-page-header">
        <p className="cm-page-eyebrow">celery-manager</p>
        <h1 className="cm-page-title">Periodic schedules</h1>
      </header>
      {schedules.length === 0 ? (
        <p className="cm-empty-state">No periodic schedules configured yet.</p>
      ) : (
        <ul className="cm-schedule-list">
          {schedules.map((s) => (
            <li key={s.name} className="cm-schedule-list-row">
              <Link to={`/schedules/${s.name}`} className="cm-schedule-list-name">
                {s.name}
              </Link>
              <span className="cm-schedule-list-meta">{s.task_name} · {s.schedule_spec}</span>
              {!s.enabled && <span className="cm-badge">disabled</span>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
```

Style `.cm-schedule-list*` using the existing design tokens (`--cm-color-*`, `--cm-space-*`, etc. established in Tasks 12–15) — this is a continuation of the same design system, not a new visual decision. Follow `TaskList.tsx`'s row/link conventions for consistency.

- [ ] **Step 11: Wire the route and nav link into `App.tsx`**

Read the file's current content first (last touched by Task 15). Add:

```tsx
import { ScheduleList } from "./pages/ScheduleList";
```

Add inside `<Routes>`, alongside the existing `/schedules/:name` route:

```tsx
        <Route path="/schedules" element={<ScheduleList />} />
```

In the nav component Task 15 added, add a "Schedules" link pointing to `/schedules`, following the same `aria-current`/active-state pattern as the existing "Tasks"/"Audit Log" links.

- [ ] **Step 12: Rebuild and copy static output**

Run: `cd frontend && npm run build && rm -rf ../src/celery_manager/dashboard/static && cp -r dist ../src/celery_manager/dashboard/static`

- [ ] **Step 13: Manual verification**

Confirm `/#/schedules` lists the seeded/synced periodic schedule(s), links through correctly to `/#/schedules/<name>`, the nav's "Schedules" link is now reachable and shows the correct active state, and an empty-schedules case renders the empty state. Use a real browser check (Playwright) as prior dashboard tasks did.

- [ ] **Step 14: Run the full test suite**

Run: `pytest tests/ -v`
Expected: PASS (all tests, no regressions)

- [ ] **Step 15: Commit**

```bash
git add src/celery_manager/control/schedules.py src/celery_manager/api/routes/schedules.py frontend/src src/celery_manager/dashboard/static tests/unit/test_control_schedules.py tests/unit/test_api_schedules.py
git commit -m "feat: periodic schedules list screen, closing the discoverability gap for per-occurrence pause/cancel"
```

---

## Backlog (Phase 2 / Phase 3 — out of scope for this plan)

Append new mid-implementation requests here too, tagged with the task they'd extend.

**Phase 2 (fast follow):**
- Worker health view (online workers, active/reserved counts, remote shutdown)
- Queue depth / broker backlog monitoring
- Trend analytics (success rate / duration over time)
- Human-friendly cron builder UI (replace raw crontab-string entry)
- Per-task/queue rate limiting from the UI

**Phase 3 (later):**
- Alerting (Slack/email/webhook) on repeated failures or backlog
- Multi-app / multi-environment support from one dashboard instance
