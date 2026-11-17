"""Connector MariaDB (PyMySQL).

READ ONLY ở phía session chỉ là lớp dự phòng; tài khoản DB dùng cho server nên chỉ có quyền
SELECT. Lớp này từ chối mọi câu lệnh không trả về tập kết quả.
"""
from __future__ import annotations

import pymysql

from ..config import SystemProfile
from ..names import norm
from .base import (ColumnInfo, IndexInfo, PlanSummary, QueryResult, TableInfo,
                   TableNotFound, get_credentials, parse_table_name, run_query, session)

# 1969: Query execution was interrupted (max_statement_time).
TIMEOUT_CODES = frozenset({1969})
# 2013 (CR_SERVER_LOST): PyMySQL báo mất kết nối, kể cả khi hết read_timeout phía client.
CR_SERVER_LOST = 2013
CONNECT_TIMEOUT_SECONDS = 10
# read/write_timeout phía client = timeout của query + biên này: server (max_statement_time)
# ngắt trước; client chỉ là chốt chặn khi server/mạng treo, để worker không giữ slot mãi.
CLIENT_TIMEOUT_MARGIN_SECONDS = 5


def _is_client_timeout(exc: BaseException) -> bool:
    """OperationalError 2013 do socket hết thời gian chờ (không đọc văn bản lỗi)."""
    if not (exc.args and exc.args[0] == CR_SERVER_LOST):
        return False
    inner = exc.__cause__ or exc.__context__
    return isinstance(inner, TimeoutError)


def summarize_mariadb_explain(rows: list[dict]) -> PlanSummary:
    """Ước lượng số dòng từ EXPLAIN.

    Trong cùng một `id` (một SELECT), các bảng được join lồng nhau nên số dòng ước lượng
    là tích `rows`. Giữa các `id` khác nhau (subquery, nhánh UNION, bảng dẫn xuất) lấy
    MAX chứ không nhân/cộng: nhân chéo các SELECT độc lập làm phình số liệu và từ chối
    oan các UNION; MAX vẫn bắt được SELECT tốn kém nhất.
    """
    full: list[str] = []
    per_id: dict[object, int] = {}
    for r in rows:
        if r.get("type") == "ALL" and r.get("table"):
            name = norm(str(r["table"]))
            if name not in full:
                full.append(name)
        n = r.get("rows")
        if n is not None:
            key = r.get("id")
            per_id[key] = int(n) if key not in per_id else per_id[key] * int(n)
    est = max(per_id.values()) if per_id else None
    text = "\n".join(
        f"id={r.get('id')} {r.get('select_type') or ''} {r.get('table')}: "
        f"type={r.get('type')} rows={r.get('rows')}".replace("  ", " ") for r in rows)
    return PlanSummary(cost=None, est_rows=est, full_scans=full, text=text)


class MariaDBConnector:
    def __init__(self, profile: SystemProfile):
        self.profile = profile

    def _connect(self, timeout_seconds: int):
        user, password = get_credentials(self.profile)
        p = self.profile
        client_timeout = int(timeout_seconds) + CLIENT_TIMEOUT_MARGIN_SECONDS
        return pymysql.connect(host=p.host, port=p.port, user=user, password=password,
                               database=p.database, charset="utf8mb4", autocommit=False,
                               connect_timeout=CONNECT_TIMEOUT_SECONDS,
                               read_timeout=client_timeout, write_timeout=client_timeout)

    def _session(self, timeout_seconds: int, streaming: bool = False):
        def setup(conn):
            # SSCursor: không nạp cả tập kết quả vào bộ nhớ trước khi fetchmany.
            cur = conn.cursor(pymysql.cursors.SSCursor) if streaming else conn.cursor()
            cur.execute(f"SET SESSION max_statement_time = {int(timeout_seconds)}")
            cur.execute("SET SESSION TRANSACTION READ ONLY")
            cur.execute("START TRANSACTION")
            return cur
        return session(lambda: self._connect(timeout_seconds), setup, TIMEOUT_CODES, timeout_seconds,
                       is_timeout=_is_client_timeout)

    def execute(self, sql: str, max_rows: int, timeout_seconds: int) -> QueryResult:
        with self._session(timeout_seconds, streaming=True) as s:
            return run_query(s, sql, max_rows)

    def explain(self, sql: str) -> PlanSummary:
        with self._session(self.profile.guard.timeout_seconds) as s:
            cur = s.cursor
            cur.execute(f"EXPLAIN {sql}")
            names = [d[0].lower() for d in (cur.description or [])]
            rows = [r if isinstance(r, dict) else dict(zip(names, r)) for r in cur.fetchall()]
            return summarize_mariadb_explain(rows)

    def table_info(self, table: str, timeout_seconds: int | None = None) -> TableInfo:
        """Cột, index và TABLE_ROWS.

        Tìm bảng không phân biệt hoa thường (lower_case_table_names khác nhau giữa các
        server); nếu có nhiều bảng chỉ khác hoa thường thì ưu tiên bảng viết đúng y hệt.
        Sau đó tra cột/index theo đúng tên thật. Định danh chỉ đi qua bind parameter.
        """
        schema, name = parse_table_name(table)
        with self._session(timeout_seconds or self.profile.guard.timeout_seconds) as s:
            cur = s.cursor
            cur.execute("SELECT table_schema, table_name, table_rows FROM information_schema.tables "
                        "WHERE LOWER(table_schema) = LOWER(COALESCE(%s, DATABASE())) "
                        "AND LOWER(table_name) = LOWER(%s) ORDER BY table_schema, table_name",
                        (schema, name))
            found = cur.fetchall()
            if not found:
                raise TableNotFound(f"Không tìm thấy bảng '{table}'")
            exact = [r for r in found if r[1] == name and (schema is None or r[0] == schema)]
            real_schema, real_name, rows = (exact or found)[0]
            b = (real_schema, real_name)
            cur.execute("SELECT column_name, column_type, is_nullable "
                        "FROM information_schema.columns "
                        "WHERE table_schema = %s AND table_name = %s "
                        "ORDER BY ordinal_position", b)
            cols = [ColumnInfo(r[0], r[1], r[2] == "YES") for r in cur.fetchall()]
            if not cols:
                raise TableNotFound(f"Không tìm thấy bảng '{table}'")
            cur.execute("SELECT index_name, non_unique, column_name "
                        "FROM information_schema.statistics "
                        "WHERE table_schema = %s AND table_name = %s "
                        "ORDER BY index_name, seq_in_index", b)
            idx: dict[str, IndexInfo] = {}
            for iname, non_unique, col in cur.fetchall():
                idx.setdefault(iname, IndexInfo(iname, [], int(non_unique) == 0)).columns.append(col)
            est = int(rows) if rows is not None else None
            return TableInfo(cols, list(idx.values()), est)
