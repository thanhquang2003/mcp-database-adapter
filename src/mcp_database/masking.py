"""Che cột nhạy cảm: quyết định trước khi chạy, áp dụng sau khi có kết quả.

`plan_masking` chỉ cho cột nhạy cảm đi ra ngoài khi nó là projection trần ở
SELECT ngoài cùng (để biết chắc vị trí mà che). Cột nhạy cảm được dùng tự do
để lọc/ghép/nhóm/sắp xếp. Mọi trường hợp khác (biểu thức, hàm, subquery, CTE,
UNION, `*`) đều bị từ chối: không xác định được thì không cho chạy.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping

from sqlglot import exp

from mcp_database.guard.parser import ParsedQuery, QueryRejected
from mcp_database.names import norm

REDACTED = "***"

# Mệnh đề của một SELECT nơi cột nhạy cảm chỉ dùng để lọc/ghép/nhóm/sắp xếp.
ALLOWED_CLAUSES = frozenset({"where", "group", "having", "order"})

HINT_BARE = (
    "Chỉ lấy cột nhạy cảm trực tiếp ở SELECT ngoài cùng (có thể đặt alias), hệ thống sẽ tự băm/che; "
    "muốn đếm hoặc lọc theo cột này thì đặt nó ở WHERE/GROUP BY/ORDER BY."
)
HINT_STAR = "Liệt kê rõ tên cột cần lấy thay cho *; cột nhạy cảm sẽ được băm/che tự động."


def plan_masking(parsed: ParsedQuery, sensitive: Mapping[str, str]) -> list[str | None]:
    tables = set(parsed.tables)
    # cột -> {bảng: chế độ}, chỉ giữ bảng thật sự có trong query
    by_column: dict[str, dict[str, str]] = {}
    for key, mode in sensitive.items():
        table, _, column = key.rpartition(".")
        if table in tables:
            by_column.setdefault(column, {})[table] = mode
    if not by_column:
        return []

    root = parsed.expression
    _reject_stars(root, by_column)

    outer = root
    while isinstance(outer, exp.Subquery):
        outer = outer.this
    if not isinstance(outer, exp.Select):
        outer = None  # UNION/INTERSECT/MINUS: không có projection nào được phép

    aliases = _alias_map(root)
    plan: list[str | None] = [None] * len(outer.expressions) if outer is not None else []
    for column in root.find_all(exp.Column):
        mode = _sensitive_mode(column, by_column, aliases, tables)
        if mode is None:
            continue
        name, mode = mode
        position = _projected_position(column, outer)
        if position is None:
            continue
        if position < 0:
            raise QueryRejected(
                f"Cột nhạy cảm {name} nằm ở vị trí không che được (trong biểu thức, hàm, subquery, CTE, "
                "UNION/INTERSECT/MINUS hoặc mệnh đề không hỗ trợ).",
                HINT_BARE,
            )
        if plan[position] != "redact":
            plan[position] = mode
    return plan if any(plan) else []


def apply_masking(rows: list[tuple], plan: list[str | None], hmac_key: bytes) -> list[tuple]:
    if not plan:
        return rows
    return [tuple(_mask(v, m, hmac_key) for v, m in zip(row, plan, strict=True)) for row in rows]


def _mask(value, mode: str | None, key: bytes):
    if value is None or mode is None:
        return value
    if mode == "hash":
        return hmac.new(key, str(value).encode(), hashlib.sha256).hexdigest()[:16]
    return REDACTED


def _reject_stars(root: exp.Expression, by_column: Mapping[str, Mapping[str, str]]) -> None:
    for star in root.find_all(exp.Star):
        holder = star.parent if isinstance(star.parent, exp.Column) else star
        if isinstance(holder.parent, exp.Count):  # COUNT(*) không lộ giá trị
            continue
        sensitive_tables = sorted({t for entries in by_column.values() for t in entries})
        raise QueryRejected(
            f"Không cho dùng * vì bảng {', '.join(sensitive_tables)} có cột nhạy cảm.", HINT_STAR
        )


def _alias_map(root: exp.Expression) -> dict[str, set[str]]:
    """Tên/alias bảng -> tập tên bảng thật. Gộp mọi scope để thà nhận thừa còn hơn sót."""
    aliases: dict[str, set[str]] = {}
    for table in root.find_all(exp.Table):
        if not table.name:
            continue
        real = norm(table.name)
        aliases.setdefault(real, set()).add(real)
        if table.alias:
            aliases.setdefault(norm(table.alias), set()).add(real)
    return aliases


def _sensitive_mode(
    column: exp.Column,
    by_column: Mapping[str, Mapping[str, str]],
    aliases: Mapping[str, set[str]],
    tables: set[str],
) -> tuple[str, str] | None:
    entries = by_column.get(norm(column.name))
    if not entries:
        return None
    qualifier = column.table
    # Không qualifier, hoặc qualifier không phải bảng thật (alias của CTE/subquery):
    # coi như có thể đến từ bất kỳ bảng nào trong query.
    candidates = aliases.get(norm(qualifier), tables) if qualifier else tables
    hits = {t: m for t, m in entries.items() if t in candidates}
    if not hits:
        return None
    mode = "redact" if "redact" in hits.values() else "hash"
    name = ", ".join(f"{t}.{norm(column.name)}" for t in sorted(hits))
    return name, mode


def _projected_position(column: exp.Column, outer: exp.Select | None) -> int | None:
    """None: cột chỉ dùng để lọc/sắp xếp (được phép). >= 0: projection trần thứ i
    của SELECT ngoài cùng. -1: mọi chỗ khác (phải từ chối)."""
    path: list[exp.Expression] = [column]
    node: exp.Expression = column
    while node.parent is not None and not isinstance(node.parent, (exp.Select, exp.SetOperation)):
        node = node.parent
        path.append(node)
    query = node.parent
    if query is None:
        return -1

    clause = node.arg_key
    if clause == "joins":
        # Chỉ điều kiện ON/USING; bảng được JOIN có thể mang PIVOT đưa giá trị ra ngoài.
        return None if len(path) >= 2 and path[-2].arg_key in ("on", "using") else -1
    if clause in ALLOWED_CLAUSES:
        return None
    if clause != "expressions" or not isinstance(query, exp.Select):
        return -1
    # PARTITION BY / ORDER BY trong OVER (...) chỉ dùng để chia nhóm, không đưa giá trị ra.
    for child in path:
        if isinstance(child.parent, exp.Window) and child.arg_key in ("partition_by", "order"):
            return None
    if query is not outer:
        return -1
    if node is column or (isinstance(node, exp.Alias) and node.this is column):
        return node.index
    return -1
