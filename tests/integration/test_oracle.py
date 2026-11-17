import os
from urllib.parse import unquote

import pytest

from mcp_database.config import Guard, SystemProfile
from mcp_database.db import make_connector

pytestmark = pytest.mark.integration
DSN = os.environ.get("TEST_ORACLE_DSN")
if not DSN:
    pytest.skip("cần TEST_ORACLE_DSN=user:password@host:port/service", allow_module_level=True)


@pytest.fixture
def conn(monkeypatch, tmp_path):
    cred, _, rest = DSN.rpartition("@")
    user, _, password = cred.partition(":")
    hostport, _, database = rest.partition("/")
    host, _, port = hostport.partition(":")
    monkeypatch.setenv("IT_USER", unquote(user))
    monkeypatch.setenv("IT_PASS", unquote(password))
    profile = SystemProfile(
        name="it", db_type="oracle", host=host, port=int(port or 1521), database=database,
        user_env="IT_USER", password_env="IT_PASS", data_doc=tmp_path / "x.md",
        guard=Guard(), is_replica=True, allow_primary=False)
    return make_connector(profile)


def test_table_info(conn):
    ti = conn.table_info("sys.dual")
    assert ti.columns
    with pytest.raises(LookupError):
        conn.table_info("khong_ton_tai_xyz")


def test_explain(conn):
    from mcp_database.db.base import PlanSummary
    assert isinstance(conn.explain("SELECT * FROM dual"), PlanSummary)


def test_timeout(conn):
    # truy vấn tốn thời gian thuần SELECT (không dùng PL/SQL)
    with pytest.raises(TimeoutError):
        conn.execute("SELECT COUNT(*) FROM dual CONNECT BY LEVEL <= 100000000", 10, 1)


def test_dml_rejected(conn):
    from mcp_database.db.base import DatabaseError
    with pytest.raises(DatabaseError):
        conn.execute("INSERT INTO dual VALUES ('X')", 10, 5)


def test_non_query_rejected(conn):
    from mcp_database.db.base import DatabaseError
    with pytest.raises(DatabaseError):
        conn.execute("BEGIN NULL; END;", 10, 5)
