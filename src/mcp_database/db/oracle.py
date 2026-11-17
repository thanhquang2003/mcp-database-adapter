"""Connector Oracle (python-oracledb, thin mode).

Lưu ý: SET TRANSACTION READ ONLY chỉ là lớp dự phòng. COMMIT/DDL bên trong PL/SQL có thể
kết thúc giao dịch chỉ-đọc, nên tài khoản DB dùng cho server PHẢI chỉ-đọc (không DDL, không
EXECUTE trên package). Lớp này từ chối mọi câu lệnh không trả về tập kết quả.
"""
from __future__ import annotations

from dataclasses import replace
from uuid import uuid4

import oracledb

from ..config import SystemProfile
from ..names import norm
from .base import (ColumnInfo, IndexInfo, PlanSummary, QueryResult, TableInfo,
                   TableNotFound, get_credentials, parse_table_ref, run_query, session)

# DPY-4024: call_timeout của driver; ORA-01013 / ORA-03156: bị ngắt do hết thời gian.
TIMEOUT_CODES = frozenset({"DPY-4024", "ORA-01013", "ORA-03156"})

CONNECT_TIMEOUT_SECONDS = 10

NLS_SQL = ("ALTER SESSION SET NLS_DATE_FORMAT = 'YYYY-MM-DD HH24:MI:SS' "
           "NLS_TIMESTAMP_FORMAT = 'YYYY-MM-DD HH24:MI:SS.FF' "
           "NLS_TIMESTAMP_TZ_FORMAT = 'YYYY-MM-DD HH24:MI:SS.FF TZH:TZM'")


def _is_full_scan(op: str, options: str) -> bool:
    op, options = op.upper(), options.upper()
    if op.startswith(("TABLE ACCESS", "MAT_VIEW ACCESS", "MAT_VIEW REWRITE ACCESS")):
        return "FULL" in options
    if op in ("INDEX", "BITMAP INDEX"):
        # FULL SCAN / FAST FULL SCAN / FULL SCAN DESCENDING đọc toàn bộ index;
        # biến thể (MIN/MAX) chỉ đọc một đầu index nên bỏ qua.
        return "FULL SCAN" in options and "MIN/MAX" not in options
    return False


def summarize_oracle_plan(rows: list[tuple]) -> PlanSummary:
    """rows: (operation, options, object_name, cost, cardinality) theo thứ tự id.

    cost = cost của dòng gốc (đã cộng dồn). est_rows = cardinality LỚN NHẤT trong mọi
    dòng: gốc của một COUNT(*) chỉ có 1 dòng dù bên dưới đọc hàng chục triệu dòng.
    Với dòng INDEX, object_name phải là tên bảng của index (explain() quy đổi sẵn).
    """
    cost = None
    if rows and rows[0][3] is not None:
        cost = int(rows[0][3])
    cards = [int(r[4]) for r in rows if r[4] is not None]
    est = max(cards) if cards else None
    full: list[str] = []
    for op, options, obj, _c, _k in rows:
        if obj and _is_full_scan(op or "", options or ""):
            name = norm(obj)
            if name not in full:
                full.append(name)
    return PlanSummary(cost=cost, est_rows=est, full_scans=full, text="")


class OracleConnector:
    def __init__(self, profile: SystemProfile):
        self.profile = profile

    def _connect(self):
        user, password = get_credentials(self.profile)
        p = self.profile
        return oracledb.connect(user=user, password=password,
                                dsn=f"{p.host}:{p.port}/{p.database}",
                                tcp_connect_timeout=CONNECT_TIMEOUT_SECONDS)

    def _session(self, timeout_seconds: int, read_only: bool = True):
        def setup(conn):
            conn.call_timeout = timeout_seconds * 1000
            cur = conn.cursor()
            cur.execute(NLS_SQL)
            if read_only:
                cur.execute("SET TRANSACTION READ ONLY")
            return cur
        return session(self._connect, setup, TIMEOUT_CODES, timeout_seconds)

    def execute(self, sql: str, max_rows: int, timeout_seconds: int) -> QueryResult:
        with self._session(timeout_seconds) as s:
            # fetchmany(max_rows + 1) trong một lượt round trip (call_timeout tính theo
            # từng round trip); prefetchrows phải đặt trước khi execute.
            s.cursor.arraysize = max_rows + 1
            s.cursor.prefetchrows = max_rows + 1
            return run_query(s, sql, max_rows)

    def explain(self, sql: str) -> PlanSummary:
        # EXPLAIN PLAN ghi vào PLAN_TABLE nên không đặt được READ ONLY; câu SQL không được chạy.
        # Dữ liệu plan chưa commit và bị rollback khi đóng kết nối nên không cần DELETE.
        sid = uuid4().hex[:30]  # chỉ ký tự hex, an toàn để nhúng literal
        with self._session(self.profile.guard.timeout_seconds, read_only=False) as s:
            cur = s.cursor
            cur.execute(f"EXPLAIN PLAN SET STATEMENT_ID = '{sid}' FOR {sql}")
            # Dòng INDEX ... FULL SCAN mang tên index: quy về tên bảng qua all_indexes để
            # so được với danh sách bảng lớn.
            cur.execute("SELECT p.operation, p.options, NVL(i.table_name, p.object_name), "
                        "p.cost, p.cardinality "
                        "FROM plan_table p LEFT JOIN all_indexes i "
                        "ON p.object_type LIKE 'INDEX%' AND i.owner = p.object_owner "
                        "AND i.index_name = p.object_name "
                        "WHERE p.statement_id = :id ORDER BY p.id", {"id": sid})
            plan = summarize_oracle_plan(cur.fetchall())
            # BASIC +ROWS +COST: có số dòng/chi phí nhưng KHÔNG có phần Predicate
            # Information (chứa literal trong WHERE, có thể là giá trị nhạy cảm).
            cur.execute("SELECT plan_table_output FROM TABLE("
                        "DBMS_XPLAN.DISPLAY('PLAN_TABLE', :id, 'BASIC +ROWS +COST'))", {"id": sid})
            text = "\n".join(str(r[0]) for r in cur.fetchall())
            return replace(plan, text=text)

    def table_info(self, table: str, timeout_seconds: int | None = None) -> TableInfo:
        """Cột, index và NUM_ROWS của bảng/view.

        Định danh như Oracle: không nháy -> viết hoa; có nháy kép -> giữ đúng hoa thường.
        Không có schema thì tra trong CURRENT_SCHEMA. Không thấy thì đi qua synonym một cấp
        (synonym riêng của owner trước, rồi PUBLIC nếu không ghi schema; bỏ qua db_link).
        View không có NUM_ROWS -> estimated_rows None.
        """
        schema_part, (name, name_quoted) = parse_table_ref(table)
        t = name if name_quoted else name.upper()
        with self._session(timeout_seconds or self.profile.guard.timeout_seconds) as s:
            cur = s.cursor
            if schema_part is not None:
                owner = schema_part[0] if schema_part[1] else schema_part[0].upper()
            else:
                cur.execute("SELECT SYS_CONTEXT('USERENV', 'CURRENT_SCHEMA') FROM dual")
                owner = cur.fetchone()[0]
            cols = self._columns(cur, owner, t)
            if not cols:
                cur.execute("SELECT table_owner, table_name FROM all_synonyms "
                            "WHERE synonym_name = :t AND db_link IS NULL "
                            "AND (owner = :owner OR (:pub = 1 AND owner = 'PUBLIC')) "
                            "ORDER BY CASE WHEN owner = 'PUBLIC' THEN 1 ELSE 0 END",
                            {"owner": owner, "t": t, "pub": 0 if schema_part is not None else 1})
                target = cur.fetchone()
                if target:
                    owner, t = target[0], target[1]
                    cols = self._columns(cur, owner, t)
            if not cols:
                raise TableNotFound(f"Không tìm thấy bảng '{table}'")
            b = {"owner": owner, "t": t}
            cur.execute("SELECT i.index_name, i.uniqueness, c.column_name "
                        "FROM all_indexes i JOIN all_ind_columns c "
                        "ON c.index_owner = i.owner AND c.index_name = i.index_name "
                        "WHERE i.table_owner = :owner AND i.table_name = :t "
                        "ORDER BY i.index_name, c.column_position", b)
            idx: dict[str, IndexInfo] = {}
            for iname, uniq, col in cur.fetchall():
                idx.setdefault(iname, IndexInfo(iname, [], uniq == "UNIQUE")).columns.append(col)
            cur.execute("SELECT num_rows FROM all_tables "
                        "WHERE owner = :owner AND table_name = :t", b)
            row = cur.fetchone()
            est = int(row[0]) if row and row[0] is not None else None
            return TableInfo(cols, list(idx.values()), est)

    @staticmethod
    def _columns(cur, owner: str, t: str) -> list[ColumnInfo]:
        cur.execute("SELECT column_name, data_type, nullable FROM all_tab_columns "
                    "WHERE owner = :owner AND table_name = :t ORDER BY column_id",
                    {"owner": owner, "t": t})
        return [ColumnInfo(r[0], r[1], r[2] == "Y") for r in cur.fetchall()]
