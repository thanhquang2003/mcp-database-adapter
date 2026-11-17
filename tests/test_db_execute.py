import oracledb
import pymysql
import pytest

from mcp_database.db.base import DatabaseError
from mcp_database.db.mariadb import MariaDBConnector
from mcp_database.db.oracle import OracleConnector
from conftest import FakeConnection

NLS = ("ALTER SESSION SET NLS_DATE_FORMAT = 'YYYY-MM-DD HH24:MI:SS' "
       "NLS_TIMESTAMP_FORMAT = 'YYYY-MM-DD HH24:MI:SS.FF' "
       "NLS_TIMESTAMP_TZ_FORMAT = 'YYYY-MM-DD HH24:MI:SS.FF TZH:TZR'")


def setup(monkeypatch, kind, conn):
    if kind == "oracle":
        monkeypatch.setattr(oracledb, "connect", lambda **kw: conn)
        return OracleConnector
    monkeypatch.setattr(pymysql, "connect", lambda **kw: conn)
    return MariaDBConnector


@pytest.mark.parametrize("kind", ["oracle", "mariadb"])
def test_readonly_and_timeout_before_query(monkeypatch, creds, make_profile, kind):
    conn = FakeConnection(script={"SELECT 1": (["A"], [(1,)])})
    cls = setup(monkeypatch, kind, conn)
    res = cls(make_profile(kind)).execute("SELECT 1", 10, 7)
    st = conn.statements()
    assert res.columns == ["A"] and res.rows == [(1,)] and res.truncated is False
    qi = st.index("SELECT 1")
    if kind == "oracle":
        assert conn.call_timeout == 7000
        assert NLS in st and st.index(NLS) < qi
        assert st.index("SET TRANSACTION READ ONLY") < qi
    else:
        assert st.index("SET SESSION max_statement_time = 7") < qi
        assert st.index("SET SESSION TRANSACTION READ ONLY") < qi
        assert st.index("START TRANSACTION") < qi
        assert st.index("SET SESSION TRANSACTION READ ONLY") < st.index("START TRANSACTION")
    assert ("commit",) not in conn.log
    assert ("rollback",) in conn.log and conn.closed


@pytest.mark.parametrize("kind", ["oracle", "mariadb"])
def test_fetchmany_and_truncation(monkeypatch, creds, make_profile, kind):
    conn = FakeConnection(script={"SELECT": (["A"], [(i,) for i in range(4)])})
    cls = setup(monkeypatch, kind, conn)
    res = cls(make_profile(kind)).execute("SELECT x", 3, 5)
    assert ("fetchmany", 4) in conn.log
    assert res.truncated is True and res.rows == [(0,), (1,), (2,)]


@pytest.mark.parametrize("kind", ["oracle", "mariadb"])
def test_closed_on_error_and_error_sanitized(monkeypatch, creds, make_profile, kind):
    conn = FakeConnection(fail={"SELECT": RuntimeError("SECRET-VALUE-123")})
    cls = setup(monkeypatch, kind, conn)
    with pytest.raises(DatabaseError) as ei:
        cls(make_profile(kind)).execute("SELECT x", 3, 5)
    assert "SECRET-VALUE-123" not in str(ei.value)
    assert "SECRET-VALUE-123" not in repr(ei.value.args)
    assert isinstance(ei.value.__cause__, RuntimeError)
    assert conn.closed and ("commit",) not in conn.log


def test_mariadb_error_code_kept(monkeypatch, creds, make_profile):
    conn = FakeConnection(fail={"SELECT": pymysql.err.OperationalError(1105, "SECRET-VALUE-123")})
    cls = setup(monkeypatch, "mariadb", conn)
    with pytest.raises(DatabaseError) as ei:
        cls(make_profile("mariadb")).execute("SELECT x", 3, 5)
    assert ei.value.code == 1105 and "SECRET-VALUE-123" not in str(ei.value)


def test_mariadb_timeout(monkeypatch, creds, make_profile):
    conn = FakeConnection(fail={"SELECT": pymysql.err.OperationalError(1969, "Query execution was interrupted")})
    cls = setup(monkeypatch, "mariadb", conn)
    with pytest.raises(TimeoutError, match="query vượt 5 giây"):
        cls(make_profile("mariadb")).execute("SELECT x", 3, 5)
    assert conn.closed


class _OraErr:
    def __init__(self, full_code, msg):
        self.full_code, self.code, self.message = full_code, int(full_code[-4:]), msg


def test_oracle_timeout_and_sanitized(monkeypatch, creds, make_profile):
    conn = FakeConnection(fail={"SELECT": oracledb.DatabaseError(_OraErr("DPY-4024", "call timeout"))})
    cls = setup(monkeypatch, "oracle", conn)
    with pytest.raises(TimeoutError, match="query vượt 5 giây"):
        cls(make_profile("oracle")).execute("SELECT x", 3, 5)
    conn = FakeConnection(fail={"SELECT": oracledb.DatabaseError(_OraErr("ORA-01722", "SECRET-VALUE-123"))})
    cls = setup(monkeypatch, "oracle", conn)
    with pytest.raises(DatabaseError) as ei:
        cls(make_profile("oracle")).execute("SELECT x", 3, 5)
    assert ei.value.code == "ORA-01722" and "SECRET-VALUE-123" not in str(ei.value)
    assert conn.closed


def test_missing_credentials(monkeypatch, make_profile):
    monkeypatch.delenv("T_USER", raising=False)
    monkeypatch.delenv("T_PASS", raising=False)
    with pytest.raises(Exception) as ei:
        OracleConnector(make_profile("oracle")).execute("SELECT 1", 1, 1)
    assert "T_USER" in str(ei.value)


# --- explain / table_info ---

def test_oracle_explain(monkeypatch, creds, make_profile):
    conn = FakeConnection(script={
        "FROM plan_table": (["a"], [("SELECT STATEMENT", "", None, 9, 3),
                                    ("TABLE ACCESS", "FULL", "t", 9, 3)]),
        "DBMS_XPLAN": (["x"], [("line1",), ("line2",)]),
    })
    cls = setup(monkeypatch, "oracle", conn)
    plan = cls(make_profile("oracle")).explain("SELECT * FROM t")
    st = conn.statements()
    ex = [s for s in st if s.startswith("EXPLAIN PLAN SET STATEMENT_ID")]
    assert len(ex) == 1 and ex[0].endswith("FOR SELECT * FROM t")
    assert plan.cost == 9 and plan.full_scans == ["T"] and plan.text == "line1\nline2"
    assert ("commit",) not in conn.log and ("rollback",) in conn.log and conn.closed


def test_oracle_explain_error_sanitized(monkeypatch, creds, make_profile):
    conn = FakeConnection(fail={"EXPLAIN PLAN": RuntimeError("SECRET-VALUE-123")})
    cls = setup(monkeypatch, "oracle", conn)
    with pytest.raises(DatabaseError) as ei:
        cls(make_profile("oracle")).explain("SELECT 1")
    assert "SECRET-VALUE-123" not in str(ei.value) and conn.closed


def test_mariadb_explain(monkeypatch, creds, make_profile):
    conn = FakeConnection(script={"EXPLAIN": (["table", "type", "rows"], [("t", "ALL", 5)])})
    cls = setup(monkeypatch, "mariadb", conn)
    plan = cls(make_profile("mariadb")).explain("SELECT * FROM t")
    assert "EXPLAIN SELECT * FROM t" in conn.statements()
    assert plan.full_scans == ["T"] and plan.est_rows == 5 and conn.closed


def test_mariadb_table_info(monkeypatch, creds, make_profile):
    conn = FakeConnection(script={
        "information_schema.columns": (["c"], [("id", "int(11)", "NO"), ("n", "varchar(5)", "YES")]),
        "information_schema.statistics": (["c"], [("PRIMARY", 0, "id"), ("ix", 1, "n"), ("ix", 1, "id")]),
        "information_schema.tables": (["c"], [(42,)]),
    })
    cls = setup(monkeypatch, "mariadb", conn)
    ti = cls(make_profile("mariadb")).table_info("db1.users")
    assert [c.name for c in ti.columns] == ["id", "n"] and ti.columns[1].nullable is True
    assert {i.name: (i.columns, i.unique) for i in ti.indexes} == {
        "PRIMARY": (["id"], True), "ix": (["n", "id"], False)}
    assert ti.estimated_rows == 42
    # identifiers only as bind params
    for e in conn.log:
        if e[0] == "execute" and e[2]:
            assert "users" not in e[1] and "db1" not in e[1]


def test_oracle_table_info_missing_raises_lookup(monkeypatch, creds, make_profile):
    conn = FakeConnection(script={"CURRENT_SCHEMA": (["s"], [("APP",)])})
    cls = setup(monkeypatch, "oracle", conn)
    with pytest.raises(LookupError):
        cls(make_profile("oracle")).table_info("nope")
    assert conn.closed


def test_oracle_table_info(monkeypatch, creds, make_profile):
    conn = FakeConnection(script={
        "CURRENT_SCHEMA": (["s"], [("APP",)]),
        "all_tab_columns": (["c"], [("ID", "NUMBER", "N")]),
        "all_ind_columns": (["c"], [("PK", "UNIQUE", "ID")]),
        "all_tables": (["c"], [(7,)]),
    })
    cls = setup(monkeypatch, "oracle", conn)
    ti = cls(make_profile("oracle")).table_info("cdr")
    assert ti.columns[0].nullable is False and ti.indexes[0].unique is True
    assert ti.estimated_rows == 7


@pytest.mark.parametrize("kind", ["oracle", "mariadb"])
@pytest.mark.parametrize("bad", ["a b", "t;drop table x", "a.b.c", "", "1abc", "t' or '1", "a.", "`t`"])
def test_table_info_rejects_bad_names(monkeypatch, creds, make_profile, kind, bad):
    conn = FakeConnection()
    cls = setup(monkeypatch, kind, conn)
    with pytest.raises(LookupError):
        cls(make_profile(kind)).table_info(bad)
    assert conn.statements() == []


def test_table_info_error_sanitized(monkeypatch, creds, make_profile):
    conn = FakeConnection(fail={"information_schema": RuntimeError("SECRET-VALUE-123")})
    cls = setup(monkeypatch, "mariadb", conn)
    with pytest.raises(DatabaseError) as ei:
        cls(make_profile("mariadb")).table_info("t")
    assert "SECRET-VALUE-123" not in str(ei.value) and conn.closed
