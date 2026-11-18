# mcp-database-adapter

**Tiếng Việt** | [English](#english)

MCP server chỉ đọc để Claude truy vấn dữ liệu trong replica Oracle và MariaDB. Dùng cho nhóm R&D và vận hành: đọc dữ liệu lịch sử người dùng, tìm điểm yếu của hệ thống, rồi đề xuất điều chỉnh có số liệu dẫn chứng.

Một server dùng chung cho nhiều hệ thống. Mỗi hệ thống là một profile trong `systems.yaml`, gồm loại DB, cách kết nối, giới hạn riêng và một file mô tả dữ liệu để Claude đọc trước khi viết SQL. Thêm hệ thống mới không cần sửa code.

```
Claude Code / Claude Desktop
        │  MCP qua stdio (Claude khởi động server như tiến trình con, không mở cổng mạng)
        ▼
  mcp-database-adapter
        │  kiểm tra SQL → che cột nhạy cảm → luật bảng lớn → EXPLAIN → chạy có timeout
        ▼
  replica Oracle / MariaDB (user chỉ đọc)
```

## Nó làm gì để không làm hại production

Lớp bảo vệ chính là user database chỉ có quyền đọc, nối vào replica. Server là lớp dự phòng, và giữ cho replica không bị query nặng làm chậm:

- Chỉ nhận đúng một câu `SELECT` hoặc `WITH ... SELECT`. DML, DDL, khóa dòng, hàm có tác dụng phụ, DB link, hàm XML/XQuery đều bị từ chối.
- Chỉ chạy lại SQL do chính server sinh ra từ cây cú pháp đã kiểm tra, không bao giờ chạy chuỗi gốc.
- Cột nhạy cảm bị băm (`hash`, vẫn đếm và ghép được) hoặc che hẳn (`redact`). Nếu không xác định được cột có bị lộ hay không thì từ chối cả query.
- Bảng lớn bắt buộc có lọc theo cột thời gian, trong một khoảng ngày tối đa cấu hình được.
- Chạy `EXPLAIN` trước; từ chối khi quét toàn bộ bảng lớn hoặc chi phí và số dòng ước tính vượt ngưỡng.
- Giới hạn số dòng trả về, timeout phía DB và phía client, số query đồng thời cho mỗi hệ thống.
- Lỗi từ DB không bao giờ đến tay Claude nguyên văn, chỉ có mã lỗi. Mọi lần chạy đều ghi audit log.

Những giới hạn còn lại (suy luận giá trị bằng cách lọc lặp lại, cột cá nhân chưa khai báo, Oracle chưa kiểm trên DB thật) được nói thẳng trong `docs/tham-khao.md`, mục 8.

## Sáu tool

| Tool | Việc nó làm |
|---|---|
| `list_systems` | Liệt kê hệ thống truy cập được (chỉ tên và loại DB) |
| `get_data_doc` | Trả file mô tả dữ liệu của hệ thống; Claude phải đọc trước khi viết SQL |
| `describe_table` | Cột, chỉ mục, số dòng ước tính của một bảng |
| `explain_query` | Chạy toàn bộ kiểm tra và EXPLAIN nhưng không chạy câu SQL |
| `run_query` | Chạy câu SELECT đã qua kiểm tra, trả kết quả đã che cột nhạy cảm |
| `profile_column` | Thống kê một cột: số khác NULL, số giá trị phân biệt, top giá trị phổ biến |

## Bắt đầu nhanh

Cần Python 3.11 trở lên và mạng tới replica.

```bash
git clone git@github.com:thanhquang2003/mcp-database-adapter.git
cd mcp-database-adapter
python3 -m venv .venv
.venv/bin/pip install -e .
cp systems.example.yaml systems.yaml     # sửa theo hệ thống của bạn; file này bị git bỏ qua
```

Tạo user DB chỉ đọc (SQL mẫu cho Oracle và MariaDB ở `docs/tham-khao.md`, mục 3), viết file mô tả dữ liệu theo mẫu `docs/example-data-doc.md`, rồi đặt biến môi trường:

```bash
export MCP_DATABASE_CONFIG=$PWD/systems.yaml
export MCP_DATABASE_HMAC_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")   # bắt buộc nếu có cột nhạy cảm; cất kỹ
export BILLING_DB_USER=... BILLING_DB_PASS=...         # tên biến do từng profile chọn
.venv/bin/python scripts/kiem_tra_server.py            # server lên được, thấy đủ 6 tool
```

Lưu ý khi sửa `systems.yaml`: `user_env` và `password_env` là **tên** biến môi trường (ví dụ `BILLING_DB_USER`), không phải user hay mật khẩu. Giá trị thật đặt vào biến môi trường đó. Ghi nhầm mật khẩu vào đây sẽ bị server từ chối ngay (và không in giá trị ra). File mẫu có hai hệ thống, `billing` và `crm`, nên cần đặt biến cho cả hai; chỉ thử một hệ thống thì xóa khối còn lại khỏi `systems.yaml`.

Nối vào Claude Code:

```bash
claude mcp add database \
  -e MCP_DATABASE_CONFIG=$PWD/systems.yaml \
  -e MCP_DATABASE_HMAC_KEY=... -e BILLING_DB_USER=... -e BILLING_DB_PASS=... \
  -- $PWD/.venv/bin/python -m mcp_database
```

Cách này để bí mật nằm nguyên văn trong cấu hình của Claude Code. Cách tốt hơn (file biến môi trường quyền 0600 cộng script bọc, dùng chung trong nhóm qua `.mcp.json`, và cấu hình Claude Desktop) ở `docs/huong-dan.md`.

Rồi hỏi Claude, ví dụ: "Dùng database billing, tìm điểm yếu trong luồng nạp tiền 7 ngày gần nhất. Đọc mô tả dữ liệu trước và chỉ chạy query tổng hợp."

### Cài trên macOS và Windows

Các lệnh ở trên là cho Linux và macOS (bash/zsh). Server chỉ dùng thư viện Python thuần, Oracle chạy ở chế độ thin nên không cần cài Oracle Client, nên mọi hệ điều hành đều cài được.

**macOS (MacBook).** Cài Python và git bằng Homebrew, rồi chạy y hệt các lệnh trên:

```bash
brew install python@3.12 git
```

Nếu `python3 --version` báo dưới 3.11, gọi thẳng bản Homebrew (`/opt/homebrew/bin/python3.12` trên Apple Silicon, `/usr/local/bin/python3.12` trên Intel). Dùng đường dẫn tuyệt đối đầy đủ khi đăng ký với Claude (ví dụ `/Users/ten-ban/mcp-database`), đừng dùng `~`.

**Windows.** Dùng PowerShell. Cài Python 3.11+ và git (python.org, git-scm.com, hoặc `winget install Python.Python.3.12 Git.Git`) rồi mở lại PowerShell:

```powershell
git clone git@github.com:thanhquang2003/mcp-database-adapter.git C:\mcp-database
cd C:\mcp-database
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -e .
Copy-Item systems.example.yaml systems.yaml

$env:MCP_DATABASE_CONFIG = "C:\mcp-database\systems.yaml"
$env:MCP_DATABASE_HMAC_KEY = (py -3 -c "import secrets; print(secrets.token_hex(32))")
$env:BILLING_DB_USER = "..."; $env:BILLING_DB_PASS = "..."
.venv\Scripts\python.exe scripts\kiem_tra_server.py

claude mcp add database `
  -e MCP_DATABASE_CONFIG=C:\mcp-database\systems.yaml `
  -e MCP_DATABASE_HMAC_KEY=... -e BILLING_DB_USER=... -e BILLING_DB_PASS=... `
  -- C:\mcp-database\.venv\Scripts\python.exe -m mcp_database
```

Hai chỗ hay vấp khi đăng ký trên Windows: (1) thay `C:\mcp-database` bằng thư mục bạn thật sự cài (ví dụ `D:\source\mcp-database-adapter`) và thay `...` bằng giá trị thật; (2) nếu PowerShell báo "running scripts is disabled on this system" khi gõ `claude`, gõ `claude.cmd` thay vào (hoặc `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned`). Đặt bí mật ngay trong lệnh `claude mcp add` thì chúng nằm nguyên văn trong cấu hình Claude; cách đặt bí mật trong file `.cmd` ngoài repo, khóa quyền bằng `icacls`, ở `docs/huong-dan.md`, mục 5.

**Bạn đang ở `cmd` (dấu nhắc dạng `C:\...>`, không có chữ `PS`) thay vì PowerShell?** Các lệnh `$env:...` ở trên chỉ chạy trong PowerShell; gõ vào `cmd` sẽ báo "The filename, directory name, or volume label syntax is incorrect". Có hai cách: gõ `powershell` để chuyển sang PowerShell rồi làm lại, hoặc dùng bản `cmd` này (biến chỉ có hiệu lực trong cửa sổ hiện tại; không đặt dấu ngoặc kép hay dấu cách quanh `=`):

```bat
set "MCP_DATABASE_CONFIG=C:\mcp-database\systems.yaml"
for /f %i in ('py -3 -c "import secrets; print(secrets.token_hex(32))"') do set "MCP_DATABASE_HMAC_KEY=%i"
set "BILLING_DB_USER=..."
set "BILLING_DB_PASS=..."
.venv\Scripts\python.exe scripts\kiem_tra_server.py
```

Trong file `.bat` thì viết `%%i` thay cho `%i`. Cách đăng ký với Claude trong `cmd`: một dòng, `claude mcp add database -e MCP_DATABASE_CONFIG=C:\mcp-database\systems.yaml -e MCP_DATABASE_HMAC_KEY=... -e BILLING_DB_USER=... -e BILLING_DB_PASS=... -- C:\mcp-database\.venv\Scripts\python.exe -m mcp_database`.

Nếu bạn chạy Claude Code trong WSL thì làm theo phần Linux bên trong WSL. Trên macOS và Windows, mật khẩu không nên để nguyên văn trong cấu hình của Claude: dùng Keychain (macOS) hoặc file `.cmd` bị khóa ACL (Windows), chi tiết và cấu hình Claude Desktop ở `docs/huong-dan.md`, mục 3 và 5. Dự án mới được kiểm với DB thật trên Linux; macOS và Windows chưa được kiểm trực tiếp, nên chạy `scripts/kiem_tra_server.py` trước khi nối Claude.

## Tài liệu

Tài liệu chi tiết hiện viết bằng tiếng Việt.

- `docs/huong-dan.md`: làm theo trình tự: cài đặt, nối Claude Code và Claude Desktop, cách dùng để phân tích, vận hành (thêm hệ thống, xoay khóa, audit log, tải lên replica), xử lý sự cố.
- `docs/tham-khao.md`: tra cứu đầy đủ: mọi khóa cấu hình, cách server kiểm soát query, thông báo từ chối và cách sửa, giới hạn đã biết, checklist kiểm tra trên replica thật.
- `docs/thiet-ke.md`: thiết kế gốc.
- `docs/example-data-doc.md`, `systems.example.yaml`: mẫu.

## Cấu trúc mã

```
src/mcp_database/
  server.py        khai báo 6 tool MCP, khởi động và kiểm tra biến môi trường
  service.py       pipeline kiểm tra → chạy, giới hạn đồng thời, cache thống kê bảng, audit
  config.py        nạp và kiểm tra systems.yaml
  masking.py       che cột nhạy cảm
  audit.py         audit log JSONL
  guard/           parser.py (kiểm tra SQL), policy.py (bảng lớn, thời gian, giới hạn dòng), plan.py (cổng EXPLAIN)
  db/              base.py (kết nối, làm sạch lỗi), oracle.py, mariadb.py
scripts/kiem_tra_server.py   chạy thử server qua stdio
tests/                       670 test đơn vị; tests/integration chạy với DB thật
```

## Chạy test

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -m "not integration"
```

Test tích hợp MariaDB cần một DB thật (cách dựng bằng Docker ở `docs/tham-khao.md`, mục 10); test Oracle cần một Oracle thật và mặc định bị bỏ qua.

## Tình trạng

Đường MariaDB đã chạy với container MariaDB 10.11 thật. Đường Oracle mới kiểm bằng driver giả, nên coi là thử nghiệm cho tới khi bạn làm xong danh sách "Kiểm tra trên replica thật" ở cuối `docs/tham-khao.md`. Các thư viện `sqlglot` và `mcp` được ghim khoảng phiên bản vì lớp kiểm tra SQL phụ thuộc vào cấu trúc bên trong của chúng.

---

<a id="english"></a>

# mcp-database-adapter (English)

[Tiếng Việt](#mcp-database-adapter) | **English**

A read-only MCP server that lets Claude query Oracle and MariaDB replicas. Built for R&D and operations teams: read historical user data, find weak points in a system, then propose changes backed by numbers.

One server serves many systems. Each system is a profile in `systems.yaml`: the DB type, how to connect, its own limits, and a data description file that Claude reads before it writes any SQL. Adding a system takes configuration, not code.

```
Claude Code / Claude Desktop
        │  MCP over stdio (Claude starts the server as a child process; no network port is opened)
        ▼
  mcp-database-adapter
        │  validate SQL → mask sensitive columns → large-table rule → EXPLAIN → run with timeout
        ▼
  Oracle / MariaDB replica (read-only user)
```

## What it does to keep production safe

The primary safeguard is a database user with read-only privileges, connected to a replica. The server is a backstop, and it keeps heavy queries from slowing the replica down:

- Accepts exactly one `SELECT` or `WITH ... SELECT`. DML, DDL, row locks, functions with side effects, DB links and XML/XQuery functions are rejected.
- Only runs SQL the server itself regenerated from the validated syntax tree, never the original string.
- Sensitive columns are hashed (`hash`, so you can still count and join) or fully masked (`redact`). If the server cannot tell whether a column would leak, it rejects the whole query.
- Large tables must be filtered on a time column, within a configurable maximum number of days.
- `EXPLAIN` runs first; the query is rejected on a full scan of a large table or when the estimated cost or row count exceeds the thresholds.
- Row limit on results, DB-side and client-side timeouts, and a cap on concurrent queries per system.
- Raw database errors never reach Claude, only an error code. Every run is written to the audit log.

The remaining limits (inferring values through repeated filtering, personal-data columns that were not declared, Oracle not yet verified against a real database) are stated plainly in `docs/tham-khao.md`, section 8.

## The six tools

| Tool | What it does |
|---|---|
| `list_systems` | Lists the systems you can reach (name and DB type only) |
| `get_data_doc` | Returns the system's data description file; Claude must read it before writing SQL |
| `describe_table` | Columns, indexes and estimated row count of a table |
| `explain_query` | Runs every check and EXPLAIN but does not execute the statement |
| `run_query` | Runs a validated SELECT and returns results with sensitive columns masked |
| `profile_column` | Column statistics: non-null count, distinct values, most frequent values |

## Quick start

You need Python 3.11 or later and network access to the replica.

```bash
git clone git@github.com:thanhquang2003/mcp-database-adapter.git
cd mcp-database-adapter
python3 -m venv .venv
.venv/bin/pip install -e .
cp systems.example.yaml systems.yaml     # edit for your systems; git ignores this file
```

Create a read-only DB user (sample SQL for Oracle and MariaDB is in `docs/tham-khao.md`, section 3), write a data description file following `docs/example-data-doc.md`, then set the environment variables:

```bash
export MCP_DATABASE_CONFIG=$PWD/systems.yaml
export MCP_DATABASE_HMAC_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")   # required if any column is sensitive; keep it secret
export BILLING_DB_USER=... BILLING_DB_PASS=...         # variable names are chosen per profile
.venv/bin/python scripts/kiem_tra_server.py            # server starts and exposes all 6 tools
```

When editing `systems.yaml`: `user_env` and `password_env` hold the **name** of an environment variable (for example `BILLING_DB_USER`), not the user or the password. Put the real value in that environment variable. A password written here by mistake is rejected immediately (and never printed). The sample file defines two systems, `billing` and `crm`, so set the variables for both; to try just one, delete the other block from `systems.yaml`.

Connect it to Claude Code:

```bash
claude mcp add database \
  -e MCP_DATABASE_CONFIG=$PWD/systems.yaml \
  -e MCP_DATABASE_HMAC_KEY=... -e BILLING_DB_USER=... -e BILLING_DB_PASS=... \
  -- $PWD/.venv/bin/python -m mcp_database
```

This stores the secrets in plain text in Claude Code's configuration. A better setup (a 0600 environment file plus a wrapper script, sharing the declaration across a team through `.mcp.json`, and Claude Desktop configuration) is in `docs/huong-dan.md`.

Then ask Claude something like: "Using the billing database, find weak points in the top-up flow over the last 7 days. Read the data description first and run aggregate queries only."

### Installing on macOS and Windows

The commands above are for Linux and macOS (bash/zsh). The server uses pure-Python libraries and Oracle runs in thin mode (no Oracle Client needed), so it installs on any operating system.

**macOS (MacBook).** Install Python and git with Homebrew, then run the same commands as above:

```bash
brew install python@3.12 git
```

If `python3 --version` reports less than 3.11, call the Homebrew one directly (`/opt/homebrew/bin/python3.12` on Apple Silicon, `/usr/local/bin/python3.12` on Intel). Use full absolute paths when registering with Claude (for example `/Users/your-name/mcp-database`), not `~`.

**Windows.** Use PowerShell. Install Python 3.11+ and git (python.org, git-scm.com, or `winget install Python.Python.3.12 Git.Git`), then reopen PowerShell:

```powershell
git clone git@github.com:thanhquang2003/mcp-database-adapter.git C:\mcp-database
cd C:\mcp-database
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -e .
Copy-Item systems.example.yaml systems.yaml

$env:MCP_DATABASE_CONFIG = "C:\mcp-database\systems.yaml"
$env:MCP_DATABASE_HMAC_KEY = (py -3 -c "import secrets; print(secrets.token_hex(32))")
$env:BILLING_DB_USER = "..."; $env:BILLING_DB_PASS = "..."
.venv\Scripts\python.exe scripts\kiem_tra_server.py

claude mcp add database `
  -e MCP_DATABASE_CONFIG=C:\mcp-database\systems.yaml `
  -e MCP_DATABASE_HMAC_KEY=... -e BILLING_DB_USER=... -e BILLING_DB_PASS=... `
  -- C:\mcp-database\.venv\Scripts\python.exe -m mcp_database
```

Two common stumbling blocks when registering on Windows: (1) replace `C:\mcp-database` with the folder you actually installed to (for example `D:\source\mcp-database-adapter`) and replace `...` with real values; (2) if PowerShell says "running scripts is disabled on this system" when you type `claude`, type `claude.cmd` instead (or run `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned`). Putting secrets directly in the `claude mcp add` command stores them in plain text in Claude's configuration; keeping them in a `.cmd` file outside the repo, locked down with `icacls`, is described in `docs/huong-dan.md`, section 5.

**Are you in `cmd` (a prompt like `C:\...>`, with no `PS`) instead of PowerShell?** The `$env:...` lines above only work in PowerShell; typing them into `cmd` gives "The filename, directory name, or volume label syntax is incorrect". Either type `powershell` to switch and run them again, or use this `cmd` version (the variables last only for the current window; no quotes or spaces around `=`):

```bat
set "MCP_DATABASE_CONFIG=C:\mcp-database\systems.yaml"
for /f %i in ('py -3 -c "import secrets; print(secrets.token_hex(32))"') do set "MCP_DATABASE_HMAC_KEY=%i"
set "BILLING_DB_USER=..."
set "BILLING_DB_PASS=..."
.venv\Scripts\python.exe scripts\kiem_tra_server.py
```

In a `.bat` file write `%%i` instead of `%i`. To register with Claude from `cmd`, use a single line: `claude mcp add database -e MCP_DATABASE_CONFIG=C:\mcp-database\systems.yaml -e MCP_DATABASE_HMAC_KEY=... -e BILLING_DB_USER=... -e BILLING_DB_PASS=... -- C:\mcp-database\.venv\Scripts\python.exe -m mcp_database`.

If you run Claude Code inside WSL, follow the Linux steps inside WSL. On macOS and Windows, don't leave the password in plain text in Claude's configuration: use the Keychain (macOS) or an ACL-locked `.cmd` file (Windows). Details, and Claude Desktop configuration, are in `docs/huong-dan.md`, sections 3 and 5. The project has so far been verified against a real database only on Linux; macOS and Windows have not been tested directly, so run `scripts/kiem_tra_server.py` before connecting Claude.

## Documentation

The detailed documentation is currently written in Vietnamese.

- `docs/huong-dan.md`: step-by-step guide: installation, connecting Claude Code and Claude Desktop, using it for analysis, operations (adding a system, rotating the key, audit log, replica load), troubleshooting.
- `docs/tham-khao.md`: full reference: every configuration key, how the server controls queries, rejection messages and how to fix them, known limits, the "test on a real replica" checklist.
- `docs/thiet-ke.md`: the original design.
- `docs/example-data-doc.md`, `systems.example.yaml`: samples.

## Code layout

```
src/mcp_database/
  server.py        declares the 6 MCP tools, startup and environment-variable checks
  service.py       validation → execution pipeline, concurrency limit, table-stats cache, audit
  config.py        loads and validates systems.yaml
  masking.py       masks sensitive columns
  audit.py         JSONL audit log
  guard/           parser.py (SQL validation), policy.py (large tables, time range, row limit), plan.py (EXPLAIN gate)
  db/              base.py (connections, error sanitizing), oracle.py, mariadb.py
scripts/kiem_tra_server.py   starts the server over stdio as a smoke check
tests/                       670 unit tests; tests/integration runs against real databases
```

## Running the tests

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest -m "not integration"
```

The MariaDB integration tests need a real database (how to start one with Docker is in `docs/tham-khao.md`, section 10); the Oracle tests need a real Oracle and are skipped by default.

## Status

The MariaDB path has run against a real MariaDB 10.11 container. The Oracle path has only been checked against test doubles, so treat it as experimental until you finish the "test on a real replica" checklist at the end of `docs/tham-khao.md`. The `sqlglot` and `mcp` libraries are pinned to a version range because the SQL validation layer depends on their internal structure.
