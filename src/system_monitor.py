import asyncio
import logging
import platform
import shutil
import subprocess
from datetime import datetime, timezone

import psutil

from .version import __version__

logger = logging.getLogger(__name__)


class SystemMonitor:
    """Collects system metrics for heartbeat reporting."""

    async def get_system_info(self) -> dict:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._collect)

    def _collect(self) -> dict:
        info: dict = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "agent_version": __version__,
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
        # Try psutil sensors
        try:
            temps = psutil.sensors_temperatures()
            if temps:
                for name in ("coretemp", "cpu_thermal", "cpu-thermal"):
                    if name in temps and temps[name]:
                        return temps[name][0].current
                # Fallback: first available sensor
                first_key = next(iter(temps))
                if temps[first_key]:
                    return temps[first_key][0].current
        except (AttributeError, StopIteration):
            pass

        # Raspberry Pi fallback
        try:
            with open("/sys/class/thermal/thermal_zone0/temp", "r") as f:
                return int(f.read().strip()) / 1000.0
        except (FileNotFoundError, ValueError):
            pass

        return None

    def _get_memory(self) -> dict:
        mem = psutil.virtual_memory()
        return {
            "total_mb": round(mem.total / (1024 * 1024)),
            "used_mb": round(mem.used / (1024 * 1024)),
            "percent": mem.percent,
        }

    def _get_disk(self) -> dict:
        usage = shutil.disk_usage("/")
        return {
            "total_gb": round(usage.total / (1024**3), 1),
            "used_gb": round(usage.used / (1024**3), 1),
            "percent": round(usage.used / usage.total * 100, 1),
        }

    def _get_uptime(self) -> int:
        return int(psutil.time.time() - psutil.boot_time())

    def _get_network(self) -> dict:
        info: dict = {"ip_address": None, "wifi_ssid": None}

        # Get primary IP
        try:
            addrs = psutil.net_if_addrs()
            for iface_name, iface_addrs in addrs.items():
                if iface_name == "lo":
                    continue
                for addr in iface_addrs:
                    if addr.family.name == "AF_INET" and addr.address != "127.0.0.1":
                        info["ip_address"] = addr.address
                        break
                if info["ip_address"]:
                    break
        except Exception as e:
            logger.debug(f"Failed to determine IP address: {e}")

        # Get WiFi SSID (Linux)
        try:
            result = subprocess.run(
                ["iwgetid", "-r"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                info["wifi_ssid"] = result.stdout.strip()
        except (FileNotFoundError, subprocess.TimeoutExpired):
            pass

        return info

    def _get_os_version(self) -> str:
        try:
            import distro
            return f"{distro.name()} {distro.version()}"
        except ImportError:
            return f"{platform.system()} {platform.release()}"
