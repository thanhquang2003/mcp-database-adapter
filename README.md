# mcp-database

MCP server (stdio) cho Claude Code, dùng chung trong nhóm, để Claude đọc dữ liệu từ replica Oracle và MariaDB phục vụ phân tích. Chỉ đọc: Claude gửi SQL, server kiểm tra, chạy, che cột nhạy cảm rồi trả kết quả.

## Mô hình an toàn

Lớp bảo vệ chính là user database chỉ có quyền đọc, kết nối vào replica. Dù server có lỗi hay bị lách, user đó không ghi được gì. Phần lọc trong server (một câu SELECT, che cột, giới hạn bảng lớn, EXPLAIN, timeout) là lớp dự phòng và giữ cho replica không bị query nặng làm chậm. Nó không thay được user chỉ đọc, nên đừng cấp quyền rộng rồi trông vào server.

Hệ quả thực tế: tạo user chỉ đọc đúng cách (mục 3) trước khi làm bất cứ việc gì khác.

## 1. Cài đặt và chạy

Cần Python 3.11 trở lên.

```bash
cd /home/aiteam/AI/mcp-database
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp systems.example.yaml systems.yaml   # rồi sửa; systems.yaml bị git bỏ qua
```

Server đọc cấu hình từ các biến môi trường sau:

- `MCP_DATABASE_CONFIG`: đường dẫn tới `systems.yaml`. Không đặt thì dùng `./systems.yaml` theo thư mục đang chạy, nên nên đặt đường dẫn tuyệt đối.
- `MCP_DATABASE_HMAC_KEY`: khóa để băm cột nhạy cảm kiểu `hash`. Bắt buộc khi có bất kỳ hệ thống nào khai báo `sensitive_columns`, tối thiểu 16 byte. Mỗi lần triển khai dùng một khóa riêng, không chia sẻ, không ghi vào log hay chat. Ai có khóa thì dò ngược được giá trị gốc từ giá trị băm (ví dụ thử từng số điện thoại).
- Biến chứa user và mật khẩu DB: tên do từng profile chọn qua `user_env` và `password_env` (ví dụ `BILLING_DB_USER`, `BILLING_DB_PASS`). Mật khẩu không bao giờ nằm trong `systems.yaml`; ghi `password:` trực tiếp là server không chịu khởi động.

Thiếu biến nào thì server thoát ngay với mã 1 và in TÊN biến còn thiếu ra stderr (không in giá trị). Cấu hình sai (khóa lạ, thiếu trường, `db_type` lạ...) cũng thoát với thông báo cụ thể.

Chạy thử bằng tay (sẽ chờ giao thức MCP trên stdin, Ctrl+C để thoát):

```bash
MCP_DATABASE_CONFIG=$PWD/systems.yaml MCP_DATABASE_HMAC_KEY='<chuỗi ngẫu nhiên ≥16 ký tự>' \
BILLING_DB_USER=... BILLING_DB_PASS=... CRM_DB_USER=... CRM_DB_PASS=... \
.venv/bin/python -m mcp_database
```

Mỗi lần gọi mở một kết nối mới và đóng ngay sau đó; chưa có connection pool.

## 2. Cấu hình `systems.yaml`

Xem `systems.example.yaml`. Cấu trúc:

```yaml
audit_log: audit.jsonl          # tính từ thư mục chứa systems.yaml; bỏ trống = không ghi

systems:
  billing:                      # tên hệ thống, Claude dùng tên này khi gọi tool
    db_type: oracle             # oracle | mariadb
    host: billing-replica.example.local
    port: 1521
    database: BILLPDB           # Oracle: service name; MariaDB: tên database
    user_env: BILLING_DB_USER
    password_env: BILLING_DB_PASS
    data_doc: docs/billing.md   # file mô tả dữ liệu, tính từ thư mục chứa systems.yaml
    is_replica: true
    guard:
      max_rows: 1000
      timeout_seconds: 30
      max_concurrent: 2
      large_table_rows: 5000000
      max_explain_cost: 100000
      max_explain_rows: 5000000
      tables:
        cdr_history:
          time_column: call_time
          max_range_days: 7
      sensitive_columns:
        cdr_history.msisdn: hash
        cdr_history.imsi: redact
```

Các trường bắt buộc của mỗi hệ thống: `db_type`, `host`, `port` (1..65535), `database`, `user_env`, `password_env`, `data_doc`, và một trong `is_replica: true` hoặc `allow_primary: true`. Khóa không nằm trong danh sách này (ở cả cấp hệ thống, `guard` lẫn `guard.tables.*`) bị từ chối, nên gõ sai tên khóa sẽ báo lỗi chứ không bị bỏ qua lặng lẽ.

`guard` có thể bỏ trống. Mặc định và ý nghĩa:

| Khóa | Mặc định | Ý nghĩa |
|---|---|---|
| `max_rows` | 1000 | Số dòng tối đa trả về |
| `timeout_seconds` | 30 | Timeout mỗi câu, đặt phía DB |
| `max_concurrent` | 2 | Số thao tác đồng thời tối đa trên hệ thống này |
| `large_table_rows` | 5.000.000 | Bảng có số dòng thống kê từ ngưỡng này trở lên coi là bảng lớn |
| `max_explain_cost` | 100.000 | Chặn nếu cost trong EXPLAIN vượt ngưỡng (chỉ Oracle) |
| `max_explain_rows` | 5.000.000 | Chặn nếu số dòng ước tính vượt ngưỡng |
| `tables` | rỗng | Bảng lớn khai báo tay: `time_column` và `max_range_days` (cả hai bắt buộc, số ngày > 0) |
| `sensitive_columns` | rỗng | `TABLE.COLUMN: hash` hoặc `redact` |

Tên bảng và cột được chuẩn hóa (bỏ nháy, bỏ schema, không phân biệt hoa thường), nên `cdr_history` khớp với `BILLING.CDR_HISTORY`. Hệ quả: hai bảng cùng tên ở hai schema dùng chung một cấu hình.

`is_replica` và `allow_primary`: `is_replica: true` là bạn khai báo rằng `host` trỏ vào replica. Server không tự kiểm tra được điều đó, nó chỉ buộc bạn phải nói rõ. Nếu chủ ý đọc từ primary thì đặt `allow_primary: true` (và nên có DBA đồng ý); thiếu cả hai là lỗi cấu hình. Đọc từ primary chỉ nên là ngoại lệ vì truy vấn phân tích có thể kéo chậm hệ thống đang chạy thật.

### Thêm một hệ thống mới

1. Nhờ DBA tạo user chỉ đọc trên replica (mục 3).
2. Viết file mô tả dữ liệu, bắt đầu từ `docs/example-data-doc.md` (bảng, cột, quan hệ, cột thời gian, cột nhạy cảm, lưu ý nghiệp vụ). File này là thứ Claude đọc đầu tiên nên đáng đầu tư.
3. Thêm profile vào `systems.yaml`, đặt biến môi trường cho user/mật khẩu.
4. Khai báo `guard`: với mỗi bảng lớn, ghi `time_column` và `max_range_days`; liệt kê mọi cột nhạy cảm (số điện thoại, IMSI, email, tên, địa chỉ, số tài khoản...) kể cả ở bảng nhỏ. Server chỉ che những cột có trong danh sách, không đoán.
5. Khởi động lại Claude Code, rồi làm checklist "Kiểm tra trên replica thật" ở cuối file này.

## 3. Tạo user chỉ đọc

Đây là lớp bảo vệ chính, làm trên replica.

Oracle:

```sql
CREATE USER mcp_ro IDENTIFIED BY "<mật khẩu mạnh>";
GRANT CREATE SESSION TO mcp_ro;
-- chỉ đúng những bảng cần phân tích, từng bảng một:
GRANT SELECT ON billing.cdr_history TO mcp_ro;
GRANT SELECT ON billing.subscriber  TO mcp_ro;
```

Không cấp: bất kỳ quyền DDL nào (`CREATE TABLE`, `ALTER`...), `EXECUTE` trên package (nhất là `DBMS_*`, `UTL_*`), `SELECT ANY TABLE`, `SELECT_CATALOG_ROLE`/`DBA`, role nào có `SELECT ANY`. Không cấp quyền trên bảng nhạy cảm không cần dùng. Giao dịch `READ ONLY` mà server đặt cho session chỉ là dự phòng thêm: PL/SQL có thể COMMIT hay chạy DDL ngầm và thoát khỏi chế độ đó, nên quyền của user mới là thứ chặn thật. User không có schema riêng nên Claude cần viết `billing.cdr_history`, nhớ ghi tên đầy đủ trong data doc. Server dùng `EXPLAIN PLAN`, cần `PLAN_TABLE` dùng được cho user; Oracle 12c trở lên thường có sẵn bảng tạm dùng chung, nếu `explain_query` báo lỗi thì nhờ DBA kiểm tra chỗ này (chưa kiểm chứng trên Oracle thật).

MariaDB:

```sql
CREATE USER 'mcp_ro'@'<địa chỉ máy chạy server>' IDENTIFIED BY '<mật khẩu mạnh>' WITH MAX_USER_CONNECTIONS 5;
GRANT SELECT ON crm.* TO 'mcp_ro'@'<địa chỉ máy chạy server>';
```

Chỉ `SELECT`, không `SHOW VIEW`/`EXECUTE`/`FILE`/`PROCESS`. Kết nối vào replica, không vào primary. Nếu không muốn cấp cả database, grant theo từng bảng: `GRANT SELECT ON crm.customers TO ...`.

Lưu ý về view và synonym: che cột chỉ áp dụng cho tên bảng/cột có trong `guard.sensitive_columns`. Một view hay synonym phủ lên bảng nhạy cảm (ví dụ `v_customers` bọc `customers`) KHÔNG tự được che; phải khai báo `v_customers.email: redact` thì mới che. Cách an toàn nhất là không cấp quyền trên view/synonym đó, hoặc liệt kê chúng ra rõ ràng.

## 4. Đăng ký với Claude Code

```bash
claude mcp add database \
  -e MCP_DATABASE_CONFIG=/home/aiteam/AI/mcp-database/systems.yaml \
  -e MCP_DATABASE_HMAC_KEY='<khóa ≥16 byte>' \
  -e BILLING_DB_USER=mcp_ro -e BILLING_DB_PASS='<mật khẩu>' \
  -e CRM_DB_USER=mcp_ro -e CRM_DB_PASS='<mật khẩu>' \
  -- /home/aiteam/AI/mcp-database/.venv/bin/python -m mcp_database
```

Giá trị truyền bằng `-e` được lưu nguyên văn trong file cấu hình của Claude Code (`~/.claude.json` với scope mặc định `local`), nên giữ file đó chỉ cho chủ sở hữu đọc. Đừng dùng `--scope project`: nó ghi vào `.mcp.json` trong repo và dễ bị commit kèm mật khẩu. Nếu muốn tránh để bí mật trong file, bỏ các `-e` nhạy cảm và export biến trong shell trước khi mở `claude` (server kế thừa môi trường của tiến trình `claude`; hãy kiểm tra bằng `claude mcp list` hoặc `/mcp` rằng server lên được).

Kiểm tra: `claude mcp list`, hoặc gõ `/mcp` trong Claude Code. Gọi thử `list_systems` sẽ thấy các hệ thống đã khai báo.

## 5. Sáu tool

Mọi tool lỗi đều trả `{"error": ..., "hint": ...}` thay vì ném ngoại lệ (riêng `get_data_doc` trả chuỗi `LỖI: ...`). Server không trả host, port, user, tên biến môi trường hay đường dẫn trên máy chủ.

- `list_systems()`: danh sách `{name, db_type}`.
- `get_data_doc(system)`: trả nguyên văn file `data_doc` (cắt ở 200.000 ký tự). Claude luôn phải đọc cái này trước khi viết SQL.
- `describe_table(system, table)`: cột (tên, kiểu, nullable, `sensitive` = chế độ che nếu có), chỉ mục, số dòng ước tính từ thống kê DB. Tên bảng dạng `bảng` hoặc `schema.bảng`.
- `explain_query(system, sql)`: chạy toàn bộ kiểm tra và EXPLAIN nhưng không chạy câu SQL. Trả `{allowed, reasons, hints, plan, cost, estimated_rows}`. Nếu `allowed` là false thì sửa theo `hints`.
- `run_query(system, sql)`: chạy câu SELECT. Thành công trả `{columns, rows, row_count, truncated, elapsed_ms, notes}`; `notes` cho biết cột nào đã bị băm/che và khi nào kết quả bị cắt.
- `profile_column(system, table, column, time_from?, time_to?, top_n=10)`: thống kê một cột (tổng dòng, khác NULL, số giá trị phân biệt, top giá trị phổ biến, `top_n` trong 1..50). `time_from`/`time_to` dạng `YYYY-MM-DD`, `time_to` không bao gồm. Server tự dựng hai câu tổng hợp và chạy qua đúng pipeline của `run_query` (cũng ghi audit). Không dùng được với cột nhạy cảm; bảng lớn vẫn bắt buộc có cả hai mốc thời gian.

Quy trình phân tích nên theo: đọc data doc, nói ra giả thuyết cần kiểm tra, chạy query tổng hợp (`COUNT`, `GROUP BY`, theo khoảng thời gian ngắn) thay vì kéo dòng chi tiết, rồi trình bày kết quả tách ba loại: số liệu lấy từ query, giả định chưa kiểm chứng, và nhận định của người phân tích. Kết luận và đề xuất phải dẫn lại được query nào chứng minh cho nó. Trước câu SQL nặng, gọi `explain_query` trước.

## 6. Cách server kiểm soát query

Pipeline chạy theo thứ tự này và dừng ở bước đầu tiên từ chối:

1. Độ dài: tối đa 20.000 ký tự.
2. Chỉ một câu `SELECT` hoặc `WITH ... SELECT`. Bị từ chối: INSERT/UPDATE/DELETE/MERGE, DDL, `SELECT ... INTO`, khóa (`FOR UPDATE`, `LOCK IN SHARE MODE`), nhiều câu lệnh, gán biến `@a := ...`. Hàm có tác dụng phụ: `SLEEP`, `BENCHMARK`, `LOAD_FILE`, `GET_LOCK` và họ hàng, `NEXTVAL`/`SETVAL`, mọi hàm trong package `DBMS_*`/`UTL_*`, gọi hàm qua `schema.package.hàm`. Database link (`bảng@link`). Hàm XML/XQuery/URI (`XMLTABLE`, `XMLQUERY`, `EXTRACTVALUE`, `EXISTSNODE`, `DBURITYPE`...) vì chúng đọc bảng bằng đường dẫn dạng chuỗi, vượt qua kiểm tra theo tên bảng. Comment thực thi của MariaDB (`/*! ... */`, `/*M! ... */`). Optimizer hint và mọi comment bị bỏ đi: SQL gửi xuống DB là SQL do server sinh lại từ cây đã parse, không phải chuỗi gốc.
3. Che cột nhạy cảm. Cột trong `sensitive_columns` được dùng tự do để lọc, ghép (`JOIN ... ON`), `GROUP BY`, `HAVING`, `ORDER BY`. Muốn lấy giá trị ra thì phải là cột trần ở `SELECT` ngoài cùng (có thể đặt alias): `hash` trả 16 ký tự hex đầu của HMAC-SHA256 (cùng giá trị gốc ra cùng giá trị băm nên vẫn đếm/nhóm/đối chiếu được), `redact` trả `***`. Nếu một cột khớp cả hai chế độ thì `redact` thắng. Đặt cột trong biểu thức, hàm, subquery, CTE, `UNION`/`MINUS`, hay dùng `SELECT *` trên bảng có cột nhạy cảm đều bị từ chối (`COUNT(*)` thì được). Cần `MCP_DATABASE_HMAC_KEY` để băm; thiếu khóa mà query lấy cột nhạy cảm thì bị từ chối.
4. Bảng lớn là bảng có trong `guard.tables`, hoặc có số dòng thống kê của DB (`all_tables.num_rows` với Oracle, `information_schema.tables.table_rows` với MariaDB) từ `large_table_rows` trở lên. Bảng không có thống kê (view, bảng chưa `ANALYZE`) không bị coi là lớn; chỉ cổng EXPLAIN bắt chúng, muốn chặt hơn thì khai báo vào `guard.tables`. Với mỗi lần đọc bảng lớn, `WHERE` của chính câu SELECT đó phải có cận dưới trên cột thời gian, nối bằng `AND`, không nằm trong `OR`/`NOT`, không bọc hàm quanh cột (`TRUNC(cot)` không được). Khoảng từ cận dưới đến cận trên không vượt `max_range_days`. Thiếu cận trên thì cận trên ngầm định là hôm nay. Cận chấp nhận được:
   - literal ngày: `'2026-11-15'`, `'2026-11-15 10:00:00'`, `DATE '2026-11-15'`, `TIMESTAMP '...'`, `CAST('...' AS DATE)`;
   - Oracle: `TO_DATE` với đúng ba định dạng `YYYY-MM-DD`, `YYYY-MM-DD HH24:MI:SS`, `YYYYMMDD`. MariaDB: `STR_TO_DATE` chỉ nhận `%Y-%m-%d` và `%Y%m%d` (dạng có giờ thì dùng literal `'2026-11-15 10:00:00'`);
   - `SYSDATE`/`NOW()`/`CURRENT_DATE`/`CURRENT_TIMESTAMP`, có thể `TRUNC(SYSDATE)`, cộng/trừ n ngày: Oracle `SYSDATE - 3` hoặc `SYSDATE - INTERVAL '3' DAY`, MariaDB `NOW() - INTERVAL 3 DAY`. Cận tương đối tính theo ngày, bỏ qua giờ, nên khoảng thực tế có thể dài hơn giới hạn tới khoảng một ngày. Cận trên ngầm định "hôm nay" cũng cùng độ lệch đó.
   
   Dạng khác (ví dụ `TO_DATE('15/11/2026','DD/MM/YYYY')`) bị từ chối kèm gợi ý, vì không tính được khoảng thì không kiểm được. Trên bảng lớn cũng cấm `PIVOT`/`UNPIVOT`, `CONNECT BY`, `MATCH_RECOGNIZE` trực tiếp (WHERE chỉ lọc kết quả của chúng, bảng vẫn bị đọc hết); lọc thời gian trong subquery rồi mới dùng.
5. Giới hạn dòng: server tự thêm `FETCH FIRST n ROWS ONLY` (Oracle) hoặc `LIMIT n` (MariaDB) với `n = max_rows`, hoặc lấy số nhỏ hơn nếu câu SQL đã có giới hạn số nguyên. `FETCH ... PERCENT`, `WITH TIES`, `LIMIT` kèm offset hoặc biểu thức bị từ chối.
6. EXPLAIN: chạy EXPLAIN trên SQL cuối. Từ chối nếu full scan trên bảng lớn, nếu cost vượt `max_explain_cost`, hoặc số dòng ước tính vượt `max_explain_rows`. MariaDB không báo cost nên chỉ có ngưỡng số dòng (số dòng ước tính là tích các dòng trong plan, hay bị ước thừa với join). Hai ngưỡng mặc định chỉ là điểm xuất phát: phải chỉnh theo replica thật.
7. Timeout: `timeout_seconds` đặt phía DB (`call_timeout` ở Oracle, `max_statement_time` ở MariaDB). Quá thì trả lỗi timeout.
8. Đồng thời: tối đa `max_concurrent` thao tác trên mỗi hệ thống. Hết chỗ thì từ chối ngay với "hệ thống đang bận", không xếp hàng; thử lại sau.

Sau khi chạy: băm/che cột, cắt kết quả ở `max_rows` (đặt `truncated`), và mỗi ô quá 2000 ký tự bị cắt, dữ liệu nhị phân chỉ trả kích thước.

## 7. Audit log

`audit_log` trong `systems.yaml` là đường dẫn file JSONL, mỗi lần `run_query` (và hai câu của `profile_column`) ghi đúng một dòng, kể cả khi bị từ chối. `explain_query` và `describe_table` không ghi. Các trường:

- `ts`: thời điểm UTC, ISO 8601
- `system`
- `sql`: câu SQL Claude gửi, cắt ở 2000 ký tự
- `status`: `ok`, `rejected`, `timeout` hoặc `error`
- `reason`: lý do từ chối hoặc thông báo lỗi đã làm sạch (null khi `ok`)
- `elapsed_ms`
- `row_count`: số dòng trả về (null khi lỗi)
- `error_type`, `code`: có khi `status` là `timeout`/`error`; `code` là mã lỗi DB (ví dụ `ORA-00942` hoặc số của MariaDB)

Quan trọng: file này chứa nguyên văn SQL, mà SQL có thể có giá trị nhạy cảm gõ trong `WHERE` (ví dụ `WHERE msisdn = '84912345678'`). Việc che cột chỉ áp dụng cho kết quả trả về, không áp dụng cho audit. Hãy coi file audit là dữ liệu nhạy cảm: đặt quyền `0600`, đặt ngoài thư mục được chia sẻ hay sao lưu rộng rãi, và xoay vòng/xóa theo chính sách của nhóm. Server không tự đặt quyền file; quyền phụ thuộc `umask` của tiến trình, nên tạo sẵn file với `touch audit.jsonl && chmod 600 audit.jsonl` hoặc đặt `umask 077` trước khi chạy. Nếu ghi log lỗi (hết đĩa, không có quyền) thì server in một cảnh báo ra stderr rồi bỏ qua, không làm hỏng query.

## 8. Giới hạn đã biết

Những điều dưới đây là rủi ro còn lại đã chấp nhận, không phải lỗi sẽ được sửa trong khuôn khổ hiện tại.

- Cột nhạy cảm vẫn dùng được trong `WHERE`/`JOIN`. Người có chủ ý có thể suy ra giá trị bằng cách lọc lặp đi lặp lại và `COUNT` (ví dụ đoán từng chữ số của số điện thoại). Server không phát hiện được kiểu thăm dò này; audit log là chỗ để thấy nó.
- Sinh hàng bằng cách `CROSS JOIN` các bảng lớn vẫn dùng để dò giá trị được. `CONNECT BY`, CTE đệ quy và bảng `seq_N_to_M` chỉ bị chặn khi query chạm bảng có cột nhạy cảm; chỗ khác vẫn dùng được.
- Với hệ thống có khai báo `sensitive_columns`, các nguồn dòng không phải bảng thật (`TABLE()`, `JSON_TABLE`, `VALUES`, `UNNEST`...) bị từ chối trên hệ thống đó, kể cả khi query không đụng tới bảng nhạy cảm. Hệ thống không khai báo cột nhạy cảm thì không bị hạn chế này.
- Văn bản lỗi gốc của DB không bao giờ đưa cho model, chỉ có mã lỗi (vì lỗi ép kiểu có thể in ra giá trị đã che). Hệ quả: khi Claude gặp lỗi `Lỗi từ cơ sở dữ liệu (mã ...)`, muốn biết nguyên nhân phải tra mã đó, xem audit log hoặc log của DB.
- Đường Oracle mới kiểm tra với test double, chưa chạy trên Oracle thật. Đường MariaDB đã chạy với container thật. Làm checklist bên dưới trước khi tin Oracle.
- Parser hiện vẫn lặng lẽ bỏ `PIVOT` khi đọc cú pháp MariaDB (MariaDB không hỗ trợ `PIVOT`; sqlglot chỉ ghi cảnh báo "PIVOT unsupported" ra stderr). Câu SQL gửi xuống DB là bản không có `PIVOT`, nên kết quả khác ý người viết nhưng không có đường nào để lách kiểm tra. Đây là lỗi đã biết, đang được sửa.
- Số dòng bảng lấy từ thống kê của DB, có thể cũ hoặc ước chừng (đặc biệt `table_rows` của InnoDB). Bảng thực tế lớn nhưng thống kê cũ có thể không bị coi là lớn; lúc đó chỉ còn cổng EXPLAIN.
- Cận thời gian tương đối lệch tới khoảng một ngày so với giới hạn (đã nói ở trên).

## 9. Thông báo từ chối thường gặp

| Thông báo (rút gọn) | Cách xử lý |
|---|---|
| `Chỉ cho phép SELECT, không cho phép ...` / `Chỉ được gửi một câu lệnh` | Gửi đúng một câu SELECT hoặc WITH ... SELECT, bỏ dấu `;` thừa giữa câu. |
| `Không parse được câu SQL theo cú pháp ...` | Sửa cú pháp theo đúng dialect của hệ thống (Oracle hay MariaDB). |
| `Hàm X bị chặn vì có tác dụng phụ` / `Không cho gọi hàm qua schema/package` | Bỏ hàm đó, chỉ dùng hàm tính toán thông thường. |
| `Không cho dùng hàm XML/XPath/URI ...` | Truy vấn trực tiếp bảng/cột bằng SELECT thường. |
| `Không cho truy vấn qua database link` | Chỉ truy vấn bảng trong chính hệ thống đó. |
| `Bảng lớn X bắt buộc có điều kiện cận dưới ...` | Thêm vào WHERE: `cột_thời_gian >= <cận dưới> AND cột_thời_gian < <cận trên>`. |
| `Khoảng thời gian ... vượt giới hạn N ngày` | Thu hẹp, hoặc chia thành nhiều query. Nếu thiếu cận trên thì cận trên bị tính là hôm nay; thêm cận trên. |
| `Không tính được cận thời gian ...` | Dùng literal ngày, `DATE '...'`, `TO_DATE` đúng ba định dạng, hoặc `SYSDATE`/`NOW()` ± n ngày. |
| `Bảng X là bảng lớn nhưng chưa khai báo cột thời gian` | Cần quản trị thêm `guard.tables.X` vào `systems.yaml`, hoặc dùng bảng khác. |
| `Không cho dùng PIVOT/UNPIVOT (CONNECT BY ...) trên bảng lớn` | Lọc thời gian trong subquery/CTE rồi mới áp toán tử trên kết quả. |
| `Cột nhạy cảm X nằm ở vị trí không che được` | Chỉ lấy cột trần ở SELECT ngoài cùng; muốn đếm thì đặt ở WHERE/GROUP BY, dùng `COUNT(*)`. |
| `Không cho dùng * vì bảng ... có cột nhạy cảm` | Liệt kê rõ tên cột cần lấy. |
| `Không cho đọc dữ liệu từ nguồn không phải bảng` | Hệ thống có cột nhạy cảm: đọc từ bảng thật hoặc subquery, không dùng `TABLE()`/`JSON_TABLE`/`VALUES`. |
| `Không cho dùng CONNECT BY / CTE đệ quy / bảng sinh dãy số khi query có bảng chứa cột nhạy cảm` | Lọc hoặc đếm trực tiếp trên bảng đó. |
| `Truy vấn lấy cột nhạy cảm nhưng server chưa được cấu hình khóa băm` | Bỏ cột khỏi SELECT, hoặc quản trị đặt `MCP_DATABASE_HMAC_KEY`. |
| `Truy vấn quét toàn bộ bảng lớn X` | Thêm điều kiện trên cột thời gian hoặc cột có chỉ mục. |
| `Chi phí kế hoạch thực thi ... vượt ngưỡng` / `Số dòng ước tính ... vượt ngưỡng` | Thu hẹp khoảng thời gian, lọc chọn lọc hơn, dùng query tổng hợp. Nếu ngưỡng quá chặt cho replica của bạn thì chỉnh `max_explain_*`. |
| `Không hỗ trợ giới hạn dòng dạng ...` | Dùng `FETCH FIRST n ROWS ONLY` hoặc `LIMIT n` với số nguyên. |
| `hệ thống đang bận` | Thử lại sau; tăng `max_concurrent` nếu replica chịu được. |
| `query vượt N giây` | Thu hẹp khoảng thời gian hoặc dùng query tổng hợp. |
| `Lỗi từ cơ sở dữ liệu (mã ...)` | Kiểm tra cú pháp, tên bảng/cột; tra mã trong audit log hoặc log DB. |
| `Hệ thống 'X': thiếu biến môi trường ...` | Quản trị đặt biến user/mật khẩu cho hệ thống đó. |

## 10. Chạy test

Unit test, không cần database:

```bash
.venv/bin/pytest -m "not integration" -q
```

Integration test chỉ chạy khi có biến DSN, dạng `user:password@host:port/database` (ký tự đặc biệt trong user/mật khẩu phải mã hóa kiểu URL). Test MariaDB tạo và xóa bảng `it_t` nên cần user có quyền ghi trên database thử, đừng trỏ vào database thật. Dựng MariaDB bằng docker:

```bash
docker run -d --name mcp-mariadb-test -e MARIADB_ROOT_PASSWORD=test -e MARIADB_DATABASE=testdb -p 3307:3306 mariadb:11
# chờ vài giây cho DB khởi động xong, rồi:
TEST_MARIADB_DSN='root:test@127.0.0.1:3307/testdb' .venv/bin/pytest tests/integration/test_mariadb.py -v
docker rm -f mcp-mariadb-test
```

Test Oracle cần một Oracle thật (đặt `TEST_ORACLE_DSN=user:password@host:port/service`), không có container đi kèm.

## Kiểm tra trên replica thật

Chưa chạy bước này trên DB thật. Với mỗi hệ thống mới, làm lần lượt trong Claude Code (hoặc gọi tool trực tiếp) và so với kết quả mong đợi:

1. `list_systems`: thấy hệ thống vừa thêm. `get_data_doc`: ra nội dung file mô tả.
2. `describe_table` trên một bảng lớn: có cột, chỉ mục, `estimated_rows` khác null; cột nhạy cảm hiện `sensitive: "hash"` hoặc `"redact"`. Nếu `estimated_rows` là null thì bảng chưa có thống kê, nhớ khai báo vào `guard.tables`.
3. `explain_query` với điều kiện thời gian hợp lệ trên bảng lớn (cận dưới và cận trên, trong `max_range_days`): `allowed: true`, có `plan`, `cost` (Oracle) và `estimated_rows`. Ghi lại cost và số dòng điển hình của query bình thường để chọn `max_explain_cost` / `max_explain_rows`, rồi cập nhật `guard` trong `systems.yaml` và `systems.example.yaml`.
4. `run_query` tổng hợp (`COUNT(*)`, `GROUP BY` theo ngày) trong khoảng ngắn: trả đúng số, `truncated: false`.
5. `run_query` lấy một cột nhạy cảm dạng `hash`: giá trị là 16 ký tự hex, `notes` nói cột đã bị băm; chạy hai lần ra cùng giá trị.
6. `profile_column` trên một cột thường, có `time_from`/`time_to`: ra tổng, số phân biệt, top giá trị.
7. Các query phải bị từ chối:
   - bảng lớn không có điều kiện thời gian: "bắt buộc có điều kiện cận dưới";
   - khoảng thời gian dài hơn `max_range_days`;
   - `SELECT *` trên bảng có cột nhạy cảm;
   - `UPDATE ...`: "Chỉ cho phép SELECT";
   - `SELECT ... FOR UPDATE`: "thành phần bị cấm";
   - query gây full scan hoặc cost/số dòng vượt ngưỡng: bị cổng EXPLAIN chặn.
8. Đăng nhập thẳng bằng user `mcp_ro` bằng công cụ SQL của bạn và thử `INSERT`/`CREATE TABLE`: phải bị DB từ chối. Đây là kiểm tra quan trọng nhất của cả hệ thống.
9. Mở file audit: có các dòng tương ứng, quyền file là `0600`.
