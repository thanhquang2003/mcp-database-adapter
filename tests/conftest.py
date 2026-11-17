"""Driver DB-API giả để test connector mà không cần database."""
import pytest


class FakeCursor:
    def __init__(self, conn, dict_rows=False):
        self.conn = conn
        self.dict_rows = dict_rows
        self.description = None
        self._rows = []

    def execute(self, sql, params=None):
        self.conn.log.append(("execute", sql, params))
        for needle, exc in self.conn.fail.items():
            if needle in sql:
                raise exc
        for needle, (cols, rows) in self.conn.script.items():
            if needle in sql:
                self.description = [(c,) + (None,) * 6 for c in cols]
                self._rows = list(rows)
                return
        self.description = []
        self._rows = []

    def fetchmany(self, n):
        self.conn.log.append(("fetchmany", n))
        out, self._rows = self._rows[:n], self._rows[n:]
        return out

    def fetchall(self):
        self.conn.log.append(("fetchall",))
        out, self._rows = self._rows, []
        return out

    def fetchone(self):
        self.conn.log.append(("fetchone",))
        if not self._rows:
            return None
        return self._rows.pop(0)

    def close(self):
        pass


class FakeConnection:
    def __init__(self, script=None, fail=None):
        self.script = script or {}
        self.fail = fail or {}
        self.log = []
        self.call_timeout = None
        self.closed = False
        self.kwargs = None

    def cursor(self, *args, **kwargs):
        return FakeCursor(self, dict_rows=bool(args or kwargs))

    def rollback(self):
        self.log.append(("rollback",))

    def commit(self):
        self.log.append(("commit",))

    def close(self):
        self.closed = True
        self.log.append(("close",))

    def statements(self):
        return [e[1] for e in self.log if e[0] == "execute"]


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("T_USER", "u1")
    monkeypatch.setenv("T_PASS", "p1")


@pytest.fixture
def make_profile(tmp_path):
    from mcp_database.config import Guard, SystemProfile

    def _make(db_type):
        return SystemProfile(
            name="t", db_type=db_type, host="h", port=1234, database="d",
            user_env="T_USER", password_env="T_PASS", data_doc=tmp_path / "x.md",
            guard=Guard(), is_replica=True, allow_primary=False)
    return _make
