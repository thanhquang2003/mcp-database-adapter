# MCP Database dùng chung — thiết kế

Ngày: 2026-11-17 · Trạng thái: đã triển khai (xem README.md; một số chi tiết đã đổi trong quá trình làm, README là bản chuẩn)

## Mục đích

Người dùng làm R&D và vận hành dịch vụ. Họ cần Claude đọc dữ liệu lịch sử người dùng trong DB của nhiều hệ thống, tìm điểm yếu của hệ thống (lỗi, chậm, người dùng bỏ giữa chừng...) và đề xuất điều chỉnh. Mỗi hệ thống dùng một loại DB (Oracle hoặc MariaDB), có bảng và vấn đề riêng, và có một file mô tả dữ liệu riêng. Cần một MCP server duy nhất dùng chung cho tất cả.

## Mục tiêu

- Một MCP server, nhiều hệ thống. Thêm hệ thống mới chỉ cần thêm profile và file mô tả, không sửa code.
- Claude tự khám phá dữ liệu bằng SQL do chính nó viết, dựa trên file mô tả dữ liệu của từng hệ thống.
- Không làm ảnh hưởng dịch vụ production: mọi query đều bị kiểm soát chi phí trước khi chạy.
- Không để Claude ghi/sửa/xóa dữ liệu, kể cả khi cố tình.
- Hạn chế dữ liệu cá nhân đi qua model.

## Không phải mục tiêu

- Không tự động sửa hệ thống hay chạy DDL/DML.
- Không xây pipeline ETL / kho phân tích ở giai đoạn này (xem hướng C bên dưới).
- Không có giao diện web. Chỉ là MCP server chạy qua stdio cho Claude Code.
- Không hỗ trợ DB khác ngoài Oracle và MariaDB (thiết kế để mở rộng sau, nhưng chưa làm).

## Tiêu chí thất bại (dù kỹ thuật vẫn chạy)

- Một query của Claude làm chậm hoặc treo DB production.
- Số thuê bao hoặc dữ liệu định danh khác lọt ra ngoài dạng rõ.
- Thêm hệ thống mới đòi hỏi phải viết lại code.

## Ràng buộc

- DB: Oracle và MariaDB. Kết nối vào replica hoặc bản sao đọc, không vào primary. Nếu hệ thống nào chưa có replica thì người dùng phải xác nhận rủi ro bằng một cờ trong profile.
- User DB dùng cho MCP chỉ có quyền SELECT (và quyền đọc metadata/EXPLAIN). Đây là lớp bảo vệ chính, lớp kiểm tra trong MCP chỉ là lớp bổ sung.
- Thông tin đăng nhập không nằm trong file cấu hình, chỉ tham chiếu biến môi trường.

## Các hướng đã cân nhắc

- **A. Một MCP dùng chung, cấu hình theo profile (đã chọn).** Ít code nhất, dễ bảo trì, thêm hệ thống bằng cấu hình.
- **B. Mỗi hệ thống một MCP.** Bị loại: code lặp, sửa lỗi phải sửa nhiều nơi.
- **C. Đẩy dữ liệu sang kho phân tích (DuckDB/Parquet).** An toàn nhất cho production nhưng tốn pipeline và dữ liệu bị trễ. Để dành làm sau nếu hướng A vẫn gây tải đáng kể.

## Thiết kế

Ngôn ngữ: Python (lựa chọn của tôi, không phải do người dùng chọn). Driver: `python-oracledb` chế độ thin (không cần Oracle client) và driver MariaDB. Parse SQL bằng `sqlglot`. Chạy qua stdio.

### Cấu hình

Một file `systems.yaml` chứa danh sách profile. Mỗi profile gồm:
- tên hệ thống, loại DB (`oracle` | `mariadb`);
- host/port/service, tên biến môi trường chứa user và password;
- đường dẫn tới file mô tả dữ liệu;
- phần `guard` (tùy chọn): giới hạn riêng của hệ thống, ghi đè mặc định.

File mô tả dữ liệu để dạng tự do, đúng như người dùng đang có. MCP chỉ đọc và trả nguyên văn cho Claude, không ép định dạng. Những thứ MCP cần thực thi bằng máy (bảng lớn, cột thời gian, cột nhạy cảm) khai báo trong `guard`, không bắt người dùng viết lại file mô tả.

Ví dụ `guard`:

```yaml
guard:
  max_rows: 1000
  timeout_seconds: 30
  max_concurrent: 2
  large_table_rows: 5000000     # ghi đè ngưỡng tự phát hiện
  tables:
    CDR_HISTORY:
      time_column: CALL_TIME     # bắt buộc có điều kiện trên cột này
      max_range_days: 31
  sensitive_columns:
    - CDR_HISTORY.MSISDN         # băm trước khi trả về
    - USERS.FULL_NAME            # che hoàn toàn
```

### Các tool

- `list_systems`: liệt kê hệ thống và loại DB.
- `get_data_doc(system)`: trả file mô tả dữ liệu.
- `describe_table(system, table)`: cột, kiểu, index, số dòng ước lượng (lấy từ thống kê của DB).
- `explain_query(system, sql)`: trả kế hoạch thực thi, không chạy query.
- `run_query(system, sql)`: chạy query đã qua kiểm tra, trả kết quả đã che cột nhạy cảm.
- `profile_column(system, table, column)`: phân bố giá trị (top N, số giá trị khác nhau, null), dùng lấy mẫu có kiểm soát thay cho `SELECT *`.

### Kiểm tra trước khi chạy (lớp bảo vệ query)

Mọi `run_query` đi qua các bước sau, dừng ngay khi một bước từ chối và trả lý do kèm gợi ý sửa:

1. Parse bằng `sqlglot` theo đúng dialect. Chỉ cho phép đúng một câu `SELECT` hoặc `WITH ... SELECT`. Chặn nhiều câu lệnh, DDL, DML, `FOR UPDATE`, các hàm có tác dụng phụ.
2. Với bảng lớn (số dòng ước lượng từ thống kê DB vượt `large_table_rows`, hoặc khai báo trong `guard.tables`): bắt buộc có điều kiện trên cột thời gian, và khoảng thời gian không vượt `max_range_days`.
3. Chạy `EXPLAIN` (Oracle: `EXPLAIN PLAN` + `DBMS_XPLAN`; MariaDB: `EXPLAIN`). Từ chối nếu full scan trên bảng lớn, hoặc cost/số dòng ước lượng vượt ngưỡng. Các ngưỡng cụ thể để người triển khai chỉnh khi thử trên dữ liệu thật.
4. Ép giới hạn số dòng (`FETCH FIRST n ROWS` / `LIMIT n`) bọc ngoài query.
5. Đặt timeout phía DB (Oracle `call_timeout`; MariaDB `max_statement_time`) và giới hạn số query chạy đồng thời theo từng hệ thống.
6. Sau khi chạy: băm hoặc che các cột nhạy cảm trong kết quả; nếu query chọn cột nhạy cảm qua alias, ghi nhận và che theo nguồn gốc cột khi xác định được, nếu không xác định được thì từ chối.
7. Ghi log (hệ thống, SQL, thời gian chạy, số dòng, kết quả kiểm tra) vào file log cục bộ.

Mô tả tool cho Claude cần nói rõ: ưu tiên query tổng hợp (`GROUP BY`, `COUNT`), luôn lọc theo thời gian, và đọc `get_data_doc` trước khi viết SQL.

### Quy trình phân tích (dùng ở phía Claude, không phải code)

Với mỗi lần phân tích: đọc file mô tả của hệ thống → hỏi lại người dùng giả thuyết cần kiểm tra → chạy các query tổng hợp → tách số liệu thật, giả định và nhận định → đề xuất điều chỉnh kèm bằng chứng. Có thể đóng gói thành skill sau khi MCP chạy ổn.

## Để người triển khai quyết định

- Giá trị cụ thể của ngưỡng cost, số dòng, timeout (mặc định hợp lý, chỉnh theo thực tế).
- Cách băm cột nhạy cảm (đề xuất HMAC với khóa cục bộ, để vẫn đếm/ghép được theo người dùng mà không lộ giá trị gốc).
- Cấu trúc thư mục, test, đóng gói.
- Cách xử lý chi tiết khi `sqlglot` không parse được một cú pháp riêng của Oracle: mặc định là từ chối.

## Những điểm tôi tự quyết (cần bạn kiểm tra)

1. Python thay vì Node/TypeScript.
2. File mô tả dữ liệu giữ nguyên định dạng tự do; luật chạy bằng máy nằm ở `guard` trong profile.
3. Số dòng của bảng lấy tự động từ thống kê DB, `guard` chỉ ghi đè khi cần.
4. Băm bằng HMAC cho cột nhạy cảm kiểu định danh, che hoàn toàn cho cột kiểu tên/địa chỉ.
5. Nếu không xác định được cột nhạy cảm bị lộ qua biểu thức hoặc alias thì từ chối query, chấp nhận mất một ít tiện lợi để an toàn hơn.
6. Chạy qua stdio, không mở cổng mạng.
