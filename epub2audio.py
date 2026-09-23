#!/usr/bin/env python3
import argparse
import asyncio
import json
import random
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

from bs4 import BeautifulSoup


def natural_title(text, fallback):
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:140] if text else fallback


def safe_name(s):
    s = re.sub(r'[\\/:*?"<>|]+', "_", s)
    s = re.sub(r"\s+", " ", s).strip(" .")
    return s[:180] or "chapter"


def find_opf(z):
    root = ET.fromstring(z.read("META-INF/container.xml"))
    for elem in root.iter():
        if elem.tag.endswith("rootfile"):
            return elem.attrib["full-path"]
    raise RuntimeError("Không tìm thấy OPF trong EPUB")


def spine_documents(z, opf_path):
    opf = ET.fromstring(z.read(opf_path))
    base = str(PurePosixPath(opf_path).parent)
    if base == ".":
        base = ""

    manifest = {}
    spine = []

    for elem in opf.iter():
        if elem.tag.endswith("item"):
            item_id = elem.attrib.get("id")
            href = elem.attrib.get("href")
            media = elem.attrib.get("media-type", "")
            if item_id and href:
                manifest[item_id] = (href, media)
        elif elem.tag.endswith("itemref"):
            idref = elem.attrib.get("idref")
            if idref:
                spine.append(idref)

    docs = []
    for idref in spine:
        if idref not in manifest:
            continue
        href, media = manifest[idref]
        if "html" not in media and not href.lower().endswith((".xhtml", ".html", ".htm")):
            continue
        path = str(PurePosixPath(base) / href) if base else href
        docs.append(path)
    return docs


def html_to_text(raw):
    soup = BeautifulSoup(raw, "html.parser")
    for tag in soup(["script", "style", "nav"]):
        tag.decompose()

    title = ""
    for selector in ["h1", "h2", "h3", "title"]:
        node = soup.find(selector)
        if node:
            title = node.get_text(" ", strip=True)
            if title:
                break

    body = soup.body or soup
    text = body.get_text("\n", strip=True)
    lines = [re.sub(r"\s+", " ", x).strip() for x in text.splitlines()]
    lines = [x for x in lines if x]

    # Một số EPUB lặp title 2 lần ở đầu chương.
    if len(lines) >= 2 and lines[0] == lines[1]:
        lines.pop(1)

    return natural_title(title, lines[0] if lines else ""), "\n".join(lines)


def split_text(text, max_chars=2800):
    text = re.sub(r"\r\n?", "\n", text).strip()
    if not text:
        return []

    paragraphs = [p.strip() for p in re.split(r"\n+", text) if p.strip()]
    chunks = []
    current = ""

    def flush():
        nonlocal current
        if current.strip():
            chunks.append(current.strip())
            current = ""

    for para in paragraphs:
        # Nếu đoạn quá dài, chia theo câu.
        sentences = re.split(r'(?<=[.!?…])\s+', para)
        for sent in sentences:
            sent = sent.strip()
            if not sent:
                continue

            if len(sent) > max_chars:
                flush()
                for i in range(0, len(sent), max_chars):
                    chunks.append(sent[i:i + max_chars].strip())
                continue

            candidate = sent if not current else current + " " + sent
            if len(candidate) <= max_chars:
                current = candidate
            else:
                flush()
                current = sent
        flush()

    return chunks


MPV_SOCKET = Path("/tmp/epub2audio-mpv.sock")

async def synthesize_chunk(text, out_file, voice, rate, pitch, volume):
    import edge_tts
    communicate = edge_tts.Communicate(
        text=text,
        voice=voice,
        rate=rate,
        pitch=pitch,
        volume=volume,
    )
    await communicate.save(str(out_file))


async def synthesize_with_retry(
    text, out_file, voice, rate, pitch, volume, *, retries=6, label=""
):
    """Generate one TTS part, tolerating Edge TTS throttling and empty responses."""
    for attempt in range(retries + 1):
        try:
            # A small gap prevents a long book from hammering the free endpoint
            # with a new websocket immediately after the previous one closes.
            if attempt == 0:
                await asyncio.sleep(random.uniform(0.35, 0.8))
            await asyncio.wait_for(
                synthesize_chunk(text, out_file, voice, rate, pitch, volume),
                timeout=120,
            )
            if not out_file.exists() or out_file.stat().st_size == 0:
                raise RuntimeError("Edge TTS trả về file audio rỗng")
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            out_file.unlink(missing_ok=True)
            if attempt >= retries:
                raise RuntimeError(f"TTS thất bại {label} sau {retries} lần retry: {exc}") from exc
            # NoAudioReceived is commonly transient or rate related. Longer,
            # jittered waits give the service time to recover without retry storms.
            delay = min(30, 2 ** attempt) + random.uniform(0.5, 1.5)
            print(
                f"[TTS ERROR] {label} retry {attempt + 1}/{retries} "
                f"sau {delay:.1f}s: {exc}",
                flush=True,
            )
            await asyncio.sleep(delay)


def concat_mp3(parts, output):
    if len(parts) == 1:
        # A part can also be referenced later when the complete chapter is built.
        shutil.copy2(str(parts[0]), str(output))
        return

    list_file = output.parent / (output.stem + ".concat.txt")
    with list_file.open("w", encoding="utf-8") as f:
        for p in parts:
            escaped = str(p.resolve()).replace("'", "'\\''")
            f.write(f"file '{escaped}'\n")

    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-f", "concat", "-safe", "0",
                "-i", str(list_file),
                "-c", "copy",
                str(output),
            ],
            check=True,
        )
    finally:
        list_file.unlink(missing_ok=True)


def build_final_mp3(parts, output):
    """Publish a chapter atomically so an interrupted run never looks complete."""
    partial = output.with_name(f".{output.stem}.partial.mp3")
    partial.unlink(missing_ok=True)
    try:
        concat_mp3(parts, partial)
        partial.replace(output)
    finally:
        partial.unlink(missing_ok=True)


def update_playlist(output_dir):
    """Keep a simple chapter playlist alongside completed MP3 files."""
    output_dir = Path(output_dir)
    tracks = sorted(
        path for path in output_dir.glob("[0-9][0-9][0-9][0-9] - *.mp3")
        if path.is_file() and path.stat().st_size > 0
    )
    playlist = output_dir / "audiobook.m3u"
    temporary = output_dir / ".audiobook.m3u.tmp"
    content = "#EXTM3U\n" + "".join(f"{track.name}\n" for track in tracks)
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(playlist)


def audio_duration(path):
    """Return an audio file's duration in seconds using ffprobe."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    return max(0.0, float(result.stdout.strip()))


class StateStore:
    """Atomically persist enough playback state to resume after shutdown."""

    def __init__(self, path, book):
        self.path = Path(path)
        self.book = str(Path(book).resolve())
        self.lock = threading.Lock()
        self.last_write = 0.0
        self.last_chapter_group = None

    def load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return None
        if data.get("book") != self.book:
            return None
        try:
            chapter = int(data["chapter"])
            position = max(0.0, float(data.get("position", 0.0)))
        except (KeyError, TypeError, ValueError):
            return None
        return {**data, "chapter": chapter, "position": position}

    def save(
        self,
        chapter,
        title,
        group,
        position,
        *,
        force=False,
        status="playing",
        current_text="",
        start_part=0,
        end_part=0,
        total_groups=0,
        text_scope="",
    ):
        now = time.monotonic()
        chapter_group = (int(chapter), int(group))
        with self.lock:
            if (
                not force
                and chapter_group == self.last_chapter_group
                and now - self.last_write < 2.0
            ):
                return
            payload = {
                "version": 1,
                "book": self.book,
                "chapter": int(chapter),
                "chapter_title": title,
                "group": int(group),
                "position": round(max(0.0, float(position)), 3),
                "status": status,
                "current_text": str(current_text).strip(),
                "start_part": int(start_part),
                "end_part": int(end_part),
                "total_groups": int(total_groups),
                "text_scope": str(text_scope),
                "updated_at": int(time.time()),
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.path.with_name(f".{self.path.name}.tmp")
            temp_path.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temp_path.replace(self.path)
            self.last_write = now
            self.last_chapter_group = chapter_group


async def synthesize_group_with_fallback(
    chunk_texts, out_file, voice, rate, pitch, volume, *, label
):
    """Prefer one request per group, then fall back to smaller requests if needed."""
    try:
        await synthesize_with_retry(
            "\n".join(chunk_texts), out_file,
            voice, rate, pitch, volume,
            retries=1,
            label=label,
        )
        return
    except RuntimeError as group_error:
        print(
            f"[TTS FALLBACK] {label}: chia group thành {len(chunk_texts)} phần nhỏ "
            f"vì Edge TTS không nhận group ({group_error})",
            flush=True,
        )

    fallback_parts = []
    try:
        for part_number, chunk_text in enumerate(chunk_texts, start=1):
            part_file = out_file.with_name(
                f".{out_file.stem}.fallback_{part_number:02d}.mp3"
            )
            await synthesize_with_retry(
                chunk_text, part_file,
                voice, rate, pitch, volume,
                label=f"{label} / phần fallback {part_number}",
            )
            fallback_parts.append(part_file)
        await asyncio.to_thread(build_final_mp3, fallback_parts, out_file)
    finally:
        for part_file in fallback_parts:
            part_file.unlink(missing_ok=True)


class MPVClosed(RuntimeError):
    pass


class MPVController:
    """One headless mpv audio engine and one long-lived IPC client."""

    def __init__(self, socket_path=MPV_SOCKET):
        self.socket_path = Path(socket_path)
        self.proc = None
        self.sock = None
        self.buffer = b""
        self.request_id = 0
        self.owns_socket = False
        self.last_time_pos = 0.0

    def start(self):
        if self.socket_path.exists():
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            probe.settimeout(0.3)
            try:
                probe.connect(str(self.socket_path))
            except OSError:
                # Socket left by a process that is no longer running.
                self.socket_path.unlink(missing_ok=True)
            else:
                raise RuntimeError(
                    "Một phiên EPUB Audio khác đang dùng MPV IPC. "
                    "Hãy đóng phiên đó trước khi chạy lại."
                )
            finally:
                probe.close()
        self.proc = subprocess.Popen(
            [
                "mpv",
                "--idle=yes",
                "--no-video",
                "--force-window=no",
                "--audio-display=no",
                "--keep-open=yes",
                "--input-default-bindings=no",
                "--really-quiet",
                f"--input-ipc-server={self.socket_path}",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.owns_socket = True
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("mpv đã thoát trước khi IPC sẵn sàng.")
            if self.socket_path.exists():
                try:
                    self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    self.sock.settimeout(1.0)
                    self.sock.connect(str(self.socket_path))
                    self._enable_playback_events()
                    return
                except OSError:
                    if self.sock:
                        self.sock.close()
                    self.sock = None
            time.sleep(0.1)
        self.close()
        raise RuntimeError("Không khởi động được mpv IPC socket.")

    def _observe_property(self, observer_id, name):
        self.request_id += 1
        request_id = self.request_id
        payload = {
            "command": ["observe_property", observer_id, name],
            "request_id": request_id,
        }
        self.sock.sendall((json.dumps(payload) + "\n").encode("utf-8"))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            message = self._read_message(deadline=deadline)
            if message.get("request_id") == request_id:
                if message.get("error") != "success":
                    raise RuntimeError(f"mpv không observe được {name}.")
                return
        raise TimeoutError(f"mpv không xác nhận observe_property {name}.")

    def _enable_playback_events(self):
        """Subscribe to EOF and position events on the persistent connection."""
        self._observe_property(1, "eof-reached")
        self._observe_property(2, "time-pos")

    def _read_message(self, deadline=None):
        while b"\n" not in self.buffer:
            if not self.proc or self.proc.poll() is not None:
                raise MPVClosed("mpv đã đóng.")
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("Hết thời gian chờ phản hồi từ mpv IPC.")
            try:
                data = self.sock.recv(65536)
            except socket.timeout:
                continue
            except OSError as exc:
                raise MPVClosed(f"Mất kết nối mpv IPC: {exc}") from exc
            if not data:
                raise MPVClosed("mpv đã đóng kết nối IPC.")
            self.buffer += data
        line, self.buffer = self.buffer.split(b"\n", 1)
        try:
            return json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return {}

    def play_and_wait(self, path, *, start_position=0.0, on_progress=None):
        """Wait for file-loaded, then end-file or keep-open's eof-reached event."""
        if not self.sock:
            raise MPVClosed("mpv IPC chưa được kết nối.")
        self.request_id += 1
        request_id = self.request_id
        payload = {
            "command": ["loadfile", str(Path(path).resolve()), "replace"],
            "request_id": request_id,
        }
        try:
            self.sock.sendall((json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"))
        except OSError as exc:
            raise MPVClosed(f"Không gửi được lệnh tới mpv: {exc}") from exc

        command_ok = False
        file_loaded = False
        progress_ready = start_position <= 0
        seek_request_id = None
        seek_deadline = None
        self.last_time_pos = max(0.0, float(start_position))
        load_deadline = time.monotonic() + 30
        while True:
            message_deadline = None
            if not file_loaded:
                message_deadline = load_deadline
            elif not progress_ready:
                message_deadline = seek_deadline
            message = self._read_message(
                deadline=message_deadline
            )
            if message.get("request_id") == request_id:
                if message.get("error") != "success":
                    raise RuntimeError(f"mpv loadfile lỗi: {message.get('error')}")
                command_ok = True
            if seek_request_id and message.get("request_id") == seek_request_id:
                if message.get("error") != "success":
                    raise RuntimeError(f"mpv seek lỗi: {message.get('error')}")
                progress_ready = True
            event = message.get("event")
            if event == "file-loaded":
                file_loaded = True
                if start_position > 0:
                    self.request_id += 1
                    seek_request_id = self.request_id
                    seek_deadline = time.monotonic() + 10
                    seek_payload = {
                        "command": ["seek", float(start_position), "absolute+exact"],
                        "request_id": seek_request_id,
                    }
                    self.sock.sendall(
                        (json.dumps(seek_payload) + "\n").encode("utf-8")
                    )
            elif event == "end-file" and file_loaded:
                reason = message.get("reason", "unknown")
                if reason in {"error", "unknown"}:
                    raise RuntimeError(f"mpv không phát được file (reason={reason}).")
                return
            elif (
                event == "property-change"
                and message.get("name") == "eof-reached"
                and message.get("data") is True
                and file_loaded
            ):
                # With --keep-open=yes MPV deliberately does not unload the file,
                # so end-file may not arrive. This event is the actual EOF signal.
                return
            elif (
                event == "property-change"
                and message.get("name") == "time-pos"
                and message.get("data") is not None
                and file_loaded
                and progress_ready
            ):
                try:
                    self.last_time_pos = max(0.0, float(message["data"]))
                except (TypeError, ValueError):
                    pass
                else:
                    if on_progress:
                        try:
                            on_progress(self.last_time_pos)
                        except OSError as exc:
                            print(f"[STATE WARNING] Không lưu được vị trí: {exc}", flush=True)
            if not file_loaded and time.monotonic() > load_deadline:
                state = "đã nhận response" if command_ok else "chưa nhận response"
                raise TimeoutError(f"mpv không phát event file-loaded trong 30s ({state}).")

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=3)
        self.proc = None
        if self.owns_socket:
            self.socket_path.unlink(missing_ok=True)
            self.owns_socket = False


@dataclass
class Chapter:
    index: int
    title: str
    text: str


@dataclass
class AudioGroup:
    chapter: Chapter
    group_index: int
    total_groups: int
    start_part: int
    end_part: int
    path: Path
    duration: float = 0.0
    existing_chapter: bool = False
    text: str = ""
    text_segments: tuple = ()


@dataclass(frozen=True)
class TextSegment:
    group_index: int
    total_groups: int
    start_part: int
    end_part: int
    start_time: float
    end_time: float
    text: str


class AudioPrefetcher:
    def __init__(self, chapters, queue, temp_root, args):
        self.chapters = chapters
        self.queue = queue
        self.temp_root = Path(temp_root)
        self.args = args

    def existing_output(self, chapter):
        matches = sorted(self.args.output_dir.glob(f"{chapter.index:04d} - *.mp3"))
        valid = [path for path in matches if path.is_file() and path.stat().st_size > 0]
        return valid[0] if valid and not self.args.overwrite else None

    @staticmethod
    def _metadata_path(audio_path):
        return Path(audio_path).with_suffix(".reader.json")

    def _load_text_segments(self, audio_path):
        try:
            payload = json.loads(
                self._metadata_path(audio_path).read_text(encoding="utf-8")
            )
            segments = tuple(
                TextSegment(
                    group_index=int(item["group_index"]),
                    total_groups=int(item["total_groups"]),
                    start_part=int(item["start_part"]),
                    end_part=int(item["end_part"]),
                    start_time=max(0.0, float(item["start_time"])),
                    end_time=max(0.0, float(item["end_time"])),
                    text=str(item["text"]).strip(),
                )
                for item in payload["segments"]
            )
            if not segments or any(
                not segment.text or segment.end_time <= segment.start_time
                for segment in segments
            ):
                return ()
            return segments
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return ()

    def _save_text_segments(self, audio_path, chapter, segments):
        metadata_path = self._metadata_path(audio_path)
        temporary = metadata_path.with_name(f".{metadata_path.name}.tmp")
        payload = {
            "version": 1,
            "chapter": chapter.index,
            "chapter_title": chapter.title,
            "segments": [segment.__dict__ for segment in segments],
        }
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(metadata_path)

    async def run(self):
        for chapter in self.chapters:
            existing = self.existing_output(chapter)
            if existing:
                print(f"[CACHE] {existing.name}", flush=True)
                duration = await asyncio.to_thread(audio_duration, existing)
                text_segments = self._load_text_segments(existing)
                await self.queue.put(
                    AudioGroup(
                        chapter, 1, 1, 0, 0, existing,
                        duration=duration,
                        existing_chapter=True,
                        text=chapter.text,
                        text_segments=text_segments,
                    )
                )
                continue
            await self._produce_chapter(chapter)
        await self.queue.put(None)

    async def _produce_chapter(self, chapter):
        chunks = split_text(chapter.text)
        if not chunks:
            print(f"[EMPTY] {chapter.index:04d} - {chapter.title}", flush=True)
            return
        group_size = max(1, self.args.group_size)
        total_groups = (len(chunks) + group_size - 1) // group_size
        chapter_dir = self.temp_root / f"chapter_{chapter.index:04d}"
        chapter_dir.mkdir(parents=True, exist_ok=True)
        parts = []
        text_segments = []
        elapsed = 0.0
        output = self.args.output_dir / (
            f"{chapter.index:04d} - {safe_name(chapter.title)}.mp3"
        )
        print(
            f"[TTS] {chapter.index:04d} - {chapter.title} "
            f"({len(chunks)} phần, {total_groups} group)",
            flush=True,
        )
        for group_index in range(1, total_groups + 1):
            start_part = (group_index - 1) * group_size + 1
            end_part = min(len(chunks), group_index * group_size)
            print(
                f"[PREP] Chương {chapter.index} / Group {group_index:02d}/{total_groups:02d} "
                f"(phần {start_part}-{end_part})",
                flush=True,
            )
            # One TTS request per group greatly reduces websocket churn and
            # throttling compared with one request for every small paragraph.
            group_chunks = chunks[start_part - 1:end_part]
            chapter_part = chapter_dir / f"chapter_group_{group_index:04d}.mp3"
            await synthesize_group_with_fallback(
                group_chunks, chapter_part,
                self.args.voice, self.args.rate, self.args.pitch, self.args.volume,
                label=f"Chương {chapter.index} / Group {group_index}",
            )
            parts.append(chapter_part)
            duration = await asyncio.to_thread(audio_duration, chapter_part)
            group_text = "\n\n".join(group_chunks)
            text_segments.append(
                TextSegment(
                    group_index=group_index,
                    total_groups=total_groups,
                    start_part=start_part,
                    end_part=end_part,
                    start_time=elapsed,
                    end_time=elapsed + duration,
                    text=group_text,
                )
            )
            elapsed += duration

            # Playback may delete its copy as soon as the group finishes while
            # chapter_part remains available for the final chapter MP3 concat.
            group_path = chapter_dir / f"group_{group_index:04d}.mp3"
            await asyncio.to_thread(shutil.copy2, chapter_part, group_path)
            await self.queue.put(
                AudioGroup(
                    chapter, group_index, total_groups,
                    start_part, end_part, group_path,
                    duration=duration,
                    text=group_text,
                )
            )

        await asyncio.to_thread(build_final_mp3, parts, output)
        await asyncio.to_thread(
            self._save_text_segments, output, chapter, tuple(text_segments)
        )
        await asyncio.to_thread(update_playlist, self.args.output_dir)
        for part in parts:
            part.unlink(missing_ok=True)
        print(f"[SAVED] {output}", flush=True)


class PlaybackSession:
    def __init__(self, chapters, args, mpv, state_store=None, resume_position=0.0):
        self.chapters = chapters
        self.args = args
        self.mpv = mpv
        self.state_store = state_store
        self.resume_position = max(0.0, float(resume_position))
        self.queue = asyncio.Queue(maxsize=max(1, args.buffer_groups))

    @staticmethod
    def _text_segment_at(group, local_position):
        if not group.text_segments:
            return None
        local_position = max(0.0, float(local_position))
        for segment in group.text_segments:
            if local_position < segment.end_time:
                return segment
        return group.text_segments[-1]

    def _save_state(
        self,
        group,
        position,
        *,
        local_position=None,
        force=False,
        status="playing",
    ):
        if self.state_store:
            segment = self._text_segment_at(group, local_position or 0.0)
            self.state_store.save(
                group.chapter.index,
                group.chapter.title,
                segment.group_index if segment else group.group_index,
                position,
                force=force,
                status=status,
                current_text=segment.text if segment else group.text,
                start_part=segment.start_part if segment else group.start_part,
                end_part=segment.end_part if segment else group.end_part,
                total_groups=(
                    segment.total_groups if segment else group.total_groups
                ),
                text_scope=(
                    "chapter"
                    if group.existing_chapter and not group.text_segments
                    else "group"
                ),
            )

    def _save_next_chapter(self, group, completed_position):
        if not self.state_store or group.group_index != group.total_groups:
            return
        try:
            current = next(
                i for i, chapter in enumerate(self.chapters)
                if chapter.index == group.chapter.index
            )
            next_chapter = self.chapters[current + 1]
        except (StopIteration, IndexError):
            self._save_state(
                group,
                completed_position,
                local_position=group.duration,
                force=True,
                status="completed",
            )
            return
        self.state_store.save(
            next_chapter.index,
            next_chapter.title,
            1,
            0.0,
            force=True,
            status="ready",
        )

    async def _consume(self):
        current_chapter = None
        chapter_elapsed = 0.0
        resume_target = self.resume_position
        while True:
            group = await self.queue.get()
            try:
                if group is None:
                    return
                if group.chapter.index != current_chapter:
                    current_chapter = group.chapter.index
                    chapter_elapsed = 0.0
                    if group.chapter.index != self.chapters[0].index:
                        resume_target = 0.0

                group_end = chapter_elapsed + group.duration
                if (
                    not group.existing_chapter
                    and group.duration > 0
                    and resume_target >= group_end - 0.05
                ):
                    print(
                        f"[RESUME] Bỏ qua Chương {group.chapter.index} / "
                        f"Group {group.group_index:02d} đã nghe.",
                        flush=True,
                    )
                    chapter_elapsed = group_end
                    group.path.unlink(missing_ok=True)
                    self._save_state(group, chapter_elapsed, force=True, status="ready")
                    self._save_next_chapter(group, chapter_elapsed)
                    continue

                start_position = max(0.0, resume_target - chapter_elapsed)
                if group.duration > 0:
                    start_position = min(start_position, max(0.0, group.duration - 0.05))
                resume_target = 0.0
                buffered = self.queue.qsize()
                if group.existing_chapter:
                    detail = "MP3 hoàn chỉnh"
                else:
                    detail = f"phần {group.start_part}-{group.end_part}"
                print(
                    f"[PLAY] Chương {group.chapter.index}: {group.chapter.title} | "
                    f"Group {group.group_index:02d}/{group.total_groups:02d} ({detail}) | "
                    f"Buffered: {buffered}",
                    flush=True,
                )
                if start_position > 0:
                    print(
                        f"[RESUME] Tiếp tục tại {start_position:.1f}s trong phần hiện tại.",
                        flush=True,
                    )

                def save_progress(local_position):
                    self._save_state(
                        group,
                        chapter_elapsed + local_position,
                        local_position=local_position,
                    )

                self._save_state(
                    group,
                    chapter_elapsed + start_position,
                    local_position=start_position,
                    force=True,
                )
                try:
                    await asyncio.to_thread(
                        self.mpv.play_and_wait,
                        group.path,
                        start_position=start_position,
                        on_progress=save_progress,
                    )
                except BaseException:
                    self._save_state(
                        group,
                        chapter_elapsed + self.mpv.last_time_pos,
                        local_position=self.mpv.last_time_pos,
                        force=True,
                        status="paused",
                    )
                    raise
                if not group.existing_chapter:
                    group.path.unlink(missing_ok=True)
                chapter_elapsed = max(
                    group_end,
                    chapter_elapsed + self.mpv.last_time_pos,
                )
                self._save_state(
                    group,
                    chapter_elapsed,
                    local_position=self.mpv.last_time_pos,
                    force=True,
                    status="ready",
                )
                self._save_next_chapter(group, chapter_elapsed)
            finally:
                self.queue.task_done()

    async def run(self, temp_root):
        producer = AudioPrefetcher(self.chapters, self.queue, temp_root, self.args)
        producer_task = asyncio.create_task(producer.run(), name="tts-producer")
        consumer_task = asyncio.create_task(self._consume(), name="mpv-consumer")
        tasks = (producer_task, consumer_task)
        try:
            await asyncio.gather(*tasks)
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


async def process_chapter(
    index, title, text, out_dir, voice, rate, pitch, volume, overwrite,
    play=False, mpv=None,
):
    filename = f"{index:04d} - {safe_name(title)}.mp3"
    output = out_dir / filename

    if output.exists() and not overwrite:
        print(f"[SKIP] {filename}")
        if play:
            print(f"[PLAY] {filename}")
            if mpv:
                await asyncio.to_thread(mpv.play_and_wait, output)
            else:
                await asyncio.to_thread(
                    subprocess.run,
                    ["mpv", "--really-quiet", "--no-video", "--keep-open=no", str(output)],
                    check=False,
                )
        return output

    chunks = split_text(text)
    if not chunks:
        print(f"[EMPTY] {index:04d} - {title}")
        return None

    print(f"[TTS] {index:04d}/{title} ({len(chunks)} phần)")

    with tempfile.TemporaryDirectory(prefix=f"epub_tts_{index:04d}_") as td:
        temp_dir = Path(td)
        parts = []

        async def make_part(i, chunk_text):
            part_file = temp_dir / f"{i:04d}.mp3"
            print(f"      [{i}/{len(chunks)}] Đang tạo audio...", flush=True)
            await synthesize_with_retry(
                chunk_text, part_file, voice, rate, pitch, volume,
                label=f"Chương {index} / phần {i}",
            )
            return part_file

        for n, chunk in enumerate(chunks, start=1):
            parts.append(await make_part(n, chunk))

        await asyncio.to_thread(build_final_mp3, parts, output)
        await asyncio.to_thread(update_playlist, out_dir)

    print(f"[OK]  {output}")
    if play:
        print(f"[PLAY] {filename}")
        if mpv:
            await asyncio.to_thread(mpv.play_and_wait, output)
        else:
            await asyncio.to_thread(
                subprocess.run,
                ["mpv", "--really-quiet", "--no-video", "--keep-open=no", str(output)],
                check=False,
            )
    return output


async def main():
    parser = argparse.ArgumentParser(
        description="EPUB -> audiobook MP3 tiếng Việt bằng Microsoft Edge Neural TTS"
    )
    parser.add_argument("epub", type=Path)
    parser.add_argument("-o", "--output-dir", type=Path, default=Path("audio"))
    parser.add_argument("--voice", default="vi-VN-NamMinhNeural",
                        help="Ví dụ: vi-VN-NamMinhNeural hoặc vi-VN-HoaiMyNeural")
    parser.add_argument("--rate", default="-5%", help="Tốc độ, ví dụ -10%%, +0%%, +15%%")
    parser.add_argument("--pitch", default="+0Hz")
    parser.add_argument("--volume", default="+0%")
    parser.add_argument("--start", type=int, default=1, help="Bắt đầu từ chương N")
    parser.add_argument("--start-title", help="Bắt đầu từ mục đầu tiên có title chứa chuỗi này")
    parser.add_argument("--resume", action="store_true", help="Tiếp tục chương và vị trí đã lưu")
    parser.add_argument(
        "--state-file", type=Path, default=Path(".audio_state.json"),
        help="File lưu trạng thái nghe (mặc định: .audio_state.json)",
    )
    parser.add_argument("--limit", type=int, default=0, help="Chỉ xử lý N chương; 0 = tất cả")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--play", action="store_true", help="Tự phát từng chương sau khi tạo xong, rồi tiếp tục đến hết")
    parser.add_argument("--stream", action="store_true", help="Phát ngay từng phần khi vừa tạo xong; không cần chờ hết cả chương")
    parser.add_argument("--group-size", type=int, default=3, help="Số phần gộp lại thành 1 cụm để phát mượt hơn khi --stream (mặc định: 3)")
    parser.add_argument("--buffer-groups", type=int, default=3, help="Số group audio giữ sẵn trong queue (mặc định: 3)")
    parser.add_argument("--prefetch-chapters", type=int, default=1, help=argparse.SUPPRESS)
    parser.add_argument(
        "--persistent-mpv", action="store_true",
        help="Dùng một MPV audio engine chạy ngầm cho toàn bộ phiên nghe",
    )
    parser.add_argument("--extract-only", action="store_true",
                        help="Chỉ tách text để kiểm tra, chưa tạo audio")
    args = parser.parse_args()

    if not args.epub.exists():
        raise SystemExit(f"Không tìm thấy file: {args.epub}")

    if not args.extract_only and shutil.which("ffmpeg") is None:
        raise SystemExit("Chưa có ffmpeg. Cài bằng: sudo apt install ffmpeg")

    if (args.play or args.stream) and shutil.which("mpv") is None:
        raise SystemExit("Chưa có mpv. Cài bằng: sudo apt install mpv")
    if args.stream and not args.persistent_mpv:
        raise SystemExit("--stream cần --persistent-mpv để bảo đảm chỉ dùng một MPV và IPC events.")

    args.output_dir.mkdir(parents=True, exist_ok=True)
    if not args.extract_only:
        await asyncio.to_thread(update_playlist, args.output_dir)
    text_dir = args.output_dir / "_text"
    if args.extract_only:
        text_dir.mkdir(parents=True, exist_ok=True)

    chapters = []
    with ZipFile(args.epub) as z:
        opf = find_opf(z)
        docs = spine_documents(z, opf)
        for doc in docs:
            try:
                title, text = html_to_text(z.read(doc))
            except KeyError:
                continue
            if len(text.strip()) < 20:
                continue
            chapters.append((title or PurePosixPath(doc).stem, text))

    print(f"Tìm thấy {len(chapters)} mục/chương có nội dung.")
    state_store = StateStore(args.state_file, args.epub)
    resume_position = 0.0
    resume_state = state_store.load() if args.resume else None
    if resume_state and 1 <= resume_state["chapter"] <= len(chapters):
        start_idx = resume_state["chapter"] - 1
        resume_position = resume_state["position"]
        print(
            f"[RESUME] Chương {resume_state['chapter']} tại "
            f"{resume_position:.1f}s.",
            flush=True,
        )
    elif args.start_title:
        needle = args.start_title.casefold()
        start_idx = next(
            (i for i, (title, _) in enumerate(chapters) if needle in title.casefold()),
            None,
        )
        if start_idx is None:
            raise SystemExit(f'Không tìm thấy title chứa: "{args.start_title}"')
    else:
        start_idx = max(args.start, 1) - 1

    selected_raw = chapters[start_idx:]
    if args.limit > 0:
        selected_raw = selected_raw[:args.limit]
    selected = [
        Chapter(index, title, text)
        for index, (title, text) in enumerate(selected_raw, start=start_idx + 1)
    ]
    if not selected:
        raise SystemExit("Không có chương nào trong phạm vi đã chọn.")

    if (args.play or args.stream) and not args.extract_only:
        state_store.save(
            selected[0].index,
            selected[0].title,
            1,
            resume_position,
            force=True,
            status="preparing",
        )

    if args.extract_only:
        for chapter in selected:
            txt = text_dir / f"{chapter.index:04d} - {safe_name(chapter.title)}.txt"
            txt.write_text(chapter.text, encoding="utf-8")
            print(f"[TEXT] {txt}")
        print("Hoàn tất.")
        return

    mpv = None
    try:
        if args.persistent_mpv and (args.play or args.stream):
            mpv = MPVController()
            await asyncio.to_thread(mpv.start)
            print(
                "[PLAYER] MPV audio engine đang chạy ngầm; điều khiển bằng dashboard.",
                flush=True,
            )

        if args.stream:
            print(
                f"[PIPELINE] group-size={max(1, args.group_size)}, "
                f"buffer={max(1, args.buffer_groups)} group, prefetch xuyên chương",
                flush=True,
            )
            with tempfile.TemporaryDirectory(prefix="epub_audio_session_") as temp_root:
                await PlaybackSession(
                    selected,
                    args,
                    mpv,
                    state_store=state_store,
                    resume_position=resume_position,
                ).run(temp_root)
        else:
            for chapter in selected:
                await process_chapter(
                    chapter.index, chapter.title, chapter.text, args.output_dir,
                    args.voice, args.rate, args.pitch, args.volume,
                    args.overwrite, args.play, mpv,
                )
    finally:
        if mpv:
            await asyncio.to_thread(mpv.close)

    print("Hoàn tất.")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nĐã dừng và dọn MPV/temp files.")
    except MPVClosed as exc:
        print(f"\n[PLAYER] {exc} Đã dừng và dọn temp files.")
    except (RuntimeError, TimeoutError, subprocess.CalledProcessError) as exc:
        print(f"\n[ERROR] {exc}")
        raise SystemExit(1)
