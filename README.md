# OpenEOS Printer Agent

Small service that runs on **any Linux or Windows PC** (Raspberry Pi included)
next to your receipt printers. It **finds USB and network printers on its
own**, reports them to the OpenEOS platform, and prints the jobs (receipts,
kitchen tickets, labels) the platform sends it. Which printer is used for
what is configured in the OpenEOS web app, not on the PC.

- Pairing with a 6-digit code - no tokens to copy; no arguments needed for
  the hosted service (`https://api.openeos.de/api` is the default)
- Discovery: USB (pyusb), Windows print spooler, mDNS/Bonjour, optional
  port-9100 scan of the local network
- Printing: ESC/POS via USB, raw TCP 9100, or the Windows spooler ("Generic / Text Only")
- Crash-safe job queue (jobs survive restarts), per-printer retries
- Jinja2 templates with per-organisation overrides from the platform
- Local status page with pairing code and found printers: `http://localhost:8080`
- Windows service, systemd unit, single-file binaries for Windows x64, Linux x64 and arm64

## Install

### Windows (exe + service)

1. Download `openeos-printer-agent-windows-x64.exe` from the
   [latest release](https://github.com/OpenEOS-Project/openeos-printer-agent/releases/latest)
   and put it somewhere permanent, e.g. `C:\Program Files\OpenEOS\`.
2. First look (optional): double-click the EXE or run
   ```
   openeos-printer-agent-windows-x64.exe
   ```
   It connects to the hosted service (`https://api.openeos.de/api`) and shows
   the pairing code in the window. Stop with Ctrl+C. Self-hosting? Add
   `--server-url https://<your-host>/api`.
3. Install as a service - open a **Command Prompt as Administrator**:
   ```
   openeos-printer-agent-windows-x64.exe install-service
   ```
   (again with `--server-url ...` for your own server; the URL in use is
   written to `config.yaml` so the service needs no flags.) The service
   "OpenEOS Printer Agent" starts automatically with Windows. The pairing
   code is in `%ProgramData%\OpenEOS Printer Agent\logs\agent.log` and on
   `http://localhost:8080`.
4. Remove: `openeos-printer-agent-windows-x64.exe remove-service` (as Administrator).

Everything the agent stores lives in `%ProgramData%\OpenEOS Printer Agent`
(`config.yaml`, `device.json` with the token, `print-jobs.db`, `logs\`).

USB printers on Windows are used through the **print spooler**: install the
printer with the vendor driver or with Windows' built-in "Generic / Text Only"
driver, make sure it prints a test page, and the agent will list it as a
`windows` printer. Raw USB via WinUSB is optional and not needed.

### Linux / Raspberry Pi (install.sh)

Either from the source checkout or with a release binary:

```bash
git clone https://github.com/OpenEOS-Project/openeos-printer-agent.git
cd openeos-printer-agent
sudo ./install.sh --server-url https://api.openeos.de/api
# or, without Python on the box:
sudo ./install.sh --server-url https://api.openeos.de/api --binary ./openeos-printer-agent-linux-arm64
```

The script creates the `openeos` user, udev rules for USB receipt printers,
the systemd unit `openeos-printer` (auto-start) and writes
`/opt/openeos-printer-agent/config/config.yaml`. Data: `/var/lib/openeos-printer`,
logs: `/var/log/openeos-printer/agent.log` and `journalctl -u openeos-printer`.

Raspberry Pi: use the **arm64** binary on a 64-bit Raspberry Pi OS (Pi 4/5).
On 32-bit OS or Pi 3 use the source install (`install.sh` without `--binary`).

### Linux binary, no service

```bash
curl -LO https://github.com/OpenEOS-Project/openeos-printer-agent/releases/latest/download/openeos-printer-agent-linux-x64
chmod +x openeos-printer-agent-linux-x64
./openeos-printer-agent-linux-x64                      # hosted service
./openeos-printer-agent-linux-x64 run --server-url https://<your-host>/api   # self-hosted
```

Without root the data goes to `~/.local/share/openeos-printer`.

### pip

```bash
pip install .
openeos-printer-agent run                              # or --server-url https://<your-host>/api
```

### Docker

```bash
docker build -t openeos-printer-agent .
docker run -d --name openeos-printer -p 8080:8080 --network host \
  -v openeos-printer-data:/var/lib/openeos-printer \
  -e OPENEOS_PRINTER_DATA_DIR=/var/lib/openeos-printer \
  openeos-printer-agent run --server-url https://api.openeos.de/api
```

`--network host` is needed for mDNS and the subnet scan; pass `--device` for USB printers.

## Pairing

1. Start the agent. It registers itself (`POST /devices/init`) and prints a
   banner with the **6-digit pairing code** - in the console, in `agent.log`
   and on `http://localhost:8080`.
2. In the OpenEOS web app open **Drucker** > **Drucker-Agent verbinden** and
   enter the code.
3. The agent notices within a few seconds, connects, and runs its first
   printer scan. Found printers appear under the agent in the web app;
   click **Übernehmen** on the ones you want to print on and give them a
   name, type (receipt/kitchen/label) and paper width.

The device token is stored locally (`device.json`); restarts reuse it.
`openeos-printer-agent pair` does only the registration + waiting and exits
once linked - handy before `install-service`.

**Removing or blocking the agent** (web app: **Drucker** > **Drucker-Agent**):

- *Agent entfernen*: the server forgets the token. The running agent notices
  (immediately on the next reconnect, otherwise with its next report to the
  server, at most a few minutes), logs it, deletes the stale `device.json`
  and **automatically shows a new pairing code** - in the log and on
  `http://localhost:8080`. No restart, no file to delete by hand. The same
  happens when the agent starts with a token the server no longer knows.
- *Agent sperren*: the agent does **not** re-pair. It disconnects, shows
  "Gesperrt" on the status page and checks every 60 s whether it was
  unblocked; then it reconnects with the same token.

## Discovery

Runs at start, every 5 minutes, and when you click **Neu suchen** in the web app.

| Source | How | Reported as |
|---|---|---|
| USB | pyusb: USB printer class 7 or known vendor IDs (Epson 04b8, Star 0519, Bixolon 1504, Custom 0dd4, Winbond 0416, GD 28e9, STM 0483, 6868, NXP 1fc9) | `usb`, fingerprint `usb:<vid>:<pid>:<serial|->` |
| Windows spooler | `win32print.EnumPrinters` (local queues; PDF/XPS/OneNote/Fax skipped) | `windows`, fingerprint `win:<printer name>` |
| mDNS | `_pdl-datastream._tcp`, `_printer._tcp`, `_ipp._tcp` | `network`, fingerprint `net:<mac>` (or `net:<ip>`) |
| Port scan | only with "auch Netzwerk durchsuchen": TCP 9100 on the local /24, 300 ms per host, ESC/POS `GS I` model query | `network` |

MAC addresses come from the neighbour table (`ip neigh` / `arp -a`) so a
printer keeps its identity when DHCP hands out a new IP. Printers the scan
cannot see (other subnet, VLAN) can be created manually in the web app with
IP and port.

## Configuration

Nothing is required: without a config file and without `--server-url` the
agent uses the hosted service `https://api.openeos.de/api` and says so in
the log at every start (together with how to change it). Self-hosters set
`server_url`. Full example: `config/config.example.yaml`.

```yaml
server_url: "https://api.openeos.de/api"
logging:
  level: INFO
local_server:
  port: 8080
discovery:
  interval_s: 300
  # subnet: "192.168.1.0/24"   # which /24 to port-scan (default: own interface)
cash_drawer_pin: 2
```

Lookup order: `--config FILE`, `<data dir>/config.yaml`, `./config/config.yaml`,
`/etc/openeos-printer/config.yaml`. Server URL precedence: `--server-url`,
then `server_url` from the file, then the hosted default. `--server-url`
is written to `<data dir>/config.yaml` when no file exists yet, and
`install-service` / `install.sh` always persist the URL in use.

Environment overrides: `OPENEOS_PRINTER_DATA_DIR`, `OPENEOS_PRINTER_LOG_DIR`.

## CLI

```
openeos-printer-agent run [--server-url URL] [--config FILE] [--dummy-printer] [--subnet-scan] [--log-level DEBUG]
openeos-printer-agent pair [--server-url URL]      # register, show code, exit once linked
openeos-printer-agent status [--json]              # ask the running agent (local status API)
openeos-printer-agent install-service [--server-url URL]   # Windows, as Administrator
openeos-printer-agent remove-service                       # Windows, as Administrator
openeos-printer-agent --version
```

`python -m src.main ...` works the same from a checkout.

## Testing without hardware

**Dummy printers** - every adopted printer is backed by an in-memory ESC/POS
buffer; the rendered ticket is written to the log as text:

```bash
openeos-printer-agent run --server-url https://api.staging.openeos.de/api --dummy-printer
```

**Network printer emulator** - `tools/fake_printer.py` listens on TCP 9100,
answers the `GS I` model query (so discovery shows "OPENEOS FAKE-9100") and
logs every job it receives as readable text (standard library only, Python 3.8+):

```bash
python tools/fake_printer.py                        # 0.0.0.0:9100
python tools/fake_printer.py --port 9101 --log-file /tmp/fake-printer.log --model "TM-T88V"
```

Then in the web app: "Neu suchen" with network scan finds it (or create it
manually with the host's IP and port), adopt it, test print - the ticket
shows up in the emulator's output.

## Status page

`http://<agent-ip>:8080` shows connection state, adopted printers and their
status, queue counters, the last discovery result and system metrics.
JSON: `/api/status`, `/api/printers`, `/api/discovery`, `/api/jobs`, `/health`.

## Troubleshooting

**USB printer not found (Linux)**
```bash
lsusb                                   # is it there? note the ID xxxx:yyyy
ls -l /dev/bus/usb/*/*                  # group should be plugdev after install.sh
sudo udevadm control --reload-rules && sudo udevadm trigger
sudo journalctl -u openeos-printer -n 50
```
If the vendor ID is not in `/etc/udev/rules.d/99-openeos-printer.rules`, add a
line for it (same pattern) and reload. Without the rule the agent still lists
the printer (IDs only, no serial) but cannot print to it.

**USB printer not found (Windows)** - the agent uses the spooler. Install the
printer in Windows (vendor driver or "Generic / Text Only" on the USB port),
print a Windows test page, then "Neu suchen". Virtual printers are hidden.

**Network printer not found** - mDNS needs UDP 5353 (multicast) and the port
scan needs outgoing TCP 9100 to the LAN; both can be blocked by a firewall or
by a VPN that owns the default route. Set `discovery.subnet` to the printer's
/24 or create the printer manually by IP. The printer must allow raw printing
on port 9100 (sometimes called "RAW" or "JetDirect").

**Agent shows as offline** - check `agent.log` / `journalctl`, the status page,
and that `server_url` includes `/api` (the log's first lines say which server
is used and why). The agent reconnects by itself; the device token is in
`device.json` in the data directory. To pair again with a new code just
remove the agent in the web app - the agent notices and shows a new code
on its own. A blocked agent waits until it is unblocked.

**Windows service does not start** - run the exe with `run` in a console to
see the error; `install-service` must be run as Administrator; logs in
`%ProgramData%\OpenEOS Printer Agent\logs`.

## Development

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
TZ=UTC pytest                 # unit + integration tests (fake API in tests/fake_api.py)
ruff check . && ruff format --check . && mypy src
pip install -r requirements-build.txt && pyinstaller openeos-printer-agent.spec   # dist/openeos-printer-agent
```

Releases: push a tag `v*` - the workflow builds the three binaries, adds
`SHA256SUMS` and attaches them to a GitHub release. The version lives in
`src/version.py` (`pyproject.toml` reads it).

## Architecture

```
OpenEOS API  <--REST (x-device-token)-->  DeviceApiClient   GET printers/templates, POST printers/discovered
             <--Socket.io-------------->  WebSocketClient   printerJob / printerConfigUpdate / printerDiscover
                                                             agentHeartbeat / printerJobComplete|Failed
                                               |
   DiscoveryService (usb, spooler, mdns, portscan, neighbours)
   PrinterManager (escpos Usb | Network | Win32Raw | Dummy)  <-  JobQueue + JobStore (SQLite)  <-  TemplateEngine
   LocalServer :8080
```

## Supported printers

Any ESC/POS printer. Tested models include Epson TM-T20/T88/m30, Star
TSP143/TSP654, Bixolon SRP-350/380, and common 58 mm no-name USB printers.

## License

AGPL-3.0-or-later - see `LICENSE` and `NOTICE`.
