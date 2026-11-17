import json
import threading
from datetime import date
from types import MappingProxyType

import pytest

from fake_connector import FakeConnector
from mcp_database.config import Config, Guard, SystemProfile, TableGuard
from mcp_database.db.base import (
    ColumnInfo,
    DatabaseError,
    MissingCredentialError,
    PlanSummary,
    QueryResult,
    QueryTimeout,
    TableInfo,
)
from mcp_database.db.base import QueryRejected as DbQueryRejected
from mcp_database.guard.parser import QueryRejected, parse_select
from mcp_database.guard.policy import apply_row_limit
from mcp_database.masking import apply_masking
from mcp_database.service import MAX_SQL_CHARS, CheckResult, QueryService, RunResult

KEY = b"KEY-SECRET-XYZ"
TODAY = date(2026, 11, 17)
WINDOW = "call_time >= DATE '2026-11-01' AND call_time < DATE '2026-11-10'"
HAPPY = f"SELECT msisdn, COUNT(*) FROM cdr_history WHERE {WINDOW} GROUP BY msisdn"
SAFE = f"SELECT status, COUNT(*) FROM cdr_history WHERE {WINDOW} GROUP BY status"
ROWS = [("84912345678", 3), ("84987654321", 5)]


def info(rows):
    return TableInfo(columns=[ColumnInfo("X", "NUMBER", True)], indexes=[], estimated_rows=rows)


def make_guard(**kw):
    base = dict(
        tables=MappingProxyType({"CDR_HISTORY": TableGuard("CALL_TIME", 31)}),
        sensitive_columns=MappingProxyType({"CDR_HISTORY.MSISDN": "hash"}),
    )
    base.update(kw)
    return Guard(**base)


def make_profile(tmp_path, name="cdr", db_type="oracle", **guard_kw):
    return SystemProfile(
        name=name, db_type=db_type, host="h", port=1521, database="d",
        user_env="U", password_env="P", data_doc=tmp_path / "doc.md",
        guard=make_guard(**guard_kw), is_replica=True, allow_primary=False)


@pytest.fixture
def audit_path(tmp_path):
    return tmp_path / "logs" / "audit.jsonl"


@pytest.fixture
def fake():
    return FakeConnector(
        # tra nghiêm ngặt theo đúng cách viết trong SQL
        tables={"CDR_HISTORY": info(50_000_000), "cdr_history": info(50_000_000),
                "USERS": info(100), "users": info(100)},
        result=QueryResult(columns=["MSISDN", "COUNT(*)"], rows=list(ROWS), truncated=False),
    )


@pytest.fixture
def build(tmp_path, audit_path, fake):
    """build(**guard_kw) -> (service, fake); hmac_key, profiles, connector có thể đổi."""

    def _build(hmac_key=KEY, profiles=None, connector=None, audit=audit_path, **guard_kw):
        profiles = profiles or [make_profile(tmp_path, **guard_kw)]
        conn = connector or fake
        cfg = Config(audit_log=audit, systems=MappingProxyType({p.name: p for p in profiles}))
        svc = QueryService(cfg, connector_factory=lambda profile: conn, hmac_key=hmac_key,
                           today=lambda: TODAY)
        return svc, conn

    return _build


def audit_events(path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def last_event(path):
    return audit_events(path)[-1]


# --- Từ brief ----------------------------------------------------------------


def test_happy_path_applies_limit_and_masks(build, audit_path):
    svc, fake = build()
    result = svc.run("cdr", HAPPY)
    assert isinstance(result, RunResult)
    assert len(fake.executed_sql) == 1
    assert "FETCH FIRST 1001 ROWS ONLY" in fake.executed_sql[0]
    assert result.rows == apply_masking(ROWS, ["hash", None], KEY)
    assert all(r[0] != orig[0] and len(r[0]) == 16 for r, orig in zip(result.rows, ROWS))
    assert [r[1] for r in result.rows] == [3, 5]
    assert result.columns == ["MSISDN", "COUNT(*)"]
    assert result.row_count == 2
    assert result.truncated is False
    assert result.elapsed_ms >= 0
    ev = last_event(audit_path)
    assert ev["status"] == "ok"
    assert ev["row_count"] == 2
    assert ev["system"] == "cdr"
    assert ev["sql"] == HAPPY
    assert ev["reason"] is None
    assert isinstance(ev["elapsed_ms"], int)
    assert "ts" in ev


@pytest.mark.parametrize(
    "sql, plan",
    [
        ("DELETE FROM cdr_history", None),
        ("SELECT status, COUNT(*) FROM cdr_history GROUP BY status", None),
        (SAFE, PlanSummary(cost=10, est_rows=10, full_scans=["CDR_HISTORY"], text="FULL")),
    ],
    ids=["delete", "no-time-range", "full-scan"],
)
def test_rejected_never_reaches_execute(build, audit_path, fake, sql, plan):
    if plan is not None:
        fake.plan = plan
    svc, _ = build()
    with pytest.raises(QueryRejected) as e:
        svc.run("cdr", sql)
    assert e.value.reason
    assert fake.executed_sql == []
    ev = last_event(audit_path)
    assert ev["status"] == "rejected"
    assert ev["reason"] == e.value.reason
    assert ev["row_count"] is None


def test_explain_runs_on_sql_without_server_limit(build, fake):
    # EXPLAIN phải tính chi phí câu không có giới hạn do server thêm (first-rows costing
    # của FETCH FIRST che full scan), còn câu thực thi thì có giới hạn max_rows + 1.
    svc, _ = build()
    svc.run("cdr", SAFE)
    parsed = parse_select(SAFE, "oracle")
    assert fake.explained_sql == [parsed.sql]
    assert "FETCH" not in fake.explained_sql[0].upper()
    assert fake.executed_sql == [apply_row_limit(parsed, 1001)]


def test_explain_keeps_user_limit(build, fake):
    svc, _ = build()
    svc.run("cdr", SAFE + " FETCH FIRST 7 ROWS ONLY")
    assert "FETCH FIRST 7 ROWS ONLY" in fake.explained_sql[0]
    assert "FETCH FIRST 7 ROWS ONLY" in fake.executed_sql[0]


def test_check_does_not_execute(build, fake):
    svc, _ = build()
    res = svc.check("cdr", SAFE)
    assert isinstance(res, CheckResult)
    assert res.allowed is True
    assert res.reasons == [] and res.hints == []
    assert len(fake.explained_sql) == 1
    assert fake.executed_sql == []
    assert res.sql == apply_row_limit(parse_select(SAFE, "oracle"), 1001)
    assert res.plan == fake.plan
    assert res.mask_plan == []


def test_check_returns_reasons_and_hints(build, fake):
    svc, _ = build()
    res = svc.check("cdr", "SELECT status FROM cdr_history")
    assert res.allowed is False
    assert res.reasons[0]
    assert res.hints[0]
    assert res.sql is None and res.plan is None
    assert fake.explained_sql == [] and fake.executed_sql == []


def test_busy_when_slots_exhausted(build, fake, audit_path):
    svc, _ = build(max_concurrent=1)
    entered, release = threading.Event(), threading.Event()

    def block():
        entered.set()
        assert release.wait(5)

    fake.on_execute = block
    out = {}
    worker = threading.Thread(target=lambda: out.setdefault("r", svc.run("cdr", SAFE)))
    worker.start()
    assert entered.wait(5)
    try:
        with pytest.raises(QueryRejected) as e:
            svc.run("cdr", SAFE)
        assert "bận" in e.value.reason
        assert e.value.hint == "thử lại sau"
        assert last_event(audit_path)["status"] == "rejected"
        # check cũng gọi EXPLAIN xuống DB nên cũng phải chờ slot
        res = svc.check("cdr", SAFE)
        assert res.allowed is False and "bận" in res.reasons[0]
    finally:
        release.set()
        worker.join(5)
    assert out["r"].row_count == 2
    fake.on_execute = None
    # slot đã được trả: chạy lại được
    assert svc.run("cdr", SAFE).row_count == 2

    # slot được trả cả khi execute ném lỗi
    fake.execute_error = DatabaseError("ORA-01555")
    for _ in range(3):
        with pytest.raises(DatabaseError):
            svc.run("cdr", SAFE)
    fake.execute_error = None
    assert svc.run("cdr", SAFE).row_count == 2


def test_unknown_system(build, audit_path):
    svc, fake = build()
    with pytest.raises(LookupError):
        svc.run("nope", SAFE)
    with pytest.raises(LookupError):
        svc.check("nope", SAFE)
    with pytest.raises(LookupError):
        svc.table_info("nope", "cdr_history")
    assert fake.explained_sql == [] and fake.table_info_calls == []
    ev = last_event(audit_path)
    assert ev["status"] == "error" and ev["system"] == "nope"


def test_sensitive_requires_key(build, fake, audit_path):
    svc, _ = build(hmac_key=None)
    with pytest.raises(QueryRejected):
        svc.run("cdr", HAPPY)
    assert fake.explained_sql == [] and fake.executed_sql == []
    assert last_event(audit_path)["status"] == "rejected"
    assert svc.check("cdr", HAPPY).allowed is False
    # query không lấy cột nhạy cảm thì vẫn chạy được khi không có khóa
    assert svc.run("cdr", SAFE).row_count == 2


def test_timeout_logged(build, fake, audit_path):
    fake.execute_error = TimeoutError("boom")
    svc, _ = build()
    with pytest.raises(TimeoutError):
        svc.run("cdr", SAFE)
    ev = last_event(audit_path)
    assert ev["status"] == "timeout"
    assert ev["row_count"] is None


def test_audit_failure_does_not_break_query(build, tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("x")
    svc, _ = build(audit=blocker / "audit.jsonl")
    assert svc.run("cdr", SAFE).row_count == 2


def test_audit_has_no_row_data(build, audit_path, fake):
    svc, _ = build()
    fake.result = QueryResult(columns=["STATUS", "N"], rows=[("ROW-VALUE-777", 42424242)], truncated=False)
    svc.run("cdr", SAFE)
    text = audit_path.read_text(encoding="utf-8")
    assert "ROW-VALUE-777" not in text
    assert "42424242" not in text


# --- Thêm: carry-over của controller ------------------------------------------


def test_db_query_rejected_is_audited_as_rejected(build, fake, audit_path):
    fake.execute_error = DbQueryRejected()
    svc, _ = build()
    with pytest.raises(QueryRejected) as e:
        svc.run("cdr", SAFE)
    assert e.value.reason == str(DbQueryRejected())
    assert last_event(audit_path)["status"] == "rejected"


def test_check_packs_database_error(build, fake):
    fake.explain_error = DatabaseError("ORA-01013")
    svc, _ = build()
    res = svc.check("cdr", SAFE)
    assert res.allowed is False
    assert res.reasons == ["Lỗi từ cơ sở dữ liệu (mã ORA-01013)"]


def test_check_packs_timeout(build, fake):
    fake.explain_error = QueryTimeout(30)
    svc, _ = build()
    res = svc.check("cdr", SAFE)
    assert res.allowed is False
    assert res.reasons == [str(QueryTimeout(30))]


def test_check_plan_rejection_keeps_sql_and_plan(build, fake):
    fake.plan = PlanSummary(cost=10**9, est_rows=10, full_scans=[], text="BIG")
    svc, _ = build()
    res = svc.check("cdr", SAFE)
    assert res.allowed is False
    assert res.plan is fake.plan
    assert res.sql == apply_row_limit(parse_select(SAFE, "oracle"), 1001)


ORACLE_TRICKS = (
    "SELECT /*+ FULL(c) INDEX(c x) */ c.status -- ghi chú\n"
    f"FROM cdr_history c /* chú thích */ WHERE c.{WINDOW.replace('call_time <', 'c.call_time <')}"
)


def test_only_regenerated_sql_reaches_connector(build, fake):
    svc, _ = build()
    svc.run("cdr", ORACLE_TRICKS)
    parsed = parse_select(ORACLE_TRICKS, "oracle")
    assert fake.explained_sql == [parsed.sql]
    assert fake.executed_sql == [apply_row_limit(parsed, 1001)]
    for sql in fake.explained_sql + fake.executed_sql:
        assert sql != ORACLE_TRICKS
        assert "/*" not in sql and "--" not in sql and "FULL(" not in sql


def test_mariadb_executable_comment_never_reaches_connector(build, tmp_path, fake):
    svc, _ = build(profiles=[make_profile(tmp_path, name="m", db_type="mariadb")])
    raw = "SELECT /*! SLEEP(100) */ id FROM users"
    with pytest.raises(QueryRejected):
        svc.run("m", raw)
    assert svc.check("m", raw).allowed is False
    assert fake.explained_sql == [] and fake.executed_sql == [] and fake.table_info_calls == []


def test_mariadb_limit_added(build, tmp_path, fake):
    svc, _ = build(profiles=[make_profile(tmp_path, name="m", db_type="mariadb")])
    svc.run("m", "SELECT id FROM users -- x")
    assert fake.executed_sql == ["SELECT id FROM users LIMIT 1001"]
    assert fake.explained_sql == ["SELECT id FROM users"]


def test_sql_length_cap_before_parse(build, fake, audit_path, monkeypatch):
    import mcp_database.service as service_mod

    def boom(*a, **k):  # pragma: no cover - không được gọi tới
        raise AssertionError("parse_select không được gọi")

    monkeypatch.setattr(service_mod, "parse_select", boom)
    svc, _ = build()
    long_sql = "SELECT 1 FROM dual WHERE 1 = 1" + " " * MAX_SQL_CHARS
    assert MAX_SQL_CHARS == 20_000
    with pytest.raises(QueryRejected) as e:
        svc.run("cdr", long_sql)
    assert e.value.reason and e.value.hint
    res = svc.check("cdr", long_sql)
    assert res.allowed is False and res.hints
    ev = last_event(audit_path)
    assert ev["status"] == "rejected"
    assert len(ev["sql"]) == 2000
    assert fake.table_info_calls == [] and fake.explained_sql == []


@pytest.mark.parametrize("where", ["execute", "explain", "table_info"])
def test_foreign_exception_text_never_relayed(build, fake, audit_path, where):
    setattr(fake, f"{where}_error", RuntimeError("SECRET-VALUE-123"))
    svc, _ = build()
    with pytest.raises(Exception) as e:
        svc.run("cdr", SAFE)
    assert "SECRET-VALUE-123" not in str(e.value)
    assert "SECRET-VALUE-123" not in repr(e.value)
    res = svc.check("cdr", SAFE)
    assert res.allowed is (where == "execute")  # check không execute
    assert "SECRET-VALUE-123" not in repr(res)
    text = audit_path.read_text(encoding="utf-8")
    assert "SECRET-VALUE-123" not in text
    ev = last_event(audit_path)
    assert ev["status"] == "error"
    assert ev["error_type"] == "RuntimeError"


@pytest.mark.parametrize(
    "exc",
    [TimeoutError("SECRET-VALUE-123"), DatabaseError("SECRET-VALUE-123")],
    ids=["timeout", "db-error-with-bogus-code"],
)
def test_untrusted_text_in_known_exceptions_not_relayed(build, fake, audit_path, exc):
    fake.execute_error = exc
    svc, _ = build()
    with pytest.raises((TimeoutError, DatabaseError)) as e:
        svc.run("cdr", SAFE)
    assert "SECRET-VALUE-123" not in str(e.value)
    assert "SECRET-VALUE-123" not in audit_path.read_text(encoding="utf-8")


def test_database_error_code_audited(build, fake, audit_path):
    fake.execute_error = DatabaseError("ORA-01555")
    svc, _ = build()
    with pytest.raises(DatabaseError) as e:
        svc.run("cdr", SAFE)
    assert str(e.value) == "Lỗi từ cơ sở dữ liệu (mã ORA-01555)"
    ev = last_event(audit_path)
    assert ev["status"] == "error"
    assert ev["code"] == "ORA-01555"
    assert ev["reason"] == str(e.value)


def test_hmac_key_never_logged_or_returned(build, audit_path):
    svc, _ = build()
    res = svc.run("cdr", HAPPY)
    chk = svc.check("cdr", HAPPY)
    svc.check("cdr", "SELECT msisdn FROM cdr_history")
    for blob in (repr(res), repr(chk), audit_path.read_text(encoding="utf-8"), repr(svc)):
        assert "KEY-SECRET-XYZ" not in blob
    assert chk.mask_plan == ["hash", None]


def test_guard_values_passed_to_execute(build, fake):
    svc, _ = build(max_rows=50, timeout_seconds=7)
    svc.run("cdr", SAFE)
    sql, max_rows, timeout = fake.execute_args[0]
    assert "FETCH FIRST 51 ROWS ONLY" in sql
    assert (max_rows, timeout) == (50, 7)


def test_today_is_injected(tmp_path, audit_path, fake):
    cfg = Config(audit_log=audit_path,
                 systems=MappingProxyType({"cdr": make_profile(tmp_path)}))
    svc = QueryService(cfg, connector_factory=lambda p: fake, hmac_key=KEY,
                       today=lambda: date(2027, 6, 1))
    # khoảng 2026-11-01 -> (thiếu cận trên => hôm nay 2027-06-01) vượt 31 ngày
    with pytest.raises(QueryRejected):
        svc.run("cdr", "SELECT status FROM cdr_history WHERE call_time >= DATE '2026-11-01'")
    svc_ok = QueryService(cfg, connector_factory=lambda p: fake, hmac_key=KEY,
                          today=lambda: date(2026, 11, 10))
    svc_ok.run("cdr", "SELECT status FROM cdr_history WHERE call_time >= DATE '2026-11-01'")


def test_table_stats_feed_large_table_detection(build, fake):
    fake.register("users", info(10_000_000))  # vượt large_table_rows nhưng không khai báo cột thời gian
    svc, _ = build()
    with pytest.raises(QueryRejected) as e:
        svc.run("cdr", "SELECT id FROM users")
    assert "USERS" in e.value.reason
    assert fake.table_info_calls == ["users"]


# --- Fix wave C: tra thống kê theo tham chiếu như đã viết + cache ------------------


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def clocked(tmp_path, audit_path):
    def _build(connector, clock, **guard_kw):
        profiles = [make_profile(tmp_path, **guard_kw)]
        cfg = Config(audit_log=audit_path, systems=MappingProxyType({p.name: p for p in profiles}))
        return QueryService(cfg, connector_factory=lambda p: connector, hmac_key=KEY,
                            today=lambda: TODAY, clock=clock)
    return _build


def test_schema_qualified_stats_mark_table_large(clocked):
    fake = FakeConnector(tables={("billing", "huge"): info(40_000_000)})
    svc = clocked(fake, Clock())
    with pytest.raises(QueryRejected) as e:
        svc.run("cdr", "SELECT COUNT(*) FROM billing.huge")
    assert "HUGE" in e.value.reason
    assert fake.table_info_calls == ["billing.huge"]
    assert fake.table_info_timeouts == [5]  # min(timeout_seconds=30, 5)


def test_schema_qualified_time_filter(clocked):
    fake = FakeConnector(tables={("billing", "cdr_history"): info(40_000_000)},
                         plan=PlanSummary(cost=10, est_rows=10, full_scans=["CDR_HISTORY"], text=""))
    svc = clocked(fake, Clock())
    with pytest.raises(QueryRejected):
        svc.run("cdr", "SELECT COUNT(*) FROM billing.cdr_history")
    fake.plan = PlanSummary(cost=10, est_rows=10, full_scans=[], text="")
    svc.run("cdr", f"SELECT COUNT(*) FROM billing.cdr_history WHERE {WINDOW}")
    assert fake.table_info_calls == ["billing.cdr_history"]  # lần hai lấy từ cache


@pytest.mark.parametrize("sql", ["SELECT COUNT(*) FROM huge", "SELECT COUNT(*) FROM BILLING.HUGE",
                                 "SELECT COUNT(*) FROM other.huge"])
def test_strict_fake_other_spellings_not_found(clocked, sql):
    fake = FakeConnector(tables={("billing", "huge"): info(40_000_000)})
    svc = clocked(fake, Clock())
    svc.run("cdr", sql)  # không có thống kê -> không coi là bảng lớn
    assert len(fake.executed_sql) == 1


def test_quoted_oracle_ref_passed_with_quotes(clocked):
    fake = FakeConnector(tables={('"Billing"', '"Huge"'): info(40_000_000)})
    svc = clocked(fake, Clock())
    with pytest.raises(QueryRejected):
        svc.run("cdr", 'SELECT COUNT(*) FROM "Billing"."Huge"')
    assert fake.table_info_calls == ['"Billing"."Huge"']


def test_same_name_two_schemas_uses_larger(clocked):
    fake = FakeConnector(tables={("a", "t"): info(100), ("b", "t"): info(40_000_000)})
    svc = clocked(fake, Clock())
    with pytest.raises(QueryRejected) as e:
        svc.run("cdr", "SELECT COUNT(*) FROM a.t JOIN b.t ON 1=1")
    assert "T" in e.value.reason
    assert sorted(fake.table_info_calls) == ["a.t", "b.t"]


def test_stats_cache_ttl(clocked):
    clock = Clock()
    fake = FakeConnector(tables={(None, "users"): info(100)})
    svc = clocked(fake, clock)
    svc.run("cdr", "SELECT id FROM users")
    svc.run("cdr", "SELECT id FROM users")
    assert fake.table_info_calls == ["users"]
    clock.now += 299
    svc.run("cdr", "SELECT id FROM users")
    assert len(fake.table_info_calls) == 1
    clock.now += 2
    svc.run("cdr", "SELECT id FROM users")
    assert len(fake.table_info_calls) == 2


@pytest.mark.parametrize("error", [None, DatabaseError(1234), QueryTimeout(5)])
def test_failed_stats_lookup_cached_briefly(clocked, error):
    clock = Clock()
    fake = FakeConnector()
    fake.table_info_error = error
    svc = clocked(fake, clock)
    svc.run("cdr", "SELECT id FROM users")
    svc.run("cdr", "SELECT id FROM users")
    assert len(fake.table_info_calls) == 1
    clock.now += 31
    svc.run("cdr", "SELECT id FROM users")
    assert len(fake.table_info_calls) == 2


@pytest.mark.parametrize("error", [None, DatabaseError(1234), QueryTimeout(5)])
def test_stats_failure_treated_as_unknown(build, fake, error):
    # bảng không có trong fake (TableNotFound) hoặc lỗi thống kê -> None, query vẫn chạy
    fake.table_info_error = error
    svc, _ = build()
    res = svc.run("cdr", "SELECT id FROM missing_table")
    assert res.row_count == 2
    assert len(fake.executed_sql) == 1


def test_truncated_only_from_connector(build, fake):
    # Đúng max_rows dòng nhưng connector báo không còn dòng nào -> không bị cắt.
    fake.result = QueryResult(columns=["STATUS"], rows=[(i,) for i in range(3)], truncated=False)
    svc, _ = build(max_rows=3)
    res = svc.run("cdr", SAFE)
    assert res.truncated is False and res.row_count == 3
    assert not any("giới hạn" in n for n in res.notes)


def test_truncated_note(build, fake):
    fake.result = QueryResult(columns=["STATUS"], rows=[(i,) for i in range(4)], truncated=False)
    svc, _ = build(max_rows=3)
    res = svc.run("cdr", SAFE)
    assert res.truncated is True and res.row_count == 3
    assert any("3" in n for n in res.notes)


def test_user_fetch_first_equal_to_max_rows_not_truncated(build, fake):
    fake.result = QueryResult(columns=["STATUS"], rows=[(i,) for i in range(3)], truncated=False)
    svc, _ = build(max_rows=3)
    res = svc.run("cdr", SAFE + " FETCH FIRST 3 ROWS ONLY")
    assert "FETCH FIRST 3 ROWS ONLY" in fake.executed_sql[0]
    assert res.truncated is False


def test_user_limit_above_max_rows_truncated(build, tmp_path, fake):
    fake.result = QueryResult(columns=["ID"], rows=[(i,) for i in range(4)], truncated=False)
    svc, _ = build(profiles=[make_profile(tmp_path, name="m", db_type="mariadb", max_rows=3)])
    res = svc.run("m", "SELECT id FROM users LIMIT 5000")
    assert fake.executed_sql == ["SELECT id FROM users LIMIT 4"]
    assert res.truncated is True and res.row_count == 3


def test_masking_note(build):
    svc, _ = build()
    res = svc.run("cdr", HAPPY)
    assert any("MSISDN" in n for n in res.notes)


def test_connector_created_lazily_and_reused(tmp_path, audit_path, fake):
    calls = []

    def factory(profile):
        calls.append(profile.name)
        return fake

    cfg = Config(audit_log=audit_path, systems=MappingProxyType({"cdr": make_profile(tmp_path)}))
    svc = QueryService(cfg, connector_factory=factory, hmac_key=KEY, today=lambda: TODAY)
    assert calls == []
    svc.run("cdr", SAFE)
    svc.check("cdr", SAFE)
    svc.table_info("cdr", "cdr_history")
    assert calls == ["cdr"]


def test_missing_credentials_reported_and_audited(tmp_path, audit_path):
    def factory(profile):
        raise MissingCredentialError("Hệ thống 'cdr': thiếu biến môi trường 'P'")

    cfg = Config(audit_log=audit_path, systems=MappingProxyType({"cdr": make_profile(tmp_path)}))
    svc = QueryService(cfg, connector_factory=factory, hmac_key=KEY, today=lambda: TODAY)
    with pytest.raises(MissingCredentialError):
        svc.run("cdr", SAFE)
    assert last_event(audit_path)["status"] == "error"
    res = svc.check("cdr", SAFE)
    assert res.allowed is False and "biến môi trường" in res.reasons[0]


def test_table_info_passthrough_and_slot(build, fake):
    svc, _ = build(max_concurrent=1)
    assert svc.table_info("cdr", "cdr_history").estimated_rows == 50_000_000
    fake.table_info_error = RuntimeError("SECRET-VALUE-123")
    with pytest.raises(Exception) as e:
        svc.table_info("cdr", "cdr_history")
    assert "SECRET-VALUE-123" not in str(e.value)
    fake.table_info_error = None
    # slot đã được trả sau lỗi
    assert svc.table_info("cdr", "users").estimated_rows == 100


def test_masking_column_mismatch_fails_closed(build, fake, audit_path):
    # connector trả số cột khác plan -> không trả dữ liệu chưa che
    fake.result = QueryResult(columns=["A"], rows=[("84912345678",)], truncated=False)
    svc, _ = build()
    with pytest.raises(Exception) as e:
        svc.run("cdr", HAPPY)
    assert "84912345678" not in str(e.value)
    assert last_event(audit_path)["status"] == "error"
