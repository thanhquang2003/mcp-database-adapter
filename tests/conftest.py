"""Fixture dùng chung."""
import pytest

from fake_connector import FakeConnector


@pytest.fixture
def fake_connector():
    """Connector giả theo protocol `Connector` (xem tests/fake_connector.py)."""
    return FakeConnector()


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("T_USER", "u1")
    monkeypatch.setenv("T_PASS", "p1")


@pytest.fixture
def make_profile(tmp_path):
    from mcp_database.config import Guard, SystemProfile

    def _make(db_type):
        return SystemProfile(
            name="t", db_type=db_type, host="h", port=1234, database="d",
            user_env="T_USER", password_env="T_PASS", data_doc=tmp_path / "x.md",
            guard=Guard(), is_replica=True, allow_primary=False)
    return _make
