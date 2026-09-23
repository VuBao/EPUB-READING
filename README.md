# EPUB Audio Reader for Linux

Ứng dụng desktop đọc EPUB tiếng Việt bằng Microsoft Edge Neural TTS trên Linux. Dashboard là giao diện duy nhất; MPV chạy ngầm như audio engine và được điều khiển qua IPC.

## Tính năng

- Chọn EPUB và chương bắt đầu từ dashboard.
- Giọng `vi-VN-NamMinhNeural` hoặc `vi-VN-HoaiMyNeural`.
- Producer/consumer pipeline với rolling buffer ba group.
- Prefetch xuyên chương và phát liên tục đến cuối sách.
- Pause/Play, tua, âm lượng và Stop ngay trên dashboard.
- Tự lưu chương và vị trí nghe; mỗi EPUB có state riêng.
- Tự dùng lại MP3 chương đã hoàn thành.
- Tự tạo `audiobook.m3u` cho từng sách.
- Edge TTS retry với backoff và tự chia group thành chunk nhỏ khi endpoint trả audio rỗng.
- MPV chạy headless; không mở thêm cửa sổ player.

## Cài đặt trên Ubuntu/Zorin

```bash
sudo apt install python3-venv ffmpeg mpv
git clone https://github.com/VuBao/EPUB-READING.git
cd EPUB-READING
chmod +x install.sh
./install.sh
```

Sau đó mở **EPUB Audio Reader** từ Desktop hoặc menu Applications.

## Sử dụng dashboard

1. Bấm **Chọn sách…** và chọn file `.epub`.
2. Bấm **Tiếp tục lần trước** để resume.
3. Hoặc nhập số chương rồi bấm **Bắt đầu chương đã chọn**.
4. Dùng các nút trên dashboard để Pause/Play, tua và chỉnh âm lượng.

Khi chuyển sách hoặc chương trong lúc đang phát, dashboard dừng phiên cũ sạch sẽ rồi khởi động phiên mới.

## Dữ liệu runtime

Các dữ liệu cá nhân không được đưa vào Git:

- EPUB do người dùng chọn.
- MP3 và playlist trong `audio/`.
- State resume trong `.audio_states/`.
- Cấu hình dashboard trong `.audio_gui.json`.

Audio và state được tách theo từng EPUB để không dùng nhầm cache hoặc ghi đè tiến độ của sách khác.

## Chạy bằng command

```bash
python3 epub2audio.py book.epub \
  --voice vi-VN-NamMinhNeural \
  --start 478 \
  --stream \
  --group-size 3 \
  --buffer-groups 3 \
  --persistent-mpv
```

Thêm `--resume` để tiếp tục từ state đã lưu.

## Yêu cầu

- Python 3.10+
- `edge-tts`
- `beautifulsoup4`
- FFmpeg/FFprobe
- MPV

Edge TTS dùng dịch vụ mạng của Microsoft nên thời gian tạo audio phụ thuộc kết nối và trạng thái endpoint.
