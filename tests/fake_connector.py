"""Connector giả (theo protocol `Connector`) để test QueryService và các tool MCP.

Khác với `fakes.py` (driver DB-API giả để test chính connector): lớp này đứng
thay cả connector, ghi lại SQL được gửi xuống để test kiểm tra.

`table_info` cố ý NGHIÊM NGẶT: không chuẩn hóa tên. Bảng chỉ được tìm thấy khi
chuỗi truyền vào khớp đúng (schema, tên) đã đăng ký, phân biệt hoa thường, giữ
nguyên dấu nháy. Nhờ vậy lỗi "tra thống kê bằng tên đã chuẩn hóa" không thể bị
fake che đi như trước.
"""
from __future__ import annotations

from typing import Callable

from mcp_database.db.base import PlanSummary, QueryResult, TableInfo, TableNotFound


def _key(table: str | tuple) -> tuple[str | None, str]:
    if isinstance(table, tuple):
        return table
    schema, dot, name = table.rpartition(".")
    return (schema if dot else None), name


class FakeConnector:
    def __init__(
        self,
        tables: dict | None = None,
        plan: PlanSummary | None = None,
        result: QueryResult | None = None,
    ):
        # Khóa: (schema hoặc None, tên) hoặc chuỗi "schema.tên" / "tên", đúng như sẽ được tra.
        self.tables: dict[tuple[str | None, str], TableInfo] = {
            _key(k): v for k, v in (tables or {}).items()}
        self.plan = plan or PlanSummary(cost=10, est_rows=10, full_scans=[], text="PLAN")
        self.result = result or QueryResult(columns=["X"], rows=[(1,)], truncated=False)
        self.table_info_calls: list[str] = []
        self.table_info_timeouts: list[int | None] = []
        self.explained_sql: list[str] = []
        self.executed_sql: list[str] = []
        self.execute_args: list[tuple[str, int, int]] = []
        # Đặt exception để mô phỏng lỗi ở từng bước.
        self.table_info_error: BaseException | None = None
        self.explain_error: BaseException | None = None
        self.execute_error: BaseException | None = None
        # Gọi trong execute trước khi trả kết quả (vd. chặn luồng để test đồng thời).
        self.on_execute: Callable[[], None] | None = None

    def register(self, table: str | tuple, info: TableInfo) -> None:
        self.tables[_key(table)] = info

    def table_info(self, table: str, timeout_seconds: int | None = None) -> TableInfo:
        self.table_info_calls.append(table)
        self.table_info_timeouts.append(timeout_seconds)
        if self.table_info_error is not None:
            raise self.table_info_error
        info = self.tables.get(_key(table))
        if info is None:
            raise TableNotFound(f"Không tìm thấy bảng {table}")
        return info

    def explain(self, sql: str) -> PlanSummary:
        self.explained_sql.append(sql)
        if self.explain_error is not None:
            raise self.explain_error
        return self.plan

    def execute(self, sql: str, max_rows: int, timeout_seconds: int) -> QueryResult:
        self.executed_sql.append(sql)
        self.execute_args.append((sql, max_rows, timeout_seconds))
        if self.on_execute is not None:
            self.on_execute()
        if self.execute_error is not None:
            raise self.execute_error
        # Như fetch_limited của connector thật: lấy tối đa max_rows, còn dư thì truncated.
        r = self.result
        if len(r.rows) > max_rows:
            return QueryResult(columns=list(r.columns), rows=list(r.rows[:max_rows]), truncated=True)
        return r
