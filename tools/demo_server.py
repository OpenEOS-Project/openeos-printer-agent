#!/usr/bin/env python3
"""Stand-alone fake OpenEOS backend for demos, screenshots and UI tests.

Wraps tests/fake_api.py (the integration-test backend) on a fixed port and
adds a few control endpoints, so a real agent + desktop window can be
walked through pairing -> connected -> printers without a real server:

    python tools/demo_server.py --port 8765
    openeos-printer-agent run --server-url http://127.0.0.1:8765 --dummy-printer
    curl -X POST 'http://127.0.0.1:8765/_demo/verify?org=TSV%20Musterstadt'
    curl -X POST 'http://127.0.0.1:8765/_demo/printers?names=Theke,K%C3%BCche'

Control endpoints (POST):
    /_demo/verify?org=NAME        "enter the code" in the web app
    /_demo/printers?names=A,B     adopt network printers with these names
    /_demo/job?printer=N          send a test job (index N, default 0)
GET /_demo/state                  devices, printers, completed jobs
"""

import argparse
import asyncio
import sys
from pathlib import Path
from typing import Any

from aiohttp import web

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tests.fake_api import FakeApi  # noqa: E402


class DemoServer(FakeApi):
    def __init__(self, port: int, host: str) -> None:
        super().__init__()
        self.code_digits = 8
        self._fixed_port = port
        self._host = host
        r = self._app.router
        # The real API lives below /api (server_url ".../api"); serve the
        # device routes there as well.
        for route in list(r.routes()):
            path = getattr(route.resource, "canonical", "")
            if route.method in ("GET", "POST") and path.startswith(("/devices", "/device-api")):
                r.add_route(route.method, "/api" + path, route.handler)
        r.add_post("/_demo/verify", self._demo_verify)
        r.add_post("/_demo/printers", self._demo_printers)
        r.add_post("/_demo/job", self._demo_job)
        r.add_get("/_demo/state", self._demo_state)

    @property
    def token(self) -> str:
        """The most recently registered device (an agent that re-paired
        registers again; the demo always means the current one)."""
        assert self.devices, "no device registered"
        return list(self.devices)[-1]

    async def start(self) -> None:
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._host, self._fixed_port)
        await site.start()
        self._port = self._fixed_port

    async def _demo_verify(self, request: web.Request) -> web.Response:
        if not self.devices:
            return web.json_response({"error": "no device registered yet"}, status=409)
        self.verify(organization_name=request.query.get("org", "TSV Musterstadt"))
        return web.json_response({"verified": True})

    async def _demo_printers(self, request: web.Request) -> web.Response:
        names = [n for n in request.query.get("names", "Theke").split(",") if n]
        for i, name in enumerate(names):
            self.printers.append(
                {
                    "id": f"printer-{len(self.printers) + 1}",
                    "name": name,
                    "type": "kitchen" if i else "receipt",
                    "fingerprint": f"net:demo-{len(self.printers) + 1}",
                    "connectionType": "network",
                    "connectionConfig": {"ipAddress": f"192.168.178.{40 + len(self.printers)}", "port": 9100},
                    "paperWidth": 80,
                    "hasCashDrawer": False,
                    "isActive": True,
                }
            )
        if self.devices and self.token in self.sids:
            await self.emit_config_update()
        return web.json_response({"printers": len(self.printers)})

    async def _demo_job(self, request: web.Request) -> web.Response:
        index = int(request.query.get("printer", "0"))
        printer = self.printers[index]
        job_id = f"demo-{len(self.job_completes) + len(self.job_failures) + 1}"
        ack = await self.send_job(
            {
                "jobId": job_id,
                "printerId": printer["id"],
                "templateName": "_admin_test",
                "payload": {"printerName": printer["name"], "jobId": job_id},
            }
        )
        return web.json_response({"jobId": job_id, "ack": ack})

    async def _demo_state(self, request: web.Request) -> web.Response:
        devices: list[dict[str, Any]] = [
            {k: v for k, v in d.items() if k != "deviceToken"} for d in self.devices.values()
        ]
        return web.json_response(
            {
                "devices": devices,
                "printers": self.printers,
                "connected": list(self.sids),
                "completed": self.job_completes,
                "failed": self.job_failures,
            }
        )


async def amain(port: int, host: str) -> None:
    server = DemoServer(port, host)
    await server.start()
    print(f"Demo backend on http://{host}:{port}  (agent: --server-url http://{host}:{port})", flush=True)
    await asyncio.Event().wait()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()
    try:
        asyncio.run(amain(args.port, args.host))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
