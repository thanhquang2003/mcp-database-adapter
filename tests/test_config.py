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
