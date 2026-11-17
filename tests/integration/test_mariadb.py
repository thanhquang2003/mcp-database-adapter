import os
import time
from urllib.parse import unquote

import pymysql
import pytest

from mcp_database.config import Guard, SystemProfile
from mcp_database.db import make_connector
from mcp_database.db.base import DatabaseError, PlanSummary

pytestmark = pytest.mark.integration
DSN = os.environ.get("TEST_MARIADB_DSN")
if not DSN:
    pytest.skip("cần TEST_MARIADB_DSN=user:password@host:port/database", allow_module_level=True)


def _parts():
    cred, _, rest = DSN.rpartition("@")
    user, _, password = cred.partition(":")
    hostport, _, database = rest.partition("/")
    host, _, port = hostport.partition(":")
    return unquote(user), unquote(password), host, int(port or 3306), database


@pytest.fixture
def writer():
    """Kết nối ghi riêng, chỉ dùng để dựng dữ liệu test (không đi qua connector)."""
    user, password, host, port, database = _parts()
    c = pymysql.connect(host=host, port=port, user=user, password=password,
                        database=database, autocommit=True)
    cur = c.cursor()
    cur.execute("DROP TABLE IF EXISTS it_t")
    cur.execute("CREATE TABLE it_t (id INT PRIMARY KEY, name VARCHAR(20) NULL, KEY ix_name (name))")
    cur.execute("INSERT INTO it_t VALUES (1, 'a'), (2, 'b')")
    yield c
    cur.execute("DROP TABLE IF EXISTS it_t")
    c.close()


@pytest.fixture
def conn(monkeypatch, tmp_path):
    user, password, host, port, database = _parts()
    monkeypatch.setenv("IT_USER", user)
    monkeypatch.setenv("IT_PASS", password)
    profile = SystemProfile(
        name="it", db_type="mariadb", host=host, port=port, database=database,
        user_env="IT_USER", password_env="IT_PASS", data_doc=tmp_path / "x.md",
        guard=Guard(), is_replica=True, allow_primary=False)
    return make_connector(profile)


def test_table_info_with_index(conn, writer):
    ti = conn.table_info("it_t")
    assert [c.name for c in ti.columns] == ["id", "name"]
    assert ti.columns[1].nullable is True
    idx = {i.name: (i.columns, i.unique) for i in ti.indexes}
    assert idx["PRIMARY"] == (["id"], True) and idx["ix_name"] == (["name"], False)
    with pytest.raises(LookupError):
        conn.table_info("khong_ton_tai_xyz")


def test_explain(conn, writer):
    plan = conn.explain("SELECT * FROM it_t")
    assert isinstance(plan, PlanSummary) and plan.est_rows is not None and plan.text


def test_timeout(conn):
    with pytest.raises(TimeoutError):
        conn.execute("SELECT SLEEP(5)", 10, 1)


def test_large_result_truncates_fast(conn):
    # Như pipeline: SQL có LIMIT max_rows + 1, connector lấy max_rows dòng; đóng bình thường.
    t0 = time.monotonic()
    r = conn.execute(
        "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i < 500000) "
        "SELECT i FROM n LIMIT 4", 3, 20)
    elapsed = time.monotonic() - t0
    print(f"large result: truncated={r.truncated} in {elapsed:.2f}s")
    assert r.truncated and len(r.rows) == 3 and elapsed < 10


def test_large_result_through_service(conn, tmp_path):
    from types import MappingProxyType
    from mcp_database.config import Config
    from mcp_database.service import QueryService
    cfg = Config(audit_log=None, systems=MappingProxyType({"it": conn.profile}))
    svc = QueryService(cfg, connector_factory=lambda p: conn)
    t0 = time.monotonic()
    r = svc.run("it", "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n WHERE i < 500000) "
                      "SELECT i FROM n")
    elapsed = time.monotonic() - t0
    print(f"service large result: truncated={r.truncated} rows={r.row_count} in {elapsed:.2f}s")
    assert r.truncated and r.row_count == conn.profile.guard.max_rows and elapsed < 10
    exact = svc.run("it", "SELECT seq FROM seq_1_to_1000")
    assert exact.row_count == 1000 and exact.truncated is False


def test_insert_rejected(conn, writer):
    with pytest.raises(DatabaseError):
        conn.execute("INSERT INTO it_t VALUES (3, 'c')", 10, 5)
    assert conn.execute("SELECT COUNT(*) FROM it_t", 10, 5).rows == [(2,)]


def test_connector_still_usable_after_truncation(conn, writer):
    conn.execute("SELECT seq FROM seq_1_to_100", 3, 5)
    assert conn.execute("SELECT COUNT(*) FROM it_t", 10, 5).rows == [(2,)]


@pytest.mark.parametrize("ref", ["it_t", "IT_T", "It_T"])
def test_table_info_case_insensitive(conn, writer, ref):
    ti = conn.table_info(ref)
    assert [c.name for c in ti.columns] == ["id", "name"]
    _, _, _, _, database = _parts()
    for qualified in (f"{database}.{ref}", f"{database.upper()}.{ref}"):
        assert [c.name for c in conn.table_info(qualified).columns] == ["id", "name"]


def test_stats_lookup_through_service(conn, writer):
    from types import MappingProxyType
    from mcp_database.config import Config
    from mcp_database.service import QueryService
    _, _, _, _, database = _parts()
    cfg = Config(audit_log=None, systems=MappingProxyType({"it": conn.profile}))
    svc = QueryService(cfg, connector_factory=lambda p: conn)
    # Linux: lower_case_table_names=0 nên câu truy vấn phải đúng hoa thường; tra thống kê thì không.
    r = svc.run("it", f"SELECT COUNT(*) FROM {database}.it_t")
    assert r.rows == [(2,)]
    key = ("it", database, "it_t")
    assert key in svc._stats and svc._stats[key][1] is not None
