import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Optional

import aiohttp

from .config import AppConfig
from .version import __version__

logger = logging.getLogger(__name__)


class DeviceRegistrar:
    """Handles device registration and verification with the OpenEOS backend.

    Supports two flows:
    A) Self-registration: No device_token configured, agent calls POST /devices/init
    B) Pre-provisioned: device_token in YAML config, agent connects directly
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._device_token: Optional[str] = None
        self._device_id: Optional[str] = None
        self._verification_code: Optional[str] = None
        self._status: str = "unknown"  # unknown, pending, verified, waiting_for_org
        self._organization_id: Optional[str] = None
        self._organization_name: Optional[str] = None

    @property
    def device_token(self) -> Optional[str]:
        return self._device_token

    @property
    def device_id(self) -> Optional[str]:
        return self._device_id

    @property
    def verification_code(self) -> Optional[str]:
        return self._verification_code

    @property
    def status(self) -> str:
        return self._status

    @property
    def organization_id(self) -> Optional[str]:
        return self._organization_id

    @property
    def organization_name(self) -> Optional[str]:
        return self._organization_name

    async def ensure_registered(self) -> str:
        """Ensure the device has a valid device_token.

        Checks in order:
        1. YAML config (pre-provisioned)
        2. Persistent token file (previous self-registration)
        3. Self-register via POST /devices/init

        Returns the device_token.
        """
        # 1. Check YAML config
        if self._config.server.device_token:
            self._device_token = self._config.server.device_token
            logger.info("Using device token from config")
            return self._device_token

        # 2. Check persistent token file
        token_file = Path(self._config.device_token_file)
        if token_file.exists():
            try:
                data = json.loads(token_file.read_text(encoding="utf-8"))
                if data.get("device_token"):
                    self._device_token = data["device_token"]
                    self._device_id = data.get("device_id")
                    self._verification_code = data.get("verification_code")
                    logger.info(f"Loaded device token from {token_file}")
                    return self._device_token
            except Exception as e:
                logger.warning(f"Failed to read token file {token_file}: {e}")

        # 3. Self-register
        logger.info("No device token found, initiating self-registration...")
        await self._self_register()
        return self._device_token  # type: ignore

    async def _self_register(self) -> None:
        """Register this device via POST /devices/init."""
        url = f"{self._config.server.url}/devices/init"
        payload = {
            "suggestedName": f"Printer-Agent-{self._config.agent.id}",
            "userAgent": f"openeos-printer-agent/{__version__}",
            "deviceType": "printer_agent",
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload) as resp:
                if resp.status != 201:
                    body = await resp.text()
                    raise RuntimeError(f"Device init failed ({resp.status}): {body}")

                result = await resp.json()
                data = result.get("data", result)

        self._device_token = data["deviceToken"]
        self._device_id = data["deviceId"]
        self._verification_code = data["verificationCode"]
        self._status = "pending"

        logger.info(f"Device registered: {self._device_id}")
        logger.info(f"Verification code: {self._verification_code}")

        # Persist token
        self._save_token()

    def _save_token(self) -> None:
        """Save device token to persistent file."""
        token_file = Path(self._config.device_token_file)
        try:
            token_file.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "device_token": self._device_token,
                "device_id": self._device_id,
                "verification_code": self._verification_code,
            }
            token_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
            # write_text doesn't take a mode, so the file lands at whatever
            # the umask leaves it — commonly 644, world-readable. This is a
            # credential (device_token authenticates as this device), so
            # lock it down explicitly rather than trust the deployment's umask.
            os.chmod(token_file, 0o600)
            logger.info(f"Device token saved to {token_file}")
        except Exception as e:
            logger.warning(f"Failed to save token to {token_file}: {e}")

    async def wait_for_verification(self, poll_interval: float = 5.0) -> None:
        """Poll GET /devices/status until the device is verified and has an org.

        This handles both:
        - Self-registered devices waiting for admin to enter the code
        - Pre-provisioned devices waiting for org assignment
        """
        url = f"{self._config.server.url}/devices/status"
        headers = {"x-device-token": self._device_token or ""}

        logger.info("Waiting for device verification...")

        while True:
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(url, headers=headers) as resp:
                        if resp.status == 200:
                            result = await resp.json()
                            data = result.get("data", result)
                            status = data.get("status", "")
                            org_id = data.get("organizationId")

                            if status == "verified":
                                # Connect as soon as the device is verified — even
                                # if no organization is assigned yet. The agent stays
                                # connected via WebSocket; the gateway routes print
                                # jobs to whichever org is currently linked.
                                self._status = "verified"
                                if org_id:
                                    self._organization_id = org_id
                                    self._organization_name = data.get("organizationName")
                                else:
                                    self._organization_id = None
                                    self._organization_name = None
                                self._device_id = data.get("deviceId", self._device_id)
                                self._verification_code = None
                                self._save_token()
                                if org_id:
                                    logger.info(
                                        f"Device verified! Org: {self._organization_name} ({self._organization_id})"
                                    )
                                else:
                                    logger.info("Device verified — no organization assigned yet, staying connected")
                                return

                            if status == "pending":
                                self._status = "pending"
                                logger.debug("Device still pending verification")
                        elif resp.status == 401:
                            logger.error("Invalid device token - cannot verify")
                            raise RuntimeError("Invalid device token")
                        else:
                            logger.warning(f"Status check failed: {resp.status}")
            except aiohttp.ClientError as e:
                logger.warning(f"Connection error during status check: {e}")
            except RuntimeError:
                raise

            await asyncio.sleep(poll_interval)
