# MCP Database dùng chung — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Một MCP server (stdio) cho Claude đọc DB Oracle/MariaDB của nhiều hệ thống theo profile, với lớp kiểm soát query để không ảnh hưởng production.

**Architecture:** `config` nạp `systems.yaml` thành profile. `QueryService` chạy pipeline kiểm tra (parse → che cột nhạy cảm → chính sách bảng lớn/thời gian → giới hạn dòng → EXPLAIN → thực thi có timeout) trên một `Connector` trừu tượng (Oracle, MariaDB). `server.py` bọc `QueryService` thành các tool MCP. Mọi kiểm tra là hàm thuần, test bằng connector giả; chỉ lớp connector cần DB thật cho test tích hợp (tùy chọn).

**Tech Stack:** Python ≥3.11, `mcp` (FastMCP, stdio), `sqlglot`, `PyYAML`, `oracledb` (thin mode), `PyMySQL`, `pytest`.

**Spec:** `docs/superpowers/specs/2026-11-17-mcp-database-design.md`

## Global Constraints

- Python; `python-oracledb` chế độ thin (không cần Oracle client); parse SQL bằng `sqlglot` (dialect `oracle` / `mysql`).
- Chạy qua stdio, không mở cổng mạng.
- Thông tin đăng nhập chỉ tham chiếu bằng tên biến môi trường, không nằm trong file cấu hình.
- Chỉ cho phép đúng một câu `SELECT` hoặc `WITH ... SELECT`; chặn nhiều câu lệnh, DDL, DML, `FOR UPDATE`, hàm có tác dụng phụ.
- Kết nối vào replica; nếu profile không phải replica thì bắt buộc cờ xác nhận rủi ro `allow_primary: true`, thiếu cả hai thì lỗi cấu hình.
- Mặc định `guard`: `max_rows: 1000`, `timeout_seconds: 30`, `max_concurrent: 2`, `large_table_rows: 5000000`.
- Timeout phía DB: Oracle `call_timeout`, MariaDB `max_statement_time`. Giới hạn dòng: `FETCH FIRST n ROWS ONLY` (Oracle) / `LIMIT n` (MariaDB).
- Cột nhạy cảm: định danh → HMAC, tên/địa chỉ → che hoàn toàn; không xác định được thì từ chối query.
- Mọi `run_query` ghi log: hệ thống, SQL, thời gian chạy, số dòng, kết quả kiểm tra. Không ghi nội dung dòng kết quả.
- Thông báo từ chối viết bằng tiếng Việt, có gợi ý cách sửa. Tên bảng/cột so sánh không phân biệt hoa thường, bỏ dấu nháy và schema.

## Review Focus

Các đầu vào mà spec ngụ ý nhưng không có task nào tự nhiên kiểm tra; mỗi dòng đã được gắn test vào task sở hữu code:

1. Câu lệnh "lén" nhiều câu: `SELECT 1; DROP TABLE x`, `;` ở cuối câu hợp lệ (phải chạy được), `;` nằm trong comment/chuỗi (không được hiểu nhầm) — Task 2.
2. Cú pháp riêng của Oracle mà `sqlglot` có thể không parse: hint `/*+ PARALLEL */` (phải bị gỡ vì kéo tải), `CONNECT BY`, `(+)`; lỗi parse phải thành thông báo từ chối rõ ràng chứ không phải exception — Task 2.
3. Tên bảng viết khác hình thức để né khai báo: `schema.cdr_history`, `"CDR_HISTORY"`, `cdr_history c` — Task 4.
4. Điều kiện thời gian "giả": nằm trong `OR`, bọc hàm `TRUNC(call_time)`, đặt trên cột khác, khoảng quá dài, chỉ có cận dưới nhưng quá xa — Task 4.
5. Rò cột nhạy cảm: `SELECT *`, `msisdn || 'x'`, `MAX(msisdn)`, alias, CTE/subquery truyền qua, `UNION` — Task 3.

---

### Task 1: Scaffold và nạp cấu hình

**Files:**
- Create: `pyproject.toml`, `.gitignore` (loại `systems.yaml`, `*.jsonl`, `.venv`, `__pycache__`), `src/mcp_database/__init__.py`, `src/mcp_database/names.py`, `src/mcp_database/config.py`, `systems.example.yaml`, `tests/test_config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces (`names.py`): `norm(name: str) -> str` — bỏ dấu nháy `"` và backtick, bỏ phần schema (`a.b` → `B`), viết hoa.
- Produces (`config.py`):
  - `class ConfigError(Exception)`
  - `@dataclass(frozen=True) TableGuard(time_column: str, max_range_days: int)`
  - `@dataclass(frozen=True) Guard(max_rows=1000, timeout_seconds=30, max_concurrent=2, large_table_rows=5_000_000, max_explain_cost=100_000, max_explain_rows=5_000_000, tables: Mapping[str, TableGuard] = {}, sensitive_columns: Mapping[str, str] = {})` — key của `tables` là `norm(table)`; key của `sensitive_columns` là `"TABLE.COLUMN"` đã chuẩn hóa, giá trị là `"hash"` hoặc `"redact"`.
  - `@dataclass(frozen=True) SystemProfile(name: str, db_type: Literal["oracle","mariadb"], host: str, port: int, database: str, user_env: str, password_env: str, data_doc: Path, guard: Guard, is_replica: bool, allow_primary: bool)` — `database` là service name (Oracle) hoặc tên database (MariaDB); `data_doc` đã resolve tuyệt đối theo thư mục chứa file cấu hình.
  - `@dataclass(frozen=True) Config(audit_log: Path | None, systems: Mapping[str, SystemProfile])`
  - `load_config(path: Path) -> Config`

- [ ] **Step 1: Tạo môi trường và `pyproject.toml`** — package `mcp_database` layout `src/`, dependencies như Tech Stack, extra `dev = [pytest]`; chạy `git init`, `python -m venv .venv`, `pip install -e ".[dev]"`. Sau khi cài, ghi lại phiên bản đã cài thành ràng buộc `>=` trong `pyproject.toml`.
- [ ] **Step 2: Viết test thất bại** trong `tests/test_config.py`:

```python
def test_norm():
    assert norm('"Cdr_History"') == "CDR_HISTORY"
    assert norm("app.cdr_history") == "CDR_HISTORY"

def test_load_valid(tmp_path):            # YAML đầy đủ 1 hệ thống oracle, guard có tables + sensitive_columns
    cfg = load_config(write_yaml(tmp_path, VALID))
    s = cfg.systems["billing"]
    assert s.guard.max_rows == 1000 and s.guard.tables["CDR_HISTORY"].time_column == "CALL_TIME"
    assert s.guard.sensitive_columns["CDR_HISTORY.MSISDN"] == "hash"
    assert s.data_doc == (tmp_path / "docs/billing.md").resolve()

def test_requires_replica_or_ack(tmp_path):    # is_replica và allow_primary đều vắng
    with pytest.raises(ConfigError, match="replica"): load_config(write_yaml(tmp_path, NO_REPLICA))

def test_primary_with_ack_ok(tmp_path): ...    # allow_primary: true → load được
def test_bad_sensitive_mode(tmp_path):         # mode "encrypt" → ConfigError
def test_bad_db_type(tmp_path):                # db_type "postgres" → ConfigError
def test_password_literal_rejected(tmp_path):  # key "password" xuất hiện → ConfigError "biến môi trường"
```

- [ ] **Step 3: Chạy `pytest tests/test_config.py -v`**, kỳ vọng FAIL (module chưa có).
- [ ] **Step 4: Cài đặt `names.norm` và `config.load_config`** theo Interfaces. Schema `systems.yaml`: `audit_log` (tùy chọn) và `systems` là mapping tên → profile (`db_type, host, port, database, user_env, password_env, data_doc, is_replica, allow_primary, guard{...}`); `sensitive_columns` trong YAML là mapping `"TABLE.COLUMN": hash|redact` (khác ví dụ danh sách trong spec để phân biệt hai chế độ). Mọi lỗi thành `ConfigError` kèm tên hệ thống và tên trường.
- [ ] **Step 5: Viết `systems.example.yaml`** có 1 hệ thống Oracle và 1 MariaDB, comment tiếng Việt giải thích từng trường.
- [ ] **Step 6: Chạy `pytest tests/test_config.py -v`**, kỳ vọng PASS.
- [ ] **Step 7: Commit** `feat: scaffold project and config loader`.

---

### Task 2: Kiểm tra và chuẩn hóa SQL

**Files:**
- Create: `src/mcp_database/guard/__init__.py`, `src/mcp_database/guard/parser.py`
- Test: `tests/test_parser.py`

**Interfaces:**
- Consumes: `names.norm`.
- Produces:
  - `class QueryRejected(Exception)` với `__init__(self, reason: str, hint: str | None = None)`, thuộc tính `reason`, `hint`.
  - `@dataclass(frozen=True) ParsedQuery(expression: sqlglot.Expression, dialect: str, sql: str, tables: tuple[str, ...])` — `sql` là SQL đã gỡ comment và hint, sinh lại theo dialect; `tables` là tên bảng thật đã `norm`, không gồm tên CTE.
  - `parse_select(sql: str, db_type: str) -> ParsedQuery`

- [ ] **Step 1: Viết test thất bại** trong `tests/test_parser.py`, tham số hóa theo `db_type` (`oracle`, `mariadb`):

```python
OK = ["SELECT a FROM t", "select a from t;", "WITH x AS (SELECT a FROM t) SELECT a FROM x",
      "SELECT a FROM t -- ; DROP TABLE t", "SELECT ';' AS s FROM t", "SELECT a FROM t /* ; */"]
@pytest.mark.parametrize("sql", OK)
def test_accepts(sql, db_type): assert parse_select(sql, db_type).tables[0] in ("T",)

BAD = ["SELECT 1; DROP TABLE t", "DELETE FROM t", "UPDATE t SET a=1", "INSERT INTO t VALUES (1)",
       "DROP TABLE t", "TRUNCATE TABLE t", "SELECT a FROM t FOR UPDATE", "SELECT a INTO x FROM t",
       "CREATE TABLE x AS SELECT 1 FROM dual", "BEGIN NULL; END;", "SELECT dbms_lock.sleep(5) FROM dual",
       "SELECT utl_http.request('http://x') FROM dual"]
def test_rejects(sql): with pytest.raises(QueryRejected)

def test_mariadb_blocked_functions():  # SLEEP, BENCHMARK, LOAD_FILE, GET_LOCK, INTO OUTFILE
def test_oracle_hint_stripped():
    assert "PARALLEL" not in parse_select("SELECT /*+ PARALLEL(t 8) */ a FROM t", "oracle").sql.upper()
def test_cte_name_not_a_table():
    assert parse_select("WITH x AS (SELECT a FROM t) SELECT a FROM x", "oracle").tables == ("T",)
def test_schema_and_quotes_normalized():
    assert parse_select('SELECT a FROM app."Cdr_History" c', "oracle").tables == ("CDR_HISTORY",)
def test_unparseable_is_rejection_not_crash():   # "SELECT a FROM t CONNECT BY PRIOR a = b" và "SELECT a FROM t, u WHERE t.id = u.id(+)"
    # kết quả: hoặc parse được, hoặc QueryRejected có hint; không bao giờ ném exception khác
def test_rejection_has_hint(): assert QueryRejected_for("DELETE FROM t").hint
```

- [ ] **Step 2: Chạy `pytest tests/test_parser.py -v`**, kỳ vọng FAIL.
- [ ] **Step 3: Cài đặt `parse_select`** — dialect map `oracle→"oracle"`, `mariadb→"mysql"`. Dùng `sqlglot.parse(sql, read=dialect)`; bỏ phần tử `None` do `;` cuối; đòi đúng 1 statement; root phải là `Select`/`Union`/`Subquery` có gốc là select (WITH nằm trong `Select`). Duyệt toàn cây, từ chối: node thuộc `Insert, Update, Delete, Merge, Create, Drop, Alter, Command, TruncateTable, Into, Lock` (FOR UPDATE), `exp.Hint` thì gỡ bỏ thay vì từ chối; hàm có tên bắt đầu `DBMS_`/`UTL_`/`SYS.`, hàm có tiền tố schema/package (dạng `a.b(...)`), và tập chặn MariaDB (`SLEEP, BENCHMARK, LOAD_FILE, GET_LOCK, RELEASE_LOCK, SLEEP`). `sqlglot.errors.ParseError` / `TokenError` → `QueryRejected` với hint "viết lại bằng cú pháp SELECT chuẩn". `sql` sinh bằng `.sql(dialect=dialect, comments=False)`.
- [ ] **Step 4: Chạy test**, kỳ vọng PASS. Nếu một câu trong `OK` hoặc `BAD` hành xử khác do hạn chế của `sqlglot` ở phiên bản đã cài, sửa mã (không nới test) trừ khi test sai về mặt dialect.
- [ ] **Step 5: Commit** `feat: SQL validator`.

---

### Task 3: Che cột nhạy cảm

**Files:**
- Create: `src/mcp_database/masking.py`
- Test: `tests/test_masking.py`

**Interfaces:**
- Consumes: `ParsedQuery`, `QueryRejected`, `norm`.
- Produces:
  - `plan_masking(parsed: ParsedQuery, sensitive: Mapping[str, str]) -> list[str | None]` — phần tử i là `None | "hash" | "redact"` cho cột kết quả thứ i; ném `QueryRejected`.
  - `apply_masking(rows: list[tuple], plan: list[str | None], hmac_key: bytes) -> list[tuple]` — `hash`: `hmac.new(key, str(v).encode(), sha256).hexdigest()[:16]`; `redact`: `"***"`; `None` giữ nguyên; giá trị NULL giữ NULL.

Quy tắc `plan_masking` (đây là thuật toán quyết định, test bên dưới ràng buộc nó): một cột được coi là nhạy cảm khi tên cột khớp mục trong `sensitive` và bảng của nó (qua alias, hoặc nếu không có qualifier thì bất kỳ bảng nào trong `parsed.tables`) khớp. Cột nhạy cảm được phép: (a) là projection trần ở SELECT ngoài cùng (có thể đổi alias) → gắn chế độ mask theo vị trí; (b) xuất hiện trong WHERE/JOIN ON/GROUP BY/ORDER BY/HAVING. Mọi chỗ khác → từ chối: nằm trong hàm/biểu thức/CASE/toán tử `||`, projection của subquery hoặc CTE, `UNION`/`INTERSECT`/`EXCEPT`, và `SELECT *` hoặc `t.*` khi có bảng nào trong query có cột nhạy cảm.

- [ ] **Step 1: Viết test thất bại** (sensitive = `{"CDR.MSISDN": "hash", "USERS.FULL_NAME": "redact"}`, helper `P(sql)` = `parse_select(sql, "oracle")`):

```python
def test_bare_columns_mapped_by_position():
    assert plan_masking(P("SELECT c.msisdn AS m, status, COUNT(*) n FROM cdr c GROUP BY c.msisdn, status"), S) == ["hash", None, None]
def test_allowed_in_where_join_order():
    plan_masking(P("SELECT status FROM cdr c WHERE c.msisdn = '8491' ORDER BY c.msisdn"), S)
@pytest.mark.parametrize("sql", [
    "SELECT * FROM cdr", "SELECT c.* FROM cdr c", "SELECT msisdn || 'x' FROM cdr",
    "SELECT MAX(msisdn) FROM cdr", "SELECT SUBSTR(msisdn,1,3) FROM cdr",
    "SELECT CASE WHEN msisdn IS NULL THEN 1 ELSE 0 END FROM cdr",
    "WITH x AS (SELECT msisdn FROM cdr) SELECT msisdn FROM x",
    "SELECT m FROM (SELECT msisdn AS m FROM cdr)",
    "SELECT msisdn FROM cdr UNION SELECT msisdn FROM cdr"])
def test_leaks_rejected(sql): with pytest.raises(QueryRejected)
def test_no_sensitive_tables_no_restriction():
    assert plan_masking(P("SELECT * FROM other"), S) == [None] * ... or == []   # SELECT * trên bảng không nhạy cảm: trả list rỗng nghĩa là "không che gì"
def test_apply_masking():
    out = apply_masking([("84912", "a", None)], ["hash", None, "hash"], b"k")
    assert out[0][1] == "a" and out[0][2] is None and len(out[0][0]) == 16 and out[0][0] != "84912"
    assert apply_masking([("84912",)], ["hash"], b"k") == apply_masking([("84912",)], ["hash"], b"k")   # ổn định
    assert apply_masking([("84912",)], ["hash"], b"k") != apply_masking([("84912",)], ["hash"], b"k2")
    assert apply_masking([("Nguyen",)], ["redact"], b"k") == [("***",)]
```

  Quy ước: khi không có cột nhạy cảm nào liên quan, `plan_masking` trả `[]`; `apply_masking` với plan rỗng trả `rows` nguyên.
- [ ] **Step 2: Chạy `pytest tests/test_masking.py -v`**, kỳ vọng FAIL.
- [ ] **Step 3: Cài đặt `plan_masking` và `apply_masking`** theo quy tắc trên, dùng `sqlglot` traversal (`find_all(exp.Column)`, `exp.Star`), không dùng `qualify`.
- [ ] **Step 4: Chạy test**, kỳ vọng PASS.
- [ ] **Step 5: Commit** `feat: sensitive column masking`.

---

### Task 4: Chính sách bảng lớn, điều kiện thời gian, giới hạn dòng

**Files:**
- Create: `src/mcp_database/guard/policy.py`
- Test: `tests/test_policy.py`

**Interfaces:**
- Consumes: `ParsedQuery`, `QueryRejected`, `Guard`, `TableGuard`, `norm`.
- Produces:
  - `large_tables(parsed: ParsedQuery, guard: Guard, table_rows: Mapping[str, int | None]) -> set[str]` — bảng (đã `norm`) nằm trong `guard.tables` hoặc có `rows >= guard.large_table_rows`; `rows is None` thì không coi là lớn (đã quyết: phần còn lại do cổng EXPLAIN bắt).
  - `enforce_time_range(parsed: ParsedQuery, guard: Guard, large: set[str], today: date) -> None`
  - `apply_row_limit(parsed: ParsedQuery, max_rows: int) -> str` — trả SQL cuối; nếu query đã có LIMIT/FETCH thì lấy `min`.

Quy tắc `enforce_time_range`: với mỗi `Select` có bảng lớn T trực tiếp ở FROM/JOIN, bảng đó phải có `time_column` trong `guard.tables` (nếu không → từ chối, hint "khai báo guard.tables.T.time_column"), và các nhánh AND cấp cao nhất của WHERE của chính `Select` đó phải chứa điều kiện so sánh **cột trần** `time_column` (qua alias hoặc không qualifier) tạo cận dưới (`>`, `>=`, `BETWEEN`) và cận trên (`<`, `<=`, `BETWEEN`). Nhánh nằm trong `OR`, cột bọc hàm, hay cột khác không được tính. Cận được tính ra `date` khi là: literal chuỗi ISO (`'2026-01-01'`, `'2026-01-01 10:00:00'`), `DATE '...'`, `TIMESTAMP '...'`, `TO_DATE('...', 'YYYY-MM-DD' | 'YYYY-MM-DD HH24:MI:SS' | 'YYYYMMDD')`, hoặc `SYSDATE`/`NOW()`/`CURRENT_DATE`/`CURDATE()`/`TRUNC(SYSDATE)` có thể cộng/trừ `n` (ngày, Oracle) hoặc `INTERVAL n DAY` (cả hai). Dạng khác → từ chối, hint "dùng literal ngày hoặc SYSDATE/NOW() ± n ngày". Khoảng = cận trên − cận dưới phải `<= max_range_days`. Chỉ có cận dưới thì ngầm hiểu cận trên là `today`.

- [ ] **Step 1: Viết test thất bại** (guard: `CDR_HISTORY` time_column `CALL_TIME`, `max_range_days=31`; `today=date(2026,11,17)`; helper `E(sql, large={"CDR_HISTORY"})`):

```python
GOOD = [
 "SELECT COUNT(*) FROM cdr_history WHERE call_time >= DATE '2026-11-01' AND call_time < DATE '2026-11-10'",
 "SELECT COUNT(*) FROM cdr_history c WHERE c.call_time BETWEEN TO_DATE('20261101','YYYYMMDD') AND TO_DATE('20261110','YYYYMMDD')",
 "SELECT COUNT(*) FROM cdr_history WHERE call_time >= SYSDATE - 7",
 "SELECT COUNT(*) FROM app.\"CDR_HISTORY\" WHERE \"CALL_TIME\" >= '2026-11-10' AND status = 1",
 "WITH x AS (SELECT * FROM cdr_history WHERE call_time >= DATE '2026-11-10') SELECT COUNT(*) FROM x"]
BAD = [
 "SELECT COUNT(*) FROM cdr_history",                                                # không có điều kiện
 "SELECT COUNT(*) FROM cdr_history WHERE status = 1",                               # cột khác
 "SELECT COUNT(*) FROM cdr_history WHERE call_time >= DATE '2026-11-10' OR 1 = 1",  # OR
 "SELECT COUNT(*) FROM cdr_history WHERE TRUNC(call_time) >= DATE '2026-11-10'",    # bọc hàm
 "SELECT COUNT(*) FROM cdr_history WHERE call_time >= DATE '2026-01-01' AND call_time < DATE '2026-11-01'",  # quá dài
 "SELECT COUNT(*) FROM cdr_history WHERE call_time >= DATE '2026-01-01'",           # cận dưới quá xa, ngầm hiểu tới today
 "SELECT COUNT(*) FROM cdr_history WHERE call_time < DATE '2026-11-10'",            # chỉ cận trên
 "SELECT COUNT(*) FROM cdr_history WHERE call_time >= some_col_expr(1)",            # cận không tính được
 "SELECT COUNT(*) FROM cdr_history c JOIN users u ON u.id = c.uid"]                 # join, không có WHERE thời gian
def test_other_table_unrestricted(): enforce_time_range(P("SELECT COUNT(*) FROM users"), G, set(), TODAY)
def test_large_table_without_time_column_config():    # large={"BIG"} nhưng BIG không có trong guard.tables → QueryRejected hint "khai báo"
def test_large_tables_detection():
    assert large_tables(P("SELECT 1 FROM a JOIN cdr_history b ON 1=1"), G, {"A": 10, "CDR_HISTORY": None}) == {"CDR_HISTORY"}
    assert large_tables(P("SELECT 1 FROM a"), G, {"A": 9_000_000}) == {"A"}
    assert large_tables(P("SELECT 1 FROM a"), G, {"A": None}) == set()
def test_limit_oracle():  assert "FETCH FIRST 1000 ROWS ONLY" in apply_row_limit(P("SELECT a FROM t"), 1000).upper()
def test_limit_mariadb(): assert apply_row_limit(PM("SELECT a FROM t"), 1000).upper().endswith("LIMIT 1000")
def test_limit_min_of_existing(): assert "50" in apply_row_limit(PM("SELECT a FROM t LIMIT 50"), 1000)
def test_limit_cap_existing_larger(): assert "1000" in apply_row_limit(PM("SELECT a FROM t LIMIT 99999"), 1000)
```

- [ ] **Step 2: Chạy `pytest tests/test_policy.py -v`**, kỳ vọng FAIL.
- [ ] **Step 3: Cài đặt ba hàm** theo quy tắc. `apply_row_limit` dùng `expression.limit(n)` của `sqlglot` rồi `.sql(dialect=...)`; với Oracle phải sinh `FETCH FIRST n ROWS ONLY` (kiểm lại bằng test; nếu bản `sqlglot` đã cài sinh `ROWNUM` hay dạng khác thì chỉnh sao cho test ở Step 1 pass).
- [ ] **Step 4: Chạy test**, kỳ vọng PASS.
- [ ] **Step 5: Commit** `feat: large-table time-range policy and row limit`.

---

### Task 5: Connector Oracle và MariaDB

**Files:**
- Create: `src/mcp_database/db/__init__.py`, `src/mcp_database/db/base.py`, `src/mcp_database/db/oracle.py`, `src/mcp_database/db/mariadb.py`
- Test: `tests/test_db_base.py`, `tests/test_db_plans.py`, `tests/test_db_execute.py`, `tests/integration/test_mariadb.py`, `tests/integration/test_oracle.py`

**Interfaces:**
- Consumes: `SystemProfile`.
- Produces (`base.py`):
  - `@dataclass ColumnInfo(name: str, type: str, nullable: bool)`, `IndexInfo(name: str, columns: list[str], unique: bool)`, `TableInfo(columns: list[ColumnInfo], indexes: list[IndexInfo], estimated_rows: int | None)`
  - `@dataclass PlanSummary(cost: int | None, est_rows: int | None, full_scans: list[str], text: str)` — `full_scans` là tên bảng đã `norm`.
  - `@dataclass QueryResult(columns: list[str], rows: list[tuple], truncated: bool)`
  - `class Connector(Protocol)`: `table_info(table: str) -> TableInfo` (ném `LookupError` nếu bảng không tồn tại); `explain(sql: str) -> PlanSummary`; `execute(sql: str, max_rows: int, timeout_seconds: int) -> QueryResult`
  - `normalize_cell(value: object) -> object` — `Decimal` → `int` nếu nguyên, ngược lại `float`; `datetime/date` → `isoformat()`; `bytes` → `"<binary N bytes>"`; LOB (có `.read()`) → đọc rồi xử lý như `str`; `str` dài hơn 2000 ký tự → cắt còn 2000 kèm `"…[cắt]"`.
  - `make_connector(profile: SystemProfile) -> Connector` (trong `db/__init__.py`, chọn theo `db_type`; đọc user/password từ `os.environ[profile.user_env/password_env]`).
- Produces (`oracle.py`, `mariadb.py`): `OracleConnector(profile)`, `MariaDBConnector(profile)`; mỗi cuộc gọi mở một kết nối mới và đóng khi xong. Hai hàm thuần để test: `summarize_oracle_plan(rows: list[tuple]) -> PlanSummary` với `rows` là `(operation, options, object_name, cost, cardinality)` theo thứ tự `id`, dòng đầu là gốc; `summarize_mariadb_explain(rows: list[dict]) -> PlanSummary` với khóa `table, type, rows`, `full_scans` = bảng có `type == "ALL"`, `est_rows` = tích các `rows` khác `None`, `cost = None`.

Hành vi `execute`: trước khi chạy đặt chế độ chỉ đọc (Oracle: `SET TRANSACTION READ ONLY`; MariaDB: `SET SESSION TRANSACTION READ ONLY`) và timeout (Oracle: `connection.call_timeout = timeout_seconds * 1000`; MariaDB: `SET SESSION max_statement_time = <timeout_seconds>`); đọc bằng `fetchmany(max_rows + 1)`, nếu trả về nhiều hơn `max_rows` thì cắt và `truncated = True`; mỗi ô qua `normalize_cell`. Timeout của DB được dịch thành `QueryRejected`-không-phải: ném `TimeoutError("query vượt N giây")`. `explain` của Oracle dùng `EXPLAIN PLAN SET STATEMENT_ID = :id FOR <sql>` rồi đọc `PLAN_TABLE`, `text` lấy từ `DBMS_XPLAN.DISPLAY`; của MariaDB dùng `EXPLAIN <sql>`. `table_info` đọc từ `ALL_TAB_COLUMNS`/`ALL_INDEXES`/`ALL_IND_COLUMNS`/`ALL_TABLES.NUM_ROWS` (Oracle, owner mặc định là schema hiện tại) và `information_schema.COLUMNS`/`STATISTICS`/`TABLES.TABLE_ROWS` (MariaDB, database mặc định `DATABASE()`).

- [ ] **Step 1: Viết test thất bại**:
  - `test_db_base.py`: `normalize_cell(Decimal("3"))==3 and isinstance(...,int)`, `Decimal("3.5")==3.5`, `datetime(2026,1,2,3,4,5)` → `"2026-01-02T03:04:05"`, `b"abc"` → `"<binary 3 bytes>"`, chuỗi 5000 ký tự → độ dài ≤ 2010 và kết thúc `"…[cắt]"`, đối tượng có `.read()` trả `"abc"` → `"abc"`, `None` → `None`.
  - `test_db_plans.py`: `summarize_oracle_plan([("SELECT STATEMENT","",None,1200,50),("TABLE ACCESS","FULL","CDR_HISTORY",1200,50)])` cho `cost==1200, est_rows==50, full_scans==["CDR_HISTORY"]`; plan dùng index không có full scan; `summarize_mariadb_explain([{"table":"cdr_history","type":"ALL","rows":1000000},{"table":"u","type":"ref","rows":2}])` cho `est_rows==2000000, full_scans==["CDR_HISTORY"], cost is None`.
  - `test_db_execute.py`: dùng module DB-API giả (monkeypatch `oracledb.connect` / `pymysql.connect`) ghi lại câu lệnh: xác nhận đã gửi lệnh READ ONLY và timeout trước query, `fetchmany` được gọi với `max_rows + 1`, `truncated` đúng khi giả trả `max_rows + 1` dòng, và kết nối được đóng cả khi query ném lỗi.
  - `tests/integration/*`: gắn `@pytest.mark.integration`, bỏ qua trừ khi có `TEST_MARIADB_DSN` / `TEST_ORACLE_DSN` (dạng `user:password@host:port/database`); kiểm tra `table_info` trên một bảng tạo sẵn, `explain` trả `PlanSummary`, `execute` với `SELECT SLEEP(5)`/`dbms_session.sleep` chạy với timeout 1 giây ném `TimeoutError`, và `INSERT` bị DB từ chối. Đăng ký marker `integration` trong `pyproject.toml`.
- [ ] **Step 2: Chạy `pytest tests -v -m "not integration"`**, kỳ vọng FAIL (module chưa có).
- [ ] **Step 3: Cài đặt `base.py`, `oracle.py`, `mariadb.py`, `make_connector`** theo Interfaces và mô tả hành vi.
- [ ] **Step 4: Chạy `pytest tests -v -m "not integration"`**, kỳ vọng PASS.
- [ ] **Step 5: Nếu có DB thử nghiệm** (MariaDB qua Docker hoặc replica thật), chạy `TEST_MARIADB_DSN=... pytest tests/integration -m integration -v`, kỳ vọng PASS. Nếu không có DB, ghi rõ trong báo cáo cuối rằng phần này chưa được chạy thật.
- [ ] **Step 6: Commit** `feat: oracle and mariadb connectors`.

---

### Task 6: Cổng kiểm tra kế hoạch thực thi

**Files:**
- Create: `src/mcp_database/guard/plan.py`
- Test: `tests/test_plan_gate.py`

**Interfaces:**
- Consumes: `PlanSummary`, `Guard`, `QueryRejected`, `norm`.
- Produces: `check_plan(plan: PlanSummary, guard: Guard, large: set[str]) -> None`.

Quy tắc: từ chối nếu `full_scans` giao với `large` (hint: thêm điều kiện trên cột có index / cột thời gian); từ chối nếu `plan.cost` không `None` và `> guard.max_explain_cost`; từ chối nếu `plan.est_rows` không `None` và `> guard.max_explain_rows`. Thông báo nêu bảng hoặc con số cụ thể và ngưỡng.

- [ ] **Step 1: Viết test thất bại**:

```python
def test_full_scan_on_large_rejected():  PlanSummary(10, 10, ["CDR_HISTORY"], "") với large={"CDR_HISTORY"} → QueryRejected, "CDR_HISTORY" in reason
def test_full_scan_on_small_ok():        full_scans=["USERS"], large=set() → không ném
def test_cost_over_threshold():          cost=100_001 → QueryRejected; cost=100_000 → ok
def test_rows_over_threshold():          est_rows=5_000_001 → QueryRejected
def test_unknown_numbers_ok():           cost=None, est_rows=None, full_scans=[] → ok
def test_guard_override():               Guard(max_explain_cost=10) và cost=11 → QueryRejected
```

- [ ] **Step 2: Chạy `pytest tests/test_plan_gate.py -v`**, kỳ vọng FAIL.
- [ ] **Step 3: Cài đặt `check_plan`.**
- [ ] **Step 4: Chạy test**, kỳ vọng PASS.
- [ ] **Step 5: Commit** `feat: explain plan gate`.

---

### Task 7: Pipeline, giới hạn đồng thời, audit log

**Files:**
- Create: `src/mcp_database/audit.py`, `src/mcp_database/service.py`
- Test: `tests/test_audit.py`, `tests/test_service.py`

**Interfaces:**
- Consumes: mọi thứ ở Task 1–6.
- Produces:
  - `AuditLog(path: Path | None)` với `write(event: dict) -> None` — thêm một dòng JSON (`ts` ISO UTC được tự gắn) vào file, tạo thư mục cha nếu thiếu; `path is None` thì không làm gì; lỗi ghi file không làm hỏng query (nuốt và in cảnh báo ra stderr).
  - `@dataclass CheckResult(allowed: bool, reasons: list[str], hints: list[str], sql: str | None, plan: PlanSummary | None, mask_plan: list[str | None])`
  - `@dataclass RunResult(columns: list[str], rows: list[tuple], row_count: int, truncated: bool, elapsed_ms: int, notes: list[str])`
  - `class QueryService`: `__init__(self, config: Config, connector_factory: Callable[[SystemProfile], Connector] = make_connector, hmac_key: bytes | None = None, today: Callable[[], date] = date.today)`; `check(system: str, sql: str) -> CheckResult` (không bao giờ ném `QueryRejected`, gói vào `allowed=False`); `run(system: str, sql: str) -> RunResult` (ném `QueryRejected` khi bị từ chối, `TimeoutError` khi quá giờ, `LookupError` khi sai tên hệ thống); `table_info(system: str, table: str) -> TableInfo`.

Thứ tự pipeline (mỗi bước dừng ngay khi từ chối): `parse_select` → `plan_masking` → `table_info(...).estimated_rows` cho từng bảng trong `parsed.tables` (bảng không tồn tại hoặc lỗi thống kê → `None`) → `large_tables` → `enforce_time_range` → `apply_row_limit` → `connector.explain(sql_cuối)` → `check_plan`. `run` thêm: lấy slot đồng thời (`threading.BoundedSemaphore(max_concurrent)` mỗi hệ thống, `acquire(blocking=False)`; không có slot → `QueryRejected("hệ thống đang bận", hint="thử lại sau")`), `execute`, `apply_masking`, ghi audit. Nếu `mask_plan` khác rỗng mà `hmac_key is None` → `QueryRejected`. Mọi lần `run`, kể cả bị từ chối hay lỗi, đều ghi audit với các trường `system, sql, status (ok|rejected|timeout|error), reason, elapsed_ms, row_count`.

- [ ] **Step 1: Viết test thất bại** với `FakeConnector` (ghi lại SQL được `explain`/`execute`, trả `table_info`/`PlanSummary`/`QueryResult` cấu hình trước):

```python
def test_happy_path_applies_limit_and_masks():   # SELECT msisdn, COUNT(*) ... GROUP BY msisdn trên cdr_history có điều kiện thời gian
    # connector.executed_sql chứa FETCH FIRST 1000; kết quả cột 0 đã bị băm; audit status=="ok", row_count đúng
def test_rejected_never_reaches_execute():       # DELETE / thiếu điều kiện thời gian / plan full scan → fake.executed_sql == [] ; audit status=="rejected"
def test_explain_runs_on_final_sql():            # fake.explained_sql[0] chứa mệnh đề giới hạn dòng
def test_check_does_not_execute():               # check() gọi explain nhưng không execute
def test_check_returns_reasons_and_hints():      # allowed False, reasons[0] không rỗng
def test_busy_when_slots_exhausted():            # max_concurrent=1, lấy slot rồi gọi run từ luồng khác → QueryRejected "bận"; slot được trả lại sau khi xong (kể cả khi execute ném lỗi)
def test_unknown_system():                       with pytest.raises(LookupError)
def test_sensitive_requires_key():               hmac_key=None + query cần che → QueryRejected
def test_timeout_logged():                       fake.execute ném TimeoutError → audit status=="timeout", ngoại lệ được ném lại
def test_audit_failure_does_not_break_query():   AuditLog trỏ vào đường dẫn không ghi được → run vẫn trả kết quả
def test_audit_has_no_row_data():                nội dung dòng kết quả không xuất hiện trong file log
```

  `test_audit.py`: ghi 2 sự kiện → 2 dòng JSON hợp lệ có `ts`; `AuditLog(None).write({})` không lỗi.
- [ ] **Step 2: Chạy `pytest tests/test_audit.py tests/test_service.py -v`**, kỳ vọng FAIL.
- [ ] **Step 3: Cài đặt `AuditLog` và `QueryService`** theo thứ tự pipeline. Slot đồng thời phải được trả trong `finally`.
- [ ] **Step 4: Chạy toàn bộ `pytest -m "not integration" -v`**, kỳ vọng PASS.
- [ ] **Step 5: Commit** `feat: query pipeline with concurrency limit and audit log`.

---

### Task 8: MCP server và các tool

**Files:**
- Create: `src/mcp_database/server.py`, `src/mcp_database/__main__.py`
- Test: `tests/test_server.py`

**Interfaces:**
- Consumes: `QueryService`, `load_config`.
- Produces (`server.py`): `build_server(service: QueryService, config: Config) -> FastMCP` và `main() -> None` (đọc đường dẫn cấu hình từ biến `MCP_DATABASE_CONFIG`, mặc định `./systems.yaml`; khóa HMAC từ `MCP_DATABASE_HMAC_KEY`; chạy `mcp.run(transport="stdio")`). Trước khi chạy, `main` kiểm tra mọi `user_env`/`password_env` của các profile và (nếu có profile dùng cột nhạy cảm) `MCP_DATABASE_HMAC_KEY` đều có trong môi trường; thiếu thì in danh sách biến thiếu ra stderr và thoát mã 1.

Các tool (hàm `async`, gọi `QueryService` qua `asyncio.to_thread` để không chặn vòng lặp sự kiện); mô tả tool phải nhắc Claude: đọc `get_data_doc` trước, luôn lọc theo thời gian, ưu tiên query tổng hợp (`GROUP BY`, `COUNT`), cột nhạy cảm sẽ bị che:
- `list_systems() -> list[dict]` — `[{"name", "db_type"}]`.
- `get_data_doc(system: str) -> str` — nội dung file `data_doc`; hệ thống không có hoặc file thiếu → thông báo lỗi rõ ràng.
- `describe_table(system: str, table: str) -> dict` — `{"columns": [...], "indexes": [...], "estimated_rows": int | None}`.
- `explain_query(system: str, sql: str) -> dict` — `{"allowed": bool, "reasons": [...], "hints": [...], "plan": str | None, "cost": int | None, "estimated_rows": int | None}`; không thực thi query.
- `run_query(system: str, sql: str) -> dict` — thành công: `{"columns", "rows", "row_count", "truncated", "elapsed_ms", "notes"}`; bị từ chối hoặc timeout: `{"error": "...", "hint": "..."}` (không ném ngoại lệ ra MCP).
- `profile_column(system: str, table: str, column: str, time_from: str | None = None, time_to: str | None = None, top_n: int = 10) -> dict` — kiểm tra `table`/`column` khớp regex `^[A-Za-z_][A-Za-z0-9_$#]*(\.[A-Za-z_][A-Za-z0-9_$#]*)?$` và tồn tại trong `table_info`; từ chối cột nhạy cảm; tự dựng hai query tổng hợp (`COUNT(*)`, `COUNT(col)`, `COUNT(DISTINCT col)`; và top-N giá trị theo `COUNT(*)` giảm dần), có `WHERE` theo `time_column` nếu `time_from`/`time_to` được đưa vào (bảng lớn bắt buộc có, nếu không pipeline từ chối) và chạy qua đúng `QueryService.run`; `top_n` bị chặn trong 1..50.

- [ ] **Step 1: Viết test thất bại** trong `tests/test_server.py` với `QueryService` dùng `FakeConnector` (dùng lại từ Task 7, đưa vào `tests/conftest.py`), gọi các hàm tool trực tiếp qua `server.call_tool` hoặc qua danh sách tool của `FastMCP`:

```python
def test_tools_registered():            tên tool == {"list_systems","get_data_doc","describe_table","explain_query","run_query","profile_column"}
def test_list_systems_has_no_secrets(): chuỗi kết quả không chứa host, user_env, password_env
def test_get_data_doc_returns_file_text(tmp_path)
def test_get_data_doc_unknown_system():  trả lỗi dạng thông báo, không ném
def test_run_query_rejection_is_payload(): "DELETE FROM t" → {"error": ..., "hint": ...}
def test_explain_query_never_executes(): fake.executed_sql == []
def test_profile_column_rejects_injection(): column='a); DROP TABLE t; --' → lỗi, fake không nhận SQL nào
def test_profile_column_rejects_sensitive(): cột trong sensitive_columns → lỗi
def test_profile_column_sql_goes_through_pipeline(): bảng lớn thiếu time_from/time_to → từ chối
def test_main_exits_on_missing_env(monkeypatch, capsys): thiếu biến → SystemExit(1) và stderr liệt kê tên biến
```

- [ ] **Step 2: Chạy `pytest tests/test_server.py -v`**, kỳ vọng FAIL.
- [ ] **Step 3: Cài đặt `build_server`, `main`, `__main__.py`** (`python -m mcp_database`).
- [ ] **Step 4: Chạy `pytest -m "not integration" -v`**, kỳ vọng toàn bộ PASS.
- [ ] **Step 5: Smoke test giao thức** bằng cách chạy `python -m mcp_database` với cấu hình ví dụ trỏ vào connector giả hoặc DB thử nghiệm, gửi `initialize` + `tools/list` qua stdin; kỳ vọng nhận danh sách 6 tool.
- [ ] **Step 6: Commit** `feat: MCP server and tools`.

---

### Task 9: Tài liệu, đăng ký với Claude Code, kiểm tra trên DB thật

**Files:**
- Create: `README.md` (tiếng Việt), `docs/example-data-doc.md`
- Modify: `systems.example.yaml` (nếu cần cho khớp thực tế)

**Interfaces:**
- Consumes: toàn bộ.
- Produces: tài liệu vận hành.

- [ ] **Step 1: Viết `README.md`** gồm: cài đặt; cấu trúc `systems.yaml` và `guard`; cách thêm một hệ thống mới (profile + file mô tả + `guard`); SQL tạo user chỉ đọc cho từng loại DB (Oracle: user với `CREATE SESSION` và `SELECT` trên đúng các bảng cần; MariaDB: `GRANT SELECT ON <db>.* TO ...`), nhấn mạnh đây là lớp bảo vệ chính; biến môi trường cần có; đăng ký bằng `claude mcp add database -- <đường dẫn .venv>/bin/python -m mcp_database` kèm `MCP_DATABASE_CONFIG`; vị trí và định dạng audit log; danh sách lý do từ chối thường gặp và cách xử lý; gợi ý quy trình phân tích cho Claude (đọc data doc → nêu giả thuyết → query tổng hợp → tách số liệu thật/giả định/nhận định → đề xuất).
- [ ] **Step 2: Viết `docs/example-data-doc.md`** là mẫu file mô tả dữ liệu (bảng, ý nghĩa cột, quan hệ, cột thời gian, ghi chú nghiệp vụ), nói rõ định dạng tự do.
- [ ] **Step 3: Kiểm tra trên replica thật (cần người dùng)**: với một hệ thống thật, chạy lần lượt `describe_table`, một `explain_query` có điều kiện thời gian hợp lệ, một `run_query` tổng hợp, và các query bị từ chối kỳ vọng (thiếu điều kiện thời gian, `SELECT *` trên bảng có cột nhạy cảm, `UPDATE`). Ghi lại các ngưỡng `max_explain_cost` / `max_explain_rows` phù hợp rồi cập nhật `guard` mẫu. Nếu chưa có DB để thử, báo rõ "chưa kiểm tra trên DB thật".
- [ ] **Step 4: Chạy `pytest -m "not integration" -v` lần cuối**, kỳ vọng PASS.
- [ ] **Step 5: Commit** `docs: README and example data doc`.

---

## Self-review

- **Spec coverage:** cấu hình theo profile (T1); validator một câu SELECT/WITH (T2); che cột nhạy cảm + HMAC (T3); bảng lớn/điều kiện thời gian/giới hạn dòng (T4); EXPLAIN cho cả hai DB, timeout phía DB, đọc chỉ đọc, `table_info` từ thống kê DB (T5, T6); đồng thời + audit (T7); sáu tool kể cả `profile_column` (T8); cờ replica/primary (T1); README, user chỉ đọc, đăng ký Claude Code (T9). Không có yêu cầu nào của spec thiếu task.
- **Nhất quán kiểu:** `Guard`, `ParsedQuery`, `PlanSummary`, `QueryResult`, `CheckResult`, `RunResult`, `Connector` dùng cùng tên và chữ ký ở mọi task.
- **Review Focus:** năm mục đều có test ở Task 2, 3, 4.

## Chỗ lệch hoặc bổ sung so với spec (cần người dùng biết)

1. `sensitive_columns` là mapping `TABLE.COLUMN: hash|redact` thay vì danh sách, để chọn chế độ che từng cột.
2. Driver MariaDB là `PyMySQL` (thuần Python, không cần thư viện C); vẫn nói chuyện được với MariaDB.
3. Bảng không có thống kê (`rows` là `None`, ví dụ view hoặc bảng chưa `ANALYZE`) không bị coi là bảng lớn; chỉ cổng EXPLAIN (cost / số dòng) bắt chúng. Muốn chặt hơn thì khai báo vào `guard.tables`.
4. Cận thời gian chỉ chấp nhận literal ngày và `SYSDATE/NOW() ± n ngày`; dạng khác bị từ chối kèm gợi ý, để chắc chắn kiểm được khoảng thời gian.
5. Hệ thống đang bận thì từ chối ngay, không xếp hàng chờ.
6. Mỗi lần gọi mở một kết nối mới, chưa có connection pool.
