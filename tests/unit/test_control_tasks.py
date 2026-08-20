from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from celery import Celery
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from celery_manager.db.models import Base, TaskRun
from celery_manager.control.tasks import (
    MAX_PAGE_SIZE,
    list_task_runs,
    count_task_runs,
    list_retries_of,
    get_task_run,
    start_task,
    cancel_task,
)


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


# --- Task 19: search, date-range filtering, and paging. ---


def _session_with_search_fixture():
    """Runs spread over three days, with searchable text in every column the
    `q` filter covers, plus one row whose args contain a literal `%` — that
    last one is the LIKE-escaping guard below."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as session:
        session.add_all([
            TaskRun(task_id="alpha-1", task_name="demo.send_report", status="succeeded",
                    args="[42]", kwargs="{}", started_at=datetime(2026, 8, 10, tzinfo=timezone.utc)),
            TaskRun(task_id="beta-2", task_name="demo.echo", status="succeeded",
                    args="[]", kwargs='{"user": "alice"}', started_at=datetime(2026, 8, 12, tzinfo=timezone.utc)),
            TaskRun(task_id="gamma-3", task_name="demo.echo", status="failed",
                    args="[]", kwargs="{}", started_at=datetime(2026, 8, 14, tzinfo=timezone.utc)),
            TaskRun(task_id="delta-4", task_name="demo.discount", status="succeeded",
                    args='["100%"]', kwargs="{}", started_at=datetime(2026, 8, 15, tzinfo=timezone.utc)),
            # Queued: no started_at at all, which is what the date-range
            # exclusion test below turns on.
            TaskRun(task_id="epsilon-5", task_name="demo.echo", status="queued",
                    args="[]", kwargs="{}"),
        ])
        session.commit()
    return Session()


def test_list_task_runs_search_matches_task_name():
    session = _session_with_search_fixture()
    assert [r.task_id for r in list_task_runs(session, q="send_report")] == ["alpha-1"]


def test_list_task_runs_search_matches_task_id():
    session = _session_with_search_fixture()
    assert [r.task_id for r in list_task_runs(session, q="beta-2")] == ["beta-2"]


def test_list_task_runs_search_matches_kwargs_content():
    # args/kwargs are stored as JSON TEXT (see db/models.py) — this is a
    # pragmatic substring match, not a real JSON query.
    session = _session_with_search_fixture()
    assert [r.task_id for r in list_task_runs(session, q="alice")] == ["beta-2"]


def test_list_task_runs_search_matches_args_content():
    session = _session_with_search_fixture()
    assert [r.task_id for r in list_task_runs(session, q="42")] == ["alpha-1"]


def test_list_task_runs_search_is_case_insensitive():
    session = _session_with_search_fixture()
    assert [r.task_id for r in list_task_runs(session, q="ALICE")] == ["beta-2"]


def test_list_task_runs_search_escapes_percent_wildcard():
    """A bare `%` is a LIKE wildcard. Unescaped it matches every row, so the
    search box would answer "everything" to a query for a literal percent —
    which reads as a broken filter rather than as a result."""
    session = _session_with_search_fixture()
    assert [r.task_id for r in list_task_runs(session, q="%")] == ["delta-4"]
    assert [r.task_id for r in list_task_runs(session, q="100%")] == ["delta-4"]


def test_list_task_runs_search_escapes_underscore_wildcard():
    """`_` is LIKE's single-character wildcard; unescaped, `d_mo` would match
    `demo.*`. Only the row with a literal underscore may match."""
    session = _session_with_search_fixture()
    assert [r.task_id for r in list_task_runs(session, q="d_mo")] == []
    assert [r.task_id for r in list_task_runs(session, q="send_report")] == ["alpha-1"]


def test_list_task_runs_search_escapes_backslash():
    """The escape character itself must be escaped, or a backslash in the
    search text would consume the character after it inside the pattern."""
    session = _session_with_search_fixture()
    assert list_task_runs(session, q="\\") == []


def test_list_task_runs_filters_by_date_range():
    session = _session_with_search_fixture()
    results = list_task_runs(
        session,
        date_from=datetime(2026, 8, 11, tzinfo=timezone.utc),
        date_to=datetime(2026, 8, 13, tzinfo=timezone.utc),
    )
    assert [r.task_id for r in results] == ["beta-2"]


def test_list_task_runs_date_range_is_inclusive_of_both_bounds():
    session = _session_with_search_fixture()
    results = list_task_runs(
        session,
        date_from=datetime(2026, 8, 10, tzinfo=timezone.utc),
        date_to=datetime(2026, 8, 12, tzinfo=timezone.utc),
    )
    assert [r.task_id for r in results] == ["beta-2", "alpha-1"]


def test_list_task_runs_date_filter_excludes_never_started_runs():
    """A queued run has no started_at, so it did not run "within this range"
    under either bound — the SQL NULL comparison and the intended meaning
    agree here, and this test pins that they keep agreeing."""
    session = _session_with_search_fixture()
    results = list_task_runs(session, date_from=datetime(2000, 1, 1, tzinfo=timezone.utc))
    assert "epsilon-5" in [r.task_id for r in list_task_runs(session)]
    assert "epsilon-5" not in [r.task_id for r in results]


def test_list_task_runs_combines_search_and_date_range():
    session = _session_with_search_fixture()
    results = list_task_runs(session, q="echo", date_from=datetime(2026, 8, 13, tzinfo=timezone.utc))
    assert [r.task_id for r in results] == ["gamma-3"]


def test_count_task_runs_counts_everything_when_unfiltered():
    session = _session_with_search_fixture()
    assert count_task_runs(session) == 5


@pytest.mark.parametrize("filters", [
    {},
    {"status": "succeeded"},
    {"task_name": "demo.echo"},
    {"q": "echo"},
    {"q": "%"},
    {"date_from": datetime(2026, 8, 11, tzinfo=timezone.utc)},
    {"date_to": datetime(2026, 8, 13, tzinfo=timezone.utc)},
    {"q": "echo", "date_from": datetime(2026, 8, 13, tzinfo=timezone.utc)},
    {"status": "succeeded", "q": "alice", "date_to": datetime(2026, 8, 20, tzinfo=timezone.utc)},
])
def test_count_task_runs_agrees_with_list_task_runs(filters):
    """The regression guard for the shared-filter-builder rule: a `total`
    that disagrees with the list it labels is the classic bug in this
    feature, and it appears the moment someone adds a filter to one function
    and not the other. `limit` is raised past the fixture size so the list is
    genuinely unpaged."""
    session = _session_with_search_fixture()
    assert count_task_runs(session, **filters) == len(list_task_runs(session, limit=1000, **filters))


def test_list_task_runs_pages_are_disjoint_and_total_is_stable():
    session = _session_with_search_fixture()
    page_1 = [r.task_id for r in list_task_runs(session, limit=2, offset=0)]
    page_2 = [r.task_id for r in list_task_runs(session, limit=2, offset=2)]
    page_3 = [r.task_id for r in list_task_runs(session, limit=2, offset=4)]

    assert len(page_1) == 2 and len(page_2) == 2 and len(page_3) == 1
    assert set(page_1).isdisjoint(page_2)
    assert set(page_1 + page_2).isdisjoint(page_3)
    # Every row reachable exactly once by walking the pages — i.e. history
    # older than the first page is no longer stranded.
    assert sorted(page_1 + page_2 + page_3) == ["alpha-1", "beta-2", "delta-4", "epsilon-5", "gamma-3"]
    assert count_task_runs(session) == 5


def test_list_task_runs_offset_past_the_end_returns_empty():
    session = _session_with_search_fixture()
    assert list_task_runs(session, limit=10, offset=99) == []


def test_max_page_size_is_the_documented_ceiling():
    """The API route clamps to this; the control layer only publishes it, so
    a Python caller can still ask for more deliberately."""
    assert MAX_PAGE_SIZE == 500
    session = _session_with_search_fixture()
    assert len(list_task_runs(session, limit=MAX_PAGE_SIZE + 1000)) == 5


def test_list_retries_of_returns_chain_children_oldest_first():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    with Session() as session:
        session.add_all([
            TaskRun(task_id="orig-1", task_name="demo.echo", status="failed"),
            TaskRun(task_id="retry-1", task_name="demo.echo", status="succeeded", retried_from_id="orig-1"),
            TaskRun(task_id="retry-2", task_name="demo.echo", status="failed", retried_from_id="orig-1"),
            TaskRun(task_id="unrelated", task_name="demo.echo", status="succeeded"),
        ])
        session.commit()
    session = Session()

    assert [r.task_id for r in list_retries_of(session, "orig-1")] == ["retry-1", "retry-2"]
    assert list_retries_of(session, "retry-1") == []
    assert list_retries_of(session, "does-not-exist") == []
