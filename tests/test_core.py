import io
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from mkv_redux.core import (
    MKVMERGE_NAME,
    Cancellation,
    Cancelled,
    Decision,
    Language,
    ReduxError,
    _stamp,
    discover_mkv_files,
    find_mkvmerge,
    output_path,
    plan_from_json,
    remux_file,
)


def audio(track_id, *, legacy=None, ietf=None):
    return {
        "type": "audio",
        "id": track_id,
        "codec": "AAC",
        "properties": {"language": legacy, "language_ietf": ietf, "track_name": "Main"},
    }


def payload(*tracks):
    return {"container": {"type": "Matroska", "supported": True}, "tracks": list(tracks)}


class EngineLookupTests(unittest.TestCase):
    def test_portable_build_uses_its_own_engine_or_reports_it_missing(self):
        with tempfile.TemporaryDirectory() as scratch:
            package = Path(scratch) / "mkv_redux"
            engine = package / "tools" / MKVMERGE_NAME
            engine.parent.mkdir(parents=True)
            engine.touch()
            with (
                patch.object(sys, "frozen", True, create=True),
                patch("mkv_redux.core.__file__", str(package / "core.py")),
                patch("mkv_redux.core.shutil.which") as which,
            ):
                self.assertEqual(find_mkvmerge(), engine.resolve())
                engine.unlink()
                self.assertIsNone(find_mkvmerge())
            which.assert_not_called()

    def test_source_launch_can_still_use_path(self):
        with (
            patch.object(sys, "frozen", False, create=True),
            patch.dict("mkv_redux.core.os.environ", {"ProgramFiles": "", "ProgramFiles(x86)": ""}),
            patch("mkv_redux.core.shutil.which", return_value=MKVMERGE_NAME) as which,
        ):
            self.assertEqual(find_mkvmerge(), Path(MKVMERGE_NAME).absolute())
        which.assert_called_once_with(MKVMERGE_NAME)


class FolderScanTests(unittest.TestCase):
    def test_finds_mkvs_at_every_depth_regardless_of_extension_case(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            nested = root / "Seasons.mkv" / "Season 01"
            nested.mkdir(parents=True)
            expected = {root / "Episode.mkv", nested / "Episode.MKV"}
            for path in expected | {root / "notes.txt", nested / "Episode.mkv.bak"}:
                path.touch()
            paths, unreadable = discover_mkv_files(root, Cancellation())
            self.assertEqual(set(paths), expected)
            self.assertEqual(unreadable, 0)

    def test_skips_linked_subfolders(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch) / "selected"
            other = root.with_name("outside")
            root.mkdir()
            other.mkdir()
            (other / "Outside.mkv").touch()
            link = root / "link"
            if os.name == "nt":
                result = subprocess.run(
                    ["cmd.exe", "/c", "mklink", "/J", str(link), str(other)],
                    check=False,
                    capture_output=True,
                    timeout=5,
                )
                if result.returncode:
                    self.skipTest("Could not create a Windows junction")
            else:
                link.symlink_to(other, target_is_directory=True)
            self.assertEqual(discover_mkv_files(root, Cancellation()), ([], 0))

    def test_reports_unreadable_entries_and_continues_scanning(self):
        with tempfile.TemporaryDirectory() as scratch:
            root = Path(scratch)
            blocked = root / "blocked"
            blocked.mkdir()
            video = root / "Found.mkv"
            video.touch()
            scandir = os.scandir

            def scan(path):
                if path == blocked:
                    raise PermissionError("Access denied")
                return scandir(path)

            with patch("mkv_redux.core.os.scandir", side_effect=scan):
                self.assertEqual(discover_mkv_files(root, Cancellation()), ([video], 1))
                with self.assertRaises(PermissionError):
                    discover_mkv_files(blocked, Cancellation())

    def test_stops_before_scanning_when_canceled(self):
        cancel = Cancellation()
        cancel.cancel()
        with patch("mkv_redux.core.os.scandir") as scan, self.assertRaises(Cancelled):
            discover_mkv_files(Path.cwd(), cancel)
        scan.assert_not_called()


class InspectionTests(unittest.TestCase):
    def plan(self, *tracks):
        return plan_from_json(Path("Movie.MKV"), (0, 0, 0, 0), payload(*tracks))

    def test_keeps_all_english_tracks_and_prefers_ietf(self):
        plan = self.plan(
            {"type": "video", "id": 0},
            audio(1, legacy="eng"),
            audio(3, legacy="jpn", ietf="EN-gb"),
            audio(5, legacy="eng", ietf="fr-CA"),
            {"type": "subtitles", "id": 6, "properties": {"language": "jpn"}},
        )
        self.assertEqual(plan.english_ids, (1, 3))
        self.assertEqual(
            [t.language for t in plan.audio], [Language.ENGLISH, Language.ENGLISH, Language.OTHER]
        )
        self.assertEqual(plan.decision, Decision.READY)

    def test_leaves_file_alone_if_any_audio_language_is_unclear(self):
        for tag in (None, "", "mul", "mis", "zxx", "qaa", "x-custom", "not a tag"):
            with self.subTest(tag=tag):
                plan = self.plan(audio(0, legacy="eng"), audio(1, legacy="spa"), audio(2, ietf=tag))
                self.assertEqual(plan.decision, Decision.REVIEW)
                self.assertEqual(plan.audio[-1].language, Language.UNKNOWN)

    def test_keeps_und_tracks_but_still_removes_known_non_english_tracks(self):
        plan = self.plan(
            audio(0, legacy="eng"),
            audio(1, legacy="eng", ietf="und"),
            audio(2, legacy="UND"),
            audio(3, ietf="und-Latn"),
            audio(4, ietf="es"),
        )
        self.assertEqual(plan.english_ids, (0,))
        self.assertEqual(plan.kept_audio_ids, (0, 1, 2, 3))
        self.assertEqual(plan.decision, Decision.READY)
        self.assertIn("3 tagged und", plan.summary)
        self.assertIn("remove 1", plan.summary)

    def test_und_does_not_count_as_english_or_require_a_remux(self):
        undetermined = audio(0, legacy="eng", ietf="und")
        self.assertEqual(self.plan(undetermined).decision, Decision.REVIEW)
        self.assertEqual(self.plan(undetermined, audio(1, ietf="es")).decision, Decision.REVIEW)
        self.assertEqual(self.plan(undetermined, audio(1, ietf="en-US")).decision, Decision.UNCHANGED)

    def test_no_english_no_audio_and_english_only_are_not_remuxed(self):
        self.assertEqual(self.plan(audio(2, legacy="spa")).decision, Decision.REVIEW)
        self.assertEqual(self.plan().decision, Decision.UNCHANGED)
        self.assertEqual(
            self.plan(audio(2, ietf="en-US"), audio(3, legacy="ENG")).decision, Decision.UNCHANGED
        )

    def test_invalid_identification_is_reported(self):
        for data in (payload(audio(1), audio(1)), payload(audio(True)), {"container": {"type": "MP4"}}, []):
            with self.subTest(data=data), self.assertRaises(ReduxError):
                plan_from_json(Path("Movie.MKV"), (0, 0, 0, 0), data)
        with self.assertRaisesRegex(ReduxError, "Invalid input"):
            plan_from_json(Path("Movie.MKV"), (0, 0, 0, 0), {"errors": ["Invalid input"]})

    def test_output_name_preserves_stem_and_uses_chosen_folder(self):
        source = Path("movies", "My Film.2025.MKV")
        self.assertEqual(output_path(source), Path("movies", "My Film.2025.english.mkv"))
        self.assertEqual(output_path(source, Path("exports")), Path("exports", "My Film.2025.english.mkv"))


class RemuxTests(unittest.TestCase):
    def setUp(self):
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.folder = Path(scratch.name)
        self.source = self.folder / "Source [test].mkv"
        self.source.write_bytes(b"original")
        self.dest = output_path(self.source)
        self.plan = plan_from_json(
            self.source, _stamp(self.source), payload(audio(3, ietf="en-US"), audio(7, ietf="de"))
        )
        self.tool = Path("mkvmerge")

    def fake_process(self, *, code=0, log="#GUI#progress 23%\n#GUI#progress 100%\n", extra=None):
        @contextmanager
        def start(command, cancel):
            self.assertEqual(command[0], str(self.tool))
            self.assertEqual(command[-3:], ["--audio-tracks", "3", str(self.source)])
            temporary = Path(command[command.index("-o") + 1])
            self.assertEqual(temporary.parent.parent, self.dest.parent)
            temporary.write_bytes(b"output")
            if extra:
                extra()

            class Child:
                stdout = io.StringIO(log)

                def wait(self):
                    return code

            yield Child()

        return patch("mkv_redux.core._start", start)

    def assert_original_and_no_temporary_files(self):
        self.assertEqual(self.source.read_bytes(), b"original")
        self.assertFalse(list(self.folder.glob(".mkv-redux-*")))

    def test_writes_copy_and_reports_progress(self):
        progress = []
        with self.fake_process():
            self.assertEqual(remux_file(self.tool, self.plan, self.dest, Cancellation(), progress.append), "")
        self.assertEqual(progress, [23, 100])
        self.assertEqual(self.dest.read_bytes(), b"output")
        self.assert_original_and_no_temporary_files()

    def test_existing_copy_is_not_overwritten_or_remuxed(self):
        self.dest.write_bytes(b"existing")
        with patch("mkv_redux.core._start") as start, self.assertRaisesRegex(ReduxError, "already exists"):
            remux_file(self.tool, self.plan, self.dest, Cancellation(), lambda _value: None)
        start.assert_not_called()
        self.assertEqual(self.dest.read_bytes(), b"existing")
        self.assert_original_and_no_temporary_files()

    def test_concurrent_destination_is_not_overwritten(self):
        with (
            self.fake_process(extra=lambda: self.dest.write_bytes(b"existing")),
            self.assertRaisesRegex(ReduxError, "already exists"),
        ):
            remux_file(self.tool, self.plan, self.dest, Cancellation(), lambda _value: None)
        self.assertEqual(self.dest.read_bytes(), b"existing")
        self.assert_original_and_no_temporary_files()

    def test_error_and_cancel_discard_partial_output(self):
        with (
            self.fake_process(code=2, log="Error: disk full\n"),
            self.assertRaisesRegex(ReduxError, "disk full"),
        ):
            remux_file(self.tool, self.plan, self.dest, Cancellation(), lambda _value: None)
        self.assertFalse(self.dest.exists())
        self.assert_original_and_no_temporary_files()
        cancel = Cancellation()
        with self.fake_process(), self.assertRaises(Cancelled):
            remux_file(self.tool, self.plan, self.dest, cancel, lambda _value: cancel.cancel())
        self.assertFalse(self.dest.exists())
        self.assert_original_and_no_temporary_files()

    def test_warnings_are_reported(self):
        with self.fake_process(code=1, log="#GUI#warning Check timestamps\n"):
            warning = remux_file(self.tool, self.plan, self.dest, Cancellation(), lambda _value: None)
        self.assertIn("Check timestamps", warning)
        self.assertTrue(self.dest.is_file())
        self.assert_original_and_no_temporary_files()

    def test_requires_rescan_when_input_has_changed(self):
        self.source.write_bytes(b"different file")
        with patch("mkv_redux.core._start") as start, self.assertRaisesRegex(ReduxError, "Rescan"):
            remux_file(self.tool, self.plan, self.dest, Cancellation(), lambda _value: None)
        start.assert_not_called()
        self.assertFalse(self.dest.exists())

    def replace_source(self, cancel=None, on_progress=lambda _value: None):
        return remux_file(
            self.tool, self.plan, self.source, cancel or Cancellation(), on_progress, replace_original=True
        )

    def test_replaces_original_only_after_remux_and_creates_no_copy(self):
        def original_during_remux(_value):
            self.assertEqual(self.source.read_bytes(), b"original")

        with self.fake_process():
            self.assertEqual(self.replace_source(on_progress=original_during_remux), "")
        self.assertEqual(self.source.read_bytes(), b"output")
        self.assertEqual(list(self.folder.iterdir()), [self.source])

    def test_replacement_with_warning_leaves_original(self):
        for code in (0, 1):
            with (
                self.subTest(code=code),
                self.fake_process(code=code, log="#GUI#warning Check timestamps\n"),
                self.assertRaisesRegex(ReduxError, "Check timestamps"),
            ):
                self.replace_source()
            self.assert_original_and_no_temporary_files()
            self.assertFalse(self.dest.exists())

    def test_failed_or_canceled_replacement_leaves_original(self):
        with self.fake_process(code=2, log="Error: disk full\n"), self.assertRaisesRegex(ReduxError, "disk full"):
            self.replace_source()
        cancel = Cancellation()
        with self.fake_process(), self.assertRaises(Cancelled):
            self.replace_source(cancel, lambda _value: cancel.cancel())
        self.assert_original_and_no_temporary_files()
        self.assertFalse(self.dest.exists())

    def test_replacement_with_locked_original_leaves_it_in_place(self):
        with (
            self.fake_process(),
            patch.object(Path, "replace", side_effect=PermissionError("Locked")),
            self.assertRaisesRegex(ReduxError, "Could not replace the original"),
        ):
            self.replace_source()
        self.assert_original_and_no_temporary_files()

    def test_replacement_cannot_overwrite_a_source_that_changed_during_remux(self):
        different = b"different file"
        with (
            self.fake_process(extra=lambda: self.source.write_bytes(different)),
            self.assertRaisesRegex(ReduxError, "Rescan"),
        ):
            self.replace_source()
        self.assertEqual(self.source.read_bytes(), different)
        self.assertEqual(list(self.folder.iterdir()), [self.source])

    def test_replacement_cannot_overwrite_an_unrelated_destination(self):
        self.dest.write_bytes(b"existing")
        with patch("mkv_redux.core._start") as start, self.assertRaisesRegex(ReduxError, "original file's path"):
            remux_file(self.tool, self.plan, self.dest, Cancellation(), lambda _value: None, replace_original=True)
        start.assert_not_called()
        self.assertEqual(self.dest.read_bytes(), b"existing")
        self.assert_original_and_no_temporary_files()
