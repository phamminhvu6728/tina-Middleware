# TopCV & OCR Middleware Service for Tina CRM

Microservice độc lập (standalone) chịu trách nhiệm:
1. Nhận Webhook ứng viên từ TopCV (hoặc ATS/Google Forms).
2. Tự động tải file CV từ link bảo vệ `onetime-download` của TopCV (vượt qua Cloudflare Bot Management bằng browser TLS impersonation).
3. Bóc tách nội dung CV:
   - **Tối ưu siêu nhanh (0.05s)**: Đọc trực tiếp text layer số bằng PyMuPDF (`fitz`).
   - **Tự động nhận diện CV scan/ảnh**: Chuyển trang sang độ phân giải 200 DPI và chạy OCR tiếng Việt (Tesseract / PaddleOCR).
4. Đẩy payload chứa thông tin ứng viên kèm `cv_text` hoàn chỉnh về Workflow Webhook của Tina CRM để AI Agent chấm điểm AHP matching.

---

## 🚀 Triển khai nhanh với Docker Compose (Khuyên dùng)

### Bước 1: Chuẩn bị biến môi trường
Tạo file `.env` từ `.env.example`:
```bash
cp .env.example .env
```
Chỉnh sửa `TINA_WEBHOOK_URL` trỏ tới webhook của Tina CRM:
```env
PORT=8000
HOST=0.0.0.0
TINA_WEBHOOK_URL=http://<IP_HOAC_DOMAIN_TINA_CRM>:3000/webhooks/workflows/5b8996b9-379d-4af4-87ab-f9215a4ebb32/85b47d46-39c1-4db7-8a97-2dd8634ef000
DEFAULT_PM_EMAIL=tuyendung@tinasoft.vn
DOWNLOAD_TIMEOUT_SECONDS=30
FORWARD_TIMEOUT_SECONDS=30
PUBLIC_BASE_URL=http://topcv-ocr-middleware:8000
TMP_CV_DIR=/var/lib/tina-middleware-cv
CV_LINK_TTL_SECONDS=3600
```

### Bắt buộc: network chung `tina-shared`

Link tải CV mà Tina CRM nhận trong webhook phải gọi được. Nếu middleware và
Twenty nằm ở hai `docker-compose` riêng thì chúng **không gọi được nhau** (mỗi
project tạo một network riêng). Cần một network chung, tạo **một lần**:
```bash
docker network create tina-shared
```
`docker-compose.yml` của middleware đã khai báo sẵn network này. **Phía Twenty
cũng phải thêm** vào compose của họ:
```yaml
networks:
  tina-shared:
    external: true
```
Nếu Twenty chạy ngoài docker (VPS riêng), đổi `PUBLIC_BASE_URL` thành URL public
thật, ví dụ `https://cv.tinasoft.vn`.

Nếu Twenty gọi bằng địa chỉ IP container cứng (`172.19.0.2`) thì đừng làm vậy —
IP đổi mỗi lần recreate container. Luôn dùng tên service.

### Bước 2: Khởi chạy container
```bash
docker compose up -d --build
```

Kiểm tra trạng thái service:
```bash
curl http://localhost:8000/health
```
Kết quả trả về:
```json
{
  "status": "healthy",
  "service": "topcv-ocr-middleware",
  "tina_webhook_target": "http://...",
  "ocr_engines": {
    "paddleocr": false,
    "tesseract": true
  }
}
```

---

## 🛠️ Triển khai trực tiếp (Python Virtualenv trên Linux / VPS)

### Bước 1: Cài đặt thư viện hệ thống
```bash
sudo apt update
sudo apt install -y python3 python3-pip python3-venv tesseract-ocr tesseract-ocr-vie libgl1 libglib2.0-0 libgomp1
```

### Bước 2: Tạo virtual environment và cài đặt package
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### Bước 3: Khởi chạy server
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

---

## 📡 API Endpoints

### 1. `POST /webhook/topcv`
Endpoint chính nhận webhook từ TopCV.
- Query params:
  - `sync=true` (Mặc định): Xử lý tải CV, OCR và đẩy sang Tina CRM ngay, trả về kết quả chi tiết.
  - `sync=false`: Đưa vào hàng đợi nền (`BackgroundTasks`), trả về `202 Accepted` ngay lập tức để tránh webhook timeout.
- Body ví dụ:
```json
{
  "candidate_name": "Nguyễn Văn A",
  "candidate_email": "candidate.test@example.com",
  "candidate_phone": "0901234567",
  "job_id": "123456",
  "job_title": "Nhân viên kinh doanh",
  "apply_at": "2026-09-29 14:38:00",
  "download_url": "https://tuyendung-api.topcv.vn/api/v1/cv-management/onetime-download?token=..."
}
```

### 2. `GET /cv/{cv_id}`
Tải file CV đã được cache. Link **có thời hạn ngắn** (`CV_LINK_TTL_SECONDS`,
mặc định 1 giờ), sau đó file tự bị xóa và link trả `404`. Đây không phải kho lưu
trữ lâu dài.

Hỗ trợ `Range` request nên trình duyệt xem trước PDF không bị lỗi.

### 3. `POST /cv/{cv_id}/release`
Xóa file ngay lập tức, dùng khi bên nhận đã tải xong và muốn dọn file sớm.

### 4. `POST /extract-url?url=<URL>`
Test bóc tách text hoặc OCR từ một URL file bất kỳ.

### 5. `POST /extract-file`
Upload file trực tiếp dạng `multipart/form-data` để test bóc tách.

### 6. `GET /health`
Kiểm tra trạng thái server, engine OCR và cấu hình link CV.

---

## Link tải CV trong payload webhook

Link TopCV dạng `onetime-download` **hết hạn sau 24 giờ** (JWT `exp` trong token).
Nên middleware không gửi link đó đi, mà tự cache file lại rồi gửi link của nó:

```json
{
  "candidate_name": "Nguyễn Văn A",
  "cv_text": "...",
  "cv_file_url": "http://topcv-ocr-middleware:8000/cv/b3a8395e504b8520d89a19a8d433fc3b",
  "cv_filename": "Nguyen-Van-A-TopCV.pdf",
  "cv_size": 296332,
  "cv_link_expires_at": "2026-09-30T13:15:00+07:00"
}
```

Vòng đời: TopCV gửi webhook → middleware tải file ngay (token còn 24h nên thử lại
được vô hạn lần) → cache tạm trên đĩa → gửi `cv_file_url` cho Tina CRM → file tự
xoá khi hết TTL. Không dùng database, không lưu lâu dài.

---

## 🧪 Kiểm thử nhanh

Chạy script test:
```bash
python scripts/test_webhook.py http://localhost:8000/webhook/topcv
```
