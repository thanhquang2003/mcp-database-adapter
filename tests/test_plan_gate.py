import pytest

from mcp_database.config import Guard
from mcp_database.db.base import PlanSummary
from mcp_database.guard.parser import QueryRejected
from mcp_database.guard.plan import check_plan


def test_full_scan_on_large_rejected():
    with pytest.raises(QueryRejected) as e:
        check_plan(PlanSummary(10, 10, ["CDR_HISTORY"], ""), Guard(), {"CDR_HISTORY"})
    assert "CDR_HISTORY" in e.value.reason


def test_full_scan_case_insensitive():
    with pytest.raises(QueryRejected):
        check_plan(PlanSummary(10, 10, ["cdr_history"], ""), Guard(), {"CDR_HISTORY"})


def test_full_scan_on_small_ok():
    check_plan(PlanSummary(10, 10, ["USERS"], ""), Guard(), set())


def test_cost_over_threshold():
    with pytest.raises(QueryRejected) as e:
        check_plan(PlanSummary(100_001, 10, [], ""), Guard(), set())
    assert "100001" in e.value.reason.replace(",", "").replace(".", "")
    check_plan(PlanSummary(100_000, 10, [], ""), Guard(), set())


def test_rows_over_threshold():
    with pytest.raises(QueryRejected):
        check_plan(PlanSummary(10, 5_000_001, [], ""), Guard(), set())
    check_plan(PlanSummary(10, 5_000_000, [], ""), Guard(), set())


def test_unknown_numbers_ok():
    check_plan(PlanSummary(None, None, [], ""), Guard(), set())


def test_guard_override():
    with pytest.raises(QueryRejected):
        check_plan(PlanSummary(11, 1, [], ""), Guard(max_explain_cost=10), set())
