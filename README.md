# OpenEOS Printer Agent

Python-based agent that runs on a Raspberry Pi (or any Linux machine) to control ESC/POS thermal printers via WebSocket communication with the OpenEOS backend.

## Features

- Multi-printer support (USB, Network, Bluetooth)
- Real-time print job reception via Socket.io
- Jinja2 template engine with server-side template override
- Per-printer job queues with retry logic
- Local status web server (http://localhost:8080)
- Heartbeat monitoring with system metrics
- Systemd service with security hardening

## Requirements

- Python 3.11+
- Raspberry Pi OS / Debian / Ubuntu
- ESC/POS compatible thermal printer

## Quick Start

### Raspberry Pi Installation

```bash
# Clone the repository
git clone https://github.com/OpenEOS-Project/openeos-printer-agent.git
cd openeos-printer-agent

# Run the installer (as root)
sudo ./install.sh

# Configure
sudo cp /opt/openeos-printer-agent/config/config.example.yaml \
        /opt/openeos-printer-agent/config/config.yaml
sudo nano /opt/openeos-printer-agent/config/config.yaml

# Start
sudo systemctl start openeos-printer
sudo systemctl status openeos-printer
```

### Docker

```bash
docker build -t openeos-printer-agent .
docker run -d \
  --name openeos-printer \
  -v ./config/config.yaml:/app/config/config.yaml:ro \
  -p 8080:8080 \
  --device /dev/usb/lp0 \
  openeos-printer-agent
```

### Development

```bash
# Create virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements-dev.txt

# Copy and edit config
cp config/config.example.yaml config/config.yaml

# Run
python -m src.main --config config/config.yaml

# Run tests
pytest tests/
```

## Configuration

See `config/config.example.yaml` for all options. Key settings:

| Setting | Description |
|---------|-------------|
| `server.url` | OpenEOS API server URL |
| `server.device_token` | Device authentication token (from admin panel). Optional — leave unset to use self-registration instead. |
| `printers[].connection_type` | `usb`, `network`, or `bluetooth` |
| `printers[].paper_width` | `80` (80mm) or `58` (58mm) |

## Self-Registration

If `server.device_token` is not configured, the agent registers itself with the backend instead of requiring a pre-provisioned token:

1. On startup, the agent checks for a token in `server.device_token`, then in the persistent token file (`device_token_file`, default `/var/lib/openeos-printer/device.json`).
2. If neither is found, it calls `POST /devices/init` with a suggested name and `deviceType: "printer_agent"`, and receives a device token plus a 6-digit **verification code**.
3. The verification code is shown in the agent logs (and on the local status page) and the token is persisted to the token file for future restarts.
4. An organization admin enters the verification code in the OpenEOS dashboard to approve the device and assign it to their organization.
5. The agent polls `GET /devices/status` until the device is verified, then connects via WebSocket and becomes available for selection when adding a printer under **Drucker** in the dashboard.

## Architecture

```
┌─────────────────────────────────────────────┐
│            OpenEOS Backend (NestJS)          │
│    Socket.io  ──────────────────────────     │
└─────────┬──────────────────────┬────────────┘
          │ printerJob           │ printerHeartbeat
          │ printerJobComplete   │ printerJobFailed
          ▼                      ▲
┌─────────────────────────────────────────────┐
│          Printer Agent (Python)              │
│                                              │
│  WebSocketClient ──► JobQueue ──► Renderer   │
│       │                  │           │       │
│  SystemMonitor     TemplateEngine  ESC/POS   │
│       │                              │       │
│  LocalServer                     Printer(s)  │
└─────────────────────────────────────────────┘
```

## Status Page

Access `http://<agent-ip>:8080` for a real-time status page showing:
- Server connection status
- Printer statuses with color indicators
- Queue statistics
- System metrics (CPU, memory, disk, network)

## Troubleshooting

### Printer not detected (USB)

```bash
# Check USB devices
lsusb | grep -i epson

# Check permissions
ls -la /dev/usb/lp*

# Reload udev rules
sudo udevadm control --reload-rules && sudo udevadm trigger
```

### Service won't start

```bash
# Check logs
sudo journalctl -u openeos-printer -n 50

# Test config
python -m src.main --config config/config.yaml
```

### Connection issues

- Verify `device_token` in config matches the token in the OpenEOS admin panel
- Check network connectivity to the API server
- Check the status page at http://localhost:8080

## Supported Printers

| Brand | Models | Connection |
|-------|--------|-----------|
| Epson | TM-T20III, TM-T88V/VI, TM-M30 | USB, Network |
| Star | TSP143III, TSP654II | USB, Network |
| Bixolon | SRP-350III, SRP-380 | USB, Network |

## License

Copyright (C) 2026 LUXCODE by Lukas Lukic-Spitznagel and contributors.

Licensed under the GNU Affero General Public License v3.0 or (at your option) any later version (`AGPL-3.0-or-later`), see [LICENSE](LICENSE) and [NOTICE](NOTICE). Earlier versions declared the MIT License in `pyproject.toml`; see NOTICE.

## Security

Please report vulnerabilities privately via GitHub (**Security → Report a vulnerability**) or as described in the [organization security policy](https://github.com/OpenEOS-Project/.github/blob/main/SECURITY.md).

