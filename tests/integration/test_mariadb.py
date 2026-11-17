import os
from urllib.parse import unquote

import pytest

from mcp_database.config import Guard, SystemProfile
from mcp_database.db import make_connector

pytestmark = pytest.mark.integration
DSN = os.environ.get("TEST_MARIADB_DSN")
if not DSN:
    pytest.skip("cần TEST_MARIADB_DSN=user:password@host:port/database", allow_module_level=True)


@pytest.fixture
def conn(monkeypatch, tmp_path):
    cred, _, rest = DSN.rpartition("@")
    user, _, password = cred.partition(":")
    hostport, _, database = rest.partition("/")
    host, _, port = hostport.partition(":")
    monkeypatch.setenv("IT_USER", unquote(user))
    monkeypatch.setenv("IT_PASS", unquote(password))
    profile = SystemProfile(
        name="it", db_type="mariadb", host=host, port=int(port or 3306), database=database,
        user_env="IT_USER", password_env="IT_PASS", data_doc=tmp_path / "x.md",
        guard=Guard(), is_replica=True, allow_primary=False)
    return make_connector(profile)


def test_table_info(conn):
    ti = conn.table_info("information_schema.tables")
    assert any(c.name.upper() == "TABLE_NAME" for c in ti.columns)
    with pytest.raises(LookupError):
        conn.table_info("khong_ton_tai_xyz")


def test_explain(conn):
    from mcp_database.db.base import PlanSummary
    assert isinstance(conn.explain("SELECT 1"), PlanSummary)


def test_timeout(conn):
    with pytest.raises(TimeoutError):
        conn.execute("SELECT SLEEP(5)", 10, 1)


def test_execute_and_truncate(conn):
    r = conn.execute("SELECT seq FROM seq_1_to_5", 3, 5)
    assert r.truncated and len(r.rows) == 3


def test_write_rejected(conn):
    from mcp_database.db.base import DatabaseError
    with pytest.raises(DatabaseError):
        conn.execute("CREATE TEMPORARY TABLE it_tmp (a int)", 10, 5)
