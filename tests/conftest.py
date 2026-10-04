"""Test isolation: every test gets a fresh, on-disk throwaway database."""

import pytest

from app import storage


@pytest.fixture(autouse=True)
def fresh_store(tmp_path):
    db_path = str(tmp_path / "test-risk.db")
    storage.reset_store(db_path)
    yield
    storage.get_store().close()
