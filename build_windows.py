"""Build the self-contained Windows x64 executable."""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import sysconfig
from importlib.metadata import PackageNotFoundError, distribution
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / "build" / "windows"
MKVTOOLNIX_VERSION = "104.0"
ARCHIVE_NAME = f"mkvtoolnix-64-bit-{MKVTOOLNIX_VERSION}.7z"
ARCHIVE_URL = f"https://mkvtoolnix.download/windows/releases/{MKVTOOLNIX_VERSION}/{ARCHIVE_NAME}"
ARCHIVE_SHA256 = "4d14b615819d2e106893d68114e45f7bbe8092dcb4c7c3599a0c0a52313321a9"


def verify_archive(path: Path) -> None:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != ARCHIVE_SHA256:
        raise SystemExit(f"MKVToolNix SHA-256 check failed for {path}. Remove it and retry.")


def prepare_archive(stage: Path) -> Path:
    cached = BUILD / ARCHIVE_NAME
    if cached.is_file():
        verify_archive(cached)
        return cached

    print(f"Downloading MKVToolNix {MKVTOOLNIX_VERSION} for the build…", flush=True)
    download = stage / ARCHIVE_NAME
    with urlopen(ARCHIVE_URL, timeout=60) as response, download.open("wb") as file:
        shutil.copyfileobj(response, file)
    verify_archive(download)
    download.replace(cached)
    return cached


def pyinstaller_license() -> Path:
    try:
        installed = distribution("PyInstaller")
    except PackageNotFoundError:
        raise SystemExit("Install PyInstaller in your build environment first; see README.md.") from None

    for entry in installed.files or ():
        if entry.name.casefold() == "copying.txt":
            path = Path(entry.locate())
            if path.is_file():
                return path
    raise SystemExit(f"PyInstaller {installed.version} is missing COPYING.txt. Reinstall PyInstaller.")


def main() -> None:
    if sysconfig.get_platform() != "win-amd64":
        raise SystemExit("Build MKV Redux on Windows using x64 Python.")

    pyi_license = pyinstaller_license()
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        raise SystemExit(f"Missing {python_license}. Use the official Windows CPython distribution.")

    tar = Path(os.environ["SystemRoot"]) / "System32" / "tar.exe"
    if not tar.is_file():
        raise SystemExit("Windows tar.exe is required to extract the MKVToolNix archive when building.")
    BUILD.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="stage-", dir=BUILD) as directory:
        stage = Path(directory)
        archive = prepare_archive(stage)
        subprocess.run(
            [
                str(tar),
                "-xf",
                str(archive),
                "-C",
                str(stage),
                "mkvtoolnix/mkvmerge.exe",
                "mkvtoolnix/doc/COPYING.txt",
                "mkvtoolnix/doc/README.txt",
                "mkvtoolnix/doc/licenses/",
            ],
            check=True,
        )
        upstream = stage / "mkvtoolnix"
        engine = upstream / "mkvmerge.exe"
        notices = upstream / "doc" / "licenses"
        shutil.copy2(upstream / "doc" / "COPYING.txt", notices / "MKVToolNix-COPYING.txt")
        shutil.copy2(upstream / "doc" / "README.txt", notices / "MKVToolNix-README.txt")
        shutil.copy2(ROOT / "LICENSE", notices / "MKV-Redux-LICENSE.txt")
        shutil.copy2(ROOT / "THIRD_PARTY_NOTICES.txt", notices)
        # The Windows CPython license includes notices for its bundled native dependencies.
        shutil.copy2(python_license, notices / "CPython-LICENSE.txt")
        shutil.copy2(pyi_license, notices / "PyInstaller-COPYING.txt")
        shutil.copytree(ROOT / "third_party_licenses", notices, dirs_exist_ok=True)

        subprocess.run([str(engine), "--version"], check=True)
        # Ship mkvmerge unmodified as a standalone executable, not a linked library.
        subprocess.run(
            [
                sys.executable,
                "-m",
                "PyInstaller",
                "--noconfirm",
                "--noupx",
                "--onefile",
                "--windowed",
                "--name",
                "MKV Redux",
                "--distpath",
                str(ROOT / "dist"),
                "--workpath",
                str(BUILD / "pyi"),
                "--specpath",
                str(BUILD),
                "--add-data",
                f"{engine}:mkv_redux/tools",
                "--add-data",
                f"{notices}:mkv_redux/tools/licenses",
                str(ROOT / "mkv_redux_gui.pyw"),
            ],
            cwd=ROOT,
            check=True,
        )


if __name__ == "__main__":
    main()
