"""Pipeline chạy query: nối các lớp bảo vệ, giới hạn đồng thời và audit log.

Thứ tự (dừng ngay khi một bước từ chối):
    giới hạn độ dài -> parse_select -> plan_masking (+ bắt buộc có khóa HMAC nếu
    phải che) -> [lấy slot đồng thời] -> thống kê số dòng từng bảng -> large_tables
    -> enforce_time_range -> apply_row_limit(max_rows + 1)
    -> connector.explain(SQL sinh lại, KHÔNG có giới hạn do server thêm) -> check_plan
    -> (run) connector.execute(SQL có giới hạn, max_rows) -> apply_masking.

EXPLAIN tính trên câu không có FETCH FIRST/LIMIT do server thêm, vì first-rows costing
của câu có giới hạn che mất full scan; giới hạn của chính người dùng vẫn được giữ.
Câu chạy thật có giới hạn max_rows + 1 và connector chỉ lấy max_rows dòng, nên
`truncated` chính xác: chỉ True khi thực sự còn dòng chưa trả về.

Nguyên tắc an toàn:
- Xuống DB chỉ có SQL sinh lại từ cây đã parse (`ParsedQuery.sql` cho EXPLAIN,
  `apply_row_limit` cho execute), không bao giờ là chuỗi gốc (comment, optimizer hint, comment thực thi MariaDB bị bỏ).
- Không bao giờ chuyển nguyên văn lỗi của driver/ngoại lệ lạ ra ngoài hay vào
  audit: chỉ thông điệp cố định của các lớp lỗi trong `db.base` (được dựng lại
  từ mã lỗi đã kiểm tra) và lý do của cổng kiểm tra.
- Khóa HMAC không bao giờ được ghi log, đưa vào kết quả hay thông báo lỗi.
"""
from __future__ import annotations

import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from typing import Callable, Iterator

from .audit import AuditLog
from .config import Config, SystemProfile
from .db import make_connector
from .db.base import (
    Connector,
    DatabaseError,
    MissingCredentialError,
    PlanSummary,
    QueryTimeout,
    TableInfo,
    TableNotFound,
)
from .db.base import QueryRejected as DbQueryRejected
from .guard.parser import HINT_SELECT_ONLY, ParsedQuery, parse_select
from .guard.parser import QueryRejected as GuardRejected
from .guard.plan import check_plan
from .guard.policy import apply_row_limit, enforce_time_range, large_tables
from .masking import apply_masking, plan_masking

MAX_SQL_CHARS = 20_000
AUDIT_SQL_CHARS = 2000
GENERIC_ERROR = "Lỗi không mong đợi khi xử lý truy vấn"
BUSY_REASON = "hệ thống đang bận"
BUSY_HINT = "thử lại sau"
# Cùng dạng mã lỗi mà db.base chấp nhận (ORA-01555, ...) hoặc số nguyên (MariaDB).
_ERROR_CODE = re.compile(r"[A-Z]{2,4}-\d{1,6}")


@dataclass
class CheckResult:
    allowed: bool
    reasons: list[str]
    hints: list[str]
    sql: str | None
    plan: PlanSummary | None
    mask_plan: list[str | None]


@dataclass
class RunResult:
    columns: list[str]
    rows: list[tuple]
    row_count: int
    truncated: bool
    elapsed_ms: int
    notes: list[str]


@dataclass
class _State:
    """Những gì pipeline đã tính được, để `check` trả lại kể cả khi bị từ chối giữa chừng."""

    sql: str | None = None
    plan: PlanSummary | None = None
    mask_plan: list[str | None] = field(default_factory=list)


class UnknownSystem(LookupError):
    pass


class QueryService:
    def __init__(
        self,
        config: Config,
        connector_factory: Callable[[SystemProfile], Connector] = make_connector,
        hmac_key: bytes | None = None,
        today: Callable[[], date] = date.today,
    ):
        self._config = config
        self._factory = connector_factory
        self._hmac_key = hmac_key
        self._today = today
        self._audit = AuditLog(config.audit_log)
        self._slots = {
            name: threading.BoundedSemaphore(p.guard.max_concurrent) for name, p in config.systems.items()
        }
        self._connectors: dict[str, Connector] = {}
        self._connectors_lock = threading.Lock()

    def __repr__(self) -> str:
        return f"QueryService(systems={list(self._config.systems)})"

    # --- API công khai -------------------------------------------------------

    def check(self, system: str, sql: str) -> CheckResult:
        """Chạy toàn bộ kiểm tra (kể cả EXPLAIN) nhưng không execute. Chỉ ném LookupError."""
        profile = self._profile(system)
        state = _State()
        try:
            parsed = self._parse(profile, sql, state)
            connector = self._connector(profile)
            with self._slot(profile.name):
                self._gate(profile, connector, parsed, state)
        except Exception as exc:
            _, public, _ = _classify(exc, profile.guard.timeout_seconds)
            hints = [public.hint] if isinstance(public, GuardRejected) and public.hint else []
            return CheckResult(False, [_reason(public)], hints, state.sql, state.plan, state.mask_plan)
        return CheckResult(True, [], [], state.sql, state.plan, state.mask_plan)

    def run(self, system: str, sql: str) -> RunResult:
        """Chạy query đã qua kiểm tra. Mọi lần gọi đều ghi đúng một sự kiện audit."""
        start = time.monotonic()
        event: dict = {"system": system, "sql": sql[:AUDIT_SQL_CHARS], "status": "error",
                       "reason": None, "elapsed_ms": None, "row_count": None}
        try:
            result = self._run(system, sql)
        except Exception as exc:
            profile = self._config.systems.get(system)
            status, public, extra = _classify(exc, profile.guard.timeout_seconds if profile else None)
            event.update(status=status, reason=_reason(public), **extra)
            if public is exc:
                raise
            raise public from None
        else:
            event.update(status="ok", row_count=result.row_count)
            result.elapsed_ms = _elapsed_ms(start)
            return result
        finally:
            event["elapsed_ms"] = _elapsed_ms(start)
            self._audit.write(event)

    def table_info(self, system: str, table: str) -> TableInfo:
        profile = self._profile(system)
        try:
            connector = self._connector(profile)
            with self._slot(profile.name):
                return connector.table_info(table)
        except Exception as exc:
            _, public, _ = _classify(exc, profile.guard.timeout_seconds)
            if public is exc:
                raise
            raise public from None

    # --- Các bước pipeline ---------------------------------------------------

    def _run(self, system: str, sql: str) -> RunResult:
        profile = self._profile(system)
        guard = profile.guard
        state = _State()
        parsed = self._parse(profile, sql, state)
        connector = self._connector(profile)
        with self._slot(profile.name):
            self._gate(profile, connector, parsed, state)
            result = connector.execute(state.sql, guard.max_rows, guard.timeout_seconds)
        rows = result.rows
        notes: list[str] = []
        if state.mask_plan:
            rows = apply_masking(rows, state.mask_plan, self._hmac_key)
            masked = [c for c, m in zip(result.columns, state.mask_plan) if m]
            notes.append(f"Cột nhạy cảm đã được băm/che: {', '.join(masked)}.")
        truncated = result.truncated
        if truncated:
            notes.append(f"Kết quả bị giới hạn ở {guard.max_rows} dòng, còn dòng chưa trả về; "
                         "nên dùng truy vấn tổng hợp (GROUP BY, COUNT) hoặc thu hẹp điều kiện.")
        return RunResult(columns=list(result.columns), rows=rows, row_count=len(rows),
                         truncated=truncated, elapsed_ms=0, notes=notes)

    def _parse(self, profile: SystemProfile, sql: str, state: _State) -> ParsedQuery:
        """Các bước không chạm DB: độ dài, parse, kế hoạch che cột, khóa HMAC."""
        if len(sql) > MAX_SQL_CHARS:
            raise GuardRejected(
                f"Câu SQL dài {len(sql)} ký tự, vượt giới hạn {MAX_SQL_CHARS} ký tự.",
                "Rút gọn câu truy vấn hoặc tách thành nhiều truy vấn nhỏ hơn.",
            )
        parsed = parse_select(sql, profile.db_type)
        state.mask_plan = plan_masking(parsed, profile.guard.sensitive_columns)
        if state.mask_plan and self._hmac_key is None:
            raise GuardRejected(
                "Truy vấn lấy cột nhạy cảm nhưng server chưa được cấu hình khóa băm nên không che được.",
                "Bỏ cột nhạy cảm khỏi SELECT (vẫn lọc/nhóm theo nó được), hoặc nhờ quản trị cấu hình khóa băm.",
            )
        return parsed

    def _gate(self, profile: SystemProfile, connector: Connector, parsed: ParsedQuery, state: _State) -> None:
        """Các bước cần DB; gọi khi đang giữ slot. Kết thúc với state.sql là SQL được phép chạy."""
        guard = profile.guard
        stats = {table: _estimated_rows(connector, table) for table in parsed.tables}
        large = large_tables(parsed, guard, stats)
        enforce_time_range(parsed, guard, large, self._today())
        state.sql = apply_row_limit(parsed, guard.max_rows + 1)
        state.plan = connector.explain(parsed.sql)
        check_plan(state.plan, guard, large)

    # --- Hạ tầng -------------------------------------------------------------

    def _profile(self, system: str) -> SystemProfile:
        profile = self._config.systems.get(system)
        if profile is None:
            known = ", ".join(sorted(self._config.systems))
            raise UnknownSystem(f"Không có hệ thống '{system}'. Các hệ thống hiện có: {known}")
        return profile

    def _connector(self, profile: SystemProfile) -> Connector:
        with self._connectors_lock:
            connector = self._connectors.get(profile.name)
            if connector is None:
                connector = self._factory(profile)
                self._connectors[profile.name] = connector
            return connector

    @contextmanager
    def _slot(self, system: str) -> Iterator[None]:
        slot = self._slots[system]
        if not slot.acquire(blocking=False):
            raise GuardRejected(BUSY_REASON, BUSY_HINT)
        try:
            yield
        finally:
            slot.release()


def _estimated_rows(connector: Connector, table: str) -> int | None:
    """Lỗi thống kê không làm hỏng query: coi như không biết (cổng EXPLAIN bắt phần còn lại)."""
    try:
        return connector.table_info(table).estimated_rows
    except (TableNotFound, DatabaseError, TimeoutError):
        return None


def _safe_code(code: object) -> str | int | None:
    if isinstance(code, int) and not isinstance(code, bool):
        return code
    if isinstance(code, str) and _ERROR_CODE.fullmatch(code):
        return code
    return None


def _classify(exc: Exception, timeout_seconds: int | None) -> tuple[str, Exception, dict]:
    """(trạng thái audit, ngoại lệ được phép đưa ra ngoài, trường audit thêm).

    Ngoại lệ đưa ra ngoài chỉ mang thông điệp do chính dự án này tạo; mọi thứ
    khác được thay bằng thông điệp cố định.
    """
    if isinstance(exc, GuardRejected):
        return "rejected", exc, {}
    if isinstance(exc, DbQueryRejected):
        return "rejected", GuardRejected(str(DbQueryRejected()), HINT_SELECT_ONLY), {}
    if isinstance(exc, TimeoutError):
        public = exc if type(exc) is QueryTimeout else QueryTimeout(timeout_seconds or 0)
        return "timeout", public, {"error_type": type(exc).__name__}
    if isinstance(exc, DatabaseError):
        code = _safe_code(exc.code)
        return "error", DatabaseError(code), {"error_type": type(exc).__name__, "code": code}
    if type(exc) in (UnknownSystem, TableNotFound, MissingCredentialError):
        return "error", exc, {"error_type": type(exc).__name__}
    return "error", RuntimeError(GENERIC_ERROR), {"error_type": type(exc).__name__}


def _reason(exc: Exception) -> str:
    return exc.reason if isinstance(exc, GuardRejected) else str(exc)


def _elapsed_ms(start: float) -> int:
    return int((time.monotonic() - start) * 1000)
