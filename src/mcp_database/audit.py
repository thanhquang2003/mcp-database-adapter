"""Audit log cục bộ: mỗi sự kiện một dòng JSON, không bao giờ làm hỏng query."""
from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path


class AuditLog:
    def __init__(self, path: Path | None):
        self._path = Path(path) if path is not None else None
        self._lock = threading.Lock()
        self._warned = False

    def write(self, event: dict) -> None:
        if self._path is None:
            return
        record = {"ts": datetime.now(timezone.utc).isoformat(), **event}
        with self._lock:
            try:
                line = json.dumps(record, ensure_ascii=False, default=str)
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with self._path.open("a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except Exception as exc:  # ghi log lỗi không được làm hỏng query
                if not self._warned:
                    self._warned = True
                    print(f"[mcp-database] Không ghi được audit log {self._path} ({type(exc).__name__}); "
                          "bỏ qua các lỗi ghi audit tiếp theo.", file=sys.stderr)
