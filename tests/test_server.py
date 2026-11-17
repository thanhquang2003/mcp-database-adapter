import asyncio
import json
import sys
import time
from datetime import date
from types import MappingProxyType

import anyio
import pytest

from fake_connector import FakeConnector
from mcp_database.config import Config, Guard, SystemProfile, TableGuard
from mcp_database.db.base import (
    ColumnInfo,
    DatabaseError,
    IndexInfo,
    QueryResult,
    QueryTimeout,
    TableInfo,
)
from mcp_database.service import QueryService

TOOLS = {"list_systems", "get_data_doc", "describe_table", "explain_query", "run_query", "profile_column"}
KEY = b"0123456789abcdef-secret"
TODAY = date(2026, 11, 17)
WINDOW = "call_time >= DATE '2026-11-01' AND call_time < DATE '2026-11-10'"


def cdr_info():
    return TableInfo(
        columns=[ColumnInfo("CALL_TIME", "DATE", False), ColumnInfo("STATUS", "VARCHAR2", True),
                 ColumnInfo("MSISDN", "VARCHAR2", False)],
        indexes=[IndexInfo("IX_CDR_TIME", ["CALL_TIME"], False)],
        estimated_rows=50_000_000,
    )


def users_info():
    return TableInfo(columns=[ColumnInfo("ID", "NUMBER", False), ColumnInfo("Region", "VARCHAR2", True)],
                     indexes=[], estimated_rows=100)


def make_profile(tmp_path, name="cdr", db_type="oracle"):
    return SystemProfile(
        name=name, db_type=db_type, host="db-secret-host.internal", port=1521, database="CDRDB",
        user_env="CDR_SECRET_USER_ENV", password_env="CDR_SECRET_PASS_ENV",
        data_doc=tmp_path / "docs" / f"{name}.md",
        guard=Guard(
            tables=MappingProxyType({"CDR_HISTORY": TableGuard("CALL_TIME", 31)}),
            sensitive_columns=MappingProxyType({"CDR_HISTORY.MSISDN": "hash"}),
        ),
        is_replica=True, allow_primary=False)


@pytest.fixture
def fake():
    return FakeConnector(tables={"CDR_HISTORY": cdr_info(), "USERS": users_info()})


@pytest.fixture
def build(tmp_path, fake):
    """build(db_type=..., connector=...) -> (server, fake, config)."""
    from mcp_database.server import build_server

    def _build(db_type="oracle", connector=None):
        conn = connector or fake
        profile = make_profile(tmp_path, db_type=db_type)
        cfg = Config(audit_log=tmp_path / "audit.jsonl", systems=MappingProxyType({profile.name: profile}))
        svc = QueryService(cfg, connector_factory=lambda p: conn, hmac_key=KEY, today=lambda: TODAY)
        return build_server(svc, cfg), conn, cfg

    return _build


def call(server, name, **args):
    """Gọi tool qua MCPServer.call_tool; trả về payload JSON đã giải mã (hoặc chuỗi)."""
    result = asyncio.run(server.call_tool(name, args))
    assert not result.is_error, result.content
    if result.structured_content is not None:
        return result.structured_content["result"]
    text = result.content[0].text
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def tool_descriptions(server):
    return {t.name: t.description for t in asyncio.run(server.list_tools())}


# --- Đăng ký tool -------------------------------------------------------------


def test_tools_registered(build):
    server, _, _ = build()
    assert set(tool_descriptions(server)) == TOOLS


def test_descriptions_guide_the_model(build):
    server, _, _ = build()
    desc = tool_descriptions(server)
    for name in ("run_query", "explain_query"):
        text = desc[name]
        assert "get_data_doc" in text
        assert "thời gian" in text
        assert "GROUP BY" in text and "COUNT" in text
        assert "nhạy cảm" in text
        assert "giới hạn" in text
        assert "hint" in text
    for name in TOOLS:
        assert desc[name], name


# --- list_systems / get_data_doc ------------------------------------------------


def test_list_systems_has_no_secrets(build):
    server, _, _ = build()
    out = call(server, "list_systems")
    assert out == [{"name": "cdr", "db_type": "oracle"}]
    text = json.dumps(out)
    for secret in ("db-secret-host", "1521", "CDRDB", "CDR_SECRET_USER_ENV", "CDR_SECRET_PASS_ENV", "docs"):
        assert secret not in text


def test_get_data_doc_returns_file_text(build, tmp_path):
    server, _, cfg = build()
    doc = cfg.systems["cdr"].data_doc
    doc.parent.mkdir()
    doc.write_text("# CDR\nBảng CDR_HISTORY: lọc theo CALL_TIME.\n", encoding="utf-8")
    assert call(server, "get_data_doc", system="cdr") == "# CDR\nBảng CDR_HISTORY: lọc theo CALL_TIME.\n"


def test_get_data_doc_truncates_large_file(build):
    from mcp_database.server import MAX_DOC_CHARS

    server, _, cfg = build()
    doc = cfg.systems["cdr"].data_doc
    doc.parent.mkdir()
    doc.write_text("a" * (MAX_DOC_CHARS + 10), encoding="utf-8")
    out = call(server, "get_data_doc", system="cdr")
    assert out.startswith("a" * MAX_DOC_CHARS)
    assert "a" * (MAX_DOC_CHARS + 1) not in out
    assert "cắt" in out


def test_get_data_doc_unknown_system(build):
    server, _, _ = build()
    out = call(server, "get_data_doc", system="nope")
    assert "nope" in out and "cdr" in out


def test_get_data_doc_missing_file_hides_host_paths(build, tmp_path):
    server, _, _ = build()
    out = call(server, "get_data_doc", system="cdr")
    assert "cdr.md" in out
    assert str(tmp_path) not in out
    assert "Errno" not in out and "No such file" not in out


def test_get_data_doc_invalid_utf8(build):
    server, _, cfg = build()
    doc = cfg.systems["cdr"].data_doc
    doc.parent.mkdir()
    doc.write_bytes(b"\xff\xfe\xfa bad")
    out = call(server, "get_data_doc", system="cdr")
    assert "cdr.md" in out and "codec" not in out


# --- describe_table -----------------------------------------------------------


def test_describe_table(build):
    server, _, _ = build()
    out = call(server, "describe_table", system="cdr", table="cdr_history")
    assert out["estimated_rows"] == 50_000_000
    assert out["columns"][0] == {"name": "CALL_TIME", "type": "DATE", "nullable": False, "sensitive": None}
    assert out["columns"][2]["sensitive"] == "hash"
    assert out["indexes"] == [{"name": "IX_CDR_TIME", "columns": ["CALL_TIME"], "unique": False}]


def test_describe_table_not_found_is_payload(build):
    server, _, _ = build()
    out = call(server, "describe_table", system="cdr", table="missing")
    assert set(out) == {"error", "hint"}
    assert "missing" in out["error"]


def test_describe_table_unknown_system_is_payload(build):
    server, _, _ = build()
    out = call(server, "describe_table", system="nope", table="users")
    assert "nope" in out["error"] and "list_systems" in out["hint"]


# --- explain_query ------------------------------------------------------------


def test_explain_query_never_executes(build):
    server, fake, _ = build()
    out = call(server, "explain_query", system="cdr",
               sql=f"SELECT status, COUNT(*) FROM cdr_history WHERE {WINDOW} GROUP BY status")
    assert out == {"allowed": True, "reasons": [], "hints": [], "plan": "PLAN", "cost": 10,
                   "estimated_rows": 10}
    assert fake.explained_sql
    assert fake.executed_sql == []


def test_explain_query_reports_rejection(build):
    server, fake, _ = build()
    out = call(server, "explain_query", system="cdr", sql="SELECT status FROM cdr_history")
    assert out["allowed"] is False
    assert out["reasons"] and out["hints"]
    assert fake.executed_sql == []


def test_explain_query_unknown_system_is_payload(build):
    server, _, _ = build()
    out = call(server, "explain_query", system="nope", sql="SELECT 1 FROM dual")
    assert set(out) == {"error", "hint"}


# --- run_query ----------------------------------------------------------------


def test_run_query_success(build, fake):
    fake.result = QueryResult(columns=["STATUS", "N"], rows=[("OK", 3), ("FAIL", 1)], truncated=False)
    server, _, _ = build()
    out = call(server, "run_query", system="cdr",
               sql=f"SELECT status, COUNT(*) n FROM cdr_history WHERE {WINDOW} GROUP BY status")
    assert out["columns"] == ["STATUS", "N"]
    assert out["rows"] == [["OK", 3], ["FAIL", 1]]
    assert out["row_count"] == 2 and out["truncated"] is False
    assert isinstance(out["elapsed_ms"], int) and out["notes"] == []


def test_run_query_rejection_is_payload(build, fake):
    server, _, _ = build()
    out = call(server, "run_query", system="cdr", sql="DELETE FROM t")
    assert set(out) == {"error", "hint"}
    assert out["error"] and out["hint"]
    assert fake.executed_sql == []


def test_run_query_timeout_is_payload(build, fake):
    fake.execute_error = QueryTimeout(30)
    server, _, _ = build()
    out = call(server, "run_query", system="cdr", sql="SELECT id FROM users")
    assert out["error"] == "query vượt 30 giây"
    assert out["hint"]


def test_run_query_database_error_is_sanitized(build, fake):
    exc = DatabaseError("ORA-00904")
    exc.__cause__ = ValueError("driver says: secret value 84912345678")
    fake.execute_error = exc
    server, _, _ = build()
    out = call(server, "run_query", system="cdr", sql="SELECT id FROM users")
    assert out["error"] == "Lỗi từ cơ sở dữ liệu (mã ORA-00904)"
    assert "84912345678" not in json.dumps(out)


def test_run_query_unexpected_error_is_generic(build, fake):
    fake.execute_error = KeyError("secret value 84912345678")
    server, _, _ = build()
    out = call(server, "run_query", system="cdr", sql="SELECT id FROM users")
    assert set(out) == {"error", "hint"}
    assert "84912345678" not in json.dumps(out) and "KeyError" not in json.dumps(out)


def test_tool_catch_all_hides_unexpected_exceptions(build, monkeypatch):
    server, _, _ = build()

    def boom(*a, **k):
        raise ValueError("secret 84912345678 /etc/secret/path")

    for method in ("run", "check", "table_info"):
        monkeypatch.setattr(QueryService, method, boom)
    outs = [
        call(server, "run_query", system="cdr", sql="SELECT 1 FROM dual"),
        call(server, "explain_query", system="cdr", sql="SELECT 1 FROM dual"),
        call(server, "describe_table", system="cdr", table="users"),
        call(server, "profile_column", system="cdr", table="users", column="ID"),
    ]
    for out in outs:
        assert set(out) == {"error", "hint"}
        text = json.dumps(out, ensure_ascii=False)
        assert "84912345678" not in text and "/etc/secret" not in text and "ValueError" not in text


# --- profile_column -----------------------------------------------------------


class RoutingFake(FakeConnector):
    """Trả kết quả khác nhau cho query thống kê và query top-N."""

    def execute(self, sql, max_rows, timeout_seconds):
        super().execute(sql, max_rows, timeout_seconds)
        if "DISTINCT" in sql.upper():
            return QueryResult(["TOTAL_ROWS", "NON_NULL", "DISTINCT_VALUES"], [(10, 8, 3)], False)
        return QueryResult(["VALUE", "CNT"], [("OK", 5), ("FAIL", 2), (None, 2)], False)


@pytest.fixture
def routing():
    return RoutingFake(tables={"CDR_HISTORY": cdr_info(), "USERS": users_info()})


@pytest.mark.parametrize("column", ["a); DROP TABLE t; --", "x y", "1abc", "a.b", "", "ID\n"])
def test_profile_column_rejects_injection(build, fake, column):
    server, _, _ = build()
    out = call(server, "profile_column", system="cdr", table="users", column=column)
    assert set(out) == {"error", "hint"}
    assert fake.executed_sql == [] and fake.explained_sql == []


@pytest.mark.parametrize("table", ["users; DROP TABLE t", "a.b.c", "users--", "\"users\""])
def test_profile_column_rejects_bad_table(build, fake, table):
    server, _, _ = build()
    out = call(server, "profile_column", system="cdr", table=table, column="ID")
    assert set(out) == {"error", "hint"}
    assert fake.executed_sql == [] and fake.explained_sql == [] and fake.table_info_calls == []


def test_profile_column_rejects_unknown_column(build, fake):
    server, _, _ = build()
    out = call(server, "profile_column", system="cdr", table="users", column="nope")
    assert "nope" in out["error"]
    assert fake.executed_sql == []


def test_profile_column_rejects_sensitive(build, fake):
    server, _, _ = build()
    for column in ("msisdn", "MSISDN"):
        out = call(server, "profile_column", system="cdr", table="cdr_history", column=column,
                   time_from="2026-11-01", time_to="2026-11-10")
        assert set(out) == {"error", "hint"}
        assert "nhạy cảm" in out["error"]
    out = call(server, "profile_column", system="cdr", table="APP.CDR_HISTORY", column="msisdn",
               time_from="2026-11-01", time_to="2026-11-10")
    assert "nhạy cảm" in out["error"]
    assert fake.executed_sql == []


def test_profile_column_sql_goes_through_pipeline(build, fake):
    server, _, _ = build()
    out = call(server, "profile_column", system="cdr", table="cdr_history", column="status")
    assert set(out) == {"error", "hint"}
    assert "CALL_TIME" in out["error"]
    assert fake.executed_sql == []


def test_profile_column_too_wide_range_rejected_by_pipeline(build, fake):
    server, _, _ = build()
    out = call(server, "profile_column", system="cdr", table="cdr_history", column="status",
               time_from="2026-01-01", time_to="2026-11-01")
    assert "31" in out["error"]
    assert fake.executed_sql == []


@pytest.mark.parametrize("bad", ["2026-11-1", "2026/11/01", "2026-13-01", "２０２６-11-01",
                                 "2026-11-01' OR 1=1 --", "2026-11-01 00:00:00"])
def test_profile_column_rejects_bad_dates(build, fake, bad):
    server, _, _ = build()
    out = call(server, "profile_column", system="cdr", table="cdr_history", column="status",
               time_from=bad, time_to="2026-11-10")
    assert set(out) == {"error", "hint"}
    assert "YYYY-MM-DD" in out["error"]
    assert fake.executed_sql == []


def test_profile_column_rejects_reversed_range(build, fake):
    server, _, _ = build()
    out = call(server, "profile_column", system="cdr", table="cdr_history", column="status",
               time_from="2026-11-10", time_to="2026-11-01")
    assert set(out) == {"error", "hint"}
    assert fake.executed_sql == []


def test_profile_column_time_args_need_time_column(build, fake):
    server, _, _ = build()
    out = call(server, "profile_column", system="cdr", table="users", column="ID",
               time_from="2026-11-01", time_to="2026-11-10")
    assert "thời gian" in out["error"]
    assert fake.executed_sql == []


def test_profile_column_oracle_success(build, routing):
    server, _, _ = build(connector=routing)
    out = call(server, "profile_column", system="cdr", table="cdr_history", column="status",
               time_from="2026-11-01", time_to="2026-11-10", top_n=5)
    assert out["total_rows"] == 10 and out["non_null"] == 8 and out["null_count"] == 2
    assert out["distinct_count"] == 3
    assert out["top_values"] == [{"value": "OK", "count": 5}, {"value": "FAIL", "count": 2},
                                 {"value": None, "count": 2}]
    stats_sql, top_sql = routing.executed_sql
    for sql in (stats_sql, top_sql):
        assert '"CDR_HISTORY"' in sql
        # sqlglot sinh lại DATE '...' thành TO_DATE('...', 'YYYY-MM-DD') cho Oracle.
        assert "\"CALL_TIME\" >= TO_DATE('2026-11-01', 'YYYY-MM-DD')" in sql
        assert "\"CALL_TIME\" < TO_DATE('2026-11-10', 'YYYY-MM-DD')" in sql
    assert 'COUNT(DISTINCT "STATUS")' in stats_sql
    assert 'GROUP BY "STATUS"' in top_sql and "FETCH FIRST 5 ROWS ONLY" in top_sql


def test_profile_column_mariadb_quotes_with_backticks(build, routing):
    server, _, _ = build(db_type="mariadb", connector=routing)
    out = call(server, "profile_column", system="cdr", table="users", column="region", top_n=500)
    assert "error" not in out
    assert out["column"] == "Region"
    stats_sql, top_sql = routing.executed_sql
    assert "`users`" in stats_sql and "COUNT(DISTINCT `Region`)" in stats_sql
    assert "GROUP BY `Region`" in top_sql and "LIMIT 50" in top_sql


def test_profile_column_clamps_top_n_low(build, routing):
    server, _, _ = build(connector=routing)
    call(server, "profile_column", system="cdr", table="users", column="ID", top_n=0)
    assert "FETCH FIRST 1 ROWS ONLY" in routing.executed_sql[1]


def test_profile_column_runs_are_audited(build, routing, tmp_path):
    server, _, _ = build(connector=routing)
    call(server, "profile_column", system="cdr", table="users", column="ID")
    lines = (tmp_path / "audit.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2


# --- main ---------------------------------------------------------------------

CONFIG_YAML = """\
audit_log: audit.jsonl
systems:
  cdr:
    db_type: oracle
    host: 127.0.0.1
    port: 1
    database: XE
    user_env: SMOKE_DB_USER
    password_env: SMOKE_DB_PASS
    data_doc: cdr.md
    is_replica: true
    guard:
      sensitive_columns:
        CDR_HISTORY.MSISDN: hash
  shop:
    db_type: mariadb
    host: 127.0.0.1
    port: 1
    database: shop
    user_env: SMOKE_SHOP_USER
    password_env: SMOKE_SHOP_PASS
    data_doc: shop.md
    is_replica: true
"""

ALL_ENV = {"SMOKE_DB_USER": "u", "SMOKE_DB_PASS": "p-very-secret", "SMOKE_SHOP_USER": "u2",
           "SMOKE_SHOP_PASS": "p2-very-secret", "MCP_DATABASE_HMAC_KEY": "k" * 32}


@pytest.fixture
def config_file(tmp_path, monkeypatch):
    path = tmp_path / "systems.yaml"
    path.write_text(CONFIG_YAML, encoding="utf-8")
    monkeypatch.setenv("MCP_DATABASE_CONFIG", str(path))
    for k in ALL_ENV:
        monkeypatch.delenv(k, raising=False)
    return path


def run_main(monkeypatch):
    from mcp_database import server

    started = []
    monkeypatch.setattr(server.MCPServer, "run", lambda self, transport="stdio": started.append(transport))
    try:
        server.main()
    except SystemExit as e:
        return e.code, started
    return None, started


def test_main_exits_on_missing_env(config_file, monkeypatch, capsys):
    monkeypatch.setenv("SMOKE_DB_USER", "u")
    monkeypatch.setenv("SMOKE_SHOP_PASS", "p2-very-secret")
    code, started = run_main(monkeypatch)
    assert code == 1 and started == []
    out, err = capsys.readouterr()
    assert out == ""
    for name in ("SMOKE_DB_PASS", "SMOKE_SHOP_USER", "MCP_DATABASE_HMAC_KEY"):
        assert name in err
    assert "SMOKE_DB_USER" not in err and "p2-very-secret" not in err


def test_main_rejects_short_hmac_key(config_file, monkeypatch, capsys):
    for k, v in ALL_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("MCP_DATABASE_HMAC_KEY", "short-key")
    code, started = run_main(monkeypatch)
    assert code == 1 and started == []
    out, err = capsys.readouterr()
    assert out == "" and "MCP_DATABASE_HMAC_KEY" in err and "16" in err and "short-key" not in err


def test_main_config_error(tmp_path, monkeypatch, capsys):
    path = tmp_path / "bad.yaml"
    path.write_text("systems: 5\n", encoding="utf-8")
    monkeypatch.setenv("MCP_DATABASE_CONFIG", str(path))
    code, started = run_main(monkeypatch)
    assert code == 1 and started == []
    out, err = capsys.readouterr()
    assert out == "" and "systems" in err and "Traceback" not in err


def test_main_unreadable_config(tmp_path, monkeypatch, capsys):
    path = tmp_path / "bin.yaml"
    path.write_bytes(b"\xff\xfe\x00bad")
    monkeypatch.setenv("MCP_DATABASE_CONFIG", str(path))
    code, started = run_main(monkeypatch)
    assert code == 1 and started == []
    out, err = capsys.readouterr()
    assert out == "" and "Traceback" not in err and err.strip()


def test_main_default_config_path(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("MCP_DATABASE_CONFIG", raising=False)
    monkeypatch.chdir(tmp_path)
    code, _ = run_main(monkeypatch)
    assert code == 1
    assert "systems.yaml" in capsys.readouterr().err


def test_main_starts_stdio_when_env_ok(config_file, monkeypatch, capsys):
    for k, v in ALL_ENV.items():
        monkeypatch.setenv(k, v)
    code, started = run_main(monkeypatch)
    assert code is None and started == ["stdio"]
    assert capsys.readouterr().out == ""


# --- Smoke test giao thức stdio ---------------------------------------------


def test_stdio_protocol_smoke(tmp_path):
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    (tmp_path / "systems.yaml").write_text(CONFIG_YAML, encoding="utf-8")
    env = dict(ALL_ENV, MCP_DATABASE_CONFIG=str(tmp_path / "systems.yaml"))
    params = StdioServerParameters(command=sys.executable, args=["-m", "mcp_database"], env=env,
                                   cwd=str(tmp_path))
    errlog = open(tmp_path / "stderr.log", "w", encoding="utf-8")

    async def scenario():
        with anyio.fail_after(30):
            async with stdio_client(params, errlog=errlog) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    result = await session.call_tool("list_systems", {})
                    return tools, result

    started = time.monotonic()
    try:
        tools, result = anyio.run(scenario)
    finally:
        errlog.close()
    assert {t.name for t in tools.tools} == TOOLS
    assert not result.is_error
    assert result.structured_content["result"] == [{"name": "cdr", "db_type": "oracle"},
                                                    {"name": "shop", "db_type": "mariadb"}]
    assert time.monotonic() - started < 30
