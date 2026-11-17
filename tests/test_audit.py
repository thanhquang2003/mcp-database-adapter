import json
import threading

from mcp_database.audit import AuditLog


def lines(path):
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]


def test_two_events_two_json_lines_with_ts(tmp_path):
    path = tmp_path / "sub" / "dir" / "audit.jsonl"  # thư mục cha chưa có
    log = AuditLog(path)
    log.write({"system": "a", "status": "ok"})
    log.write({"system": "b", "status": "rejected", "reason": "Bảng lớn thiếu điều kiện"})
    events = lines(path)
    assert [e["system"] for e in events] == ["a", "b"]
    assert all(e["ts"].endswith("+00:00") for e in events)
    # ensure_ascii=False: tiếng Việt giữ nguyên trong file
    assert "Bảng lớn" in path.read_text(encoding="utf-8")


def test_appends_to_existing_file(tmp_path):
    path = tmp_path / "audit.jsonl"
    AuditLog(path).write({"n": 1})
    AuditLog(path).write({"n": 2})
    assert [e["n"] for e in lines(path)] == [1, 2]


def test_none_path_is_noop():
    AuditLog(None).write({})


def test_unwritable_path_swallowed_and_warns_once(tmp_path, capsys):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    log = AuditLog(blocker / "audit.jsonl")  # cha là file thường -> không tạo được
    log.write({"a": 1})
    log.write({"a": 2})
    err = capsys.readouterr().err
    assert len(err.strip().splitlines()) == 1
    assert "audit" in err


def test_concurrent_writes_do_not_interleave(tmp_path):
    path = tmp_path / "audit.jsonl"
    log = AuditLog(path)

    def worker(i):
        for j in range(50):
            log.write({"i": i, "j": j, "pad": "x" * 500})

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(lines(path)) == 400


def test_new_file_created_owner_only(tmp_path):
    import os
    import stat
    old = os.umask(0)  # kể cả khi umask cho phép mọi người đọc
    try:
        path = tmp_path / "audit.jsonl"
        AuditLog(path).write({"n": 1})
    finally:
        os.umask(old)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [e["n"] for e in lines(path)] == [1]


def test_existing_file_mode_untouched(tmp_path):
    import os
    import stat
    path = tmp_path / "audit.jsonl"
    path.write_text("", encoding="utf-8")
    os.chmod(path, 0o640)
    AuditLog(path).write({"n": 1})
    assert stat.S_IMODE(path.stat().st_mode) == 0o640
