# Data Kitsu Tools

> Ý tưởng gốc: lấy file CSV shot từ Kitsu, lấy các shot rồi đồng bộ data từ network về local, và chiều ngược lại để publish.

Công cụ copy data shot cho pipeline Kitsu: đọc danh sách shot từ file CSV xuất từ Kitsu,
tự tìm các folder shot (kể cả trong sub folder), kiểm tra số frame, rồi copy sang ổ đích
với đúng cấu trúc folder của nguồn. Không cần cài đặt phức tạp, chạy được trên **Windows, macOS và Linux**.

![Data Kitsu Tools](assets/screenshot.png)

## Tính năng

- **Đọc CSV Kitsu**: kéo thả hoặc bấm vào khung *DROP CSV FILE* để nạp danh sách shot (cột `Name` và `Frames`).
  Hỗ trợ dấu `;` hoặc `,`, mã hóa UTF-8, cp1258 (tiếng Việt) và UTF-16.
- **Shots List**: hiển thị NO, NAME, VERSION, FRAMES, STATUS. Có thể kéo thả thêm folder shot hoặc file CSV mới vào danh sách trong lúc đang copy.
- **Scan kiểm tra trước khi copy**:
  - Tìm folder theo tên shot ở mọi cấp sub folder của COPY FROM.
  - Số frame thực tế so với cột Frames. Lệch frame thì dòng chuyển đỏ và shot bị bỏ qua.
  - Không thấy file thì dòng màu vàng.
  - Rê chuột vào STATUS để xem frame nào bị thiếu.
- **LOCAL**: copy các folder shot và biến thể `PL01`, `PL02`, `PL03`... giữ nguyên cấu trúc thư mục nguồn.
  Các biến thể hiện dạng cây thu gọn / mở rộng như Explorer.
- **PUBLISH**: tự chọn version cao nhất (`_v01` … `_v06` → lấy `v06`) và copy vào
  `PASTE TO/<YYMMDD>/<tên folder>`. Có tùy chọn *MAKE DAILIES FOLDER*.
- **Lọc theo loại file**: EXR, DPX, MOV, TIF/TIFF, PNG, JPG (tùy mode).
- **COPY MODE**: *SKIP* (bỏ qua file đã có) hoặc *OVERWRITE* (ghi đè). Tùy chọn bỏ qua file hệ thống
  (`.DS_Store`, `._*`, `Thumbs.db`, `desktop.ini`).
- **START / PAUSE / STOP**: tạm dừng và tiếp tục được. Khi STOP, file đang copy dở sẽ bị xóa
  và shot đó được copy lại bằng chế độ ghi đè ở lần START sau.
- **Chống lỗi copy dở**: file được ghi vào `<tên>.part` và chỉ đổi tên khi đã copy xong.
- **Tiến độ**: thanh tiến độ tổng và trạng thái từng shot đổi màu theo tiến trình.

### Màu trạng thái

| Màu | Ý nghĩa |
|---|---|
| Đỏ đậm | Số frame không khớp với cột Frames (shot bị bỏ qua) |
| Vàng | Không tìm thấy file sau khi scan |
| Xanh dương đậm | Đang copy |
| Xám, nghiêng, gạch ngang | Đã copy xong |
| Cam | Đã dừng giữa chừng (sẽ ghi đè khi chạy lại) |

## Cài đặt

Cách nhanh nhất là chạy file launcher, nó tự kiểm tra Python, cài PySide6 nếu thiếu rồi mở app:

| Hệ điều hành | Cách chạy |
|---|---|
| Windows | Double-click `run.cmd` (hoặc `run.cmd --check` để chỉ kiểm tra) |
| macOS | Double-click `run.command` (lần đầu: chuột phải > Open, và `chmod +x run.command` nếu cần) |
| Linux | `./run.sh` (hoặc `./run.sh --check`) |

Cài thủ công nếu muốn:


Yêu cầu: **Python 3.9 trở lên** và **PySide6**.

```bash
# Windows (PowerShell)
py -m pip install -r requirements.txt
py data_kitsu_tools.py

# macOS / Linux
python3 -m pip install -r requirements.txt
python3 data_kitsu_tools.py
```

## Sử dụng nhanh

1. Nạp CSV bằng khung *DROP CSV FILE*.
2. Nhập *COPY FROM* (nguồn) và *PASTE TO* (đích). Có thể kéo thả folder từ Explorer / Finder.
3. Bấm **LOCAL** hoặc **PUBLISH** để scan. Kiểm tra màu và cột STATUS.
4. Bấm **START**, xác nhận, rồi theo dõi tiến độ trong danh sách và log.

## Tính năng kỹ thuật

- Ngôn ngữ: **Python 3** với giao diện **Qt (PySide6)**.
- Copy bằng khối 8 MB, copy song song 4 file mỗi shot, và thread riêng nên giao diện không bị đơ.
- Cache: không dùng RAM cache riêng vì ổ đĩa / mạng mới là nút thắt. Hệ điều hành đã có page cache,
  app dùng buffer lớn và copy song song để tận dụng nó.
- Đường dẫn dùng `pathlib` nên chạy đúng với `W:\...` trên Windows và `/Volumes/...`, `/mnt/...` trên macOS / Linux.

### Tương thích

| Hệ điều hành | Trạng thái |
|---|---|
| Windows 10 / 11 | Hỗ trợ (ổ mạng, ổ W:, E: đều được) |
| macOS 12+ | Hỗ trợ, bỏ qua file `._*` và `.DS_Store` |
| Linux | Đã kiểm thử logic và giao diện |

## Changelog

## My Modification update

- [26-11-18]
  - **Launchers:** thêm `run.cmd` (Windows), `run.sh` và `run.command` (Linux / macOS) để kiểm tra Python, cài PySide6 và mở GUI.
  - **Initial release:** GUI theo mockup, đọc CSV Kitsu, scan LOCAL / PUBLISH, copy có skip / overwrite,
    pause / resume / stop, bỏ qua file hệ thống, màu trạng thái, README.
