# Hệ thống billing (ví dụ)

Lưu ý về file này: định dạng hoàn toàn tự do. Server không phân tích nội dung, chỉ trả nguyên văn file cho Claude khi gọi `get_data_doc`, nên viết sao cho một người mới vào nhóm đọc là hiểu và viết đúng SQL. Hệ thống dưới đây là hư cấu, chỉ để minh họa nên viết gì.

Database: Oracle, replica của billing. Mọi bảng nằm trong schema `BILLING`; luôn viết `billing.ten_bang`.

## Cần biết trước khi viết SQL

- `CDR_HISTORY` rất lớn (khoảng 2 tỷ dòng, thêm khoảng 15 triệu dòng mỗi ngày). Luôn lọc theo `CALL_TIME` với cả cận dưới và cận trên, tối đa 7 ngày mỗi query. Cần dài hơn thì chạy nhiều query rồi cộng lại.
- Ưu tiên query tổng hợp: `COUNT`, `SUM`, `GROUP BY` theo ngày/loại cuộc gọi. Kéo dòng chi tiết thường không cần.
- Cột nhạy cảm (mục cuối) bị băm hoặc che trong kết quả. Vẫn dùng để lọc, ghép, nhóm được.
- Dữ liệu trên replica trễ so với hệ thống chính thường dưới 5 phút; đừng dùng nó để kết luận về giao dịch vừa xảy ra.

## Bảng

### CDR_HISTORY (bảng lớn)

Mỗi dòng là một cuộc gọi hoặc một phiên dữ liệu đã tính cước.

| Cột | Kiểu | Ý nghĩa |
|---|---|---|
| CDR_ID | NUMBER | Khóa chính |
| SUB_ID | NUMBER | Khóa ngoại tới `SUBSCRIBER.SUB_ID` |
| MSISDN | VARCHAR2(15) | Số thuê bao, dạng 84xxxxxxxxx (nhạy cảm) |
| IMSI | VARCHAR2(20) | Mã SIM (nhạy cảm) |
| CALL_TIME | DATE | Thời điểm bắt đầu cuộc gọi. Cột thời gian bắt buộc lọc, có chỉ mục |
| DURATION_SEC | NUMBER | Thời lượng giây, 0 nếu cuộc gọi không thành |
| SERVICE_TYPE | VARCHAR2(10) | `VOICE`, `SMS`, `DATA` |
| CHARGE_AMOUNT | NUMBER(18,2) | Cước đã tính, đơn vị VND, trước thuế |
| RATING_STATUS | VARCHAR2(10) | `RATED`, `PENDING`, `FAILED` |

Chỉ mục: `CALL_TIME`; `(SUB_ID, CALL_TIME)`. Lọc theo `SUB_ID` kèm khoảng thời gian là cách nhanh nhất để xem một thuê bao.

Ghi chú nghiệp vụ: cuộc gọi `FAILED` có `CHARGE_AMOUNT = 0`. Dòng `PENDING` có thể được tính lại sau, nên tổng cước của ngày hôm nay và hôm qua chưa ổn định. Khi so sánh doanh thu giữa các ngày, chỉ lấy `RATING_STATUS = 'RATED'`.

### SUBSCRIBER

Mỗi dòng một thuê bao, khoảng 40 triệu dòng, không cần lọc thời gian.

| Cột | Kiểu | Ý nghĩa |
|---|---|---|
| SUB_ID | NUMBER | Khóa chính |
| MSISDN | VARCHAR2(15) | Số thuê bao (nhạy cảm) |
| PLAN_CODE | VARCHAR2(20) | Mã gói cước, tra ở `PLAN` |
| ACTIVATED_AT | DATE | Ngày kích hoạt |
| STATUS | VARCHAR2(10) | `ACTIVE`, `SUSPENDED`, `CLOSED` |

### PLAN

Danh mục gói cước, vài trăm dòng.

| Cột | Kiểu | Ý nghĩa |
|---|---|---|
| PLAN_CODE | VARCHAR2(20) | Khóa chính |
| PLAN_NAME | VARCHAR2(100) | Tên gói |
| MONTHLY_FEE | NUMBER(18,2) | Cước thuê bao tháng, VND |

### INVOICE

Hóa đơn hàng tháng, mỗi thuê bao một dòng mỗi kỳ.

| Cột | Kiểu | Ý nghĩa |
|---|---|---|
| INVOICE_ID | NUMBER | Khóa chính |
| SUB_ID | NUMBER | Khóa ngoại tới `SUBSCRIBER.SUB_ID` |
| PERIOD | VARCHAR2(6) | Kỳ cước, `YYYYMM` |
| TOTAL_AMOUNT | NUMBER(18,2) | Tổng tiền gồm thuế, VND |
| PAID_AT | DATE | Ngày thanh toán, NULL nếu chưa trả |

## Quan hệ

`SUBSCRIBER.SUB_ID` nối với `CDR_HISTORY.SUB_ID` và `INVOICE.SUB_ID`; `SUBSCRIBER.PLAN_CODE` nối với `PLAN.PLAN_CODE`. Nếu cần ghép `CDR_HISTORY` với bảng khác thì lọc thời gian ngay trên `CDR_HISTORY` trong cùng câu SELECT đọc nó (hoặc trong subquery/CTE).

## Ví dụ truy vấn

Số cuộc gọi và cước theo ngày, 7 ngày gần đây:

```sql
SELECT TRUNC(call_time) AS ngay, service_type, COUNT(*) AS calls, SUM(charge_amount) AS revenue
FROM billing.cdr_history
WHERE call_time >= TRUNC(SYSDATE) - 7 AND call_time < TRUNC(SYSDATE)
  AND rating_status = 'RATED'
GROUP BY TRUNC(call_time), service_type
ORDER BY ngay
```

## Cột nhạy cảm

Các cột này bị che trong kết quả. Cấu hình `guard` tương ứng:

- `CDR_HISTORY.MSISDN`, `SUBSCRIBER.MSISDN`: `hash`. Cùng một số luôn ra cùng một giá trị băm nên đếm số thuê bao khác nhau hoặc đối chiếu giữa các query được; không đổi ngược được ra số thật.
- `CDR_HISTORY.IMSI`: `redact`, luôn hiện `***`.

Không lấy cột nhạy cảm qua biểu thức hay hàm (`SUBSTR(msisdn, 1, 5)` sẽ bị từ chối); lấy trần ở SELECT ngoài cùng hoặc chỉ dùng để lọc/nhóm.
