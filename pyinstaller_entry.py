"""PyInstaller entry point (see openeos-printer-agent.spec).

A plain script rather than ``src/__main__.py`` because the bundled script
runs as ``__main__`` without a parent package, so relative imports fail.
"""

from src.cli import main

if __name__ == "__main__":
    main()
