import sys
import textwrap

from celery_manager import cli


def test_load_celery_app_imports_from_the_current_directory(tmp_path, monkeypatch):
    """Console scripts don't put the CWD on sys.path; the user's module must
    still be importable when run from their project dir."""
    (tmp_path / "cli_test_celery_app.py").write_text(
        textwrap.dedent(
            """
            from celery import Celery

            app = Celery("from-cwd")
            """
        )
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p not in ("", str(tmp_path))])
    monkeypatch.delitem(sys.modules, "cli_test_celery_app", raising=False)
    try:
        app = cli.load_celery_app("cli_test_celery_app:app")
        assert app.main == "from-cwd"
        assert sys.path[0] == str(tmp_path.resolve()) or sys.path[0] == str(tmp_path)
    finally:
        sys.modules.pop("cli_test_celery_app", None)


def test_load_celery_app_does_not_duplicate_cwd_on_sys_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    import os

    cwd = os.getcwd()
    monkeypatch.setattr(sys, "path", [cwd, *sys.path])
    before = list(sys.path)
    cli.load_celery_app("celery:Celery")
    assert sys.path == before
