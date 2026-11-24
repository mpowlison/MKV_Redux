# Windows release sources

The [v1.0.0 release](https://github.com/mpowlison/MKV_Redux/releases/tag/v1.0.0) has the portable Windows x64 executable and `Windows-Corresponding-Source-v1.0.0.zip`. The source archive contains the tagged MKV Redux source and build script, the upstream source archives, the MXE toolchain recipes and patches, and `upstream-sources.json` with filenames, origins, and SHA-256 hashes. The project license and the third-party notices are also available under **Licenses…** in the app.

## Rebuild MKV Redux

Use the included `build_windows.py` and the instructions in [README.md](README.md). The published build used the official x64 Windows release of CPython 3.14.7 (Tcl/Tk 9.0.4) and PyInstaller 6.22.3. The script downloads and checks the pinned MKVToolNix portable archive and bundles its unmodified `mkvmerge.exe`. MKV Redux starts that executable as a separate program. It does not link to it.

## Build the remux engine from source

- `mkvtoolnix-104.0.tar.xz` is the upstream release source for [MKVToolNix 104.0](https://codeberg.org/mbunkus/mkvtoolnix/src/tag/release-104.0), including vendored components, `configure`, `Rakefile`, and `packaging/windows/`.
- The official Windows x64 `mkvmerge.exe` has SHA-256 `e95ad4c93fc8a070ba7e0f2da070c65b2dd98f9660569f3edea717416fb34c4e`. Its embedded GCC 16.2.0 paths identify the maintainer's `master-20261101-a6738d9d9-qt-12-0` MXE tree. The archive includes that tree at [commit `122c9fd6`](https://codeberg.org/mbunkus/mxe/src/commit/122c9fd6c09ed837d9b307d74ca9ed4e0d8ca490) and the dependency source releases pinned by its recipes, including Qt Base 6.12.0 and the GCC and MinGW-w64 runtime sources.
- On Linux, unpack the archives, use the pinned MXE tree and its `build.sh --amd64` for the `x86_64-w64-mingw32.static` target, and put the resulting `usr/bin` on `PATH`. Set up MXE as described in its `index.html` (the local source cache is `pkg/`). The `build.sh` helper expects to run from an MXE Git checkout; to use the included snapshot, initialize Git in the unpacked MXE directory first. Then, from the MKVToolNix source directory, configure for that target with MXE's sysroot as the Boost path and run `rake apps:mkvmerge`:

  ```sh
  export PATH="/path/to/mxe/usr/bin:$PATH"
  ./configure --host=x86_64-w64-mingw32.static \
    --with-boost=/path/to/mxe/usr/x86_64-w64-mingw32.static \
    --enable-mkvtoolnix
  rake apps:mkvmerge
  ```

  See the included `Building.for.Windows.md` for required host tools and the cross-compilation process. Its GCC/Qt example predates the versions used for 104.0. The upstream [x64 Buildbot configure log](https://buildbot.mkvtoolnix.download/api/v2/logs/123627/raw) shows a nearby build using the same toolchain; it is a reference, not a byte-for-byte build record for the signed 104.0 release binary.

## Licenses

[MKV Redux is GPL-2.0-only](LICENSE). The [third-party notices](THIRD_PARTY_NOTICES.txt) identify the original licenses for the separately bundled programs and libraries. The source archive includes the upstream license files with their respective sources. Microsoft distributable runtime files are covered by the terms documented in the CPython Windows notice.
