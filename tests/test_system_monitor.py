import pytest

from src.system_monitor import SystemMonitor


class TestSystemMonitor:
    @pytest.mark.asyncio
    async def test_get_system_info_returns_dict(self):
        monitor = SystemMonitor()
        info = await monitor.get_system_info()
        assert isinstance(info, dict)
        assert "timestamp" in info
        assert "agent_version" in info
        assert "python_version" in info
        assert "cpu_percent" in info
        assert "memory" in info
        assert "disk" in info
        assert "uptime_seconds" in info
        assert "network" in info

    @pytest.mark.asyncio
    async def test_memory_structure(self):
        monitor = SystemMonitor()
        info = await monitor.get_system_info()
        mem = info["memory"]
        assert "total_mb" in mem
        assert "used_mb" in mem
        assert "percent" in mem
        assert isinstance(mem["total_mb"], int)

    @pytest.mark.asyncio
    async def test_disk_structure(self):
        monitor = SystemMonitor()
        info = await monitor.get_system_info()
        disk = info["disk"]
        assert "total_gb" in disk
        assert "used_gb" in disk
        assert "percent" in disk

    @pytest.mark.asyncio
    async def test_network_structure(self):
        monitor = SystemMonitor()
        info = await monitor.get_system_info()
        net = info["network"]
        assert "ip_address" in net
        assert "wifi_ssid" in net

    @pytest.mark.asyncio
    async def test_cpu_temp_fallback(self):
        """CPU temp should return None if no sensors available (CI/containers)."""
        monitor = SystemMonitor()
        info = await monitor.get_system_info()
        # cpu_temp can be None or a float - both are valid
        assert info["cpu_temp"] is None or isinstance(info["cpu_temp"], (int, float))

    @pytest.mark.asyncio
    async def test_uptime_positive(self):
        monitor = SystemMonitor()
        info = await monitor.get_system_info()
        assert info["uptime_seconds"] > 0
