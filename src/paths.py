"""Platform-specific directories for data, logs and configuration.

The agent runs as a systemd service on Linux (Raspberry Pi included), as a
Windows service, or interactively on a developer machine. None of these
share a sensible default location, so everything that persists (device
token, job store, cached templates, log files) goes through this module.

Resolution order mirrors the rework spec (section 7):

* Linux:   ``/var/lib/openeos-printer`` when writable (service user / root),
           otherwise ``~/.local/share/openeos-printer``.
* Windows: ``%ProgramData%\\OpenEOS Printer Agent``.
* macOS:   ``~/Library/Application Support/openeos-printer`` (best effort).

``OPENEOS_PRINTER_DATA_DIR`` / ``OPENEOS_PRINTER_LOG_DIR`` override the
defaults, which is also how the tests keep the real directories untouched.
"""

import os
import sys
from pathlib import Path

import platformdirs

APP_NAME_UNIX = "openeos-printer"
APP_NAME_WINDOWS = "OpenEOS Printer Agent"

ENV_DATA_DIR = "OPENEOS_PRINTER_DATA_DIR"
ENV_LOG_DIR = "OPENEOS_PRINTER_LOG_DIR"


def is_windows() -> bool:
    return sys.platform.startswith("win")


def is_frozen() -> bool:
    """True when running from a PyInstaller bundle."""
    return bool(getattr(sys, "frozen", False))


def _writable(path: Path) -> bool:
    """Can we create files below ``path``? Creates the directory if needed."""
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return os.access(path, os.W_OK)


def _windows_program_data() -> Path:
    base = os.environ.get("ProgramData") or os.environ.get("ALLUSERSPROFILE") or r"C:\ProgramData"
    return Path(base) / APP_NAME_WINDOWS


def default_data_dir() -> Path:
    """Directory for the device token, job store and cached templates."""
    override = os.environ.get(ENV_DATA_DIR)
    if override:
        return Path(override).expanduser()

    if is_windows():
        return _windows_program_data()

    if sys.platform == "darwin":
        return Path(platformdirs.user_data_dir(APP_NAME_UNIX))

    system_dir = Path("/var/lib") / APP_NAME_UNIX
    if _writable(system_dir):
        return system_dir
    return Path(platformdirs.user_data_dir(APP_NAME_UNIX))


def default_log_dir() -> Path:
    """Directory for rotating log files."""
    override = os.environ.get(ENV_LOG_DIR)
    if override:
        return Path(override).expanduser()

    if is_windows():
        return _windows_program_data() / "logs"

    if sys.platform == "darwin":
        return Path(platformdirs.user_log_dir(APP_NAME_UNIX))

    system_dir = Path("/var/log") / APP_NAME_UNIX
    if _writable(system_dir):
        return system_dir
    return default_data_dir() / "logs"


def default_config_search_paths() -> list[Path]:
    """Where ``load_config()`` looks for config.yaml when no path is given.

    The data directory comes first so a config written by ``install-service``
    (Windows) or ``install.sh`` (Linux) wins over whatever happens to lie in
    the current working directory.
    """
    paths = [default_data_dir() / "config.yaml", Path("config") / "config.yaml"]
    if not is_windows():
        paths.append(Path("/etc") / APP_NAME_UNIX / "config.yaml")
        paths.append(Path.home() / ".config" / APP_NAME_UNIX / "config.yaml")
    return paths


def resource_dir() -> Path:
    """Root of bundled resources (``src/`` in a checkout, the unpack dir in a
    PyInstaller bundle). Built-in templates live in ``<resource_dir>/templates``."""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "src"
    return Path(__file__).parent
