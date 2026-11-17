from datetime import date, datetime
from decimal import Decimal

import pytest

from mcp_database.db.base import DatabaseError, normalize_cell
from mcp_database.db import make_connector


def test_decimal_int_and_float():
    v = normalize_cell(Decimal("3"))
    assert v == 3 and isinstance(v, int)
    v = normalize_cell(Decimal("3.5"))
    assert v == 3.5 and isinstance(v, float)


def test_datetime_and_date():
    assert normalize_cell(datetime(2026, 1, 2, 3, 4, 5)) == "2026-01-02T03:04:05"
    assert normalize_cell(date(2026, 1, 2)) == "2026-01-02"


def test_bytes_lob_none_and_long_string():
    assert normalize_cell(b"abc") == "<binary 3 bytes>"
    assert normalize_cell(None) is None

    class Lob:
        def read(self, offset=1, amount=None):
            return "abc"
    assert normalize_cell(Lob()) == "abc"
    out = normalize_cell("x" * 5000)
    assert len(out) <= 2010 and out.endswith("…[cắt]")


def test_database_error_hides_driver_text():
    e = DatabaseError(1234)
    assert e.code == 1234
    assert "1234" in str(e)
    assert e.args == (str(e),)


def test_make_connector_missing_env_names_variable_not_value(monkeypatch, make_profile):
    monkeypatch.delenv("T_USER", raising=False)
    monkeypatch.setenv("T_PASS", "hunter2")
    with pytest.raises(Exception) as ei:
        make_connector(make_profile("mariadb"))
    assert "T_USER" in str(ei.value) and "hunter2" not in str(ei.value)


def test_make_connector_picks_type(creds, make_profile):
    from mcp_database.db.mariadb import MariaDBConnector
    from mcp_database.db.oracle import OracleConnector
    assert isinstance(make_connector(make_profile("mariadb")), MariaDBConnector)
    assert isinstance(make_connector(make_profile("oracle")), OracleConnector)


def test_nan_inf_not_returned_as_float():
    assert normalize_cell(float("nan")) == "nan"
    assert normalize_cell(float("inf")) == "inf"


def test_clob_read_bounded():
    calls = []

    class Clob:
        def read(self, offset=1, amount=None):
            calls.append((offset, amount))
            return "x" * min(amount, 10**9)

    out = normalize_cell(Clob())
    assert calls == [(1, 2001)]
    assert len(out) <= 2010 and out.endswith("…[cắt]")


def test_blob_uses_size_without_reading():
    class T:
        name = "DB_TYPE_BLOB"

    class Blob:
        type = T()

        def read(self, *a):
            raise AssertionError("không được đọc BLOB")

        def size(self):
            return 123456
    assert normalize_cell(Blob()) == "<binary 123456 bytes>"
