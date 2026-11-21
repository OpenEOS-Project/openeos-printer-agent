"""PyInstaller entry point for the desktop window (openeos-printer-agent-ui.spec)."""

import sys

from src.ui.app import main

if __name__ == "__main__":
    sys.exit(main())
