#!/bin/sh
# Build the Sentinel one-folder program on Linux.
#
#   packaging/linux/build.sh --ffmpeg-dir /path/to/folder/with/ffmpeg-and-ffprobe [--allow-dirty]
#
# Requirements on the build machine: python3 (>= 3.12) with the venv module, git, and the
# FFmpeg/FFprobe executables to bundle (statically linked builds are simplest: a dynamically
# linked distribution ffmpeg would need its shared libraries bundled too, which this recipe
# does NOT do - see docs/08_PACKAGING.md). Build on the OLDEST distribution you must support:
# PyInstaller output needs that glibc version or newer.
#
# Output: dist/Sentinel/ (copy the whole folder; start ./sentinel or run ./install-desktop-entry.sh)
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
python=${PYTHON:-python3}
venv="$root/.venv-build-linux"
if [ ! -x "$venv/bin/python" ]; then
    "$python" -m venv "$venv"
fi
"$venv/bin/python" -m pip install --disable-pip-version-check --require-hashes --no-deps \
    -r "$root/requirements/build.txt"
exec "$venv/bin/python" "$root/packaging/build.py" "$@"
