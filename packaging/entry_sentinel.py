"""PyInstaller entry script: the built ``Sentinel`` executable runs the launcher CLI."""

import sys

from sentinel.launcher.cli import main

if __name__ == "__main__":
    sys.exit(main())
