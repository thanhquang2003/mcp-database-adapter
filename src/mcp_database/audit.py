"""Audit log cục bộ: mỗi sự kiện một dòng JSON, không bao giờ làm hỏng query."""
from __future__ import annotations

import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path


class AuditLog:
    def __init__(self, path: Path | None):
        self._path = Path(path) if path is not None else None
        self._lock = threading.Lock()
        self._warned = False

    def _open(self):
        """Mở để ghi nối; file mới được tạo với quyền 0600 (audit chứa SQL người dùng gửi).

        Quyền của file đã có sẵn được giữ nguyên (không chmod ngầm file của người quản trị).
        """
        try:
            fd = os.open(self._path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            fd = os.open(self._path, os.O_WRONLY | os.O_APPEND)
        else:
            os.fchmod(fd, 0o600)  # umask chỉ bớt quyền; vẫn cố định đúng 0600
        return os.fdopen(fd, "a", encoding="utf-8")

    def write(self, event: dict) -> None:
        if self._path is None:
            return
        record = {"ts": datetime.now(timezone.utc).isoformat(), **event}
        with self._lock:
            try:
                line = json.dumps(record, ensure_ascii=False, default=str)
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._open() as f:
                    f.write(line + "\n")
            except Exception as exc:  # ghi log lỗi không được làm hỏng query
                if not self._warned:
                    self._warned = True
                    print(f"[mcp-database] Không ghi được audit log {self._path} ({type(exc).__name__}); "
                          "bỏ qua các lỗi ghi audit tiếp theo.", file=sys.stderr)
