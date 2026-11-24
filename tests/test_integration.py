import base64
import json
import os
import shutil
import struct
import subprocess
import tempfile
import time
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from mkv_redux.core import (
    Cancellation,
    Decision,
    find_mkvmerge,
    inspect_file,
    output_path,
    remux_file,
)

TOOL = Path(os.environ["MKVMERGE"]).absolute() if os.environ.get("MKVMERGE") else find_mkvmerge()


def make_sample(tool, folder):
    # A one-pixel VP8 keyframe from a WebP image, wrapped in IVF for mkvmerge.
    frame = base64.b64decode("UklGRiIAAABXRUJQVlA4IBYAAAAwAQCdASoBAAEADsD+JaQAA3AAAAAA")[20:]
    picture = folder / "picture.ivf"
    picture.write_bytes(
        struct.pack("<4sHH4sHHIIII", b"DKIF", 0, 32, b"VP80", 1, 1, 1, 1, 1, 0)
        + struct.pack("<IQ", len(frame), 0)
        + frame
    )
    pcm = folder / "silence.wav"
    with wave.open(str(pcm), "wb") as audio:
        audio.setparams((1, 2, 8000, 0, "NONE", "not compressed"))
        audio.writeframes(bytes(16_000))
    subtitles = folder / "subtitles.srt"
    subtitles.write_text("1\n00:00:00,000 --> 00:00:00,900\nSample\n", encoding="utf-8")
    chapters = folder / "chapters.txt"
    chapters.write_text("CHAPTER01=00:00:00.000\nCHAPTER01NAME=Intro\n", encoding="utf-8")
    attachment = folder / "sample.txt"
    attachment.write_text("Attachment", encoding="utf-8")
    source = folder / "Épisode [sample].mkv"
    subprocess.run(
        [
            str(tool),
            "-o",
            str(source),
            "--chapters",
            str(chapters),
            "--attachment-mime-type",
            "text/plain",
            "--attach-file",
            str(attachment),
            str(picture),
            "--language",
            "0:en-US",
            str(pcm),
            "--language",
            "0:es",
            str(pcm),
            "--language",
            "0:en-GB",
            str(pcm),
            "--language",
            "0:und",
            "--track-name",
            "0:Unspecified commentary",
            str(pcm),
            "--language",
            "0:ja",
            str(subtitles),
        ],
        check=True,
        capture_output=True,
        timeout=15,
    )
    return source


class MKVToolNixTests(unittest.TestCase):
    def test_remux_retains_video_english_and_und_tracks_and_other_content(self):
        if TOOL is None or not TOOL.is_file():
            self.skipTest("MKVToolNix not found")
        with tempfile.TemporaryDirectory() as scratch:
            source = make_sample(TOOL, Path(scratch))
            original = source.read_bytes()
            plan = inspect_file(TOOL, source, Cancellation())
            self.assertEqual(plan.decision, Decision.READY)
            self.assertEqual([track.tag for track in plan.audio], ["en-US", "es", "en-GB", "und"])

            dest = output_path(source)
            progress = []
            self.assertEqual(remux_file(TOOL, plan, dest, Cancellation(), progress.append), "")
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(inspect_file(TOOL, dest, Cancellation()).decision, Decision.UNCHANGED)
            self.assertIn(100, progress)

            data = json.loads(subprocess.check_output([str(TOOL), "-J", str(dest)], timeout=15))
            self.assertEqual([t["type"] for t in data["tracks"]], ["video", "audio", "audio", "audio", "subtitles"])
            self.assertEqual(data["tracks"][0]["properties"]["codec_id"], "V_VP8")
            self.assertEqual(
                [t["properties"]["language_ietf"] for t in data["tracks"][1:]], ["en-US", "en-GB", "und", "ja"]
            )
            self.assertEqual(data["tracks"][3]["properties"]["track_name"], "Unspecified commentary")
            self.assertEqual(data["chapters"][0]["num_entries"], 1)
            self.assertEqual(data["attachments"][0]["file_name"], "sample.txt")

    def desktop(self):
        try:
            import tkinter as tk

            from mkv_redux.ui import ReduxWindow
        except ImportError:
            self.skipTest("Tkinter not installed")
        try:
            root = tk.Tk()
        except tk.TclError:
            self.skipTest("No display available")

        def close_desktop():
            for callback in root.tk.splitlist(root.tk.call("after", "info")):
                root.after_cancel(callback)
            root.destroy()

        self.addCleanup(close_desktop)
        root.withdraw()
        with patch.object(ReduxWindow, "_load_tool", return_value=TOOL):
            app = ReduxWindow(root)

        def until_idle():
            deadline = time.monotonic() + 5
            while app.cancel is not None and time.monotonic() < deadline:
                root.update()
                time.sleep(0.01)
            self.assertIsNone(app.cancel, "The batch did not finish")

        return app, until_idle

    def test_desktop_workflow_previews_and_saves_to_selected_folder(self):
        if TOOL is None or not TOOL.is_file():
            self.skipTest("MKVToolNix not found")
        app, until_idle = self.desktop()
        with tempfile.TemporaryDirectory() as scratch:
            folder = Path(scratch)
            source = make_sample(TOOL, folder)
            exports = folder / "exports"
            exports.mkdir()
            self.assertTrue(app.create.instate(["disabled"]))
            with patch("mkv_redux.ui.filedialog.askopenfilenames", return_value=(str(source), str(source))):
                app.add.invoke()
            until_idle()
            self.assertEqual(len(app.jobs), 1)
            self.assertEqual(
                [app.track_tree.item(row, "values")[-1] for row in app.track_tree.get_children()],
                ["Keep", "Remove", "Keep", "Keep as-is"],
            )
            with patch("mkv_redux.ui.filedialog.askdirectory", return_value=str(exports)):
                app.choose.invoke()
            self.assertTrue(app.create.instate(["!disabled"]))
            app.create.invoke()
            until_idle()
            dest = output_path(source, exports)
            self.assertTrue(dest.is_file())
            self.assertEqual(app.selected_path.get(), str(dest))
            self.assertTrue(app.reveal.instate(["!disabled"]))
            self.assertTrue(app.create.instate(["disabled"]))

    def test_desktop_replaces_original_only_after_confirmation(self):
        if TOOL is None or not TOOL.is_file():
            self.skipTest("MKVToolNix not found")
        app, until_idle = self.desktop()
        with tempfile.TemporaryDirectory() as scratch:
            folder = Path(scratch)
            source = make_sample(TOOL, folder)
            original = source.read_bytes()
            self.assertFalse(app.replace_originals.get())
            with patch("mkv_redux.ui.filedialog.askopenfilenames", return_value=(str(source),)):
                app.add.invoke()
            until_idle()
            exports = folder / "exports"
            exports.mkdir()
            with patch("mkv_redux.ui.filedialog.askdirectory", return_value=str(exports)):
                app.choose.invoke()

            app.replace_toggle.invoke()
            self.assertTrue(app.choose.instate(["disabled"]))
            self.assertEqual(app.create.cget("text"), "Replace originals")
            with patch("mkv_redux.ui.messagebox.askyesno", return_value=False) as confirm:
                app.create.invoke()
                self.assertEqual(confirm.call_count, 1)
                self.assertEqual(confirm.call_args.kwargs["default"], "no")
            self.assertIsNone(app.cancel)
            self.assertEqual(source.read_bytes(), original)

            with patch("mkv_redux.ui.messagebox.askyesno", return_value=True):
                app.create.invoke()
            until_idle()
            (job,) = app.jobs.values()
            self.assertEqual(job.status, "Replaced")
            self.assertEqual(job.output, source)
            self.assertNotEqual(source.read_bytes(), original)
            self.assertEqual(list(folder.glob("*.mkv")), [source])
            self.assertFalse(list(exports.iterdir()))
            self.assertFalse(list(folder.glob(".mkv-redux-*")))
            self.assertEqual(
                [track.tag for track in inspect_file(TOOL, source, Cancellation()).audio], ["en-US", "en-GB", "und"]
            )

    def test_desktop_adds_nested_mkvs_and_avoids_duplicate_queue_entries(self):
        if TOOL is None or not TOOL.is_file():
            self.skipTest("MKVToolNix not found")
        app, until_idle = self.desktop()
        with tempfile.TemporaryDirectory() as scratch:
            folder = Path(scratch)
            source = make_sample(TOOL, folder)
            nested = folder / "Show" / "Season 01" / source.with_suffix(".MKV").name
            nested.parent.mkdir(parents=True)
            shutil.copyfile(source, nested)
            empty = folder / "Empty"
            empty.mkdir()

            with patch("mkv_redux.ui.filedialog.askdirectory", return_value=str(folder)):
                app.add_folder.invoke()
                app.stop.invoke()
                until_idle()
                self.assertFalse(app.jobs)
                self.assertEqual(app.activity.get(), "Folder scan canceled.")
            with patch("mkv_redux.ui.filedialog.askopenfilenames", return_value=(str(source),)):
                app.add.invoke()
            until_idle()
            with patch("mkv_redux.ui.filedialog.askdirectory", return_value=str(folder)):
                app.add_folder.invoke()
                self.assertTrue(app.add_folder.instate(["disabled"]))
                until_idle()
                self.assertEqual({job.path for job in app.jobs.values()}, {source, nested})
                self.assertTrue(all(job.ready for job in app.jobs.values()))
                self.assertEqual(app.selected_path.get(), str(nested))
                app.add_folder.invoke()
                until_idle()
                self.assertEqual(len(app.jobs), 2)
                self.assertEqual(app.activity.get(), "No new .mkv files were added.")
            with patch("mkv_redux.ui.filedialog.askdirectory", return_value=str(empty)):
                app.add_folder.invoke()
                until_idle()
                self.assertEqual(app.activity.get(), "No .mkv files found.")
            app.create.invoke()
            until_idle()
            self.assertTrue(output_path(source).is_file())
            self.assertTrue(output_path(nested).is_file())
