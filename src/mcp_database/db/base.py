"""Kiểu dữ liệu và tiện ích dùng chung cho các connector."""
from __future__ import annotations

import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Iterator, Protocol

from ..config import SystemProfile

MAX_CELL_CHARS = 2000
_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#]*(\.[A-Za-z_][A-Za-z0-9_$#]*)?$")


@dataclass
class ColumnInfo:
    name: str
    type: str
    nullable: bool


@dataclass
class IndexInfo:
    name: str
    columns: list[str]
    unique: bool


@dataclass
class TableInfo:
    columns: list[ColumnInfo]
    indexes: list[IndexInfo]
    estimated_rows: int | None


@dataclass
class PlanSummary:
    cost: int | None
    est_rows: int | None
    full_scans: list[str]
    text: str


@dataclass
class QueryResult:
    columns: list[str]
    rows: list[tuple]
    truncated: bool


class Connector(Protocol):
    def table_info(self, table: str) -> TableInfo: ...
    def explain(self, sql: str) -> PlanSummary: ...
    def execute(self, sql: str, max_rows: int, timeout_seconds: int) -> QueryResult: ...


class DatabaseError(Exception):
    """Lỗi từ DB đã được làm sạch: chỉ có mã lỗi, không có nội dung của driver.

    Lỗi gốc nằm ở __cause__ (chỉ dành cho audit log, không đưa cho model).
    """

    def __init__(self, code: str | int | None = None):
        self.code = code
        suffix = f" (mã {code})" if code is not None else ""
        super().__init__(f"Lỗi từ cơ sở dữ liệu{suffix}")


class MissingCredentialError(Exception):
    pass


def get_credentials(profile: SystemProfile) -> tuple[str, str]:
    values = []
    for var in (profile.user_env, profile.password_env):
        val = os.environ.get(var)
        if not val:
            raise MissingCredentialError(
                f"Hệ thống '{profile.name}': thiếu biến môi trường '{var}' "
                "(cần đặt trước khi chạy server)")
        values.append(val)
    return values[0], values[1]


def parse_table_name(table: str) -> tuple[str | None, str]:
    if not isinstance(table, str) or not _TABLE_RE.match(table):
        raise LookupError(f"Tên bảng không hợp lệ: chỉ nhận 'bảng' hoặc 'schema.bảng'")
    if "." in table:
        schema, name = table.split(".")
        return schema, name
    return None, table


def _error_code(exc: BaseException) -> str | int | None:
    if not exc.args:
        return None
    first = exc.args[0]
    if isinstance(first, int) and not isinstance(first, bool):
        return first
    full = getattr(first, "full_code", None)
    if isinstance(full, str) and re.fullmatch(r"[A-Z]{2,4}-\d{1,6}", full):
        return full
    return None


@contextmanager
def sanitize_errors(timeout_codes: frozenset, timeout_seconds: int) -> Iterator[None]:
    """Đổi mọi lỗi driver thành TimeoutError / DatabaseError không chứa nội dung driver."""
    try:
        yield
    except (DatabaseError, LookupError, TimeoutError, MissingCredentialError):
        raise
    except Exception as exc:
        code = _error_code(exc)
        if code in timeout_codes:
            raise TimeoutError(f"query vượt {timeout_seconds} giây") from exc
        raise DatabaseError(code) from exc


def normalize_cell(value: object) -> object:
    if value is None:
        return None
    if isinstance(value, Decimal):
        if not value.is_finite():
            return str(value)
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return f"<binary {len(value)} bytes>"
    if hasattr(value, "read"):
        return normalize_cell(value.read())
    if isinstance(value, str) and len(value) > MAX_CELL_CHARS:
        return value[:MAX_CELL_CHARS] + "…[cắt]"
    return value


def close_quietly(conn) -> None:
    """Rollback (không bao giờ commit) rồi đóng; lỗi lúc dọn dẹp không che lỗi gốc."""
    try:
        conn.rollback()
    except Exception:
        pass
    try:
        conn.close()
    except Exception:
        pass


def fetch_limited(cursor, max_rows: int) -> QueryResult:
    columns = [d[0] for d in (cursor.description or [])]
    rows = cursor.fetchmany(max_rows + 1)
    truncated = len(rows) > max_rows
    rows = rows[:max_rows]
    return QueryResult(columns, [tuple(normalize_cell(c) for c in r) for r in rows], truncated)
