"""PyInstaller entry point — a thin wrapper so the frozen binary is `codegraph`."""

import sys

from codegraph.cli import main

if __name__ == "__main__":
    sys.exit(main())
