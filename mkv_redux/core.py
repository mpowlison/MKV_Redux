"""Inspect Matroska language tags and make a lossless filtered copy."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

MKVMERGE_NAME = "mkvmerge.exe" if os.name == "nt" else "mkvmerge"
_TAG = re.compile(r"[a-z]{2,3}(?:-[a-z0-9]{1,8})*")
_PROGRESS = re.compile(r"(?:#GUI#progress\s+|Progress:\s*)(\d{1,3})%")


class ReduxError(Exception):
    """A file could not be safely inspected or saved."""


class Cancelled(Exception):
    """The user stopped the current batch."""


class Language(Enum):
    ENGLISH = "English"
    UNDETERMINED = "Undetermined"
    OTHER = "Other"
    UNKNOWN = "Unknown"


class Decision(Enum):
    READY = "Ready"
    UNCHANGED = "No changes"
    REVIEW = "Needs review"


@dataclass(frozen=True)
class AudioTrack:
    id: int
    tag: str
    language: Language
    name: str
    codec: str


FileStamp = tuple[int, int, int, int]


@dataclass(frozen=True)
class Plan:
    source: Path
    stamp: FileStamp
    audio: tuple[AudioTrack, ...]

    @property
    def english_ids(self) -> tuple[int, ...]:
        return tuple(track.id for track in self.audio if track.language == Language.ENGLISH)

    @property
    def kept_audio_ids(self) -> tuple[int, ...]:
        return tuple(
            track.id for track in self.audio if track.language in (Language.ENGLISH, Language.UNDETERMINED)
        )

    @property
    def decision(self) -> Decision:
        if not self.audio:
            return Decision.UNCHANGED
        if any(track.language == Language.UNKNOWN for track in self.audio) or not self.english_ids:
            return Decision.REVIEW
        if len(self.kept_audio_ids) == len(self.audio):
            return Decision.UNCHANGED
        return Decision.READY

    @property
    def summary(self) -> str:
        if not self.audio:
            return "No audio tracks. No copy is needed."
        if any(track.language == Language.UNKNOWN for track in self.audio):
            return "At least one audio language is unknown or ambiguous. This file will be skipped."
        english = len(self.english_ids)
        if not english:
            return "No audio tracks are tagged English. This file will be skipped."
        kept = len(self.kept_audio_ids)
        undetermined = kept - english
        removed = len(self.audio) - kept
        if not removed:
            if undetermined:
                return "Only English and undetermined audio tracks. No changes needed."
            return "All audio tracks are tagged English. No changes needed."
        extra = f" + {undetermined} tagged und" if undetermined else ""
        return f"Keep {english} English{extra} · remove {removed} non-English audio."


class Cancellation:
    """Share a stop request between the UI and the worker owning mkvmerge."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._child: subprocess.Popen[str] | None = None

    @property
    def requested(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self.requested:
            raise Cancelled

    def cancel(self) -> None:
        self._event.set()
        with self._lock:
            if self._child is not None:
                _kill(self._child)

    @contextmanager
    def watch(self, child: subprocess.Popen[str]) -> Iterator[None]:
        with self._lock:
            self._child = child
        try:
            self.check()
            yield
        finally:
            with self._lock:
                self._child = None


def _kill(child: subprocess.Popen[str]) -> None:
    if child.poll() is None:
        child.kill()


@contextmanager
def _start(command: list[str], cancel: Cancellation) -> Iterator[subprocess.Popen[str]]:
    cancel.check()
    with subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        encoding="utf-8-sig",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
    ) as child:
        try:
            with cancel.watch(child):
                yield child
        finally:
            _kill(child)


def find_mkvmerge() -> Path | None:
    if getattr(sys, "frozen", False):
        # A portable build must use its own engine, never a PATH or installed copy.
        bundled = Path(__file__).resolve().with_name("tools") / MKVMERGE_NAME
        return bundled if bundled.is_file() else None
    if os.name == "nt":
        for key in ("ProgramFiles", "ProgramFiles(x86)"):
            if base := os.environ.get(key):
                candidate = Path(base) / "MKVToolNix" / MKVMERGE_NAME
                if candidate.is_file():
                    return candidate
    if executable := shutil.which(MKVMERGE_NAME):
        return Path(executable).absolute()
    return None


def discover_mkv_files(folder: Path, cancel: Cancellation) -> tuple[list[Path], int]:
    """Return MKVs and a count of unreadable entries without following directory links."""
    root = folder.absolute()
    pending = [root]
    found = []
    unreadable = 0
    while pending:
        cancel.check()
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    cancel.check()
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if os.name == "nt" and (
                                entry.stat(follow_symlinks=False).st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT
                            ):
                                continue  # Windows junctions can point outside the selected tree or form loops.
                            pending.append(Path(entry.path))
                        elif entry.name.casefold().endswith(".mkv") and entry.is_file(follow_symlinks=False):
                            found.append(Path(entry.path))
                    except OSError:
                        unreadable += 1
        except OSError:
            if directory == root:
                raise
            unreadable += 1
    cancel.check()
    found.sort()
    return found, unreadable


def _stamp(path: Path) -> FileStamp:
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def _check_source(source: Path, stamp: FileStamp, *, replace_original: bool = False) -> None:
    if replace_original and source.is_symlink():
        raise ReduxError("Cannot replace a symbolic link. Add the actual video file or create a copy instead.")
    if _stamp(source) != stamp:
        raise ReduxError("The source file changed. Rescan the file before creating a copy.")


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _one_line(text: str) -> str:
    return " ".join(text.split())[:500]


def _language(tag: str) -> Language:
    normalized = tag.casefold()
    primary = normalized.partition("-")[0]
    if (
        not _TAG.fullmatch(normalized)
        or primary in {"mis", "mul", "zxx"}
        or (len(primary) == 3 and "qaa" <= primary <= "qtz")
    ):
        return Language.UNKNOWN
    if primary == "und":
        return Language.UNDETERMINED
    return Language.ENGLISH if primary in {"en", "eng"} else Language.OTHER


def plan_from_json(source: Path, stamp: FileStamp, data: object) -> Plan:
    if not isinstance(data, dict):
        raise ReduxError("MKVToolNix returned invalid track information.")
    if errors := data.get("errors"):
        message = " ".join(map(str, errors)) if isinstance(errors, list) else str(errors)
        raise ReduxError(_one_line(message))
    container = data.get("container")
    if (
        not isinstance(container, dict)
        or container.get("type") != "Matroska"
        or not container.get("supported")
    ):
        raise ReduxError("This is not a supported Matroska (.mkv) file.")
    tracks = data.get("tracks")
    if not isinstance(tracks, list):
        raise ReduxError("MKVToolNix did not return a track list.")

    audio = []
    seen = set()
    for track in tracks:
        if not isinstance(track, dict) or not isinstance(track.get("type"), str):
            raise ReduxError("MKVToolNix returned an invalid track entry.")
        if track["type"] != "audio":
            continue
        track_id = track.get("id")
        if type(track_id) is not int or track_id < 0 or track_id in seen:
            raise ReduxError("MKVToolNix returned an invalid or duplicate audio track ID.")
        seen.add(track_id)
        properties = track.get("properties")
        if not isinstance(properties, dict):
            properties = {}
        tag = _text(properties.get("language_ietf")) or _text(properties.get("language"))
        name = _text(properties.get("track_name"))
        audio.append(AudioTrack(track_id, tag, _language(tag), name, _text(track.get("codec"))))
    return Plan(source, stamp, tuple(audio))


def inspect_file(tool: Path, source: Path, cancel: Cancellation) -> Plan:
    cancel.check()
    source = source.absolute()
    stamp = _stamp(source)
    with _start([str(tool), "-J", str(source)], cancel) as child:
        output, _ = child.communicate()
        exit_code = child.returncode
    cancel.check()
    try:
        data = json.loads(output)
    except json.JSONDecodeError as exc:
        raise ReduxError(_one_line(output) or "MKVToolNix could not read this file.") from exc
    plan = plan_from_json(source, stamp, data)
    if exit_code not in (0, 1):
        raise ReduxError(f"MKVToolNix could not read this file (exit code {exit_code}).")
    _check_source(source, stamp)
    return plan


def output_path(source: Path, folder: Path | None = None) -> Path:
    return (folder if folder is not None else source.parent) / source.with_suffix(".english.mkv").name


def _publish(temporary: Path, destination: Path) -> None:
    # Path.replace would let a competing process's output get overwritten.
    if os.name == "nt":
        temporary.rename(destination)
    else:
        os.link(temporary, destination)


def remux_file(
    tool: Path,
    plan: Plan,
    destination: Path,
    cancel: Cancellation,
    on_progress: Callable[[int], None],
    *,
    replace_original: bool = False,
) -> str:
    """Remux to a copy (returning any warning) or replace the source after a clean run."""
    cancel.check()
    if plan.decision != Decision.READY:
        raise ReduxError(plan.summary)
    if replace_original:
        if destination != plan.source:
            raise ReduxError("Replacement must use the original file's path.")
        destination = plan.source
    _check_source(plan.source, plan.stamp, replace_original=replace_original)
    existing_message = "A copy with this name already exists. Choose another folder to save it."
    if not replace_original and os.path.lexists(destination):
        raise ReduxError(existing_message)
    if not destination.parent.is_dir():
        raise ReduxError("The output folder is not available.")

    with tempfile.TemporaryDirectory(prefix=".mkv-redux-", dir=destination.parent) as scratch:
        temporary = Path(scratch) / "output.mkv"
        command = [str(tool)]
        if replace_original:
            command.append("--flush-on-close")
        command += [
            "--gui-mode",
            "--ui-language",
            "en",
            "--output-charset",
            "UTF-8",
            "-o",
            str(temporary),
            "--audio-tracks",
            ",".join(map(str, plan.kept_audio_ids)),
            str(plan.source),
        ]
        warning = error = last_line = ""
        percent = -1
        with _start(command, cancel) as child:
            assert child.stdout is not None
            for line in child.stdout:
                line = line.strip()
                if match := _PROGRESS.match(line):
                    value = min(100, int(match[1]))
                    if value > percent:
                        percent = value
                        on_progress(percent)
                elif line.startswith(("Warning:", "#GUI#warning")):
                    warning = _one_line(line.removeprefix("#GUI#warning").lstrip("# :"))
                elif line.startswith(("Error:", "#GUI#error")):
                    error = _one_line(line.removeprefix("#GUI#error").lstrip("# :"))
                elif line and not line.startswith("#GUI#"):
                    last_line = _one_line(line)
            exit_code = child.wait()
        cancel.check()
        if exit_code not in (0, 1):
            raise ReduxError(error or last_line or f"MKVToolNix failed (exit code {exit_code}).")
        if replace_original and (warning or exit_code == 1):
            raise ReduxError(f"MKVToolNix reported a warning; the original was not replaced. {warning}".rstrip())
        if not temporary.is_file() or not temporary.stat().st_size:
            raise ReduxError("MKVToolNix did not produce an output file.")
        _check_source(plan.source, plan.stamp, replace_original=replace_original)
        cancel.check()
        if replace_original:
            # Swap the completed file in on the same filesystem; never delete the source first.
            try:
                temporary.replace(destination)
            except PermissionError as exc:
                raise ReduxError("Could not replace the original. Close any video players and check write permissions.") from exc
        else:
            try:
                _publish(temporary, destination)
            except FileExistsError as exc:
                raise ReduxError(existing_message) from exc
    return warning or (
        "MKVToolNix reported a warning. Check the saved copy before using it." if exit_code == 1 else ""
    )
