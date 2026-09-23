import argparse
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import epub2audio
import epub_audio_gui as gui


class ReaderSyncTests(unittest.TestCase):
    def test_state_updates_immediately_when_group_changes(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            book = root / "book.epub"
            book.write_bytes(b"test")
            store = epub2audio.StateStore(root / "state.json", book)
            store.save(
                1,
                "Chapter",
                1,
                1.0,
                force=True,
                current_text="Group one",
                start_part=1,
                end_part=3,
                total_groups=2,
                text_scope="group",
            )
            store.save(
                1,
                "Chapter",
                2,
                1.1,
                current_text="Group two",
                start_part=4,
                end_part=6,
                total_groups=2,
                text_scope="group",
            )
            state = json.loads((root / "state.json").read_text())
            self.assertEqual(state["group"], 2)
            self.assertEqual(state["current_text"], "Group two")

    def test_cached_audio_metadata_round_trip(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            audio = root / "0001 - Chapter.mp3"
            audio.write_bytes(b"audio")
            args = argparse.Namespace(
                output_dir=root,
                overwrite=False,
                voice="vi-VN-HoaiMyNeural",
            )
            prefetcher = epub2audio.AudioPrefetcher([], None, root, args)
            chapter = epub2audio.Chapter(1, "Chapter", "Text")
            segments = (
                epub2audio.TextSegment(1, 2, 1, 3, 0.0, 4.0, "First"),
                epub2audio.TextSegment(2, 2, 4, 6, 4.0, 8.0, "Second"),
            )
            prefetcher._save_text_segments(audio, chapter, segments)
            self.assertEqual(prefetcher._load_text_segments(audio), segments)

            group = epub2audio.AudioGroup(
                chapter,
                1,
                1,
                0,
                0,
                audio,
                duration=8.0,
                existing_chapter=True,
                text=chapter.text,
                text_segments=segments,
            )
            self.assertEqual(
                epub2audio.PlaybackSession._text_segment_at(group, 2.0).text,
                "First",
            )
            self.assertEqual(
                epub2audio.PlaybackSession._text_segment_at(group, 6.0).text,
                "Second",
            )

    def test_voice_cache_is_not_reused_for_another_voice(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            audio = root / "0001 - Chapter.mp3"
            audio.write_bytes(b"audio")
            chapter = epub2audio.Chapter(1, "Chapter", "Text")

            default_args = argparse.Namespace(
                output_dir=root,
                overwrite=False,
                voice="vi-VN-NamMinhNeural",
            )
            other_args = argparse.Namespace(
                output_dir=root,
                overwrite=False,
                voice="vi-VN-HoaiMyNeural",
            )
            self.assertEqual(
                epub2audio.AudioPrefetcher([], None, root, default_args)
                .existing_output(chapter),
                audio,
            )
            self.assertIsNone(
                epub2audio.AudioPrefetcher([], None, root, other_args)
                .existing_output(chapter)
            )

            metadata = audio.with_suffix(".reader.json")
            metadata.write_text(
                json.dumps({"voice": "vi-VN-HoaiMyNeural", "segments": []})
            )
            self.assertEqual(
                epub2audio.AudioPrefetcher([], None, root, other_args)
                .existing_output(chapter),
                audio,
            )


@unittest.skipUnless(os.environ.get("DISPLAY"), "Tk UI test requires DISPLAY")
class ReaderModeUITests(unittest.TestCase):
    def test_compact_mode_and_responsive_text_only_layout(self):
        import tkinter as tk

        with tempfile.TemporaryDirectory() as raw:
            root_dir = Path(raw)
            with mock.patch.object(gui, "SETTINGS_FILE", root_dir / "settings.json"), \
                    mock.patch.object(gui, "STATE_DIR", root_dir / "states"), \
                    mock.patch.object(gui, "AUDIO_ROOT", root_dir / "audio"), \
                    mock.patch.object(gui, "LEGACY_STATE_FILE", root_dir / "legacy.json"), \
                    mock.patch.object(gui.AudioDashboard, "_ipc_request", return_value=None):
                root = tk.Tk(className="EpubAudioReaderTest")
                root.withdraw()
                dashboard = gui.AudioDashboard(root)
                root.update_idletasks()

                dashboard.chapter_input.delete(0, "end")
                dashboard.chapter_input.icursor(0)
                for digit in "480":
                    dashboard._chapter_keypress(
                        SimpleNamespace(char=digit, state=0, keysym=digit)
                    )
                self.assertEqual(dashboard.chapter_var.get(), "480")
                root.geometry("900x780+3000+3000")
                root.deiconify()
                root.update()
                dashboard.chapter_input.delete(0, "end")
                dashboard.chapter_input.focus_force()
                for digit in "480":
                    dashboard.chapter_input.event_generate(f"<KeyPress-{digit}>")
                    root.update()
                self.assertEqual(dashboard.chapter_var.get(), "480")
                root.withdraw()
                with mock.patch.object(root, "clipboard_get", return_value=" 512\n"):
                    dashboard._chapter_paste()
                self.assertEqual(dashboard.chapter_var.get(), "480512")

                dashboard.toggle_setup()
                self.assertTrue(dashboard.setup_collapsed)
                self.assertEqual(dashboard.setup_body.winfo_manager(), "")

                dashboard._set_reader_content("Text đang đọc")
                dashboard._enter_reader_mode()
                self.assertTrue(dashboard.reader_mode)
                self.assertEqual(dashboard.header_card.winfo_manager(), "")
                self.assertEqual(dashboard.reader_card.winfo_manager(), "pack")
                self.assertEqual(dashboard.reader_text.winfo_manager(), "")
                self.assertEqual(dashboard.reader_play_button.winfo_manager(), "place")
                self.assertEqual(dashboard.reader_rewind_button.winfo_manager(), "place")

                event = type(
                    "Event",
                    (),
                    {"widget": root, "width": 430, "height": 240},
                )()
                dashboard._on_window_configure(event)
                self.assertFalse(dashboard.reader_chrome_visible)
                self.assertEqual(dashboard.reader_header.winfo_manager(), "")
                self.assertEqual(
                    dashboard.reader_play_button.winfo_manager(), "place"
                )

                with mock.patch.object(
                    dashboard, "mpv_command", return_value=True
                ) as mpv_command:
                    dashboard.reader_play_button.invoke()
                    mpv_command.assert_called_once_with(["cycle", "pause"])
                with mock.patch.object(
                    dashboard, "mpv_command", return_value=True
                ) as mpv_command:
                    dashboard.reader_rewind_button.invoke()
                    mpv_command.assert_called_once_with(["seek", -15, "relative"])

                book = root_dir / "book.epub"
                book.write_bytes(b"epub")
                dashboard.voice_var.set("vi-VN-HoaiMyNeural")
                dashboard._migrate_book_data = mock.Mock()
                dashboard._has_resume_for = mock.Mock(return_value=False)
                process = mock.Mock(pid=1234, stdout=None)
                with mock.patch.object(
                    gui.subprocess, "Popen", return_value=process
                ) as popen:
                    dashboard._launch(book, 4, resume=False)
                    command = popen.call_args.args[0]
                    self.assertEqual(
                        command[command.index("--voice") + 1],
                        "vi-VN-HoaiMyNeural",
                    )
                    self.assertIn(
                        "vi-VN-HoaiMyNeural",
                        command[command.index("--output-dir") + 1],
                    )

                responses = {
                    "path": {"data": "/tmp/chapter_0001/group_0001.mp3"},
                    "time-pos": {"data": 2.0},
                    "pause": {"data": True},
                }
                dashboard._ipc_request = lambda command: responses.get(command[1])
                dashboard._refresh_state()
                self.assertEqual(dashboard.reader_play_button.cget("text"), "▶")

                dashboard._leave_reader_mode()
                self.assertFalse(dashboard.reader_mode)
                self.assertEqual(dashboard.header_card.winfo_manager(), "pack")
                root.destroy()


if __name__ == "__main__":
    unittest.main()
