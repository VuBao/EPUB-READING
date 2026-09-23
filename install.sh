#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
APP_ENTRY="$PROJECT_DIR/epub_audio_gui.py"
APP_ICON="$PROJECT_DIR/assets/epub-audio-reader.png"

for command_name in python3 ffmpeg ffprobe mpv; do
    if ! command -v "$command_name" >/dev/null 2>&1; then
        echo "Thiếu $command_name. Trên Ubuntu/Zorin chạy: sudo apt install python3-venv ffmpeg mpv"
        exit 1
    fi
done

if [ ! -x "$VENV_DIR/bin/python" ]; then
    python3 -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/python" -m pip install --upgrade pip
"$VENV_DIR/bin/python" -m pip install -r "$PROJECT_DIR/requirements.txt"

DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
if [ -z "$DESKTOP_DIR" ]; then
    DESKTOP_DIR="$HOME/Desktop"
fi
APPLICATIONS_DIR="$HOME/.local/share/applications"
mkdir -p "$DESKTOP_DIR" "$APPLICATIONS_DIR"

write_launcher() {
    launcher_path="$1"
    cat >"$launcher_path" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=EPUB Audio Reader
Comment=Nghe EPUB tiếng Việt và tiếp tục từ vị trí lần trước
Exec="$VENV_DIR/bin/python" "$APP_ENTRY" --auto-resume
Icon=$APP_ICON
Path=$PROJECT_DIR
Terminal=false
Categories=AudioVideo;Audio;Player;
StartupNotify=true
StartupWMClass=EpubAudioReader
EOF
    chmod +x "$launcher_path"
}

write_launcher "$DESKTOP_DIR/EPUB Audio Reader.desktop"
write_launcher "$APPLICATIONS_DIR/epub-audio-reader.desktop"
gio set "$DESKTOP_DIR/EPUB Audio Reader.desktop" metadata::trusted true 2>/dev/null || true
update-desktop-database "$APPLICATIONS_DIR" 2>/dev/null || true

echo "Đã cài EPUB Audio Reader. Mở bằng icon trên Desktop hoặc menu Applications."
