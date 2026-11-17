"""Connector giả (theo protocol `Connector`) để test QueryService và các tool MCP.

Khác với `fakes.py` (driver DB-API giả để test chính connector): lớp này đứng
thay cả connector, ghi lại SQL được gửi xuống để test kiểm tra.
"""
from __future__ import annotations

from typing import Callable

from mcp_database.db.base import PlanSummary, QueryResult, TableInfo, TableNotFound
from mcp_database.names import norm


class FakeConnector:
    def __init__(
        self,
        tables: dict[str, TableInfo] | None = None,
        plan: PlanSummary | None = None,
        result: QueryResult | None = None,
    ):
        # Tên bảng được chuẩn hóa bằng `norm`; bảng không có trong đây -> TableNotFound.
        self.tables = {norm(k): v for k, v in (tables or {}).items()}
        self.plan = plan or PlanSummary(cost=10, est_rows=10, full_scans=[], text="PLAN")
        self.result = result or QueryResult(columns=["X"], rows=[(1,)], truncated=False)
        self.table_info_calls: list[str] = []
        self.explained_sql: list[str] = []
        self.executed_sql: list[str] = []
        self.execute_args: list[tuple[str, int, int]] = []
        # Đặt exception để mô phỏng lỗi ở từng bước.
        self.table_info_error: BaseException | None = None
        self.explain_error: BaseException | None = None
        self.execute_error: BaseException | None = None
        # Gọi trong execute trước khi trả kết quả (vd. chặn luồng để test đồng thời).
        self.on_execute: Callable[[], None] | None = None

    def table_info(self, table: str) -> TableInfo:
        self.table_info_calls.append(table)
        if self.table_info_error is not None:
            raise self.table_info_error
        info = self.tables.get(norm(table))
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
        return self.result
