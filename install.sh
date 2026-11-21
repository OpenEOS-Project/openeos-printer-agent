#!/usr/bin/env bash
# OpenEOS Printer Agent - Linux installer (Debian/Ubuntu/Raspberry Pi OS).
#
#   sudo ./install.sh --server-url https://api.openeos.de/api            # from this checkout
#   sudo ./install.sh --server-url ... --binary ./openeos-printer-agent-linux-arm64
#   sudo ./install.sh --binary ./openeos-printer-agent-linux-x64 --ui-binary ./openeos-printer-agent-ui-linux-x64
#
# Installs to /opt/openeos-printer-agent, creates the service user, udev
# rules for USB receipt printers and the systemd unit "openeos-printer".
# Printers are NOT configured here - the agent finds them and you adopt them
# in the web app.
#
# On a machine with a desktop (laptop, Pi with desktop) it also installs the
# window + tray icon (menu entry and autostart for every user), lets the
# service keep the machine awake while connected (logind inhibitor) and lets
# logged-in users restart the service from the window. Headless machines
# (Raspberry Pi OS Lite, servers) get exactly the service as before;
# --no-ui forces that, --ui forces the desktop parts.
set -euo pipefail

INSTALL_DIR="/opt/openeos-printer-agent"
SERVICE_NAME="openeos-printer"
SERVICE_USER="openeos"
LOG_DIR="/var/log/openeos-printer"
DATA_DIR="/var/lib/openeos-printer"
SERVER_URL=""
BINARY=""
UI_BINARY=""
UI_MODE="auto"   # auto | yes | no

usage() {
    echo "Usage: sudo $0 --server-url URL [--binary FILE]"
    echo "  --server-url URL   OpenEOS API base URL incl. /api (e.g. https://api.openeos.de/api)"
    echo "  --binary FILE      install a release binary instead of the Python sources"
    echo "  --ui-binary FILE   window/tray binary (default: openeos-printer-agent-ui-linux-<arch> next to --binary)"
    echo "  --ui | --no-ui     force / skip the desktop parts (default: only when a desktop is installed)"
    exit 1
}

while [ $# -gt 0 ]; do
    case "$1" in
        --server-url) SERVER_URL="$2"; shift 2 ;;
        --binary) BINARY="$2"; shift 2 ;;
        --ui-binary) UI_BINARY="$2"; shift 2 ;;
        --ui) UI_MODE="yes"; shift ;;
        --no-ui) UI_MODE="no"; shift ;;
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

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RAW_URL="https://raw.githubusercontent.com/OpenEOS-Project/openeos-printer-agent/main"

has_desktop() {
    # A display manager / graphical target or installed X/Wayland sessions.
    [ "$(systemctl get-default 2>/dev/null)" = "graphical.target" ] && return 0
    for session in /usr/share/xsessions/*.desktop /usr/share/wayland-sessions/*.desktop; do
        [ -e "$session" ] && return 0
    done
    return 1
}

WITH_UI=0
case "$UI_MODE" in
    yes) WITH_UI=1 ;;
    auto) if has_desktop; then WITH_UI=1; fi ;;
esac
if [ "$WITH_UI" = 1 ] && [ -n "$BINARY" ] && [ -z "$UI_BINARY" ]; then
    case "$(uname -m)" in
        x86_64) UI_ARCH=x64 ;;
        aarch64) UI_ARCH=arm64 ;;
        *) UI_ARCH="" ;;
    esac
    CANDIDATE="$(dirname "$BINARY")/openeos-printer-agent-ui-linux-$UI_ARCH"
    if [ -n "$UI_ARCH" ] && [ -f "$CANDIDATE" ]; then
        UI_BINARY="$CANDIDATE"
    else
        echo "Note: desktop found, but no window binary ($CANDIDATE)."
        echo "      Download openeos-printer-agent-ui-linux-$UI_ARCH next to the agent binary"
        echo "      or pass --ui-binary FILE to get the window and tray icon. Continuing without."
        WITH_UI=0
    fi
fi

# 1. Install system dependencies
echo "[1/7] Installing system dependencies..."
apt-get update -qq
if [ -n "$BINARY" ]; then
    apt-get install -y -qq libusb-1.0-0 iproute2
    if [ "$WITH_UI" = 1 ]; then
        # The window bundles Qt; EGL/GL come from the system's graphics stack.
        apt-get install -y -qq libegl1 libgl1 || true
    fi
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
if [ "$WITH_UI" = 1 ] && ! grep -q "^power:" "$INSTALL_DIR/config/config.yaml"; then
    # Laptop/desktop: do not fall asleep while connected (see README, limits).
    printf '\n# Keep the computer awake while connected (logind sleep inhibitor)\npower:\n  keep_awake: true\n' \
        >> "$INSTALL_DIR/config/config.yaml"
    echo "  Enabled power.keep_awake"
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

# 6b. Desktop: window + tray, menu entry, autostart, permissions
if [ "$WITH_UI" = 1 ]; then
    echo "[6b] Installing the window and tray icon..."
    if [ -n "$UI_BINARY" ]; then
        install -m 0755 "$UI_BINARY" "$INSTALL_DIR/openeos-printer-agent-ui"
    else
        "$INSTALL_DIR/venv/bin/pip" install --quiet -r "$SCRIPT_DIR/requirements-ui.txt"
        cat > "$INSTALL_DIR/openeos-printer-agent-ui" <<SH
#!/bin/sh
cd "$INSTALL_DIR" && exec "$INSTALL_DIR/venv/bin/python" -m src.ui "\$@"
SH
        chmod 0755 "$INSTALL_DIR/openeos-printer-agent-ui"
    fi
    ICON_DIR=/usr/share/icons/hicolor/256x256/apps
    mkdir -p "$ICON_DIR"
    if [ -f "$SCRIPT_DIR/packaging/icons/openeos-printer-agent.png" ]; then
        install -m 0644 "$SCRIPT_DIR/packaging/icons/openeos-printer-agent.png" "$ICON_DIR/openeos-printer-agent.png"
    else
        curl -fsSL "$RAW_URL/packaging/icons/openeos-printer-agent.png" -o "$ICON_DIR/openeos-printer-agent.png" || true
    fi
    gtk-update-icon-cache -q /usr/share/icons/hicolor 2>/dev/null || true
    mkdir -p /usr/share/applications
    cat > /usr/share/applications/openeos-printer-agent.desktop <<DESKTOP
[Desktop Entry]
Type=Application
Name=OpenEOS Printer Agent
Name[de]=OpenEOS Drucker-Agent
Comment=Connection, printers and pairing code of the OpenEOS printer agent
Comment[de]=Verbindung, Drucker und Kopplungscode des OpenEOS Drucker-Agents
Exec=$INSTALL_DIR/openeos-printer-agent-ui
Icon=openeos-printer-agent
Terminal=false
Categories=Office;Utility;
StartupWMClass=openeos-printer-agent-ui
DESKTOP
    # Every user's session starts the window minimised to the tray.
    mkdir -p /etc/xdg/autostart
    sed -e "s#^Exec=.*#Exec=$INSTALL_DIR/openeos-printer-agent-ui --minimized#" \
        /usr/share/applications/openeos-printer-agent.desktop > /etc/xdg/autostart/openeos-printer-agent-ui.desktop
    echo "X-GNOME-Autostart-enabled=true" >> /etc/xdg/autostart/openeos-printer-agent-ui.desktop

    # polkit: the service user may hold a sleep inhibitor (keep-awake);
    # logged-in local users may start/stop/restart only this service.
    mkdir -p /etc/polkit-1/rules.d
    cat > /etc/polkit-1/rules.d/50-openeos-printer.rules <<'RULES'
// OpenEOS Printer Agent (install.sh)
polkit.addRule(function(action, subject) {
    if ((action.id == "org.freedesktop.login1.inhibit-block-sleep" ||
         action.id == "org.freedesktop.login1.inhibit-block-idle") &&
        subject.user == "openeos") {
        return polkit.Result.YES;
    }
    if (action.id == "org.freedesktop.systemd1.manage-units" &&
        action.lookup("unit") == "openeos-printer.service" &&
        subject.local && subject.active) {
        return polkit.Result.YES;
    }
});
RULES
    # Older polkit (0.105, Ubuntu 22.04) only reads .pkla files.
    if [ -d /etc/polkit-1/localauthority ]; then
        mkdir -p /etc/polkit-1/localauthority/50-local.d
        cat > /etc/polkit-1/localauthority/50-local.d/50-openeos-printer.pkla <<'PKLA'
[OpenEOS Printer Agent: keep the computer awake while connected]
Identity=unix-user:openeos
Action=org.freedesktop.login1.inhibit-block-sleep;org.freedesktop.login1.inhibit-block-idle
ResultAny=yes
ResultInactive=yes
ResultActive=yes
PKLA
    fi
    systemctl restart "$SERVICE_NAME"
    echo "  Menu entry 'OpenEOS Drucker-Agent', starts with every desktop login"
fi

# 7. Done
echo "[7/7] Installation complete!"
echo ""
echo "============================================"
echo "  Next steps:"
echo "============================================"
echo ""
echo "  1. Read the pairing code:"
if [ "$WITH_UI" = 1 ]; then
echo "     open 'OpenEOS Drucker-Agent' from the menu (or log in again), or"
fi
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
