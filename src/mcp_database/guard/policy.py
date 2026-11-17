"""Chính sách cho bảng lớn (bắt buộc lọc theo khoảng thời gian) và giới hạn số dòng.

Nguyên tắc như cổng parser: chỉ nhận đúng các dạng đã biết là an toàn, còn lại
từ chối kèm gợi ý. Mọi so khớp tên bảng/cột đều qua `norm` (không phân biệt
hoa thường, bỏ nháy, bỏ schema).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Iterator, Mapping

from sqlglot import exp

from mcp_database.config import Guard, TableGuard
from mcp_database.guard.parser import DIALECTS, ParsedQuery, QueryRejected, generate, is_cte_ref
from mcp_database.names import norm

HINT_BOUND = "dùng literal ngày hoặc SYSDATE/NOW() ± n ngày"


def large_tables(
    parsed: ParsedQuery, guard: Guard, table_rows: Mapping[str, int | None]
) -> set[str]:
    """Bảng (đã `norm`) khai báo trong `guard.tables` hoặc có số dòng >= ngưỡng.

    Không có thống kê (`None`) thì không coi là lớn; cổng EXPLAIN bắt phần còn lại.
    """
    rows = {norm(k): v for k, v in table_rows.items()}
    out: set[str] = set()
    for name in parsed.tables:
        count = rows.get(name)
        if name in guard.tables or (count is not None and count >= guard.large_table_rows):
            out.add(name)
    return out


def enforce_time_range(parsed: ParsedQuery, guard: Guard, large: set[str], today: date) -> None:
    """Mỗi tham chiếu trực tiếp tới bảng lớn phải có khoảng thời gian hợp lệ trong
    WHERE của chính câu SELECT chứa nó."""
    if not large:
        return
    large = {norm(t) for t in large}
    for table in parsed.expression.find_all(exp.Table):
        if not table.name or is_cte_ref(table):
            continue
        name = norm(table.name)
        if name not in large:
            continue
        tg = guard.tables.get(name)
        if tg is None:
            raise QueryRejected(
                f"Bảng {name} là bảng lớn nhưng chưa khai báo cột thời gian để giới hạn truy vấn.",
                f"Nhờ người quản trị khai báo guard.tables.{name}.time_column và max_range_days "
                "trong systems.yaml, hoặc truy vấn bảng khác.",
            )
        select = _owning_select(table, name)
        operator = _row_source_operator(select, table)
        if operator is not None:
            raise QueryRejected(
                f"Không cho dùng {operator} trên bảng lớn {name}: WHERE chỉ lọc kết quả của {operator}, "
                "bảng vẫn bị đọc toàn bộ trước đó.",
                f"Lọc {name} theo {tg.time_column} trong một subquery/CTE rồi mới {operator} trên kết quả đó.",
            )
        _check_reference(select, table, name, tg, today, parsed.dialect)


def apply_row_limit(parsed: ParsedQuery, max_rows: int) -> str:
    """SQL cuối có giới hạn dòng ở gốc câu lệnh (`FETCH FIRST n ROWS ONLY` / `LIMIT n`).

    Đã có giới hạn số nguyên thì lấy `min`; giới hạn dạng khác (PERCENT, WITH TIES,
    biểu thức, FETCH không có số) bị từ chối.
    """
    root = parsed.expression.copy()
    n = max_rows
    existing = root.args.get("limit")
    if existing is not None:
        count = _plain_count(existing)
        if count is None:
            raise QueryRejected(
                f"Không hỗ trợ giới hạn dòng dạng '{existing.sql(dialect=parsed.dialect, comments=False)}' "
                "(PERCENT, WITH TIES hoặc biểu thức).",
                "Dùng số dòng cụ thể: FETCH FIRST n ROWS ONLY (Oracle) hoặc LIMIT n (MariaDB).",
            )
        n = min(count, max_rows)
    # generate() bỏ mọi comment còn sót, kể cả optimizer hint Oracle (/*+ FULL(...) */)
    # để Claude không ép full scan qua hint, và từ chối cú pháp không sinh lại được.
    return generate(root.limit(n), parsed.dialect)


# --- Phạm vi và tham chiếu ---------------------------------------------------


def _owning_select(table: exp.Table, name: str) -> exp.Select:
    parent = table.parent
    if isinstance(parent, (exp.From, exp.Join)) and isinstance(parent.parent, exp.Select):
        return parent.parent
    raise QueryRejected(
        f"Bảng lớn {name} phải nằm trực tiếp trong FROM/JOIN của một câu SELECT.",
        f"Viết {name} trực tiếp sau FROM/JOIN (không bọc ngoặc) và lọc theo cột thời gian trong WHERE.",
    )


# Thuộc tính của Table không đổi nguồn dòng: tên, alias, partition, flashback
# (AS OF), sample, index hint MariaDB. Thuộc tính khác (PIVOT/UNPIVOT, ...) coi
# như toán tử trên bảng và bị từ chối.
_PLAIN_TABLE_ARGS = frozenset({"this", "db", "catalog", "alias", "partition", "version", "sample", "hints"})


def _row_source_operator(select: exp.Select, table: exp.Table) -> str | None:
    """Toán tử làm WHERE lọc trên kết quả của nó thay vì trên chính bảng."""
    if select.args.get("connect") is not None:
        return "CONNECT BY"
    if select.args.get("match") is not None:
        return "MATCH_RECOGNIZE"
    if select.args.get("pivots"):
        return "PIVOT/UNPIVOT"
    for key, value in table.args.items():
        if key not in _PLAIN_TABLE_ARGS and value not in (None, [], False):
            return "PIVOT/UNPIVOT" if key == "pivots" else key.upper()
    return None


def _sources(select: exp.Select) -> list[exp.Expression]:
    out: list[exp.Expression] = []
    from_ = select.args.get("from_")
    if from_ is not None:
        out.append(from_.this)
    out.extend(j.this for j in select.args.get("joins") or [])
    out.extend(select.args.get("laterals") or [])
    return out


def _ref_name(source: exp.Expression) -> str | None:
    if source.alias:
        return norm(source.alias)
    if isinstance(source, exp.Table):
        return norm(source.name)
    return None


def _check_reference(
    select: exp.Select, table: exp.Table, name: str, tg: TableGuard, today: date, dialect: str
) -> None:
    sources = _sources(select)
    ref = _ref_name(table)
    unique_ref = sum(1 for s in sources if _ref_name(s) == ref) == 1
    alone = len(sources) == 1
    who = f"{ref}.{tg.time_column}" if ref != name else tg.time_column

    def is_time_col(node: exp.Expression) -> bool:
        if not isinstance(node, exp.Column) or norm(node.name) != tg.time_column:
            return False
        if node.table:
            return unique_ref and norm(node.table) == ref
        return alone

    need = (
        f"Thêm vào WHERE của chính câu SELECT đọc {name}: {who} >= <cận dưới> AND {who} < <cận trên> "
        f"(tối đa {tg.max_range_days} ngày), nối bằng AND, không đặt trong OR/NOT, không bọc hàm quanh cột; "
        f"cận {HINT_BOUND}."
    )

    lowers: list[date] = []
    uppers: list[date] = []
    where = select.args.get("where")
    for cond in _conjuncts(where.this) if where is not None else ():
        if isinstance(cond, exp.Between) and is_time_col(cond.this):
            if cond.args.get("symmetric"):
                raise QueryRejected(f"Không cho BETWEEN SYMMETRIC trên {who}.", need)
            lowers.append(_bound(cond.args["low"], who, dialect, today, upper=False))
            uppers.append(_bound(cond.args["high"], who, dialect, today, upper=True))
            continue
        if not isinstance(cond, (exp.GT, exp.GTE, exp.LT, exp.LTE, exp.EQ)):
            continue
        if is_time_col(cond.this):
            other, is_lower = cond.expression, isinstance(cond, (exp.GT, exp.GTE))
        elif is_time_col(cond.expression):
            other, is_lower = cond.this, isinstance(cond, (exp.LT, exp.LTE))
        else:
            continue
        if isinstance(cond, exp.EQ):
            lowers.append(_bound(other, who, dialect, today, upper=False))
            uppers.append(_bound(other, who, dialect, today, upper=True))
        elif is_lower:
            lowers.append(_bound(other, who, dialect, today, upper=False))
        else:
            uppers.append(_bound(other, who, dialect, today, upper=True))

    if not lowers:
        raise QueryRejected(
            f"Bảng lớn {name} bắt buộc có điều kiện cận dưới trên cột thời gian {who} "
            "trong WHERE của câu SELECT đọc bảng này.",
            need,
        )
    low = max(lowers)
    high = min(uppers) if uppers else today
    days = (high - low).days
    if days > tg.max_range_days:
        raise QueryRejected(
            f"Khoảng thời gian trên {who} là {days} ngày ({low.isoformat()} → {high.isoformat()}"
            f"{'' if uppers else ', thiếu cận trên nên tính tới hôm nay'}), "
            f"vượt giới hạn {tg.max_range_days} ngày của bảng {name}.",
            f"Thu hẹp khoảng thời gian xuống tối đa {tg.max_range_days} ngày; "
            "cần dài hơn thì chia thành nhiều truy vấn.",
        )


def _conjuncts(node: exp.Expression) -> Iterator[exp.Expression]:
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.And):
        yield from _conjuncts(node.this)
        yield from _conjuncts(node.expression)
    else:
        yield node


# --- Tính cận thành ngày ----------------------------------------------------

# Chuỗi ISO trần, CAST('...' AS DATE) và literal DATE/TIMESTAMP được hiểu là
# YYYY-MM-DD[ HH:MM:SS]. Với Oracle, chuỗi trần/CAST phụ thuộc NLS của phiên:
# tầng kết nối Oracle (Task 5) phải cố định NLS_DATE_FORMAT/NLS_TIMESTAMP_FORMAT
# theo đúng định dạng này. re.ASCII: \d chỉ khớp 0-9.
_ISO = re.compile(r"(\d{4}-\d{2}-\d{2})(?: (\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?)?", re.ASCII)
_TO_DATE_FORMATS = {
    "%Y-%m-%d": re.compile(r"\d{4}-\d{2}-\d{2}", re.ASCII),
    "%Y-%m-%d %H:%M:%S": re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", re.ASCII),
    "%Y%m%d": re.compile(r"\d{8}", re.ASCII),
}
# Định dạng sqlglot gán cho literal TIMESTAMP '...' của Oracle.
_TIMESTAMP_LITERAL_FORMAT = "%Y-%m-%d %H:%M:%S.%f"
_DATE_TYPES = {exp.DataType.Type.DATE, exp.DataType.Type.DATETIME, exp.DataType.Type.TIMESTAMP,
               exp.DataType.Type.TIMESTAMPTZ, exp.DataType.Type.TIMESTAMPLTZ}


def _bound(node: exp.Expression, who: str, dialect: str, today: date, upper: bool) -> date:
    """Ngày của cận; cận dưới làm tròn xuống, cận trên có giờ thì làm tròn lên."""
    value = _absolute(node)
    if value is not None:
        if upper and value.time() != datetime.min.time():
            return value.date() + timedelta(days=1)
        return value.date()
    # Cận tương đối (SYSDATE/NOW() ± n) tính theo ngày `today`, không làm tròn lên:
    # giờ thực tế trong ngày bị bỏ qua nên khoảng có thể dài hơn tính toán tối đa
    # 1 ngày. Chấp nhận độ lệch này để kết quả không phụ thuộc đồng hồ.
    rel = _relative(node, dialect == DIALECTS["oracle"], today)
    if rel is not None:
        return rel
    raise QueryRejected(
        f"Không tính được cận thời gian '{node.sql(dialect=dialect, comments=False)}' của {who}.", HINT_BOUND
    )


def _string(node: exp.Expression) -> str | None:
    if isinstance(node, exp.Literal) and node.is_string:
        return node.this
    return None


def _absolute(node: exp.Expression) -> datetime | None:
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, exp.Literal):
        return _iso(_string(node))
    if isinstance(node, exp.DateStrToDate):
        return _iso(_string(node.this))
    if isinstance(node, exp.Cast) and node.to.this in _DATE_TYPES:
        return _iso(_string(node.this))
    if isinstance(node, exp.StrToTime) and _string(node.args.get("format")) == _TIMESTAMP_LITERAL_FORMAT:
        return _iso(_string(node.this))
    if isinstance(node, exp.StrToDate):
        fmt, text = _string(node.args.get("format")), _string(node.this)
        pattern = _TO_DATE_FORMATS.get(fmt or "")
        if pattern is None or text is None or not pattern.fullmatch(text):
            return None
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            return None
    return None


def _iso(text: str | None) -> datetime | None:
    if text is None:
        return None
    m = _ISO.fullmatch(text)
    if m is None:
        return None
    try:
        value = datetime.strptime(f"{m[1]} {m[2] or '00:00:00'}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    if m[3] and int(m[3]):
        value += timedelta(microseconds=1)  # chỉ để đánh dấu "có giờ" khi làm tròn cận trên
    return value


def _relative(node: exp.Expression, oracle: bool, today: date) -> date | None:
    while isinstance(node, exp.Paren):
        node = node.this
    if isinstance(node, (exp.Add, exp.Sub)):
        base = _relative(node.this, oracle, today)
        days = _days(node.expression, oracle)
        if base is None or days is None:
            return None
        try:
            return base + timedelta(days=days) if isinstance(node, exp.Add) else base - timedelta(days=days)
        except OverflowError:
            return None
    if _is_now(node):
        return today
    if isinstance(node, exp.DateTrunc) and _is_now(node.this):
        unit = node.args.get("unit")
        if unit is not None and unit.name.upper() == "DAY":
            return today
    return None


def _is_now(node: exp.Expression) -> bool:
    if isinstance(node, (exp.CurrentTimestamp, exp.CurrentDate)):
        return True
    return (
        isinstance(node, exp.Anonymous)
        and node.name.upper() in ("NOW", "SYSDATE")
        and not node.expressions
    )


def _days(node: exp.Expression, oracle: bool) -> int | None:
    """Số ngày cộng/trừ: `INTERVAL n DAY` (cả hai), hoặc số nguyên trần (chỉ Oracle;
    MariaDB hiểu `NOW() - 7` là phép trừ số)."""
    if isinstance(node, exp.Interval):
        unit = node.args.get("unit")
        if unit is None or unit.name.upper() != "DAY":
            return None
        literal = node.this
    elif oracle:
        literal = node
    else:
        return None
    if isinstance(literal, exp.Literal) and _ascii_int(literal.this):
        return int(literal.this)
    return None


def _plain_count(limit: exp.Expression) -> int | None:
    """Số dòng của LIMIT/FETCH khi là số nguyên đơn giản; None nếu là dạng khác."""
    opts = limit.args.get("limit_options")
    if opts is not None and (opts.args.get("percent") or opts.args.get("with_ties")):
        return None
    if isinstance(limit, exp.Limit):
        if limit.args.get("offset") is not None or limit.args.get("expressions"):
            return None
        count = limit.expression
    elif isinstance(limit, exp.Fetch):
        count = limit.args.get("count")
    else:
        return None
    if isinstance(count, exp.Literal) and not count.is_string and _ascii_int(count.this):
        return int(count.this)
    return None


def _ascii_int(text: object) -> bool:
    # str.isdigit() nhận cả '²', '٣' (int() lỗi hoặc DB hiểu khác): chỉ nhận 0-9.
    return re.fullmatch(r"[0-9]+", str(text)) is not None
