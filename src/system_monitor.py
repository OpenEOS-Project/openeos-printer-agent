import asyncio
import logging
import os
import platform
import shutil
import socket
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any

import psutil

from .version import get_version

logger = logging.getLogger(__name__)


def normalize_arch(machine: str) -> str:
    """Map platform.machine() spellings to the few values the web app shows."""
    m = machine.lower()
    if m in ("x86_64", "amd64"):
        return "x86_64"
    if m in ("aarch64", "arm64"):
        return "arm64"
    if m.startswith("armv7"):
        return "armv7"
    return m or "unknown"


def agent_identity() -> dict[str, str]:
    """``os``/``arch``/``hostname``/``pythonVersion`` for the heartbeat
    (spec section 2: os is 'linux' | 'windows' | 'darwin')."""
    system = platform.system().lower() or sys.platform
    if system.startswith("win"):
        system = "windows"
    return {
        "os": system,
        "arch": normalize_arch(platform.machine()),
        "hostname": socket.gethostname(),
        "pythonVersion": platform.python_version(),
    }


class SystemMonitor:
    """Collects system metrics for the status page."""

    async def get_system_info(self) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._collect)

    def _collect(self) -> dict[str, Any]:
        info: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agent_version": get_version(),
            "python_version": platform.python_version(),
            "os_version": self._get_os_version(),
            "cpu_temp": self._get_cpu_temp(),
            "cpu_percent": psutil.cpu_percent(interval=0.5),
            "memory": self._get_memory(),
            "disk": self._get_disk(),
            "uptime_seconds": self._get_uptime(),
            "network": self._get_network(),
        }
        return info

    def _get_cpu_temp(self) -> float | None:
        # Try psutil sensors (not available on Windows/macOS - AttributeError)
        try:
            temps = psutil.sensors_temperatures()
            if temps:
                for name in ("coretemp", "cpu_thermal", "cpu-thermal"):
                    if name in temps and temps[name]:
                        return float(temps[name][0].current)
                # Fallback: first available sensor
                first_key = next(iter(temps))
                if temps[first_key]:
                    return float(temps[first_key][0].current)
        except (AttributeError, StopIteration, OSError):
            pass

        # Raspberry Pi fallback (Linux only; the path simply does not exist elsewhere)
        thermal = "/sys/class/thermal/thermal_zone0/temp"
        if sys.platform.startswith("linux") and os.path.exists(thermal):
            try:
                with open(thermal, "r") as f:
                    return int(f.read().strip()) / 1000.0
            except (OSError, ValueError):
                pass

        return None

    def _get_memory(self) -> dict[str, Any]:
        mem = psutil.virtual_memory()
        return {
            "total_mb": round(mem.total / (1024 * 1024)),
            "used_mb": round(mem.used / (1024 * 1024)),
            "percent": mem.percent,
        }

    def _get_disk(self) -> dict[str, Any]:
        # "/" is not a path on Windows; the drive the agent runs from is.
        root = os.path.splitdrive(os.path.abspath(os.sep))[0] + os.sep if sys.platform.startswith("win") else "/"
        try:
            usage = shutil.disk_usage(root)
        except OSError:
            return {"total_gb": None, "used_gb": None, "percent": None}
        return {
            "total_gb": round(usage.total / (1024**3), 1),
            "used_gb": round(usage.used / (1024**3), 1),
            "percent": round(usage.used / usage.total * 100, 1) if usage.total else None,
        }

    def _get_uptime(self) -> int:
        return int(psutil.time.time() - psutil.boot_time())

    def _get_network(self) -> dict[str, Any]:
        info: dict[str, Any] = {"ip_address": None, "wifi_ssid": None}

        # Get primary IP
        try:
            addrs = psutil.net_if_addrs()
            for iface_name, iface_addrs in addrs.items():
                if iface_name == "lo":
                    continue
                for addr in iface_addrs:
                    if addr.family == socket.AF_INET and addr.address != "127.0.0.1":
                        info["ip_address"] = addr.address
                        break
                if info["ip_address"]:
                    break
        except Exception as e:
            logger.debug(f"Failed to determine IP address: {e}")

        # Get WiFi SSID (Linux only - iwgetid is a wireless-tools binary)
        if sys.platform.startswith("linux"):
            try:
                result = subprocess.run(
                    ["iwgetid", "-r"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                if result.returncode == 0 and result.stdout.strip():
                    info["wifi_ssid"] = result.stdout.strip()
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                pass

        return info

    def _get_os_version(self) -> str:
        if sys.platform.startswith("linux"):
            try:
                import distro

                return f"{distro.name()} {distro.version()}"
            except ImportError:
                pass
        return f"{platform.system()} {platform.release()}"
