# MKV Redux

A Windows desktop app for removing audio tagged in languages other than English from `.mkv` videos. It processes everything locally and uses MKVToolNix to copy streams without re-encoding.

## Get started

Download the **Windows x64 `.exe`** from the [latest GitHub release](https://github.com/mpowlison/MKV_Redux/releases/latest). The release also provides a SHA-256 checksum and a corresponding-source archive.

1. Double-click the downloaded `.exe` on Windows 10 or newer (x64). You can move or copy the executable on its own. It bundles Python, Tkinter, and the MKVToolNix remux engine. No installation or internet connection is required to run it.
2. Click **Add MKV files…** to select specific videos, or **Add folder…** to find `.mkv` files in a folder and all its subfolders. Files already in the queue aren't added again.
3. Select a row to review the audio tracks. Click **Create English copies**, or turn on **Replace originals** to keep the existing filenames instead.

Folder scans run in the background. They skip linked subfolders (including Windows junctions) so the scan stays within the selected folder. Unreadable entries are reported in the status bar. To pick up new files later, add the folder again; **Rescan** refreshes the audio preview for files already in the queue.

In the project folder, **`Start MKV Redux.cmd`** opens the portable build if present and otherwise tries to run from source.

### Output modes

- **Create English copies** (default): `Movie.mkv` produces `Movie.english.mkv` beside the original, or in the folder you choose. This mode never overwrites an existing file. If two copies would have the same name in the chosen folder, choose another folder and retry the one that wasn't saved.
- **Replace originals**: Keeps each file's original name and location. The app asks you to confirm before starting. It writes a temporary MKV beside the original and swaps it in only after a successful remux without warnings. On failure, the original is left in place. A completed replacement keeps no backup, so back up any videos you might want to restore.

Both modes need enough free space to write the filtered MKV before saving it. **Cancel** stops the current batch without undoing copies or replacements that already finished.

## What gets filtered

- All audio tagged English is kept, including regional tags such as `en-US` and `en-GB`, commentary, and alternate mixes. The app prefers the newer BCP 47 tag and falls back to the legacy language tag.
- Audio tagged `und` (undetermined) is kept as-is. It does not prevent removing other non-English tracks when the file also has English audio.
- Files with no audio or nothing to remove are not rewritten. Files with audio but no English track, or with other unknown or ambiguous tags (such as `mul`), are marked **Needs review** and left alone.
- Language tags aren't speech recognition. Incorrect tags can lead to incorrect selections, and Matroska can report an omitted legacy language as `eng` by default. Review the selection before creating copies. If you correct the tags with MKVToolNix, use **Rescan** to refresh the preview.
- Video, subtitles, chapters, and attachments remain selected. Container metadata such as muxing date and track statistics may be regenerated. If MKVToolNix reports a warning, check any saved copy before relying on it. The app will not replace an original that produces a remux warning.

## Speed

The app inspects metadata first and skips files that don't need a new copy. Each remaining file is remuxed in one pass, in a background worker so the interface stays responsive. On most systems the limiting factor is storage throughput: the retained video still needs to be written to a new file. Use an SSD with enough space for the copies. Jobs run one at a time to avoid competing disk writes.

## Development

To run from source, use [Python 3.10 or newer](https://www.python.org/downloads/windows/) with Tkinter and run `py -3 mkv_redux_gui.pyw`. This mode requires [MKVToolNix](https://mkvtoolnix.download/downloads.html#windows). If it's not found automatically, click **Locate mkvmerge.exe…** and select it in the MKVToolNix folder.

Run tests with `py -3 -m unittest discover -s tests -v`. Integration tests run when `mkvmerge` is available. Set `MKVMERGE` to the full path of an executable to test a specific version.

### Build the portable Windows executable

Use the official Windows x64 distribution of CPython 3.10+ with Tkinter and the Windows `tar.exe` utility:

```powershell
py -3 -m venv .venv
.venv\Scripts\python.exe -m pip install PyInstaller==6.22.3
.venv\Scripts\python.exe build_windows.py
```

The build downloads and verifies the pinned MKVToolNix archive, caches it under `build/windows/`, and embeds `mkvmerge.exe` into `dist/MKV Redux.exe`. The download is build-time only.

## Licensing

MKV Redux itself is licensed under the [GNU General Public License, version 2 only](LICENSE) (`GPL-2.0-only`). It runs the bundled MKVToolNix 104.0 `mkvmerge.exe` as a separate command-line process without linking to it. MKVToolNix is licensed upstream under GPLv2; its dependencies, CPython, Tcl/Tk, PyInstaller, and other third-party components retain their own license terms.

See [third-party notices](THIRD_PARTY_NOTICES.txt) and [Windows release sources](WINDOWS_SOURCES.md) for license and build details. The project license and bundled notices are available from **Licenses…** in the portable app.
