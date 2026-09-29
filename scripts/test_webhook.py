import json
import urllib.request
import sys

URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000/webhook/topcv"

payload = {
    "candidate_name": "Nguyễn Văn A",
    "candidate_email": "candidate.test@example.com",
    "candidate_phone": "0901234567",
    "job_id": "123456",
    "job_title": "Nhân viên kinh doanh",
    "apply_at": "2026-09-29 14:38:00",
    "download_url": "https://tuyendung-api.topcv.vn/api/v1/cv-management/onetime-download?token=eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9.eyJwd2xpIjoiLTEiLCJ0eXBlIjoxLCJpYXQiOjE3OTA2NTIzMzcsImV4cCI6MTc5MDczODczN30.qkG-6QtK3sVX4w46ZmYOKXjk_350C020mO0dONZN8Hs"
}

print(f"Sending test candidate webhook to: {URL}")
req = urllib.request.Request(
    URL,
    data=json.dumps(payload).encode("utf-8"),
    headers={"Content-Type": "application/json"}
)

try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        print(f"Status: {resp.status}")
        data = json.loads(resp.read().decode("utf-8"))
        print(json.dumps(data, indent=2, ensure_ascii=False))
except Exception as e:
    print(f"Error: {e}")
