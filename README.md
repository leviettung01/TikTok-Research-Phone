# TikTok Research & Phone Studio (Bản Độc Lập Tối Ưu)

Tool độc lập tách biệt hoàn toàn, chạy trên cổng riêng **`9098`** (không xung đột với các tool khác), đã đóng gói sẵn **`tools/adb.exe`**, **`tools/scrcpy-server`** và **`ADBKeyboard.apk`** ngay trong repo để bất kỳ máy Windows nào kéo về là chạy được ngay.

## Cài đặt & Chạy trên máy mới (GitHub Clone)

### Yêu cầu duy nhất
- Máy cài sẵn **Windows 10/11** + **Python 3.10+** (tải tại https://www.python.org/downloads/ — nhớ tick **Add Python to PATH**) và trình duyệt **Edge** hoặc **Chrome**.
- Không cần cài đặt thêm Android SDK hay scrcpy ngoài.

### Các bước lấy về máy khác
**Cách A — Dùng Git:**
```powershell
git clone https://github.com/leviettung01/TikTok-Research-Phone.git
cd TikTok-Research-Phone
```
**Cách B — Tải trực tiếp file ZIP (không cần cài Git):**
- Tải tại: https://github.com/leviettung01/TikTok-Research-Phone/archive/refs/heads/main.zip rồi giải nén.
1. *(Tùy chọn)* Chạy **`Cai_Dat_May_Moi.bat`** để tạo môi trường ảo `.venv` sạch sẽ.
2. Click đúp **`Chay_Tool_Research_Phone.bat`** để mở ứng dụng (lần đầu chạy nếu thiếu thư viện `requests`, `openpyxl`, `qrcode`, `Pillow` tool cũng tự động cài đặt luôn).

---

## Các chức năng chính
1. **Kết nối & Điều khiển Điện thoại Realtime (`USB` · `Wi-Fi ADB` · `Quét mã QR` · `Mã 6 số` · `LAN mDNS` · `BlueStacks`)**:
   - Hiển thị màn hình điện thoại mượt 60FPS, tùy chỉnh tự do độ rộng khung màn hình (kéo viền phân cách hoặc thanh trượt `350px` → `860px`, hỗ trợ cả 2 chế độ **↕ Khớp khung** và **↔ Đầy ngang**).
   - Hỗ trợ chạm, vuốt, cuộn chuột, gõ chữ Tiếng Việt có dấu, dán nhanh `Ctrl+V`, mở/dừng TikTok, sửa lỗi mất bàn phím ảo Gboard.
2. **Kéo thả Video vào Điện thoại ➔ Tự động Copy & Paste ID Sản phẩm**:
   - Kéo thả trực tiếp file video (ví dụ `1737909342761027082_v1.mp4`) vào khung màn hình điện thoại bên phải (hoặc bấm **📲 Nạp Video**).
   - Tool tự động đẩy video vào `/sdcard/DCIM/Camera`, quét MediaStore lên đầu Thư viện TikTok, trích xuất ID sản phẩm (`1737909342761027082`), **Copy vào Clipboard điện thoại + máy tính** và **Paste thẳng vào ô đang mở trên điện thoại**.
3. **Tab Research Sản phẩm (Đầy đủ 100% trường dữ liệu & tính năng)**:
   - Tự động bắt link sản phẩm khi bấm **Chia sẻ → Sao chép liên kết** trên TikTok điện thoại (tự giải mã link rút gọn `vt.tiktok.com` / `vm.tiktok.com`).
   - Mặc định dùng **Cách 1 (SPAI Auto API)** tốc độ cao để tải trọn bộ: **Tên sản phẩm, Tên Shop, Giá bán, Giá gốc, Đã bán, Phân loại hàng, Mô tả chi tiết, Toàn bộ ảnh sản phẩm nét gốc + ảnh mô tả** về máy tính (không mở trình duyệt nên không bị lỗi chặn vùng quốc gia `region_not_match`). Nếu dùng **Cách 2 (Browser)** gặp lỗi vùng quốc gia sẽ tự động chuyển về **Cách 1**.
   - Đánh dấu **🔥 SP HOT** (tự động đồng bộ ảnh + thông tin ra thư mục `runtime/data/hot_products`), lọc theo ngày, xem Gallery chi tiết, **📲 Paste Phone**, Copy Full, Xuất Excel `.xlsx`, Sao lưu & Khôi phục.
