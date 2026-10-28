from __future__ import annotations

import os
import sys

import pytest

import celery_manager
from celery_manager import runtime
from celery_manager.config import CeleryManagerConfig


@pytest.mark.skipif(sys.platform == "win32", reason="needs os.fork")
def test_forked_child_does_not_reuse_the_parents_pooled_connection(tmp_path):
    # Prefork workers fork after setup() opened a pooled connection; sharing
    # its socket with the parent breaks on Postgres. The child must start
    # with an empty pool.
    from celery import Celery

    config = CeleryManagerConfig(celery_app=Celery("t"), db_url=f"sqlite:///{tmp_path / 'cm.db'}")
    session_factory = celery_manager.setup(config)
    with session_factory() as session:
        session.connection()
    engine = runtime._current[1]
    assert engine.pool.checkedin() == 1

    read_end, write_end = os.pipe()
    pid = os.fork()
    if pid == 0:  # child
        os.close(read_end)
        os.write(write_end, str(engine.pool.checkedin()).encode())
        os._exit(0)
    os.close(write_end)
    child_checked_in = os.read(read_end, 16).decode()
    os.close(read_end)
    os.waitpid(pid, 0)
    assert child_checked_in == "0"
    assert engine.pool.checkedin() == 1  # the parent's connection is untouched
