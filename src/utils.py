import logging
import sys
from logging.handlers import RotatingFileHandler

from .config import AppConfig

_MARKER = "_openeos_agent_handler"


def _mark(handler: logging.Handler) -> None:
    setattr(handler, _MARKER, True)


def remove_agent_handlers() -> None:
    """Detach (and close) the handlers setup_logging() installed."""
    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        if getattr(handler, _MARKER, False):
            root_logger.removeHandler(handler)
            handler.close()


def setup_logging(config: AppConfig) -> None:
    """Console + rotating file logging.

    The file handler is always attempted (``<log_dir>/agent.log``, 10 MB x 5
    by default) because a Windows service has no console at all - without
    the file the pairing code would be invisible. A console handler is only
    added when a stdout exists (it is None under the service control manager).
    """
    log_format = "%(asctime)s [%(levelname)-8s] %(name)s: %(message)s"
    date_format = "%Y-%m-%d %H:%M:%S"
    formatter = logging.Formatter(log_format, datefmt=date_format)

    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, config.logging.level.upper(), logging.INFO))
    # Only drop handlers we installed on an earlier call (the Windows service
    # may re-run setup); foreign handlers (pytest's caplog, Sentry, ...) stay.
    remove_agent_handlers()
    # python-escpos calls logging.basicConfig() at import time, which leaves a
    # bare StreamHandler on stderr behind and doubles every line. Plain
    # StreamHandler instances are never ours or pytest's (both use subclasses
    # or carry the marker), so they can go.
    for handler in list(root_logger.handlers):
        if type(handler) is logging.StreamHandler and not getattr(handler, _MARKER, False):
            root_logger.removeHandler(handler)

    if sys.stdout is not None:
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setFormatter(formatter)
        _mark(console_handler)
        root_logger.addHandler(console_handler)

    log_file = config.log_file
    try:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = RotatingFileHandler(
            str(log_file),
            maxBytes=config.logging.max_bytes,
            backupCount=config.logging.backup_count,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        _mark(file_handler)
        root_logger.addHandler(file_handler)
    except OSError as e:
        root_logger.warning(f"Log file {log_file} not writable ({e}); logging to console only")

    # Reduce noise from third-party libraries
    logging.getLogger("socketio").setLevel(logging.WARNING)
    logging.getLogger("engineio").setLevel(logging.WARNING)
    logging.getLogger("aiohttp").setLevel(logging.WARNING)
    logging.getLogger("zeroconf").setLevel(logging.WARNING)
