#!/usr/bin/env bash
# One-shot setup for a fresh Raspberry Pi OS (Bookworm/Trixie, 64-bit) install.
# Run from inside a clone of this repo, as the normal user (not root):
#   git clone <repo-url> ~/.local/share/camera-stream
#   cd ~/.local/share/camera-stream
#   ./install.sh
#
# Idempotent - safe to re-run after a fresh SD card flash + git clone.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_DIR="$HOME/.config/camera-stream"
UNIT_DIR="$HOME/.config/systemd/user"
BOOT_CONFIG="/boot/firmware/config.txt"
[ -f "$BOOT_CONFIG" ] || BOOT_CONFIG="/boot/config.txt"  # older Bullseye layout

echo "==> Installing system packages (apt)"
sudo apt-get update
sudo apt-get install -y \
    python3-picamera2 python3-libcamera rpicam-apps \
    python3-requests python3-pil python3-numpy python3-psutil \
    ffmpeg git

echo "==> Enabling camera overlay in $BOOT_CONFIG"
if ! grep -q "^camera_auto_detect=1" "$BOOT_CONFIG"; then
    echo "camera_auto_detect=1" | sudo tee -a "$BOOT_CONFIG" >/dev/null
fi
echo
echo "This app was built against an Arducam 64MP Hawkeye autofocus camera,"
echo "which needs an explicit overlay on top of camera_auto_detect=1."
echo "If that's your camera and it's not already configured, add it now:"
echo
echo "    echo 'dtoverlay=arducam-64mp' | sudo tee -a $BOOT_CONFIG"
echo
echo "If you're using an official Raspberry Pi Camera Module (v2/v3/HQ)"
echo "instead, camera_auto_detect=1 alone is enough - no overlay line needed."
echo "A reboot is required after editing $BOOT_CONFIG."

echo "==> Creating data directories"
mkdir -p "$REPO_DIR"/{snapshots,recordings,continuous}

echo "==> Setting up config directory: $CONFIG_DIR"
mkdir -p "$CONFIG_DIR"
for name in gdrive location performance auth; do
    example="$REPO_DIR/config/${name}.env.example"
    target="$CONFIG_DIR/${name}.env"
    if [ ! -f "$target" ]; then
        cp "$example" "$target"
        chmod 600 "$target"
        echo "    created $target from template - fill in real values before relying on it"
    else
        echo "    $target already exists, leaving it alone"
    fi
done

echo "==> Installing systemd user units"
mkdir -p "$UNIT_DIR"
cp "$REPO_DIR/systemd/camera-stream.service" "$UNIT_DIR/"

echo "==> Enabling lingering (services survive logout/reboot without login)"
sudo loginctl enable-linger "$USER"

systemctl --user daemon-reload
systemctl --user enable --now camera-stream.service

echo
echo "==> camera-stream.service started. Status:"
systemctl --user status camera-stream.service --no-pager || true

echo
echo "============================================================"
echo "Next steps:"
echo "  1. If you just edited $BOOT_CONFIG, reboot now: sudo reboot"
echo "  2. Fill in real secrets in:"
echo "       $CONFIG_DIR/gdrive.env      (optional - Google Drive backup)"
echo "       $CONFIG_DIR/location.env    (optional - weather overlay)"
echo "       $CONFIG_DIR/performance.env (optional - resolution/bitrate; needed on"
echo "                                    low-RAM boards like a Pi Zero 2 W - see SETUP.md)"
echo "       $CONFIG_DIR/auth.env        (optional - HTTP Basic Auth login for the"
echo "                                    stream/UI; open on your LAN with no login if absent)"
echo "     Then: systemctl --user restart camera-stream.service"
echo "  3. To enable Google Drive backup, run once (interactive):"
echo "       python3 $REPO_DIR/setup_gdrive.py"
echo "  4. View the live stream at: http://<this-pi's-ip>:8000/"
echo "============================================================"
