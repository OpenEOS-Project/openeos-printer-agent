#!/usr/bin/env bash
set -euo pipefail

# ==============================================
#  OpenEOS Station Installer
#  Sets up: Printer Agent + Kiosk Display
#  For Raspberry Pi (Debian/Raspberry Pi OS)
# ==============================================

INSTALL_DIR="/opt/openeos-printer-agent"
PRINTER_SERVICE="openeos-printer"
KIOSK_SERVICE="openeos-kiosk"
SERVICE_USER="openeos"
LOG_DIR="/var/log/openeos-printer"

echo ""
echo "============================================"
echo "  OpenEOS Station - Full Installation"
echo "  Printer Agent + Kiosk Display"
echo "============================================"
echo ""

# Check root
if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: This script must be run as root (sudo)."
    exit 1
fi

# Detect Raspberry Pi
IS_RPI=false
if grep -q "Raspberry Pi\|BCM2" /proc/cpuinfo 2>/dev/null; then
    IS_RPI=true
    echo "  Detected: Raspberry Pi"
else
    echo "  Detected: Generic Linux"
fi
echo ""

# ──────────────────────────────────────────────
# PART 1: Printer Agent
# ──────────────────────────────────────────────

echo "━━━ PART 1: Printer Agent ━━━"
echo ""

# 1. Install system dependencies
echo "[1/9] Installing system dependencies..."
apt-get update -qq
apt-get install -y -qq \
    python3 python3-venv python3-pip \
    libusb-1.0-0-dev libjpeg-dev \
    chromium-browser \
    unclutter \
    xdotool

# 2. Create system user
echo "[2/9] Creating system user..."
if ! id -u "$SERVICE_USER" &>/dev/null; then
    useradd --system --create-home --home-dir "/home/$SERVICE_USER" --shell /bin/bash "$SERVICE_USER"
    echo "  Created user: $SERVICE_USER"
else
    echo "  User $SERVICE_USER already exists"
fi
usermod -aG lp,plugdev,video,audio "$SERVICE_USER" 2>/dev/null || true

# 3. Copy printer agent files
echo "[3/9] Installing printer agent to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
cp -r src config requirements.txt kiosk.sh "$INSTALL_DIR/"
chmod +x "$INSTALL_DIR/kiosk.sh"
mkdir -p "$LOG_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR" "$LOG_DIR"

# 4. Python virtual environment
echo "[4/9] Setting up Python virtual environment..."
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"

# 5. udev rules for USB thermal printers
echo "[5/9] Installing udev rules for USB printers..."
cat > /etc/udev/rules.d/99-openeos-printer.rules << 'UDEV'
# Epson TM series
SUBSYSTEM=="usb", ATTR{idVendor}=="04b8", MODE="0666", GROUP="plugdev"
# Star Micronics
SUBSYSTEM=="usb", ATTR{idVendor}=="0519", MODE="0666", GROUP="plugdev"
# Bixolon
SUBSYSTEM=="usb", ATTR{idVendor}=="1504", MODE="0666", GROUP="plugdev"
# Custom (Italy)
SUBSYSTEM=="usb", ATTR{idVendor}=="0dd4", MODE="0666", GROUP="plugdev"
UDEV
udevadm control --reload-rules
udevadm trigger

# 6. Install printer agent systemd service
echo "[6/9] Installing printer agent service..."
cp openeos-printer.service /etc/systemd/system/${PRINTER_SERVICE}.service
systemctl daemon-reload
systemctl enable "$PRINTER_SERVICE"

# ──────────────────────────────────────────────
# PART 2: Kiosk Display
# ──────────────────────────────────────────────

echo ""
echo "━━━ PART 2: Kiosk Display ━━━"
echo ""

# 7. Install kiosk systemd service
echo "[7/9] Installing kiosk display service..."
cp openeos-kiosk.service /etc/systemd/system/${KIOSK_SERVICE}.service
systemctl daemon-reload
systemctl enable "$KIOSK_SERVICE"

# 8. RPi-specific: Disable screen blanking, configure boot
echo "[8/9] Configuring display settings..."
if $IS_RPI; then
    # Disable screen blanking in boot config
    if [ -f /boot/firmware/cmdline.txt ]; then
        CMDLINE_FILE="/boot/firmware/cmdline.txt"
    elif [ -f /boot/cmdline.txt ]; then
        CMDLINE_FILE="/boot/cmdline.txt"
    else
        CMDLINE_FILE=""
    fi

    if [ -n "$CMDLINE_FILE" ]; then
        if ! grep -q "consoleblank=0" "$CMDLINE_FILE"; then
            sed -i 's/$/ consoleblank=0/' "$CMDLINE_FILE"
            echo "  Disabled console blanking"
        fi
    fi

    # Disable screen saver via lightdm config
    LIGHTDM_CONF="/etc/lightdm/lightdm.conf.d/50-openeos.conf"
    mkdir -p "$(dirname "$LIGHTDM_CONF")"
    cat > "$LIGHTDM_CONF" << 'LIGHTDM'
[Seat:*]
xserver-command=X -s 0 -dpms -nocursor
LIGHTDM

    # Auto-login the openeos user (needed for kiosk)
    cat > /etc/lightdm/lightdm.conf.d/51-openeos-autologin.conf << AUTOLOGIN
[Seat:*]
autologin-user=$SERVICE_USER
autologin-user-timeout=0
AUTOLOGIN
    echo "  Configured auto-login for $SERVICE_USER"

    # Set default audio output
    if command -v amixer &>/dev/null; then
        amixer set Master 80% 2>/dev/null || true
    fi
fi

# 9. Create default config if not exists
echo "[9/9] Checking configuration..."
if [ ! -f "$INSTALL_DIR/config/config.yaml" ]; then
    cp "$INSTALL_DIR/config/config.example.yaml" "$INSTALL_DIR/config/config.yaml"
    chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/config/config.yaml"
    echo "  Created default config - MUST be edited before use"
fi

# ──────────────────────────────────────────────
# Done
# ──────────────────────────────────────────────

echo ""
echo "============================================"
echo "  Installation complete!"
echo "============================================"
echo ""
echo "  Services installed:"
echo "    - $PRINTER_SERVICE (Drucker-Agent)"
echo "    - $KIOSK_SERVICE   (Standortanzeige)"
echo ""
echo "  Next steps:"
echo ""
echo "  1. Edit the configuration:"
echo "     sudo nano $INSTALL_DIR/config/config.yaml"
echo ""
echo "     Set at minimum:"
echo "       - server_url         (API server address incl. /api)"
echo "     Then pair the agent with the 6-digit code from the log / :8080"
echo "       - display_url        (web app station URL)"
echo "       - printers           (your printer settings)"
echo ""
echo "  2. Start the services:"
echo "     sudo systemctl start $PRINTER_SERVICE"
echo "     sudo systemctl start $KIOSK_SERVICE"
echo ""
echo "  3. Check status:"
echo "     sudo systemctl status $PRINTER_SERVICE"
echo "     sudo systemctl status $KIOSK_SERVICE"
echo ""
echo "  4. View logs:"
echo "     sudo journalctl -u $PRINTER_SERVICE -f"
echo "     sudo journalctl -u $KIOSK_SERVICE -f"
echo ""
echo "  5. Printer agent status page:"
echo "     http://$(hostname -I | awk '{print $1}'):8080"
echo ""
if $IS_RPI; then
echo "  6. Reboot to activate auto-login + kiosk:"
echo "     sudo reboot"
echo ""
fi
