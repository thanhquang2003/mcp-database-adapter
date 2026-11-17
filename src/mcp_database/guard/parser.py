"""Cổng kiểm tra SQL: chỉ cho qua đúng một câu SELECT / WITH ... SELECT.

Mọi SQL Claude gửi xuống DB đều phải đi qua `parse_select`. Nguyên tắc là
mặc định từ chối: cú pháp không parse được, không nhận ra, hoặc có dấu hiệu
ghi/khóa/gọi hàm có tác dụng phụ đều bị chặn bằng `QueryRejected`.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp

from mcp_database.names import norm

DIALECTS = {"oracle": "oracle", "mariadb": "mysql"}

HINT_SELECT_ONLY = "Chỉ được gửi đúng một câu SELECT hoặc WITH ... SELECT, không ghi/khóa dữ liệu."
HINT_REWRITE = "Hãy viết lại bằng cú pháp SELECT chuẩn."
HINT_FUNCTION = "Bỏ hàm này; chỉ dùng hàm tính toán thông thường (COUNT, SUM, TO_CHAR...)."

# Node bị cấm ở bất kỳ đâu trong cây (kể cả trong CTE, subquery).
FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert,
    exp.Update,
    exp.Delete,
    exp.Merge,
    exp.Create,
    exp.Drop,
    exp.Alter,
    exp.Command,
    exp.TruncateTable,
    exp.Into,  # SELECT ... INTO
    exp.Lock,  # FOR UPDATE / FOR SHARE / LOCK IN SHARE MODE
    exp.PropertyEQ,  # gán biến MariaDB: @a := ...
)

BLOCKED_FUNCTIONS = frozenset(
    {"SLEEP", "BENCHMARK", "LOAD_FILE", "GET_LOCK", "RELEASE_LOCK", "RELEASE_ALL_LOCKS"}
)
BLOCKED_PREFIXES = ("DBMS_", "UTL_")


class QueryRejected(Exception):
    def __init__(self, reason: str, hint: str | None = None):
        super().__init__(reason if hint is None else f"{reason} Gợi ý: {hint}")
        self.reason = reason
        self.hint = hint


@dataclass(frozen=True)
class ParsedQuery:
    expression: exp.Expression
    dialect: str
    sql: str
    tables: tuple[str, ...]


def parse_select(sql: str, db_type: str) -> ParsedQuery:
    dialect = DIALECTS.get(db_type)
    if dialect is None:
        raise ValueError(f"db_type không hỗ trợ: {db_type!r}")

    try:
        statements = sqlglot.parse(sql, read=dialect)
    except Exception as e:  # mặc định từ chối: mọi lỗi parse đều là QueryRejected
        raise QueryRejected(f"Không parse được câu SQL theo cú pháp {db_type}: {_first_line(e)}", HINT_REWRITE) from e

    # Bỏ phần rỗng sinh ra do dấu ';' cuối câu (kể cả '; -- comment').
    statements = [s for s in statements if s is not None and not isinstance(s, exp.Semicolon)]
    if not statements:
        raise QueryRejected("Câu SQL rỗng.", HINT_SELECT_ONLY)
    if len(statements) > 1:
        raise QueryRejected(f"Chỉ được gửi một câu lệnh, nhận được {len(statements)}.", HINT_SELECT_ONLY)

    root = statements[0]
    if not _is_select_root(root):
        raise QueryRejected(f"Chỉ cho phép SELECT, không cho phép {_kind(root)}.", HINT_SELECT_ONLY)

    _check_tree(root)

    for hint in list(root.find_all(exp.Hint)):
        hint.pop()

    try:
        out_sql = root.sql(dialect=dialect, comments=False)
    except Exception as e:
        raise QueryRejected(f"Không sinh lại được câu SQL: {_first_line(e)}", HINT_REWRITE) from e

    return ParsedQuery(expression=root, dialect=dialect, sql=out_sql, tables=_real_tables(root))


def _is_select_root(node: exp.Expression) -> bool:
    if isinstance(node, exp.Subquery):
        node = node.unnest()
    return isinstance(node, (exp.Select, exp.SetOperation))


def _check_tree(root: exp.Expression) -> None:
    for node in root.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise QueryRejected(f"Câu SQL chứa thành phần bị cấm: {_kind(node)}.", HINT_SELECT_ONLY)
        if isinstance(node, exp.Func):
            name = _func_name(node)
            if name in BLOCKED_FUNCTIONS or name.startswith(BLOCKED_PREFIXES):
                raise QueryRejected(f"Hàm {name} bị chặn vì có tác dụng phụ.", HINT_FUNCTION)
        if isinstance(node, exp.Dot) and isinstance(node.expression, exp.Func):
            raise QueryRejected(
                f"Không cho gọi hàm qua schema/package: {node.sql(comments=False)}.", HINT_FUNCTION
            )
        if isinstance(node, exp.Column):
            # Oracle cho gọi hàm không đối số không cần ngoặc: dbms_random.value
            for part in (node.args.get("table"), node.args.get("db"), node.args.get("catalog")):
                if part is not None and norm(part.name).startswith(BLOCKED_PREFIXES):
                    raise QueryRejected(f"Không cho dùng package {part.name}.", HINT_FUNCTION)
        if isinstance(node, exp.Table):
            parts = [p.name for p in node.parts]
            if any("@" in p for p in parts):
                raise QueryRejected(
                    f"Không cho truy vấn qua database link: {node.sql(comments=False)}.",
                    "Chỉ truy vấn bảng trong chính hệ thống này.",
                )


def _func_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return node.name.upper()
    return node.sql_name().upper()


def _real_tables(root: exp.Expression) -> tuple[str, ...]:
    seen: dict[str, None] = {}
    for table in root.find_all(exp.Table, bfs=False):
        if not table.name or _is_cte_ref(table):
            continue
        seen.setdefault(norm(table.name), None)
    return tuple(seen)


def _is_cte_ref(table: exp.Table) -> bool:
    """Bảng không có schema và trùng tên một CTE nhìn thấy được tại vị trí đó.

    Trong cùng một WITH, CTE thứ i chỉ thấy các CTE đứng trước nó; tự tham
    chiếu (đệ quy) bị tính là bảng thật để thà báo thừa còn hơn bỏ sót.
    """
    if table.args.get("db") or table.args.get("catalog"):
        return False
    name = norm(table.name)
    child: exp.Expression = table
    node = table.parent
    while node is not None:
        if isinstance(node, exp.With) and isinstance(child, exp.CTE):
            before = node.expressions[: node.expressions.index(child)]
            if name in {norm(c.alias) for c in before}:
                return True
            # Bỏ qua query sở hữu WITH này: CTE của nó đã xét ở trên.
            child, node = node.parent, node.parent.parent if node.parent else None
            continue
        if isinstance(node, exp.Query) and name in {norm(c.alias) for c in node.ctes}:
            return True
        child, node = node, node.parent
    return False


def _kind(node: exp.Expression) -> str:
    return node.key.upper()


def _first_line(e: Exception) -> str:
    return str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
