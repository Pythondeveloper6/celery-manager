"""An existing 0.2 database survives the 0.3 release and rollback."""
from sqlalchemy import create_engine

from tests.release_upgrade import exercise_release_upgrade


def test_0014_to_0017_and_back_preserves_existing_data(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'release-upgrade.db'}")
    try:
        exercise_release_upgrade(engine)
    finally:
        engine.dispose()
