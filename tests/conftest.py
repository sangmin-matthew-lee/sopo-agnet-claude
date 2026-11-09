import pytest

import db


@pytest.fixture
def db_path(tmp_path, monkeypatch):
    path = str(tmp_path / "test_sop.db")
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db(path)
    return path
