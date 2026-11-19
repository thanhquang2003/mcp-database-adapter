# Hướng dẫn sử dụng, vận hành và tích hợp Claude

Tài liệu này đi từ đầu đến cuối: dựng server, nối Claude vào, dùng để phân tích, rồi vận hành lâu dài. docs/tham-khao.md là tài liệu tra cứu (mọi khóa cấu hình, mọi lý do từ chối); file này là trình tự làm việc. Chỗ nào cần chi tiết hơn thì có chỉ sang docs/tham-khao.md.

Một điều cần nhớ trước khi bắt đầu: lớp bảo vệ chính là user database chỉ có quyền đọc, nối vào replica. Server kiểm tra query để replica không bị làm nặng và để dữ liệu cá nhân không đi thẳng vào Claude, nhưng nó là lớp dự phòng. Đừng cấp quyền rộng cho user DB rồi trông vào server.

## 1. Cách các phần ghép với nhau

```
Claude Code / Claude Desktop
        │  (MCP qua stdio: Claude khởi động server như một tiến trình con)
        ▼
  mcp_database (Python)  ──►  kiểm tra SQL → che cột nhạy cảm → luật bảng lớn → EXPLAIN → chạy
        │
        ▼
  replica Oracle / MariaDB  (user chỉ đọc)
```

Không có cổng mạng nào được mở. Mỗi người dùng Claude chạy một tiến trình server riêng trên máy của họ, đọc cấu hình và biến môi trường của họ. Máy đó phải nối được tới replica.

## 2. Chuẩn bị trước khi cài

Bốn việc, theo thứ tự quan trọng:

1. User database chỉ đọc cho từng hệ thống, nối vào replica (không phải primary). SQL tạo user cho Oracle và MariaDB nằm ở docs/tham-khao.md mục 3.
2. File mô tả dữ liệu cho từng hệ thống. Đây là phần quyết định chất lượng phân tích, xem mục 4.
3. Danh sách cột chứa dữ liệu cá nhân (số thuê bao, IMSI, tên, địa chỉ, email...). Server chỉ che những cột được khai báo; cột không khai báo đi tới Claude ở dạng rõ, kể cả các cột văn bản tự do như ghi chú.
4. Một khóa HMAC ngẫu nhiên, ít nhất 16 byte, riêng cho mỗi lần triển khai. Lệnh sau chạy được trên Linux, macOS và Windows: `python3 -c "import secrets; print(secrets.token_hex(32))"` (trên Windows dùng `py -3 -c ...` hoặc `python -c ...`). Khóa này dùng để băm cột kiểu `hash`; ai có nó có thể dò ngược số gốc từ giá trị băm, nên giữ như mật khẩu.

## 3. Cài đặt

Cần Python 3.11 trở lên và mạng tới replica. Server chỉ dùng thư viện Python thuần cho cả hai DB (Oracle chạy ở chế độ thin, không cần cài Oracle Client), nên các bước giống nhau trên mọi hệ điều hành; chỉ khác cách gõ lệnh. Đã kiểm: đường MariaDB với DB thật trên Linux; Windows (PowerShell) từ cài đặt, chạy thử server đến đăng ký với Claude Code đã chạy thành công. macOS chưa được kiểm trực tiếp, nên làm xong mục 4 ("Chạy thử") rồi mới đi tiếp.

### Linux

```bash
git clone git@github.com:thanhquang2003/mcp-database-adapter.git /opt/mcp-database
cd /opt/mcp-database
python3 -m venv .venv
.venv/bin/python -m pip install -e .
cp systems.example.yaml systems.yaml
```

### macOS (MacBook)

Cài Python và git bằng Homebrew nếu chưa có (bỏ qua nếu đã có Python 3.11 trở lên):

```bash
brew install python@3.12 git
```

Rồi cài giống Linux. Dùng thư mục trong home của bạn thay cho `/opt` để khỏi cần `sudo`:

```bash
git clone git@github.com:thanhquang2003/mcp-database-adapter.git ~/mcp-database
cd ~/mcp-database
python3 -m venv .venv
.venv/bin/python -m pip install -e .
cp systems.example.yaml systems.yaml
```

Nếu `python3 --version` báo dưới 3.11 (Python hệ thống của macOS thường cũ), gọi thẳng bản của Homebrew: `/opt/homebrew/bin/python3.12` trên máy Apple Silicon, `/usr/local/bin/python3.12` trên máy Intel. Phần còn lại của tài liệu dùng `~/mcp-database` thay cho `/opt/mcp-database`.

### Windows

Làm trong PowerShell. Cài Python 3.11+ và git nếu chưa có (từ python.org và git-scm.com, hoặc `winget install Python.Python.3.12 Git.Git`), rồi mở lại PowerShell để nhận PATH mới:

```powershell
git clone git@github.com:thanhquang2003/mcp-database-adapter.git C:\mcp-database
cd C:\mcp-database
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -e .
Copy-Item systems.example.yaml systems.yaml
```

Không cần kích hoạt venv. Ở những chỗ tài liệu gọi `.venv/bin/python` (Linux, macOS), trên Windows gọi `.venv\Scripts\python.exe`. Hãy dùng đường dẫn đầy đủ và đặt thư mục cài ở nơi không có dấu cách trong đường dẫn (ví dụ `C:\mcp-database`) cho đỡ lỗi trích dẫn. Nếu bạn đã dùng WSL và chạy Claude Code trong WSL, hãy làm theo phần Linux ở trong WSL; khi đó mọi thứ (Python, server, Claude Code) đều nằm trong WSL, và replica phải nối được từ WSL.

Ghi chú về quyền file: cách đặt quyền `0600` ở các phần sau là của Linux/macOS. Trên Windows quyền đi theo ACL: mục 5 có lệnh `icacls` cho file chứa bí mật, và thư mục chứa audit log (`audit_log` trong `systems.yaml`) nên được khóa tương tự để chỉ tài khoản của bạn đọc được. Server không tự đặt ACL.

`systems.yaml` bị git bỏ qua, nên sửa thoải mái. Các thư viện được ghim khoảng phiên bản trong `pyproject.toml` (lý do ở docs/tham-khao.md mục 1); đừng nâng `sqlglot` hay `mcp` mà không chạy lại bộ test.

## 4. Cấu hình một hệ thống

Mỗi hệ thống là một khối trong `systems.yaml` cộng một file mô tả dữ liệu. Ví dụ một hệ thống MariaDB:

```yaml
audit_log: logs/audit.jsonl   # tính từ thư mục chứa systems.yaml; server tự tạo thư mục

systems:
  crm:
    db_type: mariadb
    host: crm-replica.noi-bo
    port: 3306
    database: crm
    user_env: CRM_DB_USER
    password_env: CRM_DB_PASS
    data_doc: docs/crm.md
    is_replica: true
    guard:
      tables:
        events:
          time_column: created_at
          max_range_days: 7
      sensitive_columns:
        users.phone: hash
        users.full_name: redact
```

Mật khẩu không nằm trong file: chỉ ghi tên biến môi trường. `is_replica: true` chỉ là khai báo, server không kiểm tra thật; nếu bạn nối vào primary thì phải ghi `allow_primary: true` thay cho nó và chịu rủi ro đó. Phần còn lại của `guard` (số dòng tối đa, timeout, số query đồng thời, ngưỡng EXPLAIN) có mặc định hợp lý; chỉ chỉnh khi có lý do, và chỉnh sau khi đã chạy thử trên replica thật.

Hai quyết định trong `guard` đáng nghĩ kỹ:

- Bảng lớn phải khai báo trong `guard.tables` kèm cột thời gian. Server tự nhận biết bảng lớn qua thống kê của DB, nhưng việc đó có giới hạn (view, bảng chưa có thống kê, tên không có schema trên Oracle), nên đừng chỉ trông vào nó. Bảng lớn mà không khai báo cột thời gian thì mọi query vào nó bị từ chối.
- `sensitive_columns` dùng `hash` cho định danh mà bạn vẫn muốn đếm hay ghép theo (cùng số thuê bao thì cùng giá trị băm), và `redact` cho thứ không bao giờ cần xem (tên, địa chỉ).

### File mô tả dữ liệu

Server trả nguyên văn file này cho Claude, và Claude được dặn đọc nó trước khi viết SQL. Viết tự do, nhưng một file tốt thường có: mỗi bảng dùng để làm gì và một dòng khoảng bao nhiêu; ý nghĩa các cột không hiển nhiên (mã trạng thái, đơn vị tiền, múi giờ); quan hệ giữa các bảng; cột thời gian nào dùng để lọc; những cạm bẫy nghiệp vụ (bản ghi bị xóa mềm, trạng thái tạm, dữ liệu trễ vài giờ); và cột nào là dữ liệu cá nhân. Có mẫu ở `docs/example-data-doc.md`. Mười phút viết kỹ chỗ này tiết kiệm nhiều vòng Claude đoán sai tên cột.

### Biến môi trường

Server cần các biến sau khi chạy:

- `MCP_DATABASE_CONFIG`: đường dẫn tuyệt đối tới `systems.yaml`.
- `MCP_DATABASE_HMAC_KEY`: khóa băm (bắt buộc nếu có hệ thống nào khai báo `sensitive_columns`).
- Các biến user/mật khẩu DB theo tên bạn chọn trong từng profile (`CRM_DB_USER`, `CRM_DB_PASS`).

Thiếu biến nào thì server in tên biến đó ra stderr và thoát với mã 1; nó không bao giờ in giá trị.

### Chạy thử trước khi nối Claude

Linux và macOS (macOS thay `/opt/mcp-database` bằng `~/mcp-database`):

```bash
export MCP_DATABASE_CONFIG=/opt/mcp-database/systems.yaml
export MCP_DATABASE_HMAC_KEY=...        # khóa ở mục 2
export CRM_DB_USER=... CRM_DB_PASS=...
cd /opt/mcp-database
.venv/bin/python scripts/kiem_tra_server.py
```

Windows (PowerShell; biến chỉ có hiệu lực trong cửa sổ này):

```powershell
$env:MCP_DATABASE_CONFIG = "C:\mcp-database\systems.yaml"
$env:MCP_DATABASE_HMAC_KEY = "..."      # khóa ở mục 2
$env:CRM_DB_USER = "..."; $env:CRM_DB_PASS = "..."
cd C:\mcp-database
.venv\Scripts\python.exe scripts\kiem_tra_server.py
```

Nếu dấu nhắc của bạn là `C:\...>` (không có chữ `PS`) thì bạn đang ở `cmd`, và các lệnh `$env:...` sẽ báo "The filename, directory name, or volume label syntax is incorrect". Gõ `powershell` để chuyển, hoặc dùng bản `cmd` (dùng dạng `set "TÊN=giá trị"` để mật khẩu có ký tự đặc biệt không làm hỏng lệnh; trong file `.bat` viết `%%i` thay cho `%i`):

```bat
set "MCP_DATABASE_CONFIG=C:\mcp-database\systems.yaml"
for /f %i in ('py -3 -c "import secrets; print(secrets.token_hex(32))"') do set "MCP_DATABASE_HMAC_KEY=%i"
set "CRM_DB_USER=..."
set "CRM_DB_PASS=..."
cd C:\mcp-database
.venv\Scripts\python.exe scripts\kiem_tra_server.py
```

Trong cả hai trường hợp, biến phải được đặt TRONG CÙNG cửa sổ chạy script: mở cửa sổ mới là mất biến. Nếu script báo "Không kết nối được tới server" mà phía trên không có dòng lỗi nào của server, kiểm tra `MCP_DATABASE_CONFIG` có trỏ đúng file `systems.yaml` đang tồn tại không (`echo %MCP_DATABASE_CONFIG%` trong `cmd`, `$env:MCP_DATABASE_CONFIG` trong PowerShell).

Script khởi động server qua stdio đúng như Claude làm, liệt kê 6 tool và gọi `list_systems`. Nó không chạm vào DB, nên chỉ chứng minh server lên được và cấu hình đọc được. Nếu in `Tool: describe_table, explain_query, ...` và danh sách hệ thống thì ổn. Nếu lỗi, thông báo của server nằm ngay phía trên dòng gợi ý.

Việc kiểm tra kết nối DB thật làm ở mục 6, từ trong Claude.

## 5. Nối Claude Code vào

### Cách nhanh cho một người

```bash
claude mcp add database \
  -e MCP_DATABASE_CONFIG=/opt/mcp-database/systems.yaml \
  -e MCP_DATABASE_HMAC_KEY=... \
  -e CRM_DB_USER=... -e CRM_DB_PASS=... \
  -- /opt/mcp-database/.venv/bin/python -m mcp_database
```

Trên macOS lệnh y hệt, chỉ thay `/opt/mcp-database` bằng `~/mcp-database` (viết đường dẫn tuyệt đối đầy đủ, ví dụ `/Users/ten-ban/mcp-database`, vì `~` có thể không được mở rộng sau `-e` và `--`). Trên Windows (PowerShell):

```powershell
claude mcp add database `
  -e MCP_DATABASE_CONFIG=C:\mcp-database\systems.yaml `
  -e MCP_DATABASE_HMAC_KEY=... `
  -e CRM_DB_USER=... -e CRM_DB_PASS=... `
  -- C:\mcp-database\.venv\Scripts\python.exe -m mcp_database
```

Mặc định lệnh này ghi vào phạm vi `local` (chỉ project hiện tại, chỉ bạn). Dùng `-s user` nếu muốn mọi project của bạn đều thấy. Có một điểm yếu: giá trị truyền bằng `-e` nằm nguyên văn trong file cấu hình của Claude Code (`~/.claude.json`), kể cả mật khẩu DB và khóa HMAC. Chấp nhận được trên máy cá nhân, không nên cho môi trường dùng chung. Cách tốt hơn ở bên dưới.

### Cách nên dùng: file biến môi trường riêng cộng script bọc

Giữ bí mật trong một file chỉ chủ sở hữu đọc được, và để Claude Code chỉ biết đường dẫn script:

```bash
# /etc/mcp-database/env   (chmod 600, thuộc về người dùng chạy Claude)
export MCP_DATABASE_CONFIG=/opt/mcp-database/systems.yaml
export MCP_DATABASE_HMAC_KEY=...
export CRM_DB_USER=...
export CRM_DB_PASS=...
```

```bash
#!/bin/sh
# /opt/mcp-database/chay-mcp.sh   (chmod 755)
. /etc/mcp-database/env
exec /opt/mcp-database/.venv/bin/python -m mcp_database
```

```bash
claude mcp add database -- /opt/mcp-database/chay-mcp.sh
```

Từ đây không còn bí mật nào trong cấu hình của Claude Code, và đổi mật khẩu chỉ là sửa một file.

### Script bọc trên macOS và Windows

macOS dùng được đúng cách của Linux ở trên, chỉ khác đường dẫn: đặt file biến môi trường ở `~/.config/mcp-database/env` (`chmod 600`), script ở `~/mcp-database/chay-mcp.sh` (`chmod 755`), và đăng ký bằng đường dẫn tuyệt đối tới script. Muốn không để bí mật nằm trong file nào cả, cất vào Keychain rồi đọc lúc khởi động:

```bash
# cất một lần (nhập giá trị khi được hỏi, hoặc thay bằng -w '<giá trị>')
security add-generic-password -a "$USER" -s mcp-database-hmac -w
security add-generic-password -a "$USER" -s mcp-database-crm-pass -w
```

```bash
#!/bin/sh
# ~/mcp-database/chay-mcp.sh
export MCP_DATABASE_CONFIG="$HOME/mcp-database/systems.yaml"
export MCP_DATABASE_HMAC_KEY="$(security find-generic-password -a "$USER" -s mcp-database-hmac -w)"
export CRM_DB_USER="ten_user_chi_doc"
export CRM_DB_PASS="$(security find-generic-password -a "$USER" -s mcp-database-crm-pass -w)"
exec "$HOME/mcp-database/.venv/bin/python" -m mcp_database
```

Lần đầu macOS có thể hỏi quyền truy cập Keychain cho tiến trình; chọn cho phép.

Windows không có `sh`, nên dùng file `.cmd`. Đặt hai file này NGOÀI thư mục repo (ví dụ `C:\Users\<tên bạn>\.mcp-database\`) để không bao giờ lỡ commit bí mật, tạo bằng Notepad (lưu UTF-8) chứ đừng gõ bí mật vào dòng lệnh, vì lệnh sẽ nằm trong lịch sử PowerShell. Dạng `set "TÊN=giá trị"` (có ngoặc kép bao cả vế) an toàn với mật khẩu chứa `&`, `|`, `<`, `>`, `#`; vẫn cần tránh `%` và `!` trong mật khẩu.

```bat
:: C:\Users\<tên bạn>\.mcp-database\env.cmd
set "MCP_DATABASE_CONFIG=D:\source\mcp-database-adapter\systems.yaml"
set "MCP_DATABASE_HMAC_KEY=<chuỗi hex 64 ký tự, sinh một lần rồi giữ nguyên>"
set "CRM_DB_USER=<user>"
set "CRM_DB_PASS=<mật khẩu>"
```

```bat
:: C:\Users\<tên bạn>\.mcp-database\chay-mcp.cmd
@echo off
call "%~dp0env.cmd"
"D:\source\mcp-database-adapter\.venv\Scripts\python.exe" -m mcp_database
```

```powershell
# chỉ tài khoản của bạn được đọc và SỬA file chứa bí mật (M = Modify)
icacls "$env:USERPROFILE\.mcp-database\env.cmd" /inheritance:r /grant:r "${env:USERNAME}:(M)"
claude.cmd mcp add database -- cmd /c "$env:USERPROFILE\.mcp-database\chay-mcp.cmd"
```

Dùng `(M)` chứ không phải `(R)`. Bản đầu của hướng dẫn này ghi `(R)` (chỉ đọc) và lỗi đó khiến chính bạn không sửa được `env.cmd` sau đó: Notepad hay `echo >` báo "Access is denied", kể cả khi mở bằng quyền Administrator, vì quyền admin không vượt qua một ACL đã tước quyền ghi của chính chủ file.

**Nếu bạn đã chạy lệnh `(R)` và giờ không sửa được file**, mở PowerShell bằng chính tài khoản đã tạo file (không cần Run as administrator) và chạy:

```powershell
icacls "$env:USERPROFILE\.mcp-database\env.cmd" /grant:r "${env:USERNAME}:(M)"
```

Chủ file luôn được quyền đổi ACL của chính nó, nên lệnh này chạy được dù hiện tại bạn không ghi được. Nếu vẫn báo "Access is denied" thì file đang thuộc về tài khoản khác (ví dụ lúc trước tạo bằng một cửa sổ admin). Khi đó chạy PowerShell bằng quyền Administrator:

```powershell
takeown /f "$env:USERPROFILE\.mcp-database\env.cmd"
icacls "$env:USERPROFILE\.mcp-database\env.cmd" /grant:r "${env:USERNAME}:(M)"
```

Nếu muốn bỏ hẳn các giới hạn đã đặt và để file kế thừa quyền từ thư mục cha: `icacls "$env:USERPROFILE\.mcp-database\env.cmd" /reset`. Mỗi lần sửa xong, cũng nên kiểm tra file không còn do một tài khoản khác sở hữu: `icacls "$env:USERPROFILE\.mcp-database\env.cmd"` (phải thấy tài khoản của bạn với `(M)`, không thấy `Everyone` hay `Users`).

Khóa HMAC chỉ sinh một lần: đổi khóa thì mọi giá trị băm đổi theo (xem mục Xoay khóa HMAC). Gõ `claude.cmd` thay cho `claude` vì PowerShell mặc định chặn chạy `claude.ps1` ("running scripts is disabled on this system"); `claude.cmd` không bị chặn. Cách khác là cho phép script của người dùng hiện tại một lần: `Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned` (đây là thay đổi cấu hình máy, tự cân nhắc). `cmd /c` là cách thông thường để Claude Code trên Windows khởi chạy file `.cmd`. Mật khẩu có `%` hoặc `!` thì đặt biến ở mức người dùng bằng `setx` thay vì `env.cmd`. Cách đăng ký bằng `env.cmd` + `chay-mcp.cmd` ở trên đã được xác nhận chạy được trên Windows; cách `setx` thì chưa được thử.

### Dùng chung trong nhóm qua `.mcp.json`

Với phạm vi `project`, Claude Code ghi vào `.mcp.json` ở thư mục project để cả nhóm dùng chung khai báo (commit được vào git), còn bí mật vẫn nằm ở máy từng người:

```bash
claude mcp add database -s project -- /opt/mcp-database/chay-mcp.sh
```

File sinh ra có dạng:

```json
{
  "mcpServers": {
    "database": {
      "type": "stdio",
      "command": "/opt/mcp-database/chay-mcp.sh",
      "args": []
    }
  }
}
```

Lần đầu mở project, Claude Code hỏi có tin server trong `.mcp.json` không; người dùng phải đồng ý một lần. Đường dẫn script phải giống nhau trên máy mọi người, hoặc dùng biến như `${MCP_DATABASE_HOME}/chay-mcp.sh` (Claude Code mở rộng `${VAR}` trong `.mcp.json`).

### Kiểm tra đã nối được chưa

```bash
claude mcp list            # thấy "database" và trạng thái kết nối
claude mcp get database    # chi tiết một server
```

Trong một phiên Claude Code, gõ `/mcp` để xem server `database` có đang `connected` không và có đủ 6 tool không. Rồi thử:

> Liệt kê các hệ thống database mà bạn truy cập được.

Claude gọi `list_systems`. Nếu trả về tên hệ thống của bạn, đường ống đã thông. Bước kế tiếp là `describe_table` trên một bảng thật để biết kết nối DB và quyền của user chỉ đọc đã đúng.

### Quyền gọi tool

Tool của server hiện ra trong Claude Code với tên `mcp__database__<tên tool>` (`mcp__database__run_query`...). Mặc định Claude hỏi bạn trước mỗi lần gọi. Gợi ý cấu hình trong `.claude/settings.json` của project (hoặc `~/.claude/settings.json`):

```json
{
  "permissions": {
    "allow": [
      "mcp__database__list_systems",
      "mcp__database__get_data_doc",
      "mcp__database__describe_table",
      "mcp__database__explain_query"
    ]
  }
}
```

Bốn tool này không trả dữ liệu dòng nên cho chạy không hỏi cũng ổn. Để `run_query` và `profile_column` vẫn hỏi trong thời gian đầu: bạn nhìn được Claude định chạy câu gì trước khi nó chạy. Khi đã quen, cho phép luôn nếu thấy phiền; audit log vẫn ghi lại mọi câu.

### Claude Desktop

Mở file cấu hình rồi thêm khối `database` vào `mcpServers`. Claude Desktop cũng có thể mở file này từ Settings, mục Developer, nút Edit Config.

- macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

macOS dùng script bọc (đường dẫn tuyệt đối, không dùng `~`):

```json
{
  "mcpServers": {
    "database": {
      "command": "/Users/ten-ban/mcp-database/chay-mcp.sh",
      "args": []
    }
  }
}
```

Windows gọi thẳng `python.exe` của venv (trong JSON phải viết hai dấu `\\` cho mỗi dấu `\`) và đặt biến môi trường trong khối `env`:

```json
{
  "mcpServers": {
    "database": {
      "command": "C:\\mcp-database\\.venv\\Scripts\\python.exe",
      "args": ["-m", "mcp_database"],
      "env": {
        "MCP_DATABASE_CONFIG": "C:\\mcp-database\\systems.yaml",
        "MCP_DATABASE_HMAC_KEY": "...",
        "CRM_DB_USER": "...",
        "CRM_DB_PASS": "..."
      }
    }
  }
}
```

Giá trị trong khối `env` nằm nguyên văn trong file này, nên chỉ dùng cách đó trên máy cá nhân; muốn tránh thì trỏ `command` tới `chay-mcp.cmd` ở mục trên và bỏ khối `env`. Thoát hẳn Claude Desktop rồi mở lại để nó đọc cấu hình mới. Với dữ liệu thật, ưu tiên Claude Code: bạn kiểm soát được từng lần gọi tool qua phần quyền ở mục trên.

## 6. Kiểm tra trên replica thật

Làm một lần cho mỗi hệ thống trước khi dùng thật, trong một phiên Claude. Danh sách đầy đủ, nhất là phần Oracle, ở docs/tham-khao.md cuối file. Bản rút gọn:

1. `describe_table` trên một bảng có thật: thấy cột, chỉ mục, số dòng ước tính.
2. `run_query` một câu đếm có lọc thời gian: ví dụ `SELECT COUNT(*) FROM events WHERE created_at >= DATE '2026-11-01' AND created_at < DATE '2026-11-03'`. Cần trả về một số.
3. Thử một query bị cấm và chắc là nó bị từ chối: thiếu lọc thời gian trên bảng lớn, `SELECT *` trên bảng có cột nhạy cảm, một câu `UPDATE`.
4. `run_query` chọn một cột nhạy cảm: giá trị phải ra dạng băm 16 ký tự hoặc `***`, và `notes` nói rõ cột nào đã bị che.
5. Đăng nhập bằng chính user chỉ đọc đó bằng công cụ DB bình thường và thử `INSERT` hoặc `CREATE TABLE`: phải bị DB từ chối. Đây là bước quan trọng nhất, vì nó kiểm tra lớp bảo vệ chính chứ không phải lớp dự phòng.

Với Oracle, đường truy cập chỉ mới được kiểm bằng test giả, nên coi là thử nghiệm cho tới khi các mục Oracle trong checklist docs/tham-khao.md đạt.

## 7. Dùng để phân tích

Claude làm tốt nhất khi bạn nói rõ câu hỏi và để nó tự khám phá, thay vì đọc cho nó từng câu SQL. Vài kiểu yêu cầu hay dùng:

> Dùng database `crm`, tìm điểm yếu trong luồng đăng ký trong 7 ngày gần nhất: người dùng rớt ở bước nào, lỗi nào phổ biến nhất, có khung giờ nào lỗi tăng không. Đọc mô tả dữ liệu trước. Chỉ chạy query tổng hợp.

> So sánh tỉ lệ giao dịch thất bại theo từng đối tác giữa tuần này và tuần trước. Tách riêng cái gì là số liệu, cái gì là giả định, cái gì là nhận định của bạn, rồi đề xuất điều chỉnh kèm câu query làm bằng chứng.

> Bảng `events` hay chậm vào giờ cao điểm. Xem phân bố số bản ghi theo giờ trong 3 ngày gần nhất và đề xuất chỉ mục hoặc thay đổi hệ thống, nói rõ chỗ nào cần thêm dữ liệu mới chắc chắn.

Trình tự Claude nên đi, và bạn có thể nhắc nếu nó đi tắt: đọc `get_data_doc`, nêu giả thuyết cần kiểm tra, chạy các query tổng hợp (`COUNT`, `GROUP BY`, theo khoảng thời gian ngắn) thay vì kéo dòng chi tiết, gọi `explain_query` trước câu nặng, rồi mới kết luận. Kết luận phải dẫn lại được query nào chứng minh cho nó.

Vài thói quen giúp tránh bực mình:

- Luôn nêu khoảng thời gian. Với bảng lớn, server bắt buộc có cả mốc đầu và mốc cuối trong `WHERE`, tối đa `max_range_days` ngày, và chỉ chấp nhận mốc dạng ngày cụ thể hoặc `SYSDATE`/`NOW()` trừ vài ngày. Nếu Claude viết kiểu khác, nó sẽ bị từ chối kèm gợi ý và tự sửa.
- Kết quả bị cắt ở `max_rows` dòng (mặc định 1000). Muốn cái nhìn tổng thể thì để Claude gộp (`GROUP BY`), đừng bắt nó liệt kê.
- Khi một query bị từ chối, đọc `hint` trong phản hồi: nó nói cách sửa. Danh sách các thông báo thường gặp và cách xử lý ở docs/tham-khao.md mục 9.
- Giá trị băm dùng được để đếm, nhóm và ghép trong cùng một lần triển khai, nhưng không đọc ngược được. Cần tra một thuê bao cụ thể thì lọc `WHERE` theo số thật; server không che điều kiện lọc, chỉ che cột trong kết quả.
- Dữ liệu trong DB có thể chứa chữ nghe như chỉ thị (một ghi chú do người dùng nhập vào, chẳng hạn). Claude đọc nó như dữ liệu, nhưng nếu thấy Claude bất ngờ làm việc không liên quan đến yêu cầu của bạn sau khi đọc một kết quả, dừng lại và xem kết quả đó. Với dữ liệu thật, nên chạy phiên phân tích không bật công cụ gửi dữ liệu ra ngoài (tìm web, gọi API...), hoặc bật xác nhận cho chúng.

## 8. Vận hành

### Thêm một hệ thống mới

1. Tạo user chỉ đọc trên replica của hệ thống đó.
2. Viết file mô tả dữ liệu và khai báo bảng lớn, cột nhạy cảm.
3. Thêm khối vào `systems.yaml` và đặt hai biến user/mật khẩu mới.
4. Chạy `scripts/kiem_tra_server.py`; mở lại phiên Claude (server chỉ đọc cấu hình lúc khởi động).
5. Làm checklist mục 6 cho hệ thống mới.

Lưu ý: nếu một hệ thống thiếu biến môi trường thì cả server không khởi động, chứ không chỉ riêng hệ thống đó. Việc này cố ý để lỗi cấu hình lộ ra ngay, nhưng cũng có nghĩa là thêm hệ thống mới mà quên đặt biến sẽ làm các hệ thống cũ không dùng được cho tới khi sửa.

### Đổi mật khẩu DB

Sửa biến trong file env (hoặc cấu hình nơi bạn đặt), rồi mở lại phiên Claude. Không có gì khác cần đụng.

### Xoay khóa HMAC

Khi khóa lộ, hoặc theo chu kỳ. Điều cần biết: sau khi đổi khóa, cùng một số thuê bao sẽ ra giá trị băm khác, nên các phân tích cũ không ghép được với phân tích mới theo giá trị băm. Nếu bạn lưu kết quả có chứa giá trị băm để so sánh theo thời gian, hãy tính đến việc này trước khi xoay. Đổi khóa không cần sửa cấu hình ngoài biến `MCP_DATABASE_HMAC_KEY`.

### Nâng cấp phiên bản

```bash
cd /opt/mcp-database
git pull
.venv/bin/pip install -e .
.venv/bin/pytest -m "not integration" -q
.venv/bin/python scripts/kiem_tra_server.py
```

Chỉ nâng cận phiên bản `sqlglot` hoặc `mcp` trong `pyproject.toml` khi chạy lại bộ test xong và (nếu có thể) cả test tích hợp MariaDB thật, vì lớp kiểm tra SQL phụ thuộc vào cấu trúc bên trong của sqlglot.

### Audit log

Mỗi lần `run_query` (và hai câu của `profile_column`) ghi một dòng JSON, kể cả khi bị từ chối. `explain_query` và `describe_table` không ghi. Các trường nằm ở docs/tham-khao.md mục 7. File này chứa nguyên văn SQL, nên có thể có số thuê bao hay giá trị nhạy cảm do người dùng gõ vào `WHERE`: giữ quyền 0600, đặt ngoài thư mục được chia sẻ hay sao lưu rộng, và xóa theo chính sách của nhóm.

Một số truy vấn hữu ích (cần `jq`):

```bash
# bao nhiêu query bị từ chối hôm nay và vì sao
jq -r 'select(.status=="rejected") | .reason' audit.jsonl | sort | uniq -c | sort -rn | head

# query chậm nhất
jq -r 'select(.status=="ok") | [.elapsed_ms, .system, .sql] | @tsv' audit.jsonl | sort -rn | head

# lỗi DB theo mã
jq -r 'select(.status=="error") | .code' audit.jsonl | sort | uniq -c | sort -rn

# timeout gần đây
jq -c 'select(.status=="timeout")' audit.jsonl | tail
```

Xoay vòng log bằng `logrotate` với `create 0600 <user> <nhóm>`; server mở file ở chế độ nối thêm cho mỗi lần ghi nên xoay vòng không cần khởi động lại gì. Server từ chối ghi vào file nếu đường dẫn là symlink, và chỉ đặt quyền 0600 cho file do chính nó tạo ra; file đã có sẵn với quyền rộng hơn thì server không tự sửa, bạn phải `chmod` tay.

### Theo dõi tải lên replica

Server đặt giới hạn đồng thời (`max_concurrent`, mặc định 2 query một hệ thống một tiến trình), timeout và trần chi phí EXPLAIN, nhưng đó là giới hạn của từng tiến trình. Mười người dùng mười tiến trình là mười lần giới hạn đó. Hai việc bổ sung nên làm, ở phía DB:

- Đặt giới hạn tài nguyên cho chính user chỉ đọc: Oracle dùng profile với `CPU_PER_CALL`, `LOGICAL_READS_PER_CALL`, `CONNECT_TIME` hoặc Resource Manager; MariaDB dùng `MAX_USER_CONNECTIONS`, `max_statement_time` và giới hạn tài nguyên theo user. Đây mới là mức chặn thật nếu nhiều người cùng chạy.
- Theo dõi độ trễ replication (replica lag) và số kết nối của user đó trong công cụ giám sát bạn đang có.

Khi nhiều người phàn nàn "hệ thống đang bận", đó là giới hạn đồng thời đang hoạt động đúng. Tăng `max_concurrent` chỉ khi replica còn dư tải.

### Chỉnh các ngưỡng

`max_explain_cost` và `max_explain_rows` nghĩa khác nhau giữa Oracle (cardinality lớn nhất trong plan) và MariaDB (tích số dòng trong một câu select, lấy lớn nhất giữa các câu). Chạy vài câu bình thường qua `explain_query` trên replica thật, xem `cost` và `estimated_rows` thường ở mức nào, rồi đặt ngưỡng cao hơn một khoảng đủ cho câu bình thường mà vẫn chặn được quét toàn bảng. Đừng sao chép con số từ hệ thống này sang hệ thống khác.

## 9. Khắc phục sự cố

| Triệu chứng | Thường là | Cách xử lý |
|---|---|---|
| Server không lên, stderr báo "Thiếu biến môi trường ..." | Chưa đặt biến user/mật khẩu hoặc khóa HMAC | Đặt biến được liệt kê, mở lại phiên. Khóa HMAC cần ít nhất 16 byte |
| Server không lên, stderr báo lỗi cấu hình | `systems.yaml` sai: gõ sai tên khóa, thiếu trường bắt buộc, trùng tên bảng sau khi chuẩn hóa | Thông báo nêu tên hệ thống và trường lỗi; khóa lạ ở mọi cấp đều bị từ chối |
| `/mcp` không thấy `database` hoặc ở trạng thái lỗi | Đường dẫn script/Python sai, hoặc server không khởi động | Chạy đúng lệnh `claude mcp get database` rồi chạy tay lệnh đó, hoặc `scripts/kiem_tra_server.py` để xem lỗi |
| Lần đầu mở project không thấy server từ `.mcp.json` | Chưa được phê duyệt | Claude Code hỏi một lần; chấp nhận, hoặc `claude mcp get database` để xem trạng thái chờ duyệt |
| Server mất ~10 giây mới sẵn sàng | Import SDK `mcp` chậm trên một số máy | Bình thường; chỉ ảnh hưởng lúc khởi động |
| Tool trả "Lỗi từ cơ sở dữ liệu (mã ...)" | Lỗi từ DB, server cố ý giấu nội dung chi tiết để lỗi không làm lộ dữ liệu | Xem mã trong audit log (`code`) rồi tra log phía DB |
| Tool báo "hệ thống đang bận" | Đủ `max_concurrent` query đang chạy | Chờ rồi thử lại; đừng ép Claude chạy song song |
| Query bị từ chối với `hint` | Một luật của server | Làm theo `hint`; bảng thông báo thường gặp ở docs/tham-khao.md mục 9 |
| Bảng lớn nhưng không bị bắt buộc lọc thời gian | Không nhận ra bảng lớn (view, chưa có thống kê, tên không có schema trên Oracle) | Khai báo bảng trong `guard.tables` kèm `time_column` |
| Cột nhạy cảm vẫn thấy ở dạng rõ | Cột không được khai báo, hoặc truy vấn qua view/synonym | Khai báo cả tên view/synonym đó trong `sensitive_columns` |
| Query hợp lệ bị từ chối "cú pháp không hỗ trợ" | Câu dùng cấu trúc mà thư viện phân tích SQL không sinh lại chính xác cho DB đích (ví dụ PIVOT trên MariaDB, bảng temporal) | Viết lại bằng cách khác (CASE + GROUP BY thay cho PIVOT) |
| Query chạy lâu rồi báo timeout | Quá `timeout_seconds` (mặc định 30 giây) | Thu hẹp khoảng thời gian hoặc gộp bớt; không tăng timeout khi chưa biết vì sao chậm |

## 10. Danh sách trước khi dùng thật

- User DB chỉ đọc, nối vào replica, không dùng chung với ứng dụng. Đã thử `INSERT` bằng user đó và bị từ chối.
- Mọi cột chứa dữ liệu cá nhân đã nằm trong `sensitive_columns`, kể cả cột văn bản tự do nghi có dữ liệu cá nhân bên trong.
- Mọi bảng lớn đã nằm trong `guard.tables` kèm cột thời gian và `max_range_days`.
- Khóa HMAC riêng cho lần triển khai này, cất ở file quyền 0600, không có trong git, chat hay log.
- Audit log đặt ở nơi an toàn, quyền 0600, có chính sách xoay vòng và xóa.
- Giới hạn tài nguyên cho user DB đã đặt ở phía DB.
- Ngưỡng EXPLAIN đã chỉnh bằng số liệu thật của replica.
- Với Oracle: đã làm xong các mục Oracle trong checklist cuối docs/tham-khao.md.
- Người dùng biết: kết quả chỉ nên đưa cho người có quyền xem dữ liệu đó, và phiên phân tích dữ liệu thật không bật công cụ gửi dữ liệu ra ngoài.
