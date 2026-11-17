"""Lớp truy cập DB chỉ-đọc."""
from __future__ import annotations

from ..config import SystemProfile
from .base import Connector, get_credentials


def make_connector(profile: SystemProfile) -> Connector:
    get_credentials(profile)  # báo thiếu biến môi trường ngay khi khởi tạo
    if profile.db_type == "oracle":
        from .oracle import OracleConnector
        return OracleConnector(profile)
    from .mariadb import MariaDBConnector
    return MariaDBConnector(profile)
