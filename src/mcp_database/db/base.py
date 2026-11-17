"""Kiểu dữ liệu và tiện ích dùng chung cho các connector."""
from __future__ import annotations

import math
import os
import re
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Callable, Iterator, Protocol

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

    Chỉ `str(e)` được phép đưa cho model. Tuyệt đối không đưa traceback, văn bản của
    `__cause__` hay đầu ra của `logging.exception` (chúng chứa lỗi gốc của driver, có
    thể làm lộ giá trị đã che). Lỗi gốc ở `__cause__` chỉ dành cho audit log nội bộ.
    """

    def __init__(self, code: str | int | None = None):
        self.code = code
        suffix = f" (mã {code})" if code is not None else ""
        super().__init__(f"Lỗi từ cơ sở dữ liệu{suffix}")


class QueryRejected(DatabaseError):
    """Câu lệnh không phải truy vấn trả về tập kết quả."""

    def __init__(self) -> None:
        Exception.__init__(self, "Câu lệnh không phải truy vấn đọc dữ liệu nên bị từ chối")
        self.code = None


class QueryTimeout(TimeoutError):
    """Timeout do chính lớp này phát ra, thông điệp cố định."""

    def __init__(self, seconds: int):
        super().__init__(f"query vượt {seconds} giây")


class TableNotFound(LookupError):
    """Bảng không tồn tại / tên không hợp lệ (thông điệp do lớp này tạo)."""


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
        raise TableNotFound("Tên bảng không hợp lệ: chỉ nhận 'bảng' hoặc 'schema.bảng'")
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
    """Chỉ cho qua exception do chính lớp này tạo; mọi thứ khác bị bọc thành DatabaseError."""
    try:
        yield
    except (DatabaseError, TableNotFound, QueryTimeout, MissingCredentialError):
        raise
    except Exception as exc:
        code = _error_code(exc)
        if code in timeout_codes:
            raise QueryTimeout(timeout_seconds) from exc
        raise DatabaseError(code) from exc


def _normalize_lob(lob: Any) -> object:
    """Đọc có giới hạn: BLOB chỉ lấy kích thước, CLOB đọc tối đa MAX_CELL_CHARS + 1 ký tự."""
    type_name = getattr(getattr(lob, "type", None), "name", "")
    if type_name == "DB_TYPE_BLOB" and hasattr(lob, "size"):
        return f"<binary {lob.size()} bytes>"
    data = lob.read(1, MAX_CELL_CHARS + 1)
    return normalize_cell(data)


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
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if hasattr(value, "read"):
        return _normalize_lob(value)
    if isinstance(value, str) and len(value) > MAX_CELL_CHARS:
        return value[:MAX_CELL_CHARS] + "…[cắt]"
    return value


def close_quietly(conn, rollback: bool = True) -> None:
    """Rollback (không bao giờ commit) rồi đóng; lỗi lúc dọn dẹp không che lỗi gốc.

    rollback=False khi còn kết quả streaming chưa đọc hết: đóng thẳng kết nối (server tự
    rollback) để driver không phải đọc nốt toàn bộ tập kết quả.
    """
    if rollback:
        try:
            conn.rollback()
        except Exception:
            pass
    try:
        conn.close()
    except Exception:
        pass


@dataclass
class Session:
    conn: Any
    cursor: Any
    abandon_result: bool = False


@contextmanager
def session(connect: Callable[[], Any], setup: Callable[[Any], Any],
            timeout_codes: frozenset, timeout_seconds: int) -> Iterator[Session]:
    """connect -> setup (trả cursor) -> yield -> rollback -> close, kể cả khi setup lỗi."""
    if timeout_seconds <= 0:
        raise ValueError("timeout_seconds phải > 0")
    conn = None
    sess: Session | None = None
    try:
        with sanitize_errors(timeout_codes, timeout_seconds):
            conn = connect()
            sess = Session(conn, setup(conn))
            yield sess
    finally:
        if conn is not None:
            abandon = bool(sess and sess.abandon_result)
            close_quietly(conn, rollback=not abandon)
            if abandon:
                # Tách cursor khỏi kết nối đã đóng để __del__ của cursor không cố đọc nốt kết quả.
                # (PyMySQL: MySQLResult.__del__ cũng đọc nốt nếu unbuffered_active còn bật.)
                try:
                    sess.cursor.connection = None
                    result = getattr(sess.cursor, "_result", None)
                    if result is not None:
                        result.unbuffered_active = False
                except Exception:
                    pass


def fetch_limited(cursor, max_rows: int) -> QueryResult:
    columns = [d[0] for d in (cursor.description or [])]
    rows = cursor.fetchmany(max_rows + 1)
    truncated = len(rows) > max_rows
    rows = rows[:max_rows]
    return QueryResult(columns, [tuple(normalize_cell(c) for c in r) for r in rows], truncated)


def run_query(sess: Session, sql: str, max_rows: int, abandon_on_truncate: bool = False) -> QueryResult:
    sess.cursor.execute(sql)
    if sess.cursor.description is None:
        raise QueryRejected()
    result = fetch_limited(sess.cursor, max_rows)
    if result.truncated and abandon_on_truncate:
        sess.abandon_result = True
    return result
