"""Kiểm tra nhanh server MCP: khởi động nó qua stdio như Claude làm, liệt kê tool và gọi list_systems.

Không chạm vào DB. Dùng sau khi cài đặt hoặc sửa systems.yaml để chắc chắn server lên được.

    MCP_DATABASE_CONFIG=/đường/dẫn/systems.yaml python scripts/kiem_tra_server.py

Các biến môi trường khác (user/mật khẩu DB, MCP_DATABASE_HMAC_KEY) phải được đặt như khi chạy thật.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from mcp_database.config import ConfigError, load_config
from mcp_database.server import CONFIG_ENV, DEFAULT_CONFIG, _check_env

EXPECTED_TOOLS = {"list_systems", "get_data_doc", "describe_table", "explain_query", "run_query", "profile_column"}


def precheck() -> int:
    """Kiểm tra cấu hình và biến môi trường ngay trong tiến trình này, trước khi chạy server.

    Làm bước này riêng vì thông báo lỗi từ stderr của tiến trình con không phải lúc nào cũng hiện ra
    (nhất là trên Windows), nên không thể chỉ trông vào nó để biết thiếu biến nào.
    """
    path = Path(os.environ.get(CONFIG_ENV) or DEFAULT_CONFIG)
    try:
        config = load_config(path)
    except ConfigError as exc:
        print(f"Cấu hình lỗi: {exc}", file=sys.stderr)
        return 1
    except Exception:
        print(f"Không đọc được file cấu hình: {path.resolve()}", file=sys.stderr)
        return 1
    try:
        _check_env(config)  # tự in TÊN biến còn thiếu (không in giá trị) rồi thoát mã 1
    except SystemExit as exc:
        return int(exc.code or 1)
    print(f"Cấu hình hợp lệ: {path.resolve()} ({len(config.systems)} hệ thống: {', '.join(config.systems)})")
    return 0


async def check() -> int:
    if (code := precheck()) != 0:
        return code
    params = StdioServerParameters(command=sys.executable, args=["-m", "mcp_database"], env=dict(os.environ))
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = {tool.name for tool in (await session.list_tools()).tools}
        missing = EXPECTED_TOOLS - tools
        if missing:
            print(f"THIẾU tool: {', '.join(sorted(missing))}", file=sys.stderr)
            return 1
        result = await session.call_tool("list_systems", {})
        print("Tool:", ", ".join(sorted(tools)))
        for block in result.content:
            print("list_systems:", getattr(block, "text", block))
    return 0


def _leaf_errors(exc: BaseException) -> list[BaseException]:
    """Trải phẳng ExceptionGroup (anyio gói lỗi vào nhóm) để thấy nguyên nhân thật."""
    if isinstance(exc, BaseExceptionGroup):
        return [leaf for sub in exc.exceptions for leaf in _leaf_errors(sub)]
    return [exc]


def main() -> int:
    try:
        return asyncio.run(check())
    except BaseException as exc:
        if isinstance(exc, KeyboardInterrupt):
            raise
        print("\nKhông kết nối được tới server. Chi tiết phía client:", file=sys.stderr)
        for leaf in _leaf_errors(exc):
            print(f"  - {type(leaf).__name__}: {leaf}", file=sys.stderr)
        print(
            "Thông báo lỗi của chính server (tên biến môi trường còn thiếu, lỗi cấu hình...), nếu có, "
            "nằm phía trên. Không thấy gì phía trên thì kiểm tra: đã đặt MCP_DATABASE_CONFIG chưa, "
            "file systems.yaml có tồn tại không, và biến môi trường có được đặt TRONG CÙNG cửa sổ "
            "terminal đang chạy script này không.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
