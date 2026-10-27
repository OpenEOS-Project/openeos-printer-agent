import asyncio
import json
import logging
import os
import socket
from pathlib import Path
from typing import Optional

import aiohttp

from .config import AppConfig
from .version import get_version

logger = logging.getLogger(__name__)

# How long to wait between attempts when the server cannot be reached during
# first registration. A freshly installed service must not crash-loop just
# because the network came up a few seconds late.
REGISTER_RETRY_S = 10.0

# Poll interval while the device is blocked in the web app (or a token from
# the config file keeps being rejected). Nothing the agent can do about
# either, so it checks back rarely instead of hammering the server.
BLOCKED_POLL_S = 60.0

# Outcomes of one GET /devices/status, see DeviceRegistrar.check_status().
STATUS_VERIFIED = "verified"
STATUS_PENDING = "pending"
STATUS_BLOCKED = "blocked"
STATUS_INVALID = "invalid"  # 401/403: the server does not know this token (device removed)
STATUS_UNREACHABLE = "unreachable"  # network error or unexpected answer, try again later


def format_pairing_banner(code: str, server_url: str, status_url: Optional[str]) -> str:
    """The pairing code, impossible to miss in a console or log file."""
    spaced = f"{code[:3]} {code[3:]}" if len(code) == 6 else code
    lines = [
        "",
        "=" * 64,
        "  OpenEOS Printer Agent - PAIRING REQUIRED",
        "",
        f"  Pairing code:   {spaced}",
        "",
        "  Open the OpenEOS web app > Drucker > 'Drucker-Agent verbinden'",
        "  and enter this code. The agent keeps waiting until it is linked.",
        f"  Server: {server_url}",
    ]
    if status_url:
        lines.append(f"  Status page: {status_url}")
    lines += ["=" * 64, ""]
    return "\n".join(lines)


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

    @property
    def token_file(self) -> Path:
        return self._config.device_token_path

    def _status_url(self) -> Optional[str]:
        cfg = self._config.local_server
        if not cfg.enabled:
            return None
        host = "localhost" if cfg.host in ("0.0.0.0", "::", "") else cfg.host
        return f"http://{host}:{cfg.port}"

    async def ensure_registered(self) -> str:
        """Ensure the device has a valid device_token.

        Checks in order:
        1. YAML config (pre-provisioned)
        2. Persistent token file (previous self-registration)
        3. Self-register via POST /devices/init (retried until the server answers)

        Returns the device_token.
        """
        # 1. Check YAML config
        if self._config.server.device_token:
            self._device_token = self._config.server.device_token
            logger.info("Using device token from config")
            return self._device_token

        # 2. Check persistent token file
        token_file = self.token_file
        if token_file.exists():
            try:
                data = json.loads(token_file.read_text(encoding="utf-8"))
                if data.get("device_token"):
                    self._device_token = data["device_token"]
                    self._device_id = data.get("device_id")
                    self._verification_code = data.get("verification_code")
                    if self._verification_code:
                        # Registered earlier but never linked: show the code again.
                        self._status = "pending"
                        logger.info(
                            format_pairing_banner(self._verification_code, self._config.server.url, self._status_url())
                        )
                    logger.info(f"Loaded device token from {token_file}")
                    return str(self._device_token)
            except Exception as e:
                logger.warning(f"Failed to read token file {token_file}: {e}")

        # 3. Self-register
        logger.info("No device token found, initiating self-registration...")
        await self._register_with_retry()
        return str(self._device_token)

    async def _register_with_retry(self) -> None:
        while True:
            try:
                await self._self_register()
                return
            except (aiohttp.ClientError, asyncio.TimeoutError, RuntimeError) as e:
                logger.warning(f"Registration failed ({e}); retrying in {REGISTER_RETRY_S:.0f}s")
                await asyncio.sleep(REGISTER_RETRY_S)

    @property
    def token_from_config(self) -> bool:
        """A pre-provisioned token cannot be replaced by re-pairing."""
        return bool(self._config.server.device_token)

    def discard_token(self) -> None:
        """Forget the stored device (device.json) so the next
        ``ensure_registered()`` pairs from scratch with a new code. Called
        when the server no longer knows the token - typically because the
        agent was removed in the web app."""
        token_file = self.token_file
        try:
            if token_file.exists():
                token_file.unlink()
                logger.info(f"Removed stale device token file {token_file}")
        except OSError as e:
            logger.warning(f"Could not remove {token_file}: {e}")
        self._device_token = None
        self._device_id = None
        self._verification_code = None
        self._organization_id = None
        self._organization_name = None
        self._status = "unknown"

    async def _self_register(self) -> None:
        """Register this device via POST /devices/init."""
        url = f"{self._config.server.url}/devices/init"
        payload = {
            "suggestedName": f"Drucker-Agent {socket.gethostname()}",
            "userAgent": f"openeos-printer-agent/{get_version()}",
            "deviceType": "printer_agent",
        }

        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
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
        logger.info(format_pairing_banner(str(self._verification_code), self._config.server.url, self._status_url()))

        # Persist token
        self._save_token()

    def _save_token(self) -> None:
        """Save device token to persistent file."""
        token_file = self.token_file
        try:
            token_file.parent.mkdir(parents=True, exist_ok=True)
            data = {
                "device_token": self._device_token,
                "device_id": self._device_id,
                "verification_code": self._verification_code,
                "server_url": self._config.server.url,
            }
            token_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
            # write_text doesn't take a mode, so the file lands at whatever
            # the umask leaves it — commonly 644, world-readable. This is a
            # credential (device_token authenticates as this device), so
            # lock it down explicitly rather than trust the deployment's umask.
            # (No-op on Windows; ProgramData ACLs apply there.)
            os.chmod(token_file, 0o600)
            logger.info(f"Device token saved to {token_file}")
        except Exception as e:
            logger.warning(f"Failed to save token to {token_file}: {e}")

    async def check_status(self) -> str:
        """One ``GET /devices/status`` with the current token.

        Returns one of STATUS_VERIFIED / STATUS_PENDING / STATUS_BLOCKED /
        STATUS_INVALID / STATUS_UNREACHABLE and updates the organisation
        fields (and the token file) when the device is verified. The agent
        calls this whenever the server rejects the token at runtime to find
        out whether the device was removed (re-pair), blocked (wait) or the
        rejection was transient.
        """
        url = f"{self._config.server.url}/devices/status"
        headers = {"x-device-token": self._device_token or ""}
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
                async with session.get(url, headers=headers) as resp:
                    if resp.status in (401, 403):
                        return STATUS_INVALID
                    if resp.status != 200:
                        logger.warning(f"Status check failed: {resp.status}")
                        return STATUS_UNREACHABLE
                    result = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as e:
            logger.warning(f"Connection error during status check: {e}")
            return STATUS_UNREACHABLE

        data = result.get("data", result) if isinstance(result, dict) else {}
        status = str(data.get("status", ""))
        if status == STATUS_VERIFIED:
            # Connect as soon as the device is verified - even if no
            # organization is assigned yet. The agent stays connected via
            # WebSocket; the gateway routes print jobs to whichever org is
            # currently linked.
            org_id = data.get("organizationId")
            self._organization_id = org_id or None
            self._organization_name = data.get("organizationName") if org_id else None
            self._device_id = data.get("deviceId", self._device_id)
            if self._status != STATUS_VERIFIED or self._verification_code:
                self._status = STATUS_VERIFIED
                self._verification_code = None
                self._save_token()
            return STATUS_VERIFIED
        if status == STATUS_BLOCKED:
            self._status = STATUS_BLOCKED
            return STATUS_BLOCKED
        if status == STATUS_PENDING:
            self._status = STATUS_PENDING
            return STATUS_PENDING
        logger.warning(f"Unexpected device status from server: {status!r}")
        return STATUS_UNREACHABLE

    async def wait_for_verification(self, poll_interval: float = 5.0) -> None:
        """Poll GET /devices/status until the device is verified.

        This handles both:
        - Self-registered devices waiting for admin to enter the code
        - Pre-provisioned devices waiting for org assignment

        and the two ways the server can turn the device away meanwhile:
        - token unknown (device removed in the web app): the stored token is
          discarded and a fresh registration shows a new pairing code -
          without a restart and without crashing the service;
        - device blocked: nothing to do but wait; checked every
          ``BLOCKED_POLL_S`` until an admin unblocks it.

        While pending, the pairing banner is repeated every minute so it
        stays visible in a scrolling log.
        """
        logger.info("Waiting for device verification...")
        polls = 0
        blocked_announced = False

        while True:
            result = await self.check_status()

            if result == STATUS_VERIFIED:
                if self._organization_id:
                    logger.info(f"Device verified! Org: {self._organization_name} ({self._organization_id})")
                else:
                    logger.info("Device verified - no organization assigned yet, staying connected")
                return

            if result == STATUS_INVALID:
                if self.token_from_config:
                    logger.error(
                        "The device token from the config file was rejected by the server. "
                        f"Fix or remove 'device_token' in the config; retrying in {BLOCKED_POLL_S:.0f}s"
                    )
                    await asyncio.sleep(BLOCKED_POLL_S)
                    continue
                logger.warning(
                    "The server does not know this device any more (token rejected) - "
                    "it was probably removed in the OpenEOS web app. Starting a new pairing."
                )
                self.discard_token()
                await self._register_with_retry()  # logs the new banner
                polls = 0
                continue

            if result == STATUS_BLOCKED:
                # Loud once, quiet afterwards: one line per minute is enough
                # to show in the log that the agent is alive and waiting.
                logger.log(
                    logging.INFO if blocked_announced else logging.WARNING,
                    "This device is BLOCKED in the OpenEOS web app. Unblock it there "
                    f"(Drucker > Drucker-Agent) - checking again in {BLOCKED_POLL_S:.0f}s",
                )
                blocked_announced = True
                await asyncio.sleep(BLOCKED_POLL_S)
                continue

            if result == STATUS_PENDING:
                logger.debug("Device still pending verification")

            polls += 1
            if self._verification_code and polls % max(1, int(60 / poll_interval)) == 0:
                logger.info(format_pairing_banner(self._verification_code, self._config.server.url, self._status_url()))
            await asyncio.sleep(poll_interval)
