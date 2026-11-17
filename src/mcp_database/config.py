"""Nạp và kiểm tra systems.yaml."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Mapping

import yaml

from .names import norm

DB_TYPES = ("oracle", "mariadb")
SENSITIVE_MODES = ("hash", "redact")


class ConfigError(Exception):
    pass


def _empty() -> Mapping:
    return MappingProxyType({})


@dataclass(frozen=True)
class TableGuard:
    time_column: str
    max_range_days: int


@dataclass(frozen=True)
class Guard:
    max_rows: int = 1000
    timeout_seconds: int = 30
    max_concurrent: int = 2
    large_table_rows: int = 5_000_000
    max_explain_cost: int = 100_000
    max_explain_rows: int = 5_000_000
    tables: Mapping[str, TableGuard] = field(default_factory=_empty)
    sensitive_columns: Mapping[str, str] = field(default_factory=_empty)


@dataclass(frozen=True)
class SystemProfile:
    name: str
    db_type: Literal["oracle", "mariadb"]
    host: str
    port: int
    database: str
    user_env: str
    password_env: str
    data_doc: Path
    guard: Guard
    is_replica: bool
    allow_primary: bool


@dataclass(frozen=True)
class Config:
    audit_log: Path | None
    systems: Mapping[str, SystemProfile]


def _require(d: Mapping, key: str, typ: type, ctx: str) -> Any:
    if key not in d:
        raise ConfigError(f"Hệ thống '{ctx}': thiếu trường '{key}'")
    return _typed(d[key], typ, key, ctx)


def _typed(value: Any, typ: type, key: str, ctx: str) -> Any:
    ok = isinstance(value, typ) and not (typ is int and isinstance(value, bool))
    if not ok:
        raise ConfigError(f"Hệ thống '{ctx}': trường '{key}' phải là {typ.__name__}")
    return value


SYSTEM_KEYS = ("db_type", "host", "port", "database", "user_env", "password_env",
               "data_doc", "is_replica", "allow_primary", "guard")
GUARD_KEYS = ("max_rows", "timeout_seconds", "max_concurrent", "large_table_rows",
              "max_explain_cost", "max_explain_rows", "tables", "sensitive_columns")
TABLE_KEYS = ("time_column", "max_range_days")


def _check_keys(raw: Mapping, allowed: tuple, level: str, ctx: str) -> None:
    for k in raw:
        if k not in allowed:
            raise ConfigError(
                f"Hệ thống '{ctx}': khóa lạ '{k}' ở mục {level}; "
                f"chỉ chấp nhận: {', '.join(allowed)}")


def _parse_guard(raw: Any, ctx: str) -> Guard:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError(f"Hệ thống '{ctx}': trường 'guard' phải là mapping")
    _check_keys(raw, GUARD_KEYS, "guard", ctx)
    kwargs: dict[str, Any] = {}
    for key in ("max_rows", "timeout_seconds", "max_concurrent", "large_table_rows",
                "max_explain_cost", "max_explain_rows"):
        if key in raw:
            v = _typed(raw[key], int, f"guard.{key}", ctx)
            if v <= 0:
                raise ConfigError(f"Hệ thống '{ctx}': trường 'guard.{key}' phải > 0")
            kwargs[key] = v
    tables: dict[str, TableGuard] = {}
    raw_tables = raw.get("tables") or {}
    if not isinstance(raw_tables, dict):
        raise ConfigError(f"Hệ thống '{ctx}': trường 'guard.tables' phải là mapping")
    for tname, t in raw_tables.items():
        if not isinstance(t, dict):
            raise ConfigError(f"Hệ thống '{ctx}': guard.tables.{tname} phải là mapping")
        _check_keys(t, TABLE_KEYS, f"guard.tables.{tname}", ctx)
        tkey = norm(str(tname))
        if tkey in tables:
            raise ConfigError(
                f"Hệ thống '{ctx}': guard.tables.{tname} trùng tên bảng '{tkey}' sau khi chuẩn hóa")
        days = _require(t, "max_range_days", int, f"{ctx}/guard.tables.{tname}")
        if days <= 0:
            raise ConfigError(
                f"Hệ thống '{ctx}': guard.tables.{tname}.max_range_days phải > 0")
        tables[norm(str(tname))] = TableGuard(
            time_column=norm(_require(t, "time_column", str, f"{ctx}/guard.tables.{tname}")),
            max_range_days=days,
        )
    sensitive: dict[str, str] = {}
    raw_sens = raw.get("sensitive_columns") or {}
    if not isinstance(raw_sens, dict):
        raise ConfigError(
            f"Hệ thống '{ctx}': trường 'guard.sensitive_columns' phải là mapping "
            "dạng 'TABLE.COLUMN: hash|redact'")
    for col, mode in raw_sens.items():
        if mode not in SENSITIVE_MODES:
            raise ConfigError(
                f"Hệ thống '{ctx}': guard.sensitive_columns.{col} có chế độ '{mode}' "
                f"không hợp lệ (chỉ nhận {', '.join(SENSITIVE_MODES)})")
        parts = str(col).split(".")
        if len(parts) < 2:
            raise ConfigError(
                f"Hệ thống '{ctx}': guard.sensitive_columns.{col} phải có dạng TABLE.COLUMN")
        skey = f"{norm(parts[-2])}.{norm(parts[-1])}"
        if skey in sensitive:
            raise ConfigError(
                f"Hệ thống '{ctx}': guard.sensitive_columns.{col} trùng cột '{skey}' sau khi chuẩn hóa")
        sensitive[skey] = mode
    return Guard(**kwargs, tables=MappingProxyType(tables),
                 sensitive_columns=MappingProxyType(sensitive))


def _parse_system(name: str, raw: Any, base: Path) -> SystemProfile:
    if not isinstance(raw, dict):
        raise ConfigError(f"Hệ thống '{name}': cấu hình phải là mapping")
    if "password" in raw:
        raise ConfigError(
            f"Hệ thống '{name}': không được ghi 'password' trực tiếp; "
            "dùng 'password_env' trỏ tới biến môi trường")
    _check_keys(raw, SYSTEM_KEYS, "hệ thống", name)
    db_type = _require(raw, "db_type", str, name)
    if db_type not in DB_TYPES:
        raise ConfigError(
            f"Hệ thống '{name}': db_type '{db_type}' không hợp lệ (chỉ nhận {', '.join(DB_TYPES)})")
    is_replica = _typed(raw.get("is_replica", False), bool, "is_replica", name)
    allow_primary = _typed(raw.get("allow_primary", False), bool, "allow_primary", name)
    if not (is_replica or allow_primary):
        raise ConfigError(
            f"Hệ thống '{name}': phải khai báo is_replica: true (kết nối vào replica) "
            "hoặc allow_primary: true (xác nhận chủ ý đọc từ primary)")
    port = _require(raw, "port", int, name)
    if not 1 <= port <= 65535:
        raise ConfigError(f"Hệ thống '{name}': trường 'port' phải trong khoảng 1..65535")
    return SystemProfile(
        name=name,
        db_type=db_type,
        host=_require(raw, "host", str, name),
        port=port,
        database=_require(raw, "database", str, name),
        user_env=_require(raw, "user_env", str, name),
        password_env=_require(raw, "password_env", str, name),
        data_doc=(base / _require(raw, "data_doc", str, name)).resolve(),
        guard=_parse_guard(raw.get("guard"), name),
        is_replica=is_replica,
        allow_primary=allow_primary,
    )


def load_config(path: Path) -> Config:
    path = Path(path)
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise ConfigError(f"Không đọc được file cấu hình {path}: {e}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"File cấu hình {path} không phải YAML hợp lệ: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError("File cấu hình phải là mapping ở cấp cao nhất")
    base = path.resolve().parent
    systems_raw = data.get("systems")
    if not isinstance(systems_raw, dict) or not systems_raw:
        raise ConfigError("Thiếu mục 'systems' (mapping tên hệ thống -> profile)")
    audit = data.get("audit_log")
    if audit is not None and not isinstance(audit, str):
        raise ConfigError("Trường 'audit_log' phải là chuỗi đường dẫn")
    systems = {str(n): _parse_system(str(n), r, base) for n, r in systems_raw.items()}
    return Config(
        audit_log=(base / audit).resolve() if audit else None,
        systems=MappingProxyType(systems),
    )
