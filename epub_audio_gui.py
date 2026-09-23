#!/usr/bin/env python3
"""Small native dashboard for EPUB Audio Reader."""

import hashlib
import json
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


PROJECT_DIR = Path(__file__).resolve().parent
BACKEND = PROJECT_DIR / "epub2audio.py"
ICON = PROJECT_DIR / "assets" / "epub-audio-reader.png"
LEGACY_STATE_FILE = PROJECT_DIR / ".audio_state.json"
STATE_DIR = PROJECT_DIR / ".audio_states"
SETTINGS_FILE = PROJECT_DIR / ".audio_gui.json"
MPV_SOCKET = Path("/tmp/epub2audio-mpv.sock")
AUDIO_ROOT = PROJECT_DIR / "audio"


def format_time(seconds):
    try:
        seconds = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        seconds = 0
    hours, remainder = divmod(seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


class AudioDashboard:
    def __init__(self, root):
        self.root = root
        self.root.title("EPUB Audio Reader")
        self.root.geometry("820x650")
        self.root.minsize(720, 580)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self.process = None
        self.active_book = None
        self.log_queue = queue.Queue()
        self.icon_image = None
        self.header_icon = None

        self.book_var = tk.StringVar(value=self._default_book())
        self.chapter_var = tk.StringVar(value="1")
        self.voice_var = tk.StringVar(value="vi-VN-NamMinhNeural")
        self.status_var = tk.StringVar(value="Sẵn sàng")
        self.current_var = tk.StringVar(value="Chưa bắt đầu")
        self.position_var = tk.StringVar(value="00:00:00")
        self.buffer_var = tk.StringVar(value="Buffer sẽ tự chuẩn bị 3 group phía trước")

        self._load_settings()
        self._configure_style()
        self._build_ui()
        self._load_icon()
        self._refresh_state()
        self._drain_logs()

    def _configure_style(self):
        self.root.configure(bg="#101827")
        style = ttk.Style(self.root)
        style.theme_use("clam")
        style.configure("App.TFrame", background="#101827")
        style.configure("Card.TFrame", background="#182438")
        style.configure(
            "Title.TLabel", background="#101827", foreground="#f8fafc",
            font=("Sans", 22, "bold"),
        )
        style.configure(
            "Subtitle.TLabel", background="#101827", foreground="#91a4bd",
            font=("Sans", 10),
        )
        style.configure(
            "CardTitle.TLabel", background="#182438", foreground="#89e5dc",
            font=("Sans", 10, "bold"),
        )
        style.configure(
            "Value.TLabel", background="#182438", foreground="#f8fafc",
            font=("Sans", 14, "bold"),
        )
        style.configure(
            "Info.TLabel", background="#182438", foreground="#b8c7d9",
            font=("Sans", 10),
        )
        style.configure(
            "Accent.TButton", font=("Sans", 11, "bold"), padding=(16, 10),
            background="#19b8aa", foreground="#071a1a",
        )
        style.map("Accent.TButton", background=[("active", "#46d5c8")])
        style.configure("Control.TButton", font=("Sans", 10), padding=(10, 8))
        style.configure("TEntry", padding=7)
        style.configure("TCombobox", padding=6)

    def _build_ui(self):
        outer = ttk.Frame(self.root, style="App.TFrame", padding=24)
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer, style="App.TFrame")
        header.pack(fill="x", pady=(0, 18))
        self.icon_label = ttk.Label(header, style="App.TFrame")
        self.icon_label.pack(side="left", padx=(0, 14))
        header_text = ttk.Frame(header, style="App.TFrame")
        header_text.pack(side="left", fill="x", expand=True)
        ttk.Label(header_text, text="EPUB Audio Reader", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            header_text,
            text="Nghe EPUB tiếng Việt • tự tải trước • tự nhớ vị trí",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(3, 0))

        setup = ttk.Frame(outer, style="Card.TFrame", padding=18)
        setup.pack(fill="x", pady=(0, 14))
        setup.columnconfigure(1, weight=1)
        ttk.Label(setup, text="SÁCH EPUB", style="CardTitle.TLabel").grid(
            row=0, column=0, columnspan=3, sticky="w", pady=(0, 8)
        )
        ttk.Entry(setup, textvariable=self.book_var).grid(
            row=1, column=0, columnspan=2, sticky="ew", padx=(0, 8)
        )
        ttk.Button(setup, text="Chọn sách…", command=self.choose_book).grid(row=1, column=2)

        ttk.Label(setup, text="Bắt đầu từ chương", style="Info.TLabel").grid(
            row=2, column=0, sticky="w", pady=(14, 4)
        )
        ttk.Label(setup, text="Giọng đọc", style="Info.TLabel").grid(
            row=2, column=1, sticky="w", pady=(14, 4), padx=(12, 0)
        )
        self.chapter_input = tk.Entry(
            setup,
            textvariable=self.chapter_var,
            width=12,
            justify="center",
            bg="#ffffff",
            fg="#111827",
            insertbackground="#111827",
            selectbackground="#19b8aa",
            selectforeground="#071a1a",
            relief="flat",
            font=("Sans", 12),
            highlightthickness=1,
            highlightbackground="#cbd5e1",
            highlightcolor="#19b8aa",
        )
        self.chapter_input.grid(row=3, column=0, sticky="w", ipady=7)
        self.chapter_input.bind("<Return>", lambda _event: self.start(resume=False))
        self.chapter_input.bind("<Control-a>", self._select_chapter)
        voice = ttk.Combobox(
            setup,
            textvariable=self.voice_var,
            values=("vi-VN-NamMinhNeural", "vi-VN-HoaiMyNeural"),
            state="readonly",
        )
        voice.grid(row=3, column=1, sticky="ew", padx=(12, 8))

        actions = ttk.Frame(setup, style="Card.TFrame")
        actions.grid(row=4, column=0, columnspan=3, sticky="ew", pady=(18, 0))
        ttk.Button(
            actions, text="▶  Tiếp tục lần trước", style="Accent.TButton",
            command=lambda: self.start(resume=True),
        ).pack(side="left")
        ttk.Button(
            actions, text="Bắt đầu chương đã chọn", style="Control.TButton",
            command=lambda: self.start(resume=False),
        ).pack(side="left", padx=8)
        ttk.Button(
            actions, text="Mở thư mục Audio", style="Control.TButton",
            command=self.open_audio_folder,
        ).pack(side="right")

        now = ttk.Frame(outer, style="Card.TFrame", padding=18)
        now.pack(fill="x", pady=(0, 14))
        now.columnconfigure(0, weight=1)
        ttk.Label(now, text="ĐANG NGHE", style="CardTitle.TLabel").grid(
            row=0, column=0, sticky="w"
        )
        self.status_label = ttk.Label(now, textvariable=self.status_var, style="Info.TLabel")
        self.status_label.grid(row=0, column=1, sticky="e")
        ttk.Label(now, textvariable=self.current_var, style="Value.TLabel").grid(
            row=1, column=0, sticky="w", pady=(8, 2)
        )
        ttk.Label(now, textvariable=self.position_var, style="Value.TLabel").grid(
            row=1, column=1, sticky="e", pady=(8, 2)
        )
        ttk.Label(now, textvariable=self.buffer_var, style="Info.TLabel").grid(
            row=2, column=0, columnspan=2, sticky="w", pady=(5, 12)
        )

        controls = ttk.Frame(now, style="Card.TFrame")
        controls.grid(row=3, column=0, columnspan=2, sticky="ew")
        for text, command in (
            ("↶ 15s", lambda: self.mpv_command(["seek", -15, "relative"])),
            ("⏯  Pause / Play", lambda: self.mpv_command(["cycle", "pause"])),
            ("30s ↷", lambda: self.mpv_command(["seek", 30, "relative"])),
            ("Vol −", lambda: self.mpv_command(["add", "volume", -5])),
            ("Vol +", lambda: self.mpv_command(["add", "volume", 5])),
            ("■  Dừng", self.stop),
        ):
            ttk.Button(controls, text=text, command=command, style="Control.TButton").pack(
                side="left", padx=(0, 7)
            )

        log_card = ttk.Frame(outer, style="Card.TFrame", padding=14)
        log_card.pack(fill="both", expand=True)
        ttk.Label(log_card, text="HOẠT ĐỘNG", style="CardTitle.TLabel").pack(anchor="w")
        self.log = tk.Text(
            log_card, height=8, wrap="word", bg="#0d1522", fg="#c9d6e6",
            insertbackground="white", relief="flat", padx=10, pady=8,
            font=("Monospace", 9), state="disabled",
        )
        self.log.pack(fill="both", expand=True, pady=(8, 0))

    def _load_icon(self):
        if not ICON.exists():
            return
        try:
            self.icon_image = tk.PhotoImage(file=ICON)
            self.root.iconphoto(True, self.icon_image)
            factor = max(1, self.icon_image.width() // 86)
            self.header_icon = self.icon_image.subsample(factor, factor)
            self.icon_label.configure(image=self.header_icon)
        except tk.TclError:
            pass

    def _default_book(self):
        books = sorted(PROJECT_DIR.glob("*.epub"))
        return str(books[0]) if books else ""

    def _select_chapter(self, _event=None):
        self.chapter_input.selection_range(0, "end")
        self.chapter_input.icursor("end")
        return "break"

    @staticmethod
    def _book_key(book):
        safe = re.sub(r'[\\/:*?"<>|]+', "_", Path(book).stem)
        safe = re.sub(r"\s+", " ", safe).strip(" .") or "book"
        resolved = str(Path(book).expanduser().resolve())
        digest = hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:10]
        return f"{safe[:100]}-{digest}"

    @classmethod
    def _book_audio_dir(cls, book):
        return AUDIO_ROOT / cls._book_key(book)

    @classmethod
    def _book_state_file(cls, book):
        return STATE_DIR / f"{cls._book_key(book)}.json"

    @staticmethod
    def _legacy_audio_dir(book):
        safe = re.sub(r'[\\/:*?"<>|]+', "_", Path(book).stem)
        safe = re.sub(r"\s+", " ", safe).strip(" .") or "book"
        return AUDIO_ROOT / safe[:120]

    def _migrate_book_data(self, book):
        """Copy matching legacy data into the per-book namespace safely."""
        try:
            legacy = json.loads(LEGACY_STATE_FILE.read_text(encoding="utf-8"))
            same_book = (
                Path(legacy.get("book", "")).resolve() == Path(book).resolve()
            )
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            legacy = None
            same_book = False

        output_dir = self._book_audio_dir(book)
        output_dir.mkdir(parents=True, exist_ok=True)
        legacy_dir = self._legacy_audio_dir(book)
        if same_book and legacy_dir != output_dir and legacy_dir.is_dir():
            for source in legacy_dir.glob("[0-9][0-9][0-9][0-9] - *.mp3"):
                target = output_dir / source.name
                if not target.exists() and source.stat().st_size > 0:
                    shutil.copy2(source, target)

        state_file = self._book_state_file(book)
        try:
            current = json.loads(state_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            current = None

        if same_book:
            try:
                legacy_updated = float(legacy.get("updated_at", 0))
                current_updated = float((current or {}).get("updated_at", -1))
            except (TypeError, ValueError):
                same_book = False

        if same_book and (current is None or legacy_updated > current_updated):
            state_file.parent.mkdir(parents=True, exist_ok=True)
            temporary = state_file.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(legacy, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(state_file)

        tracks = sorted(output_dir.glob("[0-9][0-9][0-9][0-9] - *.mp3"))
        playlist = output_dir / "audiobook.m3u"
        temporary = output_dir / ".audiobook.m3u.tmp"
        temporary.write_text(
            "#EXTM3U\n" + "".join(f"{track.name}\n" for track in tracks),
            encoding="utf-8",
        )
        temporary.replace(playlist)

    def _load_book_state(self, book):
        state_file = self._book_state_file(book)
        try:
            data = json.loads(state_file.read_text(encoding="utf-8"))
            if Path(data.get("book", "")).resolve() == Path(book).resolve():
                return data
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
        return None

    def _load_settings(self):
        try:
            settings = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        book = settings.get("book")
        if book and Path(book).exists():
            self.book_var.set(book)
        chapter = str(settings.get("chapter", self.chapter_var.get())).strip()
        self.chapter_var.set(chapter if chapter.isdigit() else "1")
        if settings.get("voice"):
            self.voice_var.set(settings["voice"])

    def _save_settings(self):
        payload = {
            "book": self.book_var.get().strip(),
            "chapter": self.chapter_var.get().strip(),
            "voice": self.voice_var.get(),
        }
        temp = SETTINGS_FILE.with_name(f".{SETTINGS_FILE.name}.tmp")
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(SETTINGS_FILE)

    def choose_book(self):
        selected = filedialog.askopenfilename(
            title="Chọn sách EPUB",
            initialdir=str(PROJECT_DIR),
            filetypes=(("EPUB books", "*.epub"), ("All files", "*")),
        )
        if selected:
            self.book_var.set(selected)
            self._save_settings()

    def start(self, resume):
        book = Path(self.book_var.get().strip()).expanduser()
        if not book.is_file() or book.suffix.casefold() != ".epub":
            messagebox.showerror("Không tìm thấy sách", "Hãy chọn một file EPUB hợp lệ.")
            return
        try:
            chapter = max(1, int(self.chapter_var.get()))
        except (TypeError, ValueError):
            messagebox.showerror("Chương không hợp lệ", "Số chương phải là một số nguyên.")
            self.chapter_var.set("1")
            self.chapter_input.focus_set()
            return
        self.chapter_var.set(str(chapter))

        self._save_settings()
        process_running = self.process and self.process.poll() is None
        mpv_running = self._ipc_request(["get_property", "idle-active"]) is not None
        if process_running or mpv_running:
            if resume:
                self.status_var.set("Đang điều khiển phiên hiện tại")
                return
            self.status_var.set(f"Đang chuyển sang chương {chapter}…")
            self._append_log(f"Chuyển phiên nghe sang chương {chapter}…")
            if mpv_running:
                self.mpv_command(["quit"], quiet=True)
            elif process_running:
                self.process.terminate()
            self.root.after(
                250,
                lambda: self._launch_when_stopped(book, chapter, attempts_left=40),
            )
            return
        self._launch(book, chapter, resume)

    def _launch_when_stopped(self, book, chapter, attempts_left):
        process_running = self.process and self.process.poll() is None
        mpv_running = self._ipc_request(["get_property", "idle-active"]) is not None
        if (process_running or mpv_running) and attempts_left > 0:
            self.root.after(
                250,
                lambda: self._launch_when_stopped(book, chapter, attempts_left - 1),
            )
            return
        if process_running or mpv_running:
            messagebox.showerror(
                "Không chuyển được chương",
                "Phiên nghe cũ chưa dừng. Hãy bấm Dừng rồi thử lại.",
            )
            return
        self._launch(book, chapter, resume=False)

    def _launch(self, book, chapter, resume):
        self._migrate_book_data(book)
        output_dir = self._book_audio_dir(book)
        state_file = self._book_state_file(book)
        command = [
            sys.executable, "-u", str(BACKEND), str(book.resolve()),
            "--voice", self.voice_var.get(),
            "--stream", "--group-size", "3", "--buffer-groups", "3",
            "--persistent-mpv",
            "--output-dir", str(output_dir),
            "--state-file", str(state_file),
        ]
        if resume and self._has_resume_for(book):
            command.append("--resume")
        else:
            command.extend(("--start", str(chapter)))

        try:
            self.process = subprocess.Popen(
                command,
                cwd=PROJECT_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            messagebox.showerror("Không thể khởi động", str(exc))
            return
        self.active_book = Path(book).resolve()
        self.status_var.set("Đang khởi động…")
        self._append_log("Khởi động phiên nghe…")
        threading.Thread(
            target=self._read_process_output,
            args=(self.process,),
            daemon=True,
        ).start()

    def _has_resume_for(self, book):
        self._migrate_book_data(book)
        return self._load_book_state(book) is not None

    def _read_process_output(self, process):
        if not process or not process.stdout:
            return
        for line in process.stdout:
            self.log_queue.put(line.rstrip())
        code = process.wait()
        self.log_queue.put(f"__PROCESS_EXIT__:{process.pid}:{code}")

    def _drain_logs(self):
        try:
            while True:
                line = self.log_queue.get_nowait()
                if line.startswith("__PROCESS_EXIT__:"):
                    _, pid, code = line.split(":", 2)
                    if self.process and self.process.pid == int(pid):
                        self.active_book = None
                        self.status_var.set(
                            "Đã dừng" if int(code) == 0 else "Có lỗi — xem hoạt động"
                        )
                    continue
                self._append_log(line)
                if line.startswith("[PLAY]"):
                    self.status_var.set("Đang phát")
                    self.buffer_var.set(line)
                elif line.startswith("[PREP]"):
                    self.buffer_var.set(line)
                elif line.startswith("[TTS ERROR]"):
                    self.status_var.set("TTS đang thử lại")
                elif line.startswith("[TTS FALLBACK]"):
                    self.status_var.set("TTS đang chia nhỏ group")
        except queue.Empty:
            pass
        self.root.after(150, self._drain_logs)

    def _append_log(self, line):
        if not line:
            return
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        line_count = int(self.log.index("end-1c").split(".")[0])
        if line_count > 250:
            self.log.delete("1.0", "50.0")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _refresh_state(self):
        loaded_state = False
        process_running = bool(self.process and self.process.poll() is None)
        state_book = self.active_book if process_running and self.active_book else None
        if state_book is None:
            selected = self.book_var.get().strip()
            state_book = Path(selected).expanduser().resolve() if selected else None
        data = self._load_book_state(state_book) if state_book else None
        if data:
            loaded_state = True
            chapter = data.get("chapter", "?")
            title = str(data.get("chapter_title", "")).strip()
            group = data.get("group", 1)
            if title.casefold().startswith("chương"):
                chapter_label = title
            elif title:
                chapter_label = f"Chương {chapter}: {title}"
            else:
                chapter_label = f"Chương {chapter}"
            self.current_var.set(f"{chapter_label}  •  Group {group}")
            self.position_var.set(format_time(data.get("position", 0)))
            if not process_running:
                status = data.get("status", "ready")
                labels = {
                    "playing": "Đã lưu vị trí",
                    "paused": "Sẵn sàng tiếp tục",
                    "ready": "Sẵn sàng tiếp tục",
                    "preparing": "Đang chuẩn bị audio",
                    "completed": "Đã nghe hết",
                }
                self.status_var.set(labels.get(status, "Sẵn sàng tiếp tục"))

        path_response = self._ipc_request(["get_property", "path"])
        current_path = path_response.get("data") if path_response else None
        if current_path:
            position_response = self._ipc_request(["get_property", "time-pos"])
            pause_response = self._ipc_request(["get_property", "pause"])
            current_position = position_response.get("data") if position_response else None
            paused = pause_response.get("data") if pause_response else False
            if current_position is not None and not loaded_state:
                self.position_var.set(format_time(current_position))
            if not loaded_state:
                path = Path(current_path)
                chapter_hint = path.parent.name.replace("chapter_", "Chương ")
                self.current_var.set(f"{chapter_hint}  •  {path.stem}")
            if not process_running:
                self.status_var.set("Đang tạm dừng" if paused else "Đang phát")
        self.root.after(1000, self._refresh_state)

    def _ipc_request(self, command):
        if not MPV_SOCKET.exists():
            return None
        payload = (
            json.dumps({"command": command, "request_id": 1}, ensure_ascii=False) + "\n"
        ).encode()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(0.4)
                client.connect(str(MPV_SOCKET))
                client.sendall(payload)
                buffer = b""
                while b"\n" not in buffer:
                    data = client.recv(4096)
                    if not data:
                        return None
                    buffer += data
            for line in buffer.splitlines():
                result = json.loads(line.decode())
                if result.get("request_id") == 1:
                    if result.get("error") != "success":
                        return None
                    return result
        except (OSError, json.JSONDecodeError):
            return None
        return None

    def mpv_command(self, command, quiet=False):
        result = self._ipc_request(command)
        if result is not None:
            return True
        if not quiet:
            try:
                raise RuntimeError("MPV chưa chạy hoặc không phản hồi.")
            except RuntimeError as exc:
                messagebox.showerror("Không điều khiển được MPV", str(exc))
        return False

    def stop(self):
        if self.mpv_command(["quit"], quiet=True):
            self.status_var.set("Đang dừng…")
        elif self.process and self.process.poll() is None:
            self.process.terminate()

    def open_audio_folder(self):
        book = Path(self.book_var.get().strip()).expanduser()
        if book.name:
            self._migrate_book_data(book)
            output_dir = self._book_audio_dir(book)
        else:
            output_dir = AUDIO_ROOT
        output_dir.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.Popen(["xdg-open", str(output_dir)])
        except OSError as exc:
            messagebox.showerror("Không mở được thư mục", str(exc))

    def close(self):
        self._save_settings()
        if self.process and self.process.poll() is None:
            self.stop()
        self.root.after(250, self.root.destroy)


def main():
    root = tk.Tk(className="EpubAudioReader")
    dashboard = AudioDashboard(root)
    if "--auto-resume" in sys.argv[1:]:
        root.after(500, lambda: dashboard.start(resume=True))
    root.mainloop()


if __name__ == "__main__":
    main()
