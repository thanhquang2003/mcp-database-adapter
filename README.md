# mcp-database-adapter

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
export MCP_DATABASE_HMAC_KEY=$(openssl rand -hex 32)   # bắt buộc nếu có cột nhạy cảm; cất kỹ
export BILLING_DB_USER=... BILLING_DB_PASS=...         # tên biến do từng profile chọn
.venv/bin/python scripts/kiem_tra_server.py            # server lên được, thấy đủ 6 tool
```

Nối vào Claude Code:

```bash
claude mcp add database \
  -e MCP_DATABASE_CONFIG=$PWD/systems.yaml \
  -e MCP_DATABASE_HMAC_KEY=... -e BILLING_DB_USER=... -e BILLING_DB_PASS=... \
  -- $PWD/.venv/bin/python -m mcp_database
```

Cách này để bí mật nằm nguyên văn trong cấu hình của Claude Code. Cách tốt hơn (file biến môi trường quyền 0600 cộng script bọc, dùng chung trong nhóm qua `.mcp.json`, và cấu hình Claude Desktop) ở `docs/huong-dan.md`.

Rồi hỏi Claude, ví dụ: "Dùng database billing, tìm điểm yếu trong luồng nạp tiền 7 ngày gần nhất. Đọc mô tả dữ liệu trước và chỉ chạy query tổng hợp."

## Tài liệu

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
