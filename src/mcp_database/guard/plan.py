from mcp_database.config import Guard
from mcp_database.db.base import PlanSummary
from mcp_database.guard.parser import QueryRejected
from mcp_database.names import norm


def _fmt(n: int) -> str:
    return f"{n:,}".replace(",", ".")


def check_plan(plan: PlanSummary, guard: Guard, large: set[str]) -> None:
    large_norm = {norm(t) for t in large}
    for table in plan.full_scans:
        if norm(table) in large_norm:
            raise QueryRejected(
                f"Truy vấn quét toàn bộ bảng lớn {norm(table)}.",
                "Thêm điều kiện trên cột thời gian hoặc cột có index để tránh full scan.",
            )
    if plan.cost is not None and plan.cost > guard.max_explain_cost:
        raise QueryRejected(
            f"Chi phí kế hoạch thực thi {_fmt(plan.cost)} vượt ngưỡng {_fmt(guard.max_explain_cost)}.",
            "Thu hẹp khoảng thời gian, thêm điều kiện lọc chọn lọc hơn, hoặc dùng truy vấn tổng hợp.",
        )
    if plan.est_rows is not None and plan.est_rows > guard.max_explain_rows:
        raise QueryRejected(
            f"Số dòng ước tính {_fmt(plan.est_rows)} vượt ngưỡng {_fmt(guard.max_explain_rows)}.",
            "Thu hẹp khoảng thời gian, thêm điều kiện lọc chọn lọc hơn, hoặc dùng truy vấn tổng hợp.",
        )
