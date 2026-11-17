import oracledb
import pymysql
import pytest

from mcp_database.db.base import DatabaseError
from mcp_database.db.mariadb import MariaDBConnector
from mcp_database.db.oracle import OracleConnector
from fakes import FakeConnection

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
    # Dòng INDEX được quy về bảng gốc qua all_indexes (để so với danh sách bảng lớn).
    plan_sql = next(s for s in st if "FROM plan_table" in s)
    assert "all_indexes" in plan_sql


def test_oracle_explain_text_has_rows_cost_but_no_predicates(monkeypatch, creds, make_profile):
    conn = FakeConnection(script={"FROM plan_table": (["a"], []), "DBMS_XPLAN": (["x"], [])})
    cls = setup(monkeypatch, "oracle", conn)
    cls(make_profile("oracle")).explain("SELECT 1 FROM dual")
    xplan = [e for e in conn.log if e[0] == "execute" and "DBMS_XPLAN" in e[1]]
    assert len(xplan) == 1
    sql = xplan[0][1].upper()
    assert "'BASIC +ROWS +COST'" in sql
    for bad in ("PREDICATE", "'ALL'", "TYPICAL", "ADVANCED"):
        assert bad not in sql


def test_oracle_execute_one_round_trip(monkeypatch, creds, make_profile):
    conn = FakeConnection(script={"SELECT": (["A"], [(i,) for i in range(5)])})
    cls = setup(monkeypatch, "oracle", conn)
    cls(make_profile("oracle")).execute("SELECT x", 3, 5)
    cur = conn.cursors[-1]
    assert cur.arraysize == 4 and cur.prefetchrows == 4
    # đặt trước khi chạy câu truy vấn
    assert cur.set_before_execute == {"arraysize": 4, "prefetchrows": 4}


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
        "information_schema.tables": (["s", "t", "r"], [("db1", "users", 42)]),
    })
    cls = setup(monkeypatch, "mariadb", conn)
    ti = cls(make_profile("mariadb")).table_info("db1.USERS")
    assert [c.name for c in ti.columns] == ["id", "n"] and ti.columns[1].nullable is True
    assert {i.name: (i.columns, i.unique) for i in ti.indexes} == {
        "PRIMARY": (["id"], True), "ix": (["n", "id"], False)}
    assert ti.estimated_rows == 42
    execs = [e for e in conn.log if e[0] == "execute" and e[2]]
    # identifiers only as bind params
    for e in execs:
        assert "users" not in e[1].lower().replace("information_schema", "") and "db1" not in e[1]
    # tìm bảng không phân biệt hoa thường, sau đó tra cột/index theo tên thật
    lookup = next(e for e in execs if "information_schema.tables" in e[1])
    assert "LOWER(table_schema) = LOWER(COALESCE(%s, DATABASE()))" in lookup[1]
    assert "LOWER(table_name) = LOWER(%s)" in lookup[1] and lookup[2] == ("db1", "USERS")
    cols = next(e for e in execs if "information_schema.columns" in e[1])
    assert cols[2] == ("db1", "users")


def test_mariadb_table_info_prefers_exact_case(monkeypatch, creds, make_profile):
    conn = FakeConnection(script={
        "information_schema.tables": (["s", "t", "r"], [("d", "T", 5), ("d", "t", 7)]),
        "information_schema.columns": (["c"], [("id", "int", "NO")]),
        "information_schema.statistics": (["c"], []),
    })
    cls = setup(monkeypatch, "mariadb", conn)
    assert cls(make_profile("mariadb")).table_info("t").estimated_rows == 7


def test_mariadb_table_info_not_found(monkeypatch, creds, make_profile):
    conn = FakeConnection(script={"information_schema.tables": (["s", "t", "r"], [])})
    cls = setup(monkeypatch, "mariadb", conn)
    with pytest.raises(LookupError):
        cls(make_profile("mariadb")).table_info("nope")


@pytest.mark.parametrize("kind", ["oracle", "mariadb"])
def test_table_info_timeout_parameter(monkeypatch, creds, make_profile, kind):
    conn = FakeConnection(script={"CURRENT_SCHEMA": (["s"], [("APP",)])})
    cls = setup(monkeypatch, kind, conn)
    with pytest.raises(LookupError):
        cls(make_profile(kind)).table_info("t", timeout_seconds=3)
    if kind == "oracle":
        assert conn.call_timeout == 3000
    else:
        assert "SET SESSION max_statement_time = 3" in conn.statements()


def _oracle_dict(owner_tables, synonyms=(), num_rows=None):
    """Từ điển Oracle giả: owner_tables = {(owner, table): [cột]}; synonyms = [(owner, syn, t_owner, t_name)]."""
    def cols(params):
        found = owner_tables.get((params["owner"], params["t"]))
        return ["c"], [(c, "NUMBER", "Y") for c in (found or [])]

    def syns(params):
        owners = {params["owner"], "PUBLIC"} if params.get("pub") else {params["owner"]}
        rows = [(to, tn) for (o, sn, to, tn) in synonyms if o in owners and sn == params["t"]]
        rows.sort(key=lambda r: 0)
        return ["o", "t"], rows[:1]

    def nrows(params):
        found = (params["owner"], params["t"]) in owner_tables
        return ["n"], [(num_rows.get((params["owner"], params["t"])),)] if found and num_rows else []

    return {
        "CURRENT_SCHEMA": (["s"], [("APP",)]),
        "all_tab_columns": cols,
        "all_synonyms": syns,
        "all_ind_columns": (["c"], []),
        "all_tables": nrows,
    }


def _binds(conn, needle):
    return [e[2] for e in conn.log if e[0] == "execute" and needle in e[1]]


@pytest.mark.parametrize("ref,owner,table", [
    ("billing.cdr", "BILLING", "CDR"),
    ("Billing.Cdr", "BILLING", "CDR"),
    ('"Billing"."CdrX"', "Billing", "CdrX"),
    ('billing."cdr"', "BILLING", "cdr"),
    ("cdr", "APP", "CDR"),
])
def test_oracle_table_info_identifier_case(monkeypatch, creds, make_profile, ref, owner, table):
    conn = FakeConnection(script=_oracle_dict({(owner, table): ["ID"]}, num_rows={(owner, table): 9}))
    cls = setup(monkeypatch, "oracle", conn)
    ti = cls(make_profile("oracle")).table_info(ref)
    assert [c.name for c in ti.columns] == ["ID"] and ti.estimated_rows == 9
    assert _binds(conn, "all_tab_columns")[0] == {"owner": owner, "t": table}


def test_oracle_table_info_via_private_or_public_synonym(monkeypatch, creds, make_profile):
    conn = FakeConnection(script=_oracle_dict(
        {("BILLING", "CDR_HISTORY"): ["ID"]},
        synonyms=[("PUBLIC", "CDR_HISTORY", "BILLING", "CDR_HISTORY")],
        num_rows={("BILLING", "CDR_HISTORY"): 40_000_000}))
    cls = setup(monkeypatch, "oracle", conn)
    ti = cls(make_profile("oracle")).table_info("cdr_history")
    assert ti.estimated_rows == 40_000_000
    syn_sql = next(e[1] for e in conn.log if e[0] == "execute" and "all_synonyms" in e[1])
    assert "db_link IS NULL" in syn_sql and "'PUBLIC'" in syn_sql
    assert _binds(conn, "all_tab_columns")[-1] == {"owner": "BILLING", "t": "CDR_HISTORY"}


def test_oracle_table_info_schema_qualified_synonym_no_public(monkeypatch, creds, make_profile):
    conn = FakeConnection(script=_oracle_dict(
        {("REAL", "T"): ["ID"]}, synonyms=[("PUBLIC", "T", "REAL", "T")]))
    cls = setup(monkeypatch, "oracle", conn)
    with pytest.raises(LookupError):
        cls(make_profile("oracle")).table_info("other.t")
    assert all(not b.get("pub") for b in _binds(conn, "all_synonyms"))


def test_oracle_table_info_view_has_no_estimate(monkeypatch, creds, make_profile):
    conn = FakeConnection(script=_oracle_dict({("APP", "V"): ["ID"]}))  # không có trong all_tables
    cls = setup(monkeypatch, "oracle", conn)
    assert cls(make_profile("oracle")).table_info("v").estimated_rows is None


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
@pytest.mark.parametrize("bad", ["a b", "t;drop table x", "a.b.c", "", "1abc", "t' or '1", "a.", "`t`",
                                 '"a"b"', '"a b"', '"t', '""'])
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


# --- vòng 2: đóng khi setup lỗi, streaming, lỗi sạch, chặn non-query ---

SETUP_FAIL = {"oracle": "SET TRANSACTION READ ONLY", "mariadb": "SET SESSION max_statement_time"}


@pytest.mark.parametrize("kind", ["oracle", "mariadb"])
def test_setup_failure_closes_and_rolls_back(monkeypatch, creds, make_profile, kind):
    conn = FakeConnection(fail={SETUP_FAIL[kind]: RuntimeError("SECRET-VALUE-123")})
    cls = setup(monkeypatch, kind, conn)
    with pytest.raises(DatabaseError) as ei:
        cls(make_profile(kind)).execute("SELECT 1", 5, 5)
    assert "SECRET-VALUE-123" not in str(ei.value)
    assert conn.closed and ("rollback",) in conn.log and ("commit",) not in conn.log


@pytest.mark.parametrize("kind", ["oracle", "mariadb"])
@pytest.mark.parametrize("method", ["explain", "table_info"])
def test_setup_failure_closes_other_methods(monkeypatch, creds, make_profile, kind, method):
    fail_on = "SET SESSION max_statement_time" if kind == "mariadb" else "ALTER SESSION"
    conn = FakeConnection(fail={fail_on: RuntimeError("x")})
    cls = setup(monkeypatch, kind, conn)
    with pytest.raises(DatabaseError):
        getattr(cls(make_profile(kind)), method)("SELECT 1" if method == "explain" else "t")
    assert conn.closed


def test_mariadb_execute_uses_streaming_cursor(monkeypatch, creds, make_profile):
    conn = FakeConnection(script={"SELECT": (["A"], [(1,)])})
    cls = setup(monkeypatch, "mariadb", conn)
    cls(make_profile("mariadb")).execute("SELECT 1", 5, 5)
    assert (pymysql.cursors.SSCursor,) in conn.cursor_args


def test_mariadb_truncated_closes_normally(monkeypatch, creds, make_profile):
    # Truncation chính xác: SQL có LIMIT max_rows+1 nên còn tối đa một dòng chưa đọc;
    # đóng cursor/rollback/đóng kết nối bình thường, không còn mẹo bỏ dở kết quả.
    conn = FakeConnection(script={"SELECT": (["A"], [(i,) for i in range(4)])})
    cls = setup(monkeypatch, "mariadb", conn)
    res = cls(make_profile("mariadb")).execute("SELECT 1", 3, 5)
    assert res.truncated and conn.closed
    assert ("rollback",) in conn.log and ("cursor_close",) in conn.log
    assert conn.log.index(("cursor_close",)) < conn.log.index(("rollback",))


@pytest.mark.parametrize("kind", ["oracle", "mariadb"])
def test_non_query_rejected(monkeypatch, creds, make_profile, kind):
    conn = FakeConnection()  # câu lệnh không có tập kết quả -> description None
    cls = setup(monkeypatch, kind, conn)
    with pytest.raises(DatabaseError) as ei:
        cls(make_profile(kind)).execute("BEGIN NULL; COMMIT; END;", 5, 5)
    assert "từ chối" in str(ei.value) and conn.closed
    assert ("commit",) not in conn.log


@pytest.mark.parametrize("kind", ["oracle", "mariadb"])
@pytest.mark.parametrize("exc", [KeyError("SECRET-ROW"), TimeoutError("SECRET-T"),
                                 IndexError("SECRET-ROW"), LookupError("SECRET-ROW")])
def test_foreign_exceptions_are_wrapped(monkeypatch, creds, make_profile, kind, exc):
    conn = FakeConnection(fail={"SELECT": exc})
    cls = setup(monkeypatch, kind, conn)
    with pytest.raises(DatabaseError) as ei:
        cls(make_profile(kind)).execute("SELECT x", 3, 5)
    assert "SECRET" not in str(ei.value) and "SECRET" not in repr(ei.value.args)
    assert conn.closed


@pytest.mark.parametrize("kind", ["oracle", "mariadb"])
@pytest.mark.parametrize("secs", [0, -1])
def test_timeout_must_be_positive(monkeypatch, creds, make_profile, kind, secs):
    conn = FakeConnection()
    cls = setup(monkeypatch, kind, conn)
    with pytest.raises(ValueError):
        cls(make_profile(kind)).execute("SELECT 1", 5, secs)
    assert conn.log == []


def test_session_has_no_abandon_hack():
    from mcp_database.db import base
    assert not hasattr(base.Session, "abandon_result")
    import inspect
    assert "abandon_on_truncate" not in inspect.signature(base.run_query).parameters
