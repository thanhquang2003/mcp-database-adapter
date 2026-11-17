"""Cổng kiểm tra SQL: chỉ cho qua đúng một câu SELECT / WITH ... SELECT.

Mọi SQL Claude gửi xuống DB đều phải đi qua `parse_select`. Nguyên tắc là
mặc định từ chối: cú pháp không parse được, không nhận ra, hoặc có dấu hiệu
ghi/khóa/gọi hàm có tác dụng phụ đều bị chặn bằng `QueryRejected`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

import sqlglot
from sqlglot import exp
from sqlglot.errors import ErrorLevel, UnsupportedError
from sqlglot.dialects.mysql import MySQL
from sqlglot.dialects.oracle import Oracle

from mcp_database.names import norm


class McpOracle(Oracle):
    """Oracle 12c/19c: sinh lại MINUS thay vì EXCEPT (EXCEPT chỉ có từ 21c)."""

    class Generator(Oracle.Generator):
        def set_operation(self, expression: exp.SetOperation) -> str:
            out = super().set_operation(expression)
            if isinstance(expression, exp.Except) and out.startswith("EXCEPT"):
                return "MINUS" + out[len("EXCEPT"):]
            return out


class McpMariadb(MySQL):
    """MariaDB không có hàm REGEXP_LIKE: sinh lại toán tử REGEXP / NOT REGEXP."""

    class Generator(MySQL.Generator):
        TRANSFORMS = {
            **MySQL.Generator.TRANSFORMS,
            exp.RegexpLike: lambda self, e: self.binary(e, "REGEXP"),
        }

        def not_sql(self, expression: exp.Not) -> str:
            if isinstance(expression.this, exp.RegexpLike):
                return self.binary(expression.this, "NOT REGEXP")
            return super().not_sql(expression)

        def version_sql(self, expression: exp.Version) -> str:
            # sqlglot đọc `FOR SYSTEM_TIME ...` của MariaDB nhưng sinh lại thành
            # `FOR TIMESTAMP ...` (sai cú pháp/ngữ nghĩa): báo không hỗ trợ để bị từ chối.
            self.unsupported("FOR SYSTEM_TIME (system-versioned table)")
            return ""


# Tên đăng ký của các dialect con trong registry của sqlglot (tên lớp viết thường),
# để nơi khác gọi `.sql(dialect=parsed.dialect)` vẫn sinh SQL chạy được.
DIALECTS = {"oracle": "mcporacle", "mariadb": "mcpmariadb"}

HINT_SELECT_ONLY = "Chỉ được gửi đúng một câu SELECT hoặc WITH ... SELECT, không ghi/khóa dữ liệu."
HINT_REWRITE = "Hãy viết lại bằng cú pháp SELECT chuẩn."
UNSUPPORTED_REASON = ("Câu lệnh dùng cú pháp mà DB đích không hỗ trợ / không thể sinh lại chính xác"
                      " ({detail}).")
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
    {
        "SLEEP",
        "BENCHMARK",
        "LOAD_FILE",
        "GET_LOCK",
        "RELEASE_LOCK",
        "RELEASE_ALL_LOCKS",
        "MASTER_POS_WAIT",
        "MASTER_GTID_WAIT",
        "NEXTVAL",
        "SETVAL",
        "LASTVAL",
    }
)
BLOCKED_PREFIXES = ("DBMS_", "UTL_")

# Hàm XQuery/XPath/URI nhận một chuỗi làm đường dẫn: chuỗi đó có thể chỉ tới bảng
# bất kỳ (fn:collection("oradb:/APP/CDR"), ora:view("CDR"), DBURITYPE('/APP/CDR'))
# nên vượt qua mọi kiểm tra theo tên bảng; EXTRACTVALUE/UPDATEXML còn là kênh
# rò dữ liệu qua thông báo lỗi của MariaDB.
XPATH_FUNCTIONS = frozenset(
    {
        "XMLTABLE",
        "XMLQUERY",
        "XMLEXISTS",
        "XMLCAST",
        "XMLSEQUENCE",
        "XMLTRANSFORM",
        "XMLPATCH",
        "XMLDIFF",
        "XMLGET",
        "EXTRACTVALUE",
        "EXISTSNODE",
        "UPDATEXML",
        "DELETEXML",
        "APPENDCHILDXML",
        "INSERTCHILDXML",
        "INSERTCHILDXMLAFTER",
        "INSERTCHILDXMLBEFORE",
        "INSERTXMLAFTER",
        "INSERTXMLBEFORE",
        "DBURITYPE",
        "XDBURITYPE",
        "HTTPURITYPE",
        "URIFACTORY",
    }
)
HINT_XPATH = "Truy vấn trực tiếp bảng/cột bằng SELECT thông thường, không dùng hàm XML/XPath/URI."

# Đơn vị hợp lệ của EXTRACT(<đơn vị> FROM ngày). Dạng còn lại của Oracle,
# EXTRACT(xml, 'xpath'), là hàm XPath và bị chặn.
DATETIME_UNITS = frozenset(
    {
        "YEAR",
        "QUARTER",
        "MONTH",
        "WEEK",
        "DAY",
        "HOUR",
        "MINUTE",
        "SECOND",
        "MICROSECOND",
        "TIMEZONE_HOUR",
        "TIMEZONE_MINUTE",
        "TIMEZONE_REGION",
        "TIMEZONE_ABBR",
        "YEAR_MONTH",
        "DAY_HOUR",
        "DAY_MINUTE",
        "DAY_SECOND",
        "DAY_MICROSECOND",
        "HOUR_MINUTE",
        "HOUR_SECOND",
        "HOUR_MICROSECOND",
        "MINUTE_SECOND",
        "MINUTE_MICROSECOND",
        "SECOND_MICROSECOND",
    }
)


class QueryRejected(Exception):
    def __init__(self, reason: str, hint: str | None = None):
        super().__init__(reason if hint is None else f"{reason} Gợi ý: {hint}")
        self.reason = reason
        self.hint = hint


class TableRef(NamedTuple):
    """Tham chiếu bảng đúng như người dùng viết (giữ hoa thường và thông tin nháy).

    Dùng để tra thống kê trên DB: Oracle phân biệt định danh có nháy / không nháy,
    còn tên chuẩn hóa (`norm`) mất cả schema lẫn cách viết.
    """

    schema: str | None
    name: str
    schema_quoted: bool = False
    name_quoted: bool = False


@dataclass(frozen=True)
class ParsedQuery:
    expression: exp.Expression
    dialect: str
    sql: str
    tables: tuple[str, ...]  # tên đã norm (không schema, viết hoa) cho policy/masking
    table_refs: tuple[TableRef, ...] = ()  # như đã viết, bỏ CTE, không trùng, theo thứ tự


def parse_select(sql: str, db_type: str) -> ParsedQuery:
    dialect = DIALECTS.get(db_type)
    if dialect is None:
        raise ValueError(f"db_type không hỗ trợ: {db_type!r}")

    try:
        if db_type == "mariadb":
            _reject_executable_comments(sql, dialect)
        statements = sqlglot.parse(sql, read=dialect)
    except QueryRejected:
        raise
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
    if db_type == "oracle":
        _check_oracle_round_trip(root)
    else:
        _check_mariadb_round_trip(root)

    for hint in list(root.find_all(exp.Hint)):
        hint.pop()

    out_sql = generate(root, dialect)
    return ParsedQuery(expression=root, dialect=dialect, sql=out_sql, tables=_real_tables(root),
                       table_refs=_table_refs(root))


def generate(expression: exp.Expression, dialect: str) -> str:
    """Sinh lại SQL từ cây; cú pháp sqlglot không sinh lại được chính xác thì từ chối.

    Mặc định sqlglot chỉ cảnh báo rồi lặng lẽ bỏ phần không hỗ trợ (vd. PIVOT trên
    MariaDB), làm đổi nghĩa câu truy vấn; ở đây buộc ném lỗi. comments=False: bỏ
    mọi comment, kể cả optimizer hint.
    """
    try:
        return expression.sql(dialect=dialect, comments=False, unsupported_level=ErrorLevel.RAISE)
    except UnsupportedError as e:
        raise QueryRejected(UNSUPPORTED_REASON.format(detail=_first_line(e)), HINT_REWRITE) from e
    except Exception as e:
        raise QueryRejected(f"Không sinh lại được câu SQL: {_first_line(e)}", HINT_REWRITE) from e


def _is_select_root(node: exp.Expression) -> bool:
    if isinstance(node, exp.Subquery):
        node = node.unnest()
    return isinstance(node, (exp.Select, exp.SetOperation))


def _reject_executable_comments(sql: str, dialect: str) -> None:
    """MariaDB chạy nội dung `/*! ... */` và `/*M! ... */` như SQL thật.

    Dùng tokenizer nên `/*!` nằm trong chuỗi ký tự không bị bắt nhầm. Tokenizer
    không phân biệt comment khối với comment dòng, nên `--!x` / `#!x` cũng bị
    từ chối (thà chặn thừa).
    """
    for token in sqlglot.Dialect.get_or_raise(dialect).tokenize(sql):
        for comment in token.comments:
            if comment.startswith("!") or comment[:2].upper() == "M!":
                raise QueryRejected(
                    "Không cho dùng comment thực thi của MariaDB (/*! ... */ hoặc /*M! ... */).",
                    "Bỏ comment này; nếu cần điều kiện thì viết thẳng vào câu SELECT.",
                )


def _check_oracle_round_trip(root: exp.Expression) -> None:
    for node in root.find_all(exp.Except, exp.Intersect):
        if node.args.get("distinct") is False:
            raise QueryRejected(
                f"Oracle 12c/19c không hỗ trợ {_kind(node)} ALL.",
                "Dùng MINUS / INTERSECT (không có ALL), hoặc viết lại bằng NOT EXISTS / EXISTS.",
            )


def _check_mariadb_round_trip(root: exp.Expression) -> None:
    for node in root.find_all(exp.RegexpLike):
        if node.args.get("flag") is not None or node.args.get("full_match"):
            raise QueryRejected(
                "MariaDB không có hàm REGEXP_LIKE với tham số cờ.",
                "Dùng toán tử: cột REGEXP 'mẫu' (thêm (?i) vào mẫu nếu cần không phân biệt hoa thường).",
            )


def _check_tree(root: exp.Expression) -> None:
    for node in root.walk():
        if isinstance(node, FORBIDDEN_NODES):
            raise QueryRejected(f"Câu SQL chứa thành phần bị cấm: {_kind(node)}.", HINT_SELECT_ONLY)
        if isinstance(node, (exp.XMLTable, exp.XMLGet)):
            raise QueryRejected(f"Không cho dùng hàm XML/XPath {_kind(node)}.", HINT_XPATH)
        if isinstance(node, exp.Extract) and not (
            isinstance(node.this, exp.Var) and node.this.name.upper() in DATETIME_UNITS
        ):
            raise QueryRejected("Không cho dùng EXTRACT dạng XML/XPath.", HINT_XPATH)
        if isinstance(node, exp.Func):
            name = _func_name(node)
            if name in XPATH_FUNCTIONS:
                raise QueryRejected(f"Không cho dùng hàm XML/XPath/URI {name}.", HINT_XPATH)
            if name in BLOCKED_FUNCTIONS or name.startswith(BLOCKED_PREFIXES):
                raise QueryRejected(f"Hàm {name} bị chặn vì có tác dụng phụ.", HINT_FUNCTION)
            if "@" in name:
                raise QueryRejected(
                    f"Không cho gọi hàm qua database link: {name}.",
                    "Chỉ dùng hàm trong chính hệ thống này.",
                )
        if isinstance(node, exp.Dot) and isinstance(node.expression, exp.Func):
            raise QueryRejected(
                f"Không cho gọi hàm qua schema/package: {node.sql(comments=False)}.", HINT_FUNCTION
            )
        if isinstance(node, exp.Column):
            # Oracle cho gọi hàm không đối số không cần ngoặc: dbms_random.value
            for part in (node.args.get("table"), node.args.get("db"), node.args.get("catalog")):
                if part is not None and norm(part.name).startswith(BLOCKED_PREFIXES):
                    raise QueryRejected(f"Không cho dùng package {part.name}.", HINT_FUNCTION)
            if node.args.get("table") is not None and norm(node.name) == "NEXTVAL":
                raise QueryRejected(
                    "Không cho dùng sequence.NEXTVAL vì làm tăng sequence.",
                    "Bỏ NEXTVAL; nếu cần giá trị hiện tại thì dùng CURRVAL hoặc đọc từ bảng.",
                )
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
        if not table.name or is_cte_ref(table):
            continue
        seen.setdefault(norm(table.name), None)
    return tuple(seen)


def _table_refs(root: exp.Expression) -> tuple[TableRef, ...]:
    seen: dict[TableRef, None] = {}
    for table in root.find_all(exp.Table, bfs=False):
        if not table.name or is_cte_ref(table) or table.args.get("catalog") is not None:
            continue
        this, db = table.args.get("this"), table.args.get("db")
        if not isinstance(this, exp.Identifier) or (db is not None and not isinstance(db, exp.Identifier)):
            continue
        ref = TableRef(
            schema=db.name if db is not None else None,
            name=this.name,
            schema_quoted=bool(db is not None and db.quoted),
            name_quoted=bool(this.quoted),
        )
        seen.setdefault(ref, None)
    return tuple(seen)


def is_cte_ref(table: exp.Table) -> bool:
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
