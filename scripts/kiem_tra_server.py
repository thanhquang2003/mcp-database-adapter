"""Kiểm tra nhanh server MCP: khởi động nó qua stdio như Claude làm, liệt kê tool và gọi list_systems.

Không chạm vào DB. Dùng sau khi cài đặt hoặc sửa systems.yaml để chắc chắn server lên được.

    MCP_DATABASE_CONFIG=/đường/dẫn/systems.yaml python scripts/kiem_tra_server.py

Các biến môi trường khác (user/mật khẩu DB, MCP_DATABASE_HMAC_KEY) phải được đặt như khi chạy thật.
"""
from __future__ import annotations

import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXPECTED_TOOLS = {"list_systems", "get_data_doc", "describe_table", "explain_query", "run_query", "profile_column"}


async def check() -> int:
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


def main() -> int:
    try:
        return asyncio.run(check())
    except BaseException as exc:
        if isinstance(exc, KeyboardInterrupt):
            raise
        print(
            "\nServer không khởi động hoặc không trả lời được. Thông báo lỗi của server (tên biến "
            "môi trường còn thiếu, lỗi cấu hình...) nằm ngay phía trên, ở stderr của nó.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
