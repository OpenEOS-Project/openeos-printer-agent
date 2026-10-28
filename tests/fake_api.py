"""Minimal stand-in for the OpenEOS backend, as far as the agent uses it.

Implements the pairing endpoints, the device API routes from the rework
spec (section 4) and the socket events, all in-process on a random port.
The integration tests drive it (verify the device, adopt a discovered
printer, push a job) and inspect what the agent sent.
"""

import asyncio
import secrets
import time
from typing import Any, Callable, Optional

import socketio
from aiohttp import web


class FakeApi:
    """``reject_mode`` controls how the socket turns an unknown/blocked
    token away: ``"kick"`` mirrors the real gateway (accept, emit
    ``error`` {"message": "Authentication required"}, disconnect),
    ``"refuse"`` rejects the namespace (client sees ``connect_error``)."""

    def __init__(self, reject_mode: str = "kick") -> None:
        self.reject_mode = reject_mode
        self.devices: dict[str, dict[str, Any]] = {}  # token -> device row
        self.status_changes: list[dict[str, Any]] = []  # deviceStatusChanged emitted
        self.discovered_reports: list[dict[str, Any]] = []  # every POST body
        self.discovered_rows: dict[str, dict[str, Any]] = {}  # fingerprint -> row
        self.printers: list[dict[str, Any]] = []  # adopted printers
        self.templates: dict[str, str] = {}
        self.sync_calls = 0
        self.status_calls = 0  # GET /devices/status
        self.heartbeats: list[dict[str, Any]] = []
        self.printer_heartbeats: list[dict[str, Any]] = []
        self.job_completes: list[dict[str, Any]] = []
        self.job_failures: list[dict[str, Any]] = []
        self.sids: dict[str, str] = {}  # token -> sid

        self._app = web.Application()
        self._sio = socketio.AsyncServer(async_mode="aiohttp", logger=False, engineio_logger=False)
        self._sio.attach(self._app)
        self._runner: Optional[web.AppRunner] = None
        self._port = 0
        self._routes()
        self._socket_handlers()

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        assert site._server is not None
        self._port = site._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        if self._runner:
            await self._runner.cleanup()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    # -- helpers for tests ---------------------------------------------------

    async def wait_for(self, predicate: Callable[[], Any], timeout: float = 10.0) -> Any:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            await asyncio.sleep(0.05)
        raise AssertionError("condition not met within timeout")

    @property
    def token(self) -> str:
        assert self.devices, "no device registered"
        return next(iter(self.devices))

    def verify(self, organization_id: str = "org-1", organization_name: str = "Testverein") -> None:
        row = self.devices[self.token]
        row["status"] = "verified"
        row["organizationId"] = organization_id
        row["organizationName"] = organization_name
        row["verificationCode"] = None

    def remove(self) -> str:
        """Admin removes the agent in the web app: the row is deleted, the
        token is unknown from now on. The real backend also closes the
        agent's socket - see ``disconnect_socket``; tests combine the two
        or leave the socket alone to exercise the REST 401 path."""
        token = self.token
        del self.devices[token]
        return token

    async def disconnect_socket(self, token: str) -> None:
        """Server-initiated disconnect of the agent's socket (DELETE
        /printer-agents/:id does this, so does a server restart). The
        client sees reason "server disconnect"."""
        sid = self.sids.pop(token, None)
        if sid:
            await self._sio.disconnect(sid)

    async def block(self) -> None:
        """``blockDevice``: status blocked, inactive, status event to the socket."""
        row = self.devices[self.token]
        row["status"] = "blocked"
        row["isActive"] = False
        await self._notify_status(row)

    async def unblock(self) -> None:
        row = self.devices[self.token]
        row["status"] = "verified"
        row["isActive"] = True
        await self._notify_status(row)

    async def _notify_status(self, row: dict[str, Any]) -> None:
        payload = {"deviceId": row["deviceId"], "status": row["status"]}
        self.status_changes.append(payload)
        sid = self.sids.get(row["deviceToken"])
        if sid:
            await self._sio.emit("deviceStatusChanged", payload, to=sid)

    def adopt(self, fingerprint: str, printer_id: str, **overrides: Any) -> dict[str, Any]:
        """Create a Printer row from a discovered entry (like the org API's adopt)."""
        row = self.discovered_rows[fingerprint]
        details = row["details"]
        connection_config: dict[str, Any] = {}
        if row["connectionType"] == "usb":
            connection_config = {
                "usbVendorId": details.get("usbVendorId"),
                "usbProductId": details.get("usbProductId"),
                "usbSerialNumber": details.get("serialNumber"),
            }
        elif row["connectionType"] == "network":
            connection_config = {"ipAddress": details.get("ipAddress"), "port": details.get("port", 9100)}
        elif row["connectionType"] == "windows":
            connection_config = {"windowsPrinterName": details.get("windowsPrinterName")}
        printer = {
            "id": printer_id,
            "name": overrides.get("name", row["displayName"]),
            "type": overrides.get("type", "receipt"),
            "fingerprint": fingerprint,
            "connectionType": row["connectionType"],
            "connectionConfig": connection_config,
            "paperWidth": overrides.get("paperWidth", 80),
            "hasCashDrawer": overrides.get("hasCashDrawer", False),
            "isActive": True,
        }
        self.printers.append(printer)
        row["printerId"] = printer_id
        return printer

    async def emit_config_update(self) -> None:
        await self._sio.emit("printerConfigUpdate", {}, to=self.sids[self.token])

    async def emit_discover(self, subnet_scan: bool, token: Optional[str] = None) -> Any:
        sid = self.sids[token or self.token]
        return await self._sio.call("printerDiscover", {"subnetScan": subnet_scan}, to=sid, timeout=5)

    async def send_job(self, job: dict[str, Any]) -> Any:
        return await self._sio.call("printerJob", job, to=self.sids[self.token], timeout=10)

    # -- REST ----------------------------------------------------------------

    def _routes(self) -> None:
        r = self._app.router
        r.add_post("/devices/init", self._init)
        r.add_get("/devices/status", self._status)
        r.add_post("/device-api/printers/discovered", self._discovered)
        r.add_get("/device-api/printers", self._printers)
        r.add_post("/device-api/printers/sync", self._sync_gone)
        r.add_get("/device-api/templates", self._templates)

    def _device(self, request: web.Request) -> Optional[dict[str, Any]]:
        token = request.headers.get("x-device-token", "")
        return self.devices.get(token)

    async def _init(self, request: web.Request) -> web.Response:
        body = await request.json()
        assert body.get("deviceType") == "printer_agent"
        token = "tok-" + secrets.token_hex(8)
        device = {
            "deviceId": "dev-" + secrets.token_hex(4),
            "deviceToken": token,
            "verificationCode": f"{secrets.randbelow(900000) + 100000}",
            "status": "pending",
            "isActive": True,
            "organizationId": None,
            "organizationName": None,
            "suggestedName": body.get("suggestedName"),
            "userAgent": body.get("userAgent"),
        }
        self.devices[token] = device
        return web.json_response(
            {
                "data": {
                    "deviceId": device["deviceId"],
                    "deviceToken": token,
                    "verificationCode": device["verificationCode"],
                }
            },
            status=201,
        )

    async def _status(self, request: web.Request) -> web.Response:
        # Like the real getDeviceStatus: a blocked (inactive) device still
        # gets its status; only an unknown token is a 401.
        self.status_calls += 1
        device = self._device(request)
        if device is None:
            return web.json_response({"message": "unauthorized"}, status=401)
        return web.json_response(
            {
                "data": {
                    "deviceId": device["deviceId"],
                    "status": device["status"],
                    "organizationId": device["organizationId"],
                    "organizationName": device["organizationName"],
                }
            }
        )

    def _require_verified(self, request: web.Request) -> Optional[web.Response]:
        device = self._device(request)
        if device is None or device["status"] != "verified" or not device.get("isActive", True):
            return web.json_response({"message": "unauthorized"}, status=401)
        return None

    async def _discovered(self, request: web.Request) -> web.Response:
        denied = self._require_verified(request)
        if denied:
            return denied
        body = await request.json()
        self.discovered_reports.append(body)
        rows = []
        for item in body["printers"]:
            fp = item["fingerprint"]
            row = self.discovered_rows.get(fp)
            if row is None:
                row = {"id": "disc-" + secrets.token_hex(3), "printerId": None, "firstSeenAt": time.time()}
                self.discovered_rows[fp] = row
            row.update(
                {
                    "fingerprint": fp,
                    "connectionType": item["connectionType"],
                    "displayName": item["displayName"],
                    "details": item.get("details", {}),
                    "discoveredVia": item["discoveredVia"],
                    "lastSeenAt": time.time(),
                }
            )
            rows.append({"id": row["id"], "fingerprint": fp, "printerId": row["printerId"]})
        return web.json_response({"data": {"printers": rows}})

    async def _printers(self, request: web.Request) -> web.Response:
        denied = self._require_verified(request)
        if denied:
            return denied
        return web.json_response({"data": {"printers": self.printers}})

    async def _sync_gone(self, request: web.Request) -> web.Response:
        self.sync_calls += 1
        return web.json_response({"message": "gone"}, status=410)

    async def _templates(self, request: web.Request) -> web.Response:
        denied = self._require_verified(request)
        if denied:
            return denied
        return web.json_response({"data": {"templates": self.templates}})

    # -- socket --------------------------------------------------------------

    def _socket_handlers(self) -> None:
        sio = self._sio

        async def kick(sid: str) -> None:
            await asyncio.sleep(0.05)
            await sio.emit("error", {"message": "Authentication required"}, to=sid)
            await sio.disconnect(sid)

        @sio.event
        async def connect(sid: str, environ: dict[str, Any], auth: Optional[dict[str, Any]]) -> bool:
            token = (auth or {}).get("deviceToken", "")
            device = self.devices.get(token)
            if device is None or device["status"] != "verified" or not device.get("isActive", True):
                if self.reject_mode == "refuse":
                    raise socketio.exceptions.ConnectionRefusedError("Authentication required")
                asyncio.get_running_loop().create_task(kick(sid))
                return True
            self.sids[token] = sid
            await sio.emit(
                "connected",
                {"deviceId": device["deviceId"], "hasOrganization": bool(device["organizationId"])},
                to=sid,
            )
            return True

        @sio.on("agentHeartbeat")
        async def agent_heartbeat(sid: str, data: dict[str, Any]) -> dict[str, bool]:
            self.heartbeats.append(data)
            return {"success": True}

        @sio.on("printerHeartbeat")
        async def printer_heartbeat(sid: str, data: dict[str, Any]) -> dict[str, bool]:
            self.printer_heartbeats.append(data)
            return {"success": True}

        @sio.on("printerJobComplete")
        async def job_complete(sid: str, data: dict[str, Any]) -> dict[str, bool]:
            self.job_completes.append(data)
            return {"success": True}

        @sio.on("printerJobFailed")
        async def job_failed(sid: str, data: dict[str, Any]) -> dict[str, bool]:
            self.job_failures.append(data)
            return {"success": True}
