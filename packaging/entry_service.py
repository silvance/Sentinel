"""PyInstaller entry script for ``SentinelService.exe`` (Windows only; integration of the
single-executable and windows-service branches, docs/INTEGRATION.md).

The Service Control Manager starts ``"SentinelService.exe" host --config "<cfg>"`` (the binPath
written by ``sentinel.winsvc.commands.host_command``). The other sub-commands of
``python -m sentinel.winsvc`` (install, uninstall, status, set-camera-credentials) are available
through this executable too, for the technician in an elevated Command Prompt. With no
sub-command, ``host`` is assumed (``SentinelService.exe --config <cfg>``).
"""

import sys

from sentinel.winsvc.__main__ import main

SUBCOMMANDS = ("install", "uninstall", "status", "set-camera-credentials", "host", "-h", "--help")

if __name__ == "__main__":
    argv = sys.argv[1:]
    if not argv or argv[0] not in SUBCOMMANDS:
        argv = ["host", *argv]
    sys.exit(main(argv))
