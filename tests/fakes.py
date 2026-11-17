"""Driver DB-API giả để test connector mà không cần database."""


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.description = None
        self._rows = []
        self.arraysize = 100
        self.prefetchrows = 2
        self.set_before_execute = None

    def execute(self, sql, params=None):
        self.conn.log.append(("execute", sql, params))
        if self.set_before_execute is None and not sql.startswith(("ALTER SESSION", "SET ")):
            self.set_before_execute = {"arraysize": self.arraysize, "prefetchrows": self.prefetchrows}
        for needle, exc in self.conn.fail.items():
            if needle in sql:
                raise exc
        for needle, (cols, rows) in self.conn.script.items():
            if needle in sql:
                self.description = [(c,) + (None,) * 6 for c in cols]
                self._rows = list(rows)
                return
        self.description = None
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
        self.conn.log.append(("cursor_close",))


class FakeConnection:
    def __init__(self, script=None, fail=None):
        self.script = script or {}
        self.fail = fail or {}
        self.log = []
        self.call_timeout = None
        self.closed = False
        self.cursor_args = []
        self.cursors = []

    def cursor(self, *args, **kwargs):
        self.cursor_args.append(args)
        cur = FakeCursor(self)
        self.cursors.append(cur)
        return cur

    def rollback(self):
        self.log.append(("rollback",))

    def commit(self):
        self.log.append(("commit",))

    def close(self):
        self.closed = True
        self.log.append(("close",))

    def statements(self):
        return [e[1] for e in self.log if e[0] == "execute"]
