"""Talks to the running agent over its local HTTP server (``/api/ui/*``).

Plain ``urllib`` so it works in a worker thread without any Qt involvement
and is trivial to test against a real aiohttp server.
"""

import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

import yaml

DEFAULT_PORT = 8080
UI_HEADER = "X-OpenEOS-Agent-UI"


class AgentUnreachable(Exception):
    pass


class AgentError(Exception):
    def __init__(self, status: int, code: str) -> None:
        super().__init__(f"{status} {code}")
        self.status = status
        self.code = code


def config_candidates() -> list[Path]:
    """Config files the installed service reads (first hit wins there, too).

    Only read here - the window never creates directories or files.
    """
    if sys.platform.startswith("win"):
        base = os.environ.get("ProgramData") or r"C:\ProgramData"
        return [Path(base) / "OpenEOS Printer Agent" / "config.yaml"]
    return [
        Path("/opt/openeos-printer-agent/config/config.yaml"),
        Path("/var/lib/openeos-printer/config.yaml"),
        Path("/etc/openeos-printer/config.yaml"),
        Path.home() / ".config" / "openeos-printer" / "config.yaml",
        Path.home() / ".local" / "share" / "openeos-printer" / "config.yaml",
    ]


def discover_port(candidates: Optional[list[Path]] = None) -> int:
    """``local_server.port`` from the agent's config.yaml, else 8080."""
    for path in candidates if candidates is not None else config_candidates():
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError, UnicodeDecodeError):
            continue
        if not isinstance(data, dict):
            continue
        local = data.get("local_server")
        if isinstance(local, dict) and isinstance(local.get("port"), int):
            return int(local["port"])
        return DEFAULT_PORT
    return DEFAULT_PORT


class AgentClient:
    def __init__(self, port: int = DEFAULT_PORT, host: str = "127.0.0.1", timeout: float = 3.0) -> None:
        self.port = port
        self.base_url = f"http://{host}:{port}"
        self._timeout = timeout

    def _request(self, method: str, path: str) -> Any:
        req = urllib.request.Request(self.base_url + path, method=method)
        req.add_header("Accept", "application/json")
        if method != "GET":
            req.add_header(UI_HEADER, "1")
            req.data = b""
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                body = resp.read()
        except urllib.error.HTTPError as e:
            code = "http_error"
            try:
                code = json.loads(e.read().decode("utf-8")).get("error", code)
            except Exception:
                pass
            raise AgentError(e.code, code) from e
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise AgentUnreachable(str(e)) from e
        try:
            return json.loads(body.decode("utf-8"))
        except ValueError as e:
            raise AgentUnreachable(f"invalid response: {e}") from e

    def state(self) -> dict[str, Any]:
        data = self._request("GET", "/api/ui/state")
        if not isinstance(data, dict):
            raise AgentUnreachable("unexpected response")
        return data

    def test_print(self, printer_id: str) -> str:
        data = self._request("POST", f"/api/ui/printers/{urllib.parse.quote(printer_id, safe='')}/test-print")
        return str(data.get("jobId", "")) if isinstance(data, dict) else ""
