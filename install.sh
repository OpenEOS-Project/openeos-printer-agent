#!/usr/bin/env bash
# OpenEOS Printer Agent - Linux installer (Debian/Ubuntu/Raspberry Pi OS).
#
#   sudo ./install.sh --server-url https://api.openeos.de/api            # from this checkout
#   sudo ./install.sh --server-url ... --binary ./openeos-printer-agent-linux-arm64
#
# Installs to /opt/openeos-printer-agent, creates the service user, udev
# rules for USB receipt printers and the systemd unit "openeos-printer".
# Printers are NOT configured here - the agent finds them and you adopt them
# in the web app.
set -euo pipefail

INSTALL_DIR="/opt/openeos-printer-agent"
SERVICE_NAME="openeos-printer"
SERVICE_USER="openeos"
LOG_DIR="/var/log/openeos-printer"
DATA_DIR="/var/lib/openeos-printer"
SERVER_URL=""
BINARY=""

usage() {
    echo "Usage: sudo $0 --server-url URL [--binary FILE]"
    echo "  --server-url URL   OpenEOS API base URL incl. /api (e.g. https://api.openeos.de/api)"
    echo "  --binary FILE      install a release binary instead of the Python sources"
    exit 1
}

while [ $# -gt 0 ]; do
    case "$1" in
        --server-url) SERVER_URL="$2"; shift 2 ;;
        --binary) BINARY="$2"; shift 2 ;;
        -h|--help) usage ;;
        *) echo "Unknown argument: $1"; usage ;;
    esac
done

echo "============================================"
echo "  OpenEOS Printer Agent - Installation"
echo "============================================"
echo ""

# Check root
if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: This script must be run as root (sudo)."
    exit 1
fi

if [ -z "$SERVER_URL" ]; then
    read -r -p "OpenEOS API URL (incl. /api) [https://api.openeos.de/api]: " SERVER_URL
    SERVER_URL="${SERVER_URL:-https://api.openeos.de/api}"
fi

# 1. Install system dependencies
echo "[1/7] Installing system dependencies..."
apt-get update -qq
if [ -n "$BINARY" ]; then
    apt-get install -y -qq libusb-1.0-0 iproute2
else
    apt-get install -y -qq python3 python3-venv python3-pip libusb-1.0-0-dev libjpeg-dev iproute2
fi

# 2. Create system user
echo "[2/7] Creating system user..."
if ! id -u "$SERVICE_USER" &>/dev/null; then
    useradd --system --home-dir "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER"
    echo "  Created user: $SERVICE_USER"
else
    echo "  User $SERVICE_USER already exists"
fi
usermod -aG lp,plugdev "$SERVICE_USER" 2>/dev/null || true

# 3. Copy files
echo "[3/7] Installing to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR/config" "$LOG_DIR" "$DATA_DIR"
if [ -n "$BINARY" ]; then
    install -m 0755 "$BINARY" "$INSTALL_DIR/openeos-printer-agent"
else
    cp -r src requirements.txt "$INSTALL_DIR/"
fi
if [ ! -f "$INSTALL_DIR/config/config.yaml" ]; then
    cat > "$INSTALL_DIR/config/config.yaml" <<YAML
# OpenEOS Printer Agent
# Base URL of the OpenEOS API (including the /api prefix).
server_url: "$SERVER_URL"
YAML
    echo "  Wrote $INSTALL_DIR/config/config.yaml"
else
    echo "  Keeping existing $INSTALL_DIR/config/config.yaml"
fi
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR" "$LOG_DIR" "$DATA_DIR"

# 4. Python virtual environment (source install only)
if [ -z "$BINARY" ]; then
    echo "[4/7] Setting up Python virtual environment..."
    python3 -m venv "$INSTALL_DIR/venv"
    "$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
    "$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"
    chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/venv"
else
    echo "[4/7] Binary install - no Python environment needed"
fi

# 5. udev rules for USB thermal printers (vendor IDs the discovery knows)
echo "[5/7] Installing udev rules..."
cat > /etc/udev/rules.d/99-openeos-printer.rules << 'UDEV'
# OpenEOS Printer Agent - USB receipt printers (group plugdev, rw)
# Epson
SUBSYSTEM=="usb", ATTR{idVendor}=="04b8", MODE="0664", GROUP="plugdev"
# Star Micronics
SUBSYSTEM=="usb", ATTR{idVendor}=="0519", MODE="0664", GROUP="plugdev"
# Bixolon
SUBSYSTEM=="usb", ATTR{idVendor}=="1504", MODE="0664", GROUP="plugdev"
# Custom (Italy)
SUBSYSTEM=="usb", ATTR{idVendor}=="0dd4", MODE="0664", GROUP="plugdev"
# Winbond (many no-name China printers)
SUBSYSTEM=="usb", ATTR{idVendor}=="0416", MODE="0664", GROUP="plugdev"
# GD Microelectronics
SUBSYSTEM=="usb", ATTR{idVendor}=="28e9", MODE="0664", GROUP="plugdev"
# STMicroelectronics
SUBSYSTEM=="usb", ATTR{idVendor}=="0483", MODE="0664", GROUP="plugdev"
# Generic 6868
SUBSYSTEM=="usb", ATTR{idVendor}=="6868", MODE="0664", GROUP="plugdev"
# NXP
SUBSYSTEM=="usb", ATTR{idVendor}=="1fc9", MODE="0664", GROUP="plugdev"
# Any device announcing the USB printer class (7)
SUBSYSTEM=="usb", ENV{ID_USB_INTERFACES}=="*:0701*:*", MODE="0664", GROUP="plugdev"
UDEV
udevadm control --reload-rules
udevadm trigger

# 6. Install systemd service
echo "[6/7] Installing systemd service..."
if [ -n "$BINARY" ]; then
    # Unit from the repo is for the source install; swap the ExecStart.
    sed "s#^ExecStart=.*#ExecStart=$INSTALL_DIR/openeos-printer-agent run --config $INSTALL_DIR/config/config.yaml#" \
        "$(dirname "$0")/openeos-printer.service" > /etc/systemd/system/${SERVICE_NAME}.service 2>/dev/null \
        || curl -fsSL "https://raw.githubusercontent.com/OpenEOS-Project/openeos-printer-agent/main/openeos-printer.service" \
        | sed "s#^ExecStart=.*#ExecStart=$INSTALL_DIR/openeos-printer-agent run --config $INSTALL_DIR/config/config.yaml#" \
        > /etc/systemd/system/${SERVICE_NAME}.service
else
    cp openeos-printer.service /etc/systemd/system/${SERVICE_NAME}.service
fi
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

# 7. Done
echo "[7/7] Installation complete!"
echo ""
echo "============================================"
echo "  Next steps:"
echo "============================================"
echo ""
echo "  1. Read the pairing code:"
echo "     sudo journalctl -u $SERVICE_NAME -f"
echo "     or open http://$(hostname -I | awk '{print $1}'):8080"
echo ""
echo "  2. In the OpenEOS web app go to Drucker > 'Drucker-Agent verbinden'"
echo "     and enter the code. Found printers appear there - adopt the ones"
echo "     you want to print on."
echo ""
echo "  3. Check status:"
echo "     sudo systemctl status $SERVICE_NAME"
echo ""
