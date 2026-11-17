"""MCP server (stdio): đưa `QueryService` ra thành các tool cho Claude.

Kết quả tool đi thẳng vào model nên đây là ranh giới bảo mật:
- Tool không bao giờ ném ngoại lệ ra tầng MCP; lỗi được trả thành
  `{"error": ..., "hint": ...}` với thông điệp đã kiểm soát (lý do của cổng
  kiểm tra, `str()` của các lớp lỗi do dự án tự tạo, hoặc chuỗi cố định).
- Không trả host, port, user, tên biến môi trường hay đường dẫn trên máy chủ.
- stdout là kênh giao thức MCP: mọi chẩn đoán đi ra stderr.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from datetime import date
from functools import wraps
from pathlib import Path
from typing import Any, Awaitable, Callable

from mcp.server.mcpserver import MCPServer

from .config import Config, ConfigError, SystemProfile, load_config
from .db.base import DatabaseError, MissingCredentialError, QueryTimeout, TableNotFound
from .guard.parser import QueryRejected as GuardRejected
from .names import norm
from .service import GENERIC_ERROR, QueryService, RunResult, UnknownSystem

CONFIG_ENV = "MCP_DATABASE_CONFIG"
HMAC_ENV = "MCP_DATABASE_HMAC_KEY"
DEFAULT_CONFIG = "./systems.yaml"
MIN_HMAC_BYTES = 16
MAX_DOC_CHARS = 200_000
TOP_N_MIN, TOP_N_MAX = 1, 50

_TABLE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#]*(\.[A-Za-z_][A-Za-z0-9_$#]*)?$")
_COLUMN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_$#]*$")
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")

logger = logging.getLogger(__name__)

INSTRUCTIONS = """\
Truy vấn chỉ-đọc vào replica Oracle/MariaDB của các hệ thống nội bộ.
Quy trình: list_systems -> get_data_doc (đọc kỹ trước khi viết SQL) -> describe_table -> \
explain_query -> run_query. Luôn lọc theo cột thời gian (bảng lớn bắt buộc, có giới hạn số ngày), \
ưu tiên truy vấn tổng hợp (GROUP BY, COUNT) thay vì lấy dòng chi tiết. Cột nhạy cảm bị băm/che, \
kết quả bị giới hạn số dòng. Query bị từ chối sẽ trả về lý do và hint: làm theo hint rồi thử lại."""

_HINT_SYSTEMS = "Gọi list_systems để xem tên hệ thống hợp lệ."
_HINT_TABLE = "Kiểm tra tên bảng trong get_data_doc; dùng dạng 'bảng' hoặc 'schema.bảng'."
_HINT_TIMEOUT = ("Thu hẹp khoảng thời gian, lọc theo cột có chỉ mục, hoặc dùng truy vấn tổng hợp "
                 "(GROUP BY, COUNT).")
_HINT_DB = "Kiểm tra lại cú pháp, tên bảng/cột theo get_data_doc và describe_table."
_HINT_CREDS = "Báo người quản trị cấu hình biến môi trường kết nối cho hệ thống này."
_HINT_GUARD = "Sửa truy vấn theo lý do ở trên rồi thử lại."
_HINT_GENERIC = "Thử lại; nếu vẫn lỗi, báo người quản trị kiểm tra log của server."


# --- Chuyển lỗi thành payload ----------------------------------------------------


def _error(message: str, hint: str) -> dict:
    return {"error": message, "hint": hint}


def _error_payload(exc: Exception) -> dict:
    """Chỉ dùng thông điệp do dự án tự tạo; mọi thứ khác thành thông điệp cố định."""
    if isinstance(exc, GuardRejected):
        return _error(exc.reason, exc.hint or _HINT_GUARD)
    kind = type(exc)
    if kind is UnknownSystem:
        return _error(str(exc), _HINT_SYSTEMS)
    if kind is TableNotFound:
        return _error(str(exc), _HINT_TABLE)
    if kind is QueryTimeout:
        return _error(str(exc), _HINT_TIMEOUT)
    if kind is DatabaseError:
        return _error(str(exc), _HINT_DB)
    if kind is MissingCredentialError:
        return _error(str(exc), _HINT_CREDS)
    return _error(GENERIC_ERROR, _HINT_GENERIC)


def _safe(fn: Callable[..., Awaitable[Any]], on_error: Callable[[dict], Any] = lambda p: p):
    """Không để ngoại lệ nào lọt ra tầng MCP (tầng đó sẽ gửi nguyên `str(exc)` cho model)."""

    @wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - ranh giới bảo mật
            known = isinstance(exc, GuardRejected) or type(exc) in (
                UnknownSystem, TableNotFound, QueryTimeout, DatabaseError, MissingCredentialError)
            if not known:
                # Chỉ ghi tên lớp lỗi: nội dung có thể chứa dữ liệu hoặc lỗi gốc của driver.
                logger.warning("Tool %s gặp lỗi không mong đợi: %s", fn.__name__, type(exc).__name__)
            return on_error(_error_payload(exc))

    return wrapper


def _doc_error(payload: dict) -> str:
    return f"LỖI: {payload['error']} {payload['hint']}"


# --- Tiện ích --------------------------------------------------------------------


def _run_payload(result: RunResult) -> dict:
    return {
        "columns": list(result.columns),
        "rows": [list(r) for r in result.rows],
        "row_count": result.row_count,
        "truncated": result.truncated,
        "elapsed_ms": result.elapsed_ms,
        "notes": list(result.notes),
    }


def _parse_day(value: str | None, field: str) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _DATE_RE.fullmatch(value):
        raise ValueError(field)
    return date.fromisoformat(value)  # ValueError nếu ngày không tồn tại


def _quote(name: str, db_type: str) -> str:
    """Chỉ gọi với tên đã khớp regex định danh (không chứa ký tự nháy)."""
    return f'"{name}"' if db_type == "oracle" else f"`{name}`"


def _quote_table(table: str, db_type: str) -> str:
    # Oracle: connector tra bảng theo tên viết hoa (như định danh không nháy).
    parts = table.split(".")
    if db_type == "oracle":
        parts = [p.upper() for p in parts]
    return ".".join(_quote(p, db_type) for p in parts)


def _find_column(columns, wanted: str) -> str | None:
    """Tên thật của cột (so khớp không phân biệt hoa thường), chỉ nhận tên ASCII hợp lệ."""
    for col in columns:
        if _COLUMN_RE.fullmatch(col.name) and col.name.upper() == wanted.upper():
            return col.name
    return None


def _date_literal(day: date, db_type: str) -> str:
    return f"DATE '{day.isoformat()}'" if db_type == "oracle" else f"'{day.isoformat()}'"


# --- Server ----------------------------------------------------------------------


def build_server(service: QueryService, config: Config) -> MCPServer:
    mcp = MCPServer("mcp-database", instructions=INSTRUCTIONS)

    def profile_of(system: str) -> SystemProfile:
        profile = config.systems.get(system)
        if profile is None:
            known = ", ".join(sorted(config.systems))
            raise UnknownSystem(f"Không có hệ thống '{system}'. Các hệ thống hiện có: {known}")
        return profile

    @mcp.tool()
    async def list_systems() -> list[dict]:
        """Liệt kê các hệ thống (database) có thể truy vấn: tên và loại DB (oracle/mariadb).

        Bước tiếp theo: gọi get_data_doc(system) để đọc tài liệu dữ liệu trước khi viết SQL."""
        return [{"name": p.name, "db_type": p.db_type} for p in config.systems.values()]

    async def _get_data_doc(system: str) -> str:
        profile = profile_of(system)

        def read() -> str:
            with open(profile.data_doc, encoding="utf-8") as f:
                return f.read(MAX_DOC_CHARS + 1)

        try:
            text = await asyncio.to_thread(read)
        except (OSError, UnicodeError):
            return _doc_error(_error(
                f"Không đọc được tài liệu dữ liệu '{profile.data_doc.name}' của hệ thống '{system}'.",
                "Báo người quản trị kiểm tra file data_doc trong cấu hình; trong lúc chờ có thể dùng "
                "describe_table để xem cấu trúc bảng."))
        if len(text) > MAX_DOC_CHARS:
            text = (text[:MAX_DOC_CHARS]
                    + f"\n\n[Tài liệu đã bị cắt ở {MAX_DOC_CHARS} ký tự; phần sau không được hiển thị.]")
        return text

    @mcp.tool()
    async def get_data_doc(system: str) -> str:
        """Đọc tài liệu dữ liệu của hệ thống: ý nghĩa bảng/cột, cột thời gian để lọc, bảng lớn,
        cột nhạy cảm (bị băm/che) và ví dụ truy vấn. LUÔN gọi tool này trước khi viết SQL
        cho một hệ thống."""
        return await _safe(_get_data_doc, _doc_error)(system)

    @mcp.tool()
    @_safe
    async def describe_table(system: str, table: str) -> dict:
        """Xem cấu trúc bảng ('bảng' hoặc 'schema.bảng'): cột (tên, kiểu, nullable, sensitive =
        chế độ che nếu là cột nhạy cảm), chỉ mục và số dòng ước tính. Bảng lớn bắt buộc lọc theo
        cột thời gian; nên lọc theo cột có chỉ mục. Lỗi trả về {"error", "hint"}."""
        profile = profile_of(system)
        info = await asyncio.to_thread(service.table_info, system, table)
        tname = norm(table)
        sensitive = profile.guard.sensitive_columns
        return {
            "columns": [{"name": c.name, "type": c.type, "nullable": c.nullable,
                         "sensitive": sensitive.get(f"{tname}.{norm(c.name)}")} for c in info.columns],
            "indexes": [{"name": i.name, "columns": list(i.columns), "unique": i.unique}
                        for i in info.indexes],
            "estimated_rows": info.estimated_rows,
        }

    @mcp.tool()
    @_safe
    async def explain_query(system: str, sql: str) -> dict:
        """Kiểm tra một câu SELECT và xem kế hoạch thực thi (EXPLAIN) mà KHÔNG chạy nó.

        Đọc get_data_doc trước. Quy tắc giống run_query: chỉ SELECT; luôn lọc theo cột thời gian
        (bảng lớn bắt buộc có cận dưới và cận trên, trong giới hạn số ngày); ưu tiên truy vấn tổng
        hợp (GROUP BY, COUNT); cột nhạy cảm sẽ bị băm/che; kết quả bị giới hạn số dòng.
        Trả về {"allowed", "reasons", "hints", "plan", "cost", "estimated_rows"}; nếu
        allowed = false thì sửa query theo hint rồi thử lại."""
        check = await asyncio.to_thread(service.check, system, sql)
        plan = check.plan
        return {
            "allowed": check.allowed,
            "reasons": list(check.reasons),
            "hints": list(check.hints),
            "plan": plan.text if plan else None,
            "cost": plan.cost if plan else None,
            "estimated_rows": plan.est_rows if plan else None,
        }

    @mcp.tool()
    @_safe
    async def run_query(system: str, sql: str) -> dict:
        """Chạy một câu SELECT chỉ-đọc trên replica.

        Đọc get_data_doc trước. Luôn lọc theo cột thời gian (bảng lớn bắt buộc có cận dưới và
        cận trên, trong giới hạn số ngày). Ưu tiên truy vấn tổng hợp (GROUP BY, COUNT) thay vì lấy
        dòng chi tiết. Cột nhạy cảm sẽ bị băm/che trong kết quả (vẫn lọc/nhóm theo được). Kết quả
        bị giới hạn số dòng (truncated = true khi bị cắt) và có thời gian chờ tối đa.
        Thành công: {"columns", "rows", "row_count", "truncated", "elapsed_ms", "notes"}.
        Bị từ chối/timeout/lỗi: {"error", "hint"}; làm theo hint rồi thử lại."""
        result = await asyncio.to_thread(service.run, system, sql)
        return _run_payload(result)

    @mcp.tool()
    @_safe
    async def profile_column(system: str, table: str, column: str, time_from: str | None = None,
                             time_to: str | None = None, top_n: int = 10) -> dict:
        """Thống kê nhanh một cột: tổng số dòng, số giá trị khác NULL, số giá trị phân biệt và
        top_n giá trị phổ biến nhất (top_n trong 1..50). Server tự dựng truy vấn tổng hợp và chạy
        qua cùng các lớp kiểm tra như run_query. Đọc get_data_doc trước.

        time_from / time_to dạng YYYY-MM-DD, lọc trên cột thời gian đã khai báo của bảng
        (time_from <= t < time_to, time_to không bao gồm); bảng lớn bắt buộc có cả hai và
        khoảng không vượt giới hạn số ngày. Không dùng được với cột nhạy cảm.
        Lỗi trả về {"error", "hint"}; làm theo hint rồi thử lại."""
        if not isinstance(table, str) or not _TABLE_RE.fullmatch(table):
            return _error("Tên bảng không hợp lệ.", "Dùng dạng 'bảng' hoặc 'schema.bảng' (chữ, số, _, $, #).")
        if not isinstance(column, str) or not _COLUMN_RE.fullmatch(column):
            return _error("Tên cột không hợp lệ.",
                          "Chỉ truyền đúng một tên cột (chữ, số, _, $, #), không kèm tên bảng hay biểu thức.")
        try:
            day_from = _parse_day(time_from, "time_from")
            day_to = _parse_day(time_to, "time_to")
        except ValueError:
            return _error("time_from/time_to phải là ngày hợp lệ dạng YYYY-MM-DD.",
                          "Ví dụ: time_from='2026-11-01', time_to='2026-11-08'.")
        if day_from and day_to and day_from >= day_to:
            return _error("time_from phải nhỏ hơn time_to.", "time_to không bao gồm: dùng ngày sau ngày cuối cần xem.")
        top_n = max(TOP_N_MIN, min(TOP_N_MAX, top_n))

        profile = profile_of(system)
        db_type = profile.db_type
        tname = norm(table)
        if f"{tname}.{norm(column)}" in profile.guard.sensitive_columns:
            return _error(f"Cột {tname}.{norm(column)} là cột nhạy cảm nên không được thống kê giá trị.",
                          "Chọn cột khác, hoặc dùng run_query với COUNT/GROUP BY theo cột không nhạy cảm.")
        table_guard = profile.guard.tables.get(tname)
        if (day_from or day_to) and table_guard is None:
            return _error(f"Bảng {tname} chưa được khai báo cột thời gian nên không lọc theo thời gian được.",
                          "Bỏ time_from/time_to, hoặc dùng run_query với điều kiện WHERE tự viết.")

        info = await asyncio.to_thread(service.table_info, system, table)
        col = _find_column(info.columns, column)
        if col is None:
            return _error(f"Bảng {tname} không có cột '{column}'.", "Dùng describe_table để xem danh sách cột.")

        conds = []
        if day_from or day_to:
            time_col = _find_column(info.columns, table_guard.time_column)
            if time_col is None:
                return _error(f"Không tìm thấy cột thời gian đã khai báo của bảng {tname}.",
                              "Báo người quản trị kiểm tra guard.tables trong cấu hình.")
            qt = _quote(time_col, db_type)
            if day_from:
                conds.append(f"{qt} >= {_date_literal(day_from, db_type)}")
            if day_to:
                conds.append(f"{qt} < {_date_literal(day_to, db_type)}")
        where = f" WHERE {' AND '.join(conds)}" if conds else ""
        qtable, qcol = _quote_table(table, db_type), _quote(col, db_type)
        limit = f"FETCH FIRST {top_n} ROWS ONLY" if db_type == "oracle" else f"LIMIT {top_n}"
        stats_sql = (f"SELECT COUNT(*) AS TOTAL_ROWS, COUNT({qcol}) AS NON_NULL, "
                     f"COUNT(DISTINCT {qcol}) AS DISTINCT_VALUES FROM {qtable}{where}")
        top_sql = (f"SELECT {qcol} AS VALUE, COUNT(*) AS CNT FROM {qtable}{where} "
                   f"GROUP BY {qcol} ORDER BY COUNT(*) DESC {limit}")

        stats = await asyncio.to_thread(service.run, system, stats_sql)
        top = await asyncio.to_thread(service.run, system, top_sql)
        total, non_null, distinct = stats.rows[0] if stats.rows else (None, None, None)
        null_count = total - non_null if isinstance(total, int) and isinstance(non_null, int) else None
        return {
            "table": table,
            "column": col,
            "total_rows": total,
            "non_null": non_null,
            "null_count": null_count,
            "distinct_count": distinct,
            "top_values": [{"value": r[0], "count": r[1]} for r in top.rows],
            "notes": list(dict.fromkeys(stats.notes + top.notes)),
        }

    return mcp


# --- Khởi chạy -------------------------------------------------------------------


def _fail(message: str) -> None:
    print(message, file=sys.stderr)
    sys.exit(1)


def _check_env(config: Config) -> bytes | None:
    """Kiểm tra biến môi trường; chỉ in TÊN biến, không bao giờ in giá trị."""
    missing: list[str] = []
    for profile in config.systems.values():
        for var in (profile.user_env, profile.password_env):
            if not os.environ.get(var) and var not in missing:
                missing.append(var)
    raw_key = os.environ.get(HMAC_ENV)
    key = raw_key.encode("utf-8") if raw_key else None
    needs_key = any(p.guard.sensitive_columns for p in config.systems.values())
    if needs_key and key is None:
        missing.append(HMAC_ENV)
    if missing:
        _fail("Thiếu biến môi trường (cần đặt trước khi chạy server): " + ", ".join(missing))
    if needs_key and len(key) < MIN_HMAC_BYTES:
        _fail(f"Biến môi trường {HMAC_ENV} phải dài ít nhất {MIN_HMAC_BYTES} byte.")
    return key


def main() -> None:
    path = Path(os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG)
    try:
        config = load_config(path)
    except ConfigError as exc:
        _fail(str(exc))
    except Exception:  # noqa: BLE001 - vd. file không phải UTF-8
        _fail(f"Không đọc được file cấu hình {path}.")
    hmac_key = _check_env(config)
    service = QueryService(config, hmac_key=hmac_key)
    build_server(service, config).run(transport="stdio")
