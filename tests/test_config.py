import textwrap

import pytest

from mcp_database.config import ConfigError, load_config
from mcp_database.names import norm

VALID = """
audit_log: audit.jsonl
systems:
  billing:
    db_type: oracle
    host: db1.local
    port: 1521
    database: BILLPDB
    user_env: BILLING_USER
    password_env: BILLING_PASS
    data_doc: docs/billing.md
    is_replica: true
    guard:
      tables:
        app.cdr_history:
          time_column: call_time
          max_range_days: 7
      sensitive_columns:
        cdr_history.msisdn: hash
        cdr_history.imsi: redact
"""

NO_REPLICA = """
systems:
  billing:
    db_type: oracle
    host: h
    port: 1521
    database: S
    user_env: U
    password_env: P
    data_doc: docs/b.md
"""


def write_yaml(tmp_path, text):
    p = tmp_path / "systems.yaml"
    p.write_text(textwrap.dedent(text), encoding="utf-8")
    return p


def variant(old, new):
    assert old in VALID
    return VALID.replace(old, new)


def test_norm():
    assert norm('"Cdr_History"') == "CDR_HISTORY"
    assert norm("app.cdr_history") == "CDR_HISTORY"
    assert norm("`db`.`t`") == "T"


def test_load_valid(tmp_path):
    cfg = load_config(write_yaml(tmp_path, VALID))
    s = cfg.systems["billing"]
    assert s.guard.max_rows == 1000 and s.guard.timeout_seconds == 30
    assert s.guard.max_concurrent == 2 and s.guard.large_table_rows == 5_000_000
    assert s.guard.tables["CDR_HISTORY"].time_column == "CALL_TIME"
    assert s.guard.tables["CDR_HISTORY"].max_range_days == 7
    assert s.guard.sensitive_columns["CDR_HISTORY.MSISDN"] == "hash"
    assert s.guard.sensitive_columns["CDR_HISTORY.IMSI"] == "redact"
    assert s.data_doc == (tmp_path / "docs/billing.md").resolve()
    assert s.is_replica and not s.allow_primary
    assert cfg.audit_log == (tmp_path / "audit.jsonl").resolve()


def test_requires_replica_or_ack(tmp_path):
    with pytest.raises(ConfigError, match="replica"):
        load_config(write_yaml(tmp_path, NO_REPLICA))


def test_primary_with_ack_ok(tmp_path):
    cfg = load_config(write_yaml(tmp_path, NO_REPLICA + "    allow_primary: true\n"))
    assert cfg.systems["billing"].allow_primary


def test_bad_sensitive_mode(tmp_path):
    with pytest.raises(ConfigError, match="billing"):
        load_config(write_yaml(tmp_path, variant(": hash", ": encrypt")))


def test_bad_db_type(tmp_path):
    with pytest.raises(ConfigError, match="db_type"):
        load_config(write_yaml(tmp_path, variant("oracle", "postgres")))


def test_password_literal_rejected(tmp_path):
    with pytest.raises(ConfigError, match="biến môi trường"):
        load_config(write_yaml(tmp_path, variant("    port: 1521\n", "    port: 1521\n    password: x\n")))


@pytest.mark.parametrize("old,new,key", [
    ("    is_replica: true\n", "    is_replica: true\n    gaurd: {}\n", "gaurd"),
    ("    guard:\n", "    guard:\n      max_row: 5\n", "max_row"),
    ("      sensitive_columns:\n", "      sensitive_column:\n        a.b: hash\n      sensitive_columns:\n", "sensitive_column"),
    ("          max_range_days: 7\n", "          max_range_days: 7\n          time_col: x\n", "time_col"),
])
def test_unknown_keys_rejected(tmp_path, old, new, key):
    with pytest.raises(ConfigError, match=key) as e:
        load_config(write_yaml(tmp_path, variant(old, new)))
    assert "billing" in str(e.value)


def test_duplicate_table_rejected(tmp_path):
    text = variant("      sensitive_columns:\n",
                   "        CDR_HISTORY:\n          time_column: t\n          max_range_days: 1\n"
                   "      sensitive_columns:\n")
    with pytest.raises(ConfigError, match="trùng"):
        load_config(write_yaml(tmp_path, text))


def test_duplicate_sensitive_rejected(tmp_path):
    text = variant("        cdr_history.imsi: redact\n",
                   "        cdr_history.imsi: redact\n        A.CDR_HISTORY.MSISDN: redact\n")
    with pytest.raises(ConfigError, match="trùng"):
        load_config(write_yaml(tmp_path, text))


def test_max_range_days_positive(tmp_path):
    with pytest.raises(ConfigError, match="max_range_days"):
        load_config(write_yaml(tmp_path, variant("max_range_days: 7", "max_range_days: 0")))


@pytest.mark.parametrize("port", ["0", "70000"])
def test_port_range(tmp_path, port):
    with pytest.raises(ConfigError, match="port"):
        load_config(write_yaml(tmp_path, variant("port: 1521", f"port: {port}")))
