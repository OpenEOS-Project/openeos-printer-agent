#!/usr/bin/env bash
# Build the Linux release binaries in containers with an old glibc, so they
# run on older distributions. Binaries built directly on ubuntu-24.04
# (glibc 2.39) fail on Ubuntu 22.04 with "GLIBC_2.38 not found".
#
#   packaging/linux/build-binaries.sh [--no-ui] [--out DIR]
#
# Builds for the host architecture (x86_64 or aarch64); run it on an arm64
# machine/runner for the arm64 binaries - no QEMU involved.
#
#   service  openeos-printer-agent-linux-<arch>
#            Debian 11 container (glibc 2.31): runs on Debian 11/12,
#            Ubuntu 22.04+, Raspberry Pi OS Bullseye/Bookworm (64-bit).
#   window   openeos-printer-agent-ui-linux-<arch>        (unless --no-ui)
#            Ubuntu 22.04 container (glibc 2.35): runs on Ubuntu 22.04+,
#            Debian 12+, Raspberry Pi OS Bookworm. Built on a supported
#            release because the X/font libraries Qt needs are bundled.
#
# Python comes from python-build-standalone (via uv), not from the distro:
# it is built for glibc 2.17 and ships a current OpenSSL, so the binaries
# carry a maintained TLS stack even though Debian 11 itself is end of life.
set -euo pipefail

UV_IMAGE="${UV_IMAGE:-ghcr.io/astral-sh/uv:0.13.6}"
PYTHON_VERSION="${PYTHON_VERSION:-3.12}"
SERVICE_IMAGE="${SERVICE_IMAGE:-debian:bullseye}"
UI_IMAGE="${UI_IMAGE:-ubuntu:22.04}"
OUT="dist-linux"
WITH_UI=1
while [ $# -gt 0 ]; do
    case "$1" in
        --no-ui) WITH_UI=0; shift ;;
        --out) OUT="$2"; shift 2 ;;
        *) echo "Unknown argument: $1" >&2; exit 2 ;;
    esac
done

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
case "$OUT" in /*) ;; *) OUT="$ROOT/$OUT" ;; esac
case "$(uname -m)" in
    x86_64|amd64) ARCH=x64 ;;
    aarch64|arm64) ARCH=arm64 ;;
    *) echo "Unsupported architecture $(uname -m)" >&2; exit 2 ;;
esac
mkdir -p "$OUT"

# uv (static binary) out of its official image
UVDIR="$(mktemp -d)"
trap 'rm -rf "$UVDIR"' EXIT
cid="$(docker create "$UV_IMAGE")"
docker cp "$cid:/uv" "$UVDIR/uv" >/dev/null
docker rm "$cid" >/dev/null

# Shared build steps; $1 = spec file, $2 = extra requirements file or "".
BUILD='
set -euo pipefail
export UV_PYTHON_INSTALL_DIR=/opt/python UV_LINK_MODE=copy
/opt/uv/uv venv -q --python "$PYTHON_VERSION" /venv
. /venv/bin/activate
cp -r /src /build && cd /build && rm -rf .venv build dist
/opt/uv/uv pip install -q -r requirements.txt -r requirements-build.txt ${EXTRA_REQ:+-r "$EXTRA_REQ"}
/opt/uv/uv pip install -q --no-deps .
python -c "import ssl, sys; print(sys.version.split()[0], ssl.OPENSSL_VERSION)"
pyinstaller --clean --noconfirm --log-level ERROR "$SPEC"
'
RUN=(docker run --rm -e PYTHON_VERSION="$PYTHON_VERSION" -e ARCH="$ARCH"
     -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)"
     -v "$ROOT":/src:ro -v "$OUT":/out -v "$UVDIR":/opt/uv:ro)

echo "==> service binary ($SERVICE_IMAGE)"
"${RUN[@]}" -e SPEC=openeos-printer-agent.spec -e EXTRA_REQ= "$SERVICE_IMAGE" bash -c '
    set -euo pipefail
    export DEBIAN_FRONTEND=noninteractive
    if grep -q bullseye /etc/os-release; then
        # Debian 11 is end of life; its packages live on archive.debian.org.
        printf "deb http://archive.debian.org/debian bullseye main\ndeb http://archive.debian.org/debian-security bullseye-security main\n" > /etc/apt/sources.list
    fi
    apt-get -o Acquire::Check-Valid-Until=false update -qq
    # objdump for PyInstaller; libusb is loaded from the target system.
    apt-get install -y -qq --no-install-recommends binutils ca-certificates >/dev/null
    '"$BUILD"'
    ./dist/openeos-printer-agent --version
    cp dist/openeos-printer-agent "/out/openeos-printer-agent-linux-$ARCH"
    chown "$HOST_UID:$HOST_GID" "/out/openeos-printer-agent-linux-$ARCH"
'

if [ "$WITH_UI" = 1 ]; then
    echo "==> window binary ($UI_IMAGE)"
    "${RUN[@]}" -e SPEC=openeos-printer-agent-ui.spec -e EXTRA_REQ=requirements-ui.txt "$UI_IMAGE" bash -c '
        set -euo pipefail
        export DEBIAN_FRONTEND=noninteractive
        apt-get update -qq
        # Qt platform plugins (xcb, wayland) and what they link against get
        # bundled; libxcb-cursor0 in particular is missing on many desktops.
        apt-get install -y -qq --no-install-recommends binutils ca-certificates \
            libegl1 libgl1 libglib2.0-0 libfontconfig1 libdbus-1-3 libxkbcommon0 libxkbcommon-x11-0 \
            libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 \
            libxcb-render-util0 libxcb-shape0 libxcb-xfixes0 libxcb-xinerama0 libxcb-xkb1 \
            libwayland-client0 libwayland-cursor0 libwayland-egl1 >/dev/null
        '"$BUILD"'
        QT_QPA_PLATFORM=offscreen ./dist/openeos-printer-agent-ui --version
        cp dist/openeos-printer-agent-ui "/out/openeos-printer-agent-ui-linux-$ARCH"
        chown "$HOST_UID:$HOST_GID" "/out/openeos-printer-agent-ui-linux-$ARCH"
    '
fi

ls -la "$OUT"
