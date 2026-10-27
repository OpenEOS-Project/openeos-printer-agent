"""REST calls against the device API (``x-device-token`` auth).

Everything the agent fetches or reports outside the socket goes through
here: adopted printers, templates and the discovery report.
"""

import logging
from typing import Any, Callable, Optional

import aiohttp

logger = logging.getLogger(__name__)

# Called with a short reason when the server answers 401/403 - the agent
# then checks whether the device was removed or blocked (see
# PrinterAgent._on_auth_rejected) instead of logging the same error forever.
UnauthorizedCallback = Callable[[str], None]


def _unwrap(result: Any) -> Any:
    """The API wraps responses in ``{"data": ...}``; accept the bare shape too."""
    if isinstance(result, dict) and "data" in result:
        return result["data"]
    return result


class DeviceApiClient:
    def __init__(
        self,
        base_url: str,
        device_token: str,
        timeout_s: float = 15.0,
        on_unauthorized: Optional[UnauthorizedCallback] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._device_token = device_token
        self._timeout = aiohttp.ClientTimeout(total=timeout_s)
        self._session: Optional[aiohttp.ClientSession] = None
        self._on_unauthorized = on_unauthorized

    @property
    def base_url(self) -> str:
        return self._base_url

    async def set_device_token(self, device_token: str) -> None:
        """Switch to a new token (after re-pairing); the session carries the
        token as a default header, so it is rebuilt on the next call."""
        self._device_token = device_token
        await self.close()

    def _check_unauthorized(self, status: int, what: str) -> None:
        if status in (401, 403) and self._on_unauthorized:
            self._on_unauthorized(f"{what}: HTTP {status}")

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=self._timeout,
                headers={"x-device-token": self._device_token},
            )
        return self._session

    async def close(self) -> None:
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def get_printers(self) -> list[dict[str, Any]]:
        """``GET /device-api/printers`` - the adopted printers of this agent.

        Returns an empty list on any error; the caller keeps the previous
        printer set in that case (a flaky network must not drop workers).
        """
        url = f"{self._base_url}/device-api/printers"
        session = await self._get_session()
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error(f"Failed to fetch printers ({resp.status}): {body}")
                    self._check_unauthorized(resp.status, "GET /device-api/printers")
                    return []
                payload = _unwrap(await resp.json())
        except aiohttp.ClientError as e:
            logger.error(f"Failed to fetch printers: {e}")
            return []

        if isinstance(payload, list):
            printers = payload
        elif isinstance(payload, dict):
            printers = payload.get("printers", [])
        else:
            printers = []
        logger.info(f"Fetched {len(printers)} adopted printer(s) from backend")
        return [p for p in printers if isinstance(p, dict)]

    async def get_templates(self) -> dict[str, str]:
        """``GET /device-api/templates`` - organisation template overrides."""
        url = f"{self._base_url}/device-api/templates"
        session = await self._get_session()
        try:
            async with session.get(url) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error(f"Failed to fetch templates ({resp.status}): {body}")
                    self._check_unauthorized(resp.status, "GET /device-api/templates")
                    return {}
                payload = _unwrap(await resp.json())
        except aiohttp.ClientError as e:
            logger.error(f"Failed to fetch templates: {e}")
            return {}

        templates = payload.get("templates", {}) if isinstance(payload, dict) else {}
        logger.info(f"Fetched {len(templates)} template(s) from backend")
        return {str(k): str(v) for k, v in templates.items()} if isinstance(templates, dict) else {}

    async def report_discovered(self, printers: list[dict[str, Any]], full: bool) -> list[dict[str, Any]]:
        """``POST /device-api/printers/discovered`` with ``{printers, full}``.

        Raises on transport/HTTP errors so the discovery service can record
        the failure; returns the backend's ``[{id, fingerprint, printerId}]``.
        """
        url = f"{self._base_url}/device-api/printers/discovered"
        session = await self._get_session()
        async with session.post(url, json={"printers": printers, "full": full}) as resp:
            if resp.status not in (200, 201):
                body = await resp.text()
                self._check_unauthorized(resp.status, "POST /device-api/printers/discovered")
                raise RuntimeError(f"discovery report rejected ({resp.status}): {body}")
            payload = _unwrap(await resp.json())

        if isinstance(payload, list):
            rows = payload
        elif isinstance(payload, dict):
            rows = payload.get("printers", [])
        else:
            rows = []
        adopted = sum(1 for r in rows if isinstance(r, dict) and r.get("printerId"))
        logger.info(f"Reported {len(printers)} discovered printer(s) (full={full}); {adopted} already adopted")
        return [r for r in rows if isinstance(r, dict)]
