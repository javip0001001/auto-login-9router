# JAVIP AI STORE - Import 9router Tool v2.1

Auto Login OAuth (ChatGPT/Codex) hàng loạt và import refresh token vào 9router.

---

## 🇻🇳 Tiếng Việt

### Yêu cầu trước khi chạy
- Windows 10/11 64-bit (hoặc Ubuntu/Linux 64-bit — dùng `start_here.sh`).
- Máy phải cài sẵn Python 3.11 trở lên (64-bit).
- Lúc cài Python trên Windows **phải tick**: `Add python.exe to PATH`.
- **⚠️ Đã cài, đăng nhập [9router](https://9router.com) trên máy VÀ ĐANG BẬT (đang chạy) trước khi dùng tool.**

### Tính năng
- Auto Login OAuth Codex hàng loạt, chạy song song nhiều tài khoản.
- Tự nhập số luồng ngay trên giao diện (mặc định 3, không giới hạn cứng).
- Callback OAuth dùng `localhost:1455` và tự phân luồng theo `state`, tránh lẫn kết quả giữa các nick.
- Hỗ trợ định dạng `email|password|2FA`.
- Tự động convert định dạng paste từ file/tab/comma/space.
- Mỗi tài khoản chạy browser riêng, sạch session.
- Realtime logs, tiến độ và danh sách nick đang chạy trên giao diện.
- Import refresh token vào 9router và kiểm chứng SQLite sau khi ghi.

### Cách chạy (tự setup tự động)

**Windows:**
1. Giải nén file ZIP vào một thư mục riêng.
2. Double-click: `start_here.bat`.
3. Lần đầu file bat sẽ tự động:
   - Kiểm tra Python và pip.
   - Cài/cập nhật `pyotp` + `playwright`.
   - Tải Chromium cho Playwright.
   - Khởi động server tool.
4. Trình duyệt sẽ tự mở tại: `http://localhost:9876`
5. Vào tab **Auto Login**, dán danh sách nick và nhập số lượng muốn chạy.

**Ubuntu/Linux:**
1. Giải nén vào một thư mục riêng.
2. Mở terminal tại thư mục đó, chạy: `./start_here.sh`
3. Script sẽ tự tạo virtualenv, cài `pyotp` + `playwright`, tải Chromium và khởi động server.
4. Trình duyệt mở tại: `http://localhost:9876`

Nếu báo không tìm thấy Python: cài Python 3.11 64-bit trở lên, sau đó đóng và mở lại `start_here.bat` (Windows) hoặc cài `python3-venv`/`python3-pip` (Linux) rồi chạy lại `start_here.sh`.

### Định dạng file tài khoản (`accounts_sample.txt`)
```
# Format: email|password|2fa_secret
# 2fa_secret là tùy chọn (bỏ trống nếu không có 2FA)
user@gmail.com|mypassword123|JBSWY3DPEHPK3PXP
user2@gmail.com|password456|
user3@gmail.com|pass789
```

### Lưu ý
- Cần internet khi cài lần đầu và trong lúc đăng nhập OAuth.
- Nhiều luồng hơn sẽ tốn RAM/CPU và có thể gặp captcha/xác minh nhiều hơn.
- Không nên xóa `auto_login.py`, `server.py` hoặc `index.html` trong thư mục đã giải nén.
- Không cần cài thủ công nếu `start_here.bat`/`start_here.sh` chạy thành công.

---

## 🇬🇧 English

### Requirements
- Windows 10/11 64-bit (or Ubuntu/Linux 64-bit — use `start_here.sh`).
- Python 3.11+ (64-bit) must be installed.
- On Windows, when installing Python you **must check**: `Add python.exe to PATH`.
- **⚠️ [9router](https://9router.com) must be installed, logged in, AND ACTIVELY RUNNING on the machine before using this tool.**

### Features
- Bulk OAuth (Codex) auto login, running multiple accounts in parallel.
- Configure the number of parallel workers directly in the UI (default 3, no hard limit).
- OAuth callback server on `localhost:1455`, routing results by `state` to avoid mixing results between accounts.
- Supports `email|password|2FA` format.
- Automatically converts pasted lists from file/tab/comma/space-separated formats.
- Each account runs in its own isolated browser session.
- Realtime logs, progress, and the list of currently running accounts in the UI.
- Imports refresh tokens into 9router and verifies against SQLite after writing.

### How to run (auto setup)

**Windows:**
1. Extract the ZIP into its own folder.
2. Double-click `start_here.bat`.
3. On first run, the script automatically:
   - Checks Python and pip.
   - Installs/updates `pyotp` + `playwright`.
   - Downloads Chromium for Playwright.
   - Starts the tool's server.
4. A browser window opens automatically at: `http://localhost:9876`
5. Go to the **Auto Login** tab, paste the account list, and set how many to run.

**Ubuntu/Linux:**
1. Extract into its own folder.
2. Open a terminal in that folder and run: `./start_here.sh`
3. The script creates a virtualenv, installs `pyotp` + `playwright`, downloads Chromium, and starts the server.
4. Browser opens at: `http://localhost:9876`

If Python isn't found: install Python 3.11+ 64-bit, then close and reopen `start_here.bat` (Windows), or install `python3-venv`/`python3-pip` and rerun `start_here.sh` (Linux).

### Account file format (`accounts_sample.txt`)
```
# Format: email|password|2fa_secret
# 2fa_secret is optional (leave empty if no 2FA)
user@gmail.com|mypassword123|JBSWY3DPEHPK3PXP
user2@gmail.com|password456|
user3@gmail.com|pass789
```

### Notes
- Internet is required for the first-time install and while going through OAuth login.
- More parallel workers use more RAM/CPU and may trigger more captcha/verification challenges.
- Do not delete `auto_login.py`, `server.py`, or `index.html` from the extracted folder.
- No manual setup is needed if `start_here.bat`/`start_here.sh` runs successfully.
# auto-login-9router
