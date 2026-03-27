#!/bin/bash
set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

# Must run as root
if [ "$EUID" -ne 0 ]; then
    echo -e "${RED}Please run with sudo: sudo bash install.sh${NC}"
    exit 1
fi

INSTALL_USER=${SUDO_USER:-pi}
INSTALL_HOME=$(getent passwd "$INSTALL_USER" | cut -d: -f6)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  OptoCamZero Installer${NC}"
echo -e "${GREEN}========================================${NC}"
echo "User:     $INSTALL_USER"
echo "Home:     $INSTALL_HOME"
echo ""

# ── 1. System packages ────────────────────────────────────────────────────────
echo -e "${YELLOW}[1/8] Installing system packages...${NC}"
apt-get update -q
apt-get install -y hostapd dnsmasq pigpio python3-pip python3-flask \
    python3-numpy python3-pil python3-picamera2

# ── 2. Python packages (pip) ──────────────────────────────────────────────────
echo -e "${YELLOW}[2/8] Installing Python packages...${NC}"
pip3 install spidev pigpio --break-system-packages 2>/dev/null || \
pip3 install spidev pigpio

# ── 3. Copy scripts ───────────────────────────────────────────────────────────
echo -e "${YELLOW}[3/8] Copying scripts and assets...${NC}"
cp "$SCRIPT_DIR/scripts/optocamzero.py"  "$INSTALL_HOME/optocamzero.py"
cp "$SCRIPT_DIR/scripts/gallery_server.py" "$INSTALL_HOME/gallery_server.py"

cp "$SCRIPT_DIR/assets/cmunvt.ttf"       "$INSTALL_HOME/cmunvt.ttf"
cp "$SCRIPT_DIR/assets/optocamlogo.svg"  "$INSTALL_HOME/optocamlogo.svg"
cp "$SCRIPT_DIR/assets/splash.raw"       "$INSTALL_HOME/splash.raw"

# Replace hardcoded paths with actual user home
sed -i "s|/home/dkumkum|$INSTALL_HOME|g" "$INSTALL_HOME/optocamzero.py"
sed -i "s|/home/dkumkum|$INSTALL_HOME|g" "$INSTALL_HOME/gallery_server.py"

# Create photos directory
mkdir -p "$INSTALL_HOME/photos"
chown -R "$INSTALL_USER:$INSTALL_USER" \
    "$INSTALL_HOME/optocamzero.py" \
    "$INSTALL_HOME/gallery_server.py" \
    "$INSTALL_HOME/cmunvt.ttf" \
    "$INSTALL_HOME/optocamlogo.svg" \
    "$INSTALL_HOME/splash.raw" \
    "$INSTALL_HOME/photos"

# ── 4. Service files ──────────────────────────────────────────────────────────
echo -e "${YELLOW}[4/8] Installing systemd services...${NC}"
for svc in camera-auto optocam-hotspot optocam-gallery uap0; do
    cp "$SCRIPT_DIR/services/$svc.service" "/etc/systemd/system/$svc.service"
    sed -i "s|/home/dkumkum|$INSTALL_HOME|g" "/etc/systemd/system/$svc.service"
    sed -i "s|dkumkum|$INSTALL_USER|g"       "/etc/systemd/system/$svc.service"
done

# ── 5. Hotspot config ─────────────────────────────────────────────────────────
echo -e "${YELLOW}[5/8] Configuring hotspot...${NC}"
cp "$SCRIPT_DIR/services/hostapd.conf" "/etc/hostapd/hostapd.conf"
cp "$SCRIPT_DIR/services/dnsmasq-optocam.conf" "/etc/dnsmasq.d/optocam.conf"

# Point hostapd to its config file (masked by default on Pi OS)
systemctl unmask hostapd
sed -i 's|#DAEMON_CONF=.*|DAEMON_CONF="/etc/hostapd/hostapd.conf"|' /etc/default/hostapd

# Tell NetworkManager to leave uap0 alone
mkdir -p /etc/NetworkManager/conf.d
cat > /etc/NetworkManager/conf.d/optocam-unmanaged.conf << 'EOF'
[keyfile]
unmanaged-devices=interface-name:uap0
EOF

# ── 6. Boot config ────────────────────────────────────────────────────────────
echo -e "${YELLOW}[6/8] Configuring /boot/firmware/config.txt...${NC}"
CONFIG=/boot/firmware/config.txt

# Fix conflicting default settings
sed -i 's/^camera_auto_detect=1/camera_auto_detect=0/' "$CONFIG"
sed -i 's/^display_auto_detect=1/display_auto_detect=0/' "$CONFIG"
sed -i 's/^dtparam=audio=on/dtparam=audio=off/' "$CONFIG"

# Helper: append line only if not already present
add_if_missing() {
    grep -qxF "$1" "$CONFIG" || echo "$1" >> "$CONFIG"
}

add_if_missing "dtparam=spi=on"
add_if_missing "dtparam=i2c_arm=off"
add_if_missing "dtoverlay=imx708"
add_if_missing "dtoverlay=st7789,cs=1,dc=9,rst=25,bl=13,width=240,height=240"
add_if_missing "arm_boost=1"
add_if_missing "arm_freq=1200"
add_if_missing "over_voltage=2"
add_if_missing "initial_turbo=30"
add_if_missing "boot_delay=0"
add_if_missing "disable_splash=1"
add_if_missing "dtoverlay=disable-bt"
add_if_missing "dtoverlay=spi1-3cs"
add_if_missing "disable_fw_kms_setup=1"
add_if_missing "disable_overscan=1"

# Boot parameters
CMDLINE=/boot/firmware/cmdline.txt
if ! grep -q "spidev.bufsiz" "$CMDLINE"; then
    sed -i 's/$/ spidev.bufsiz=65536/' "$CMDLINE"
fi
if ! grep -q "quiet" "$CMDLINE"; then
    sed -i 's/$/ quiet loglevel=3 logo.nologo vt.global_cursor_default=0/' "$CMDLINE"
fi

# ── 7. Enable services ────────────────────────────────────────────────────────
echo -e "${YELLOW}[7/8] Enabling services...${NC}"
systemctl daemon-reload
systemctl enable pigpiod
systemctl enable uap0
systemctl enable camera-auto
systemctl disable optocam-hotspot 2>/dev/null || true
systemctl disable optocam-gallery 2>/dev/null || true
systemctl disable hostapd 2>/dev/null || true
systemctl disable dnsmasq 2>/dev/null || true
systemctl disable ModemManager 2>/dev/null || true
systemctl disable NetworkManager-wait-online.service 2>/dev/null || true
systemctl disable avahi-daemon 2>/dev/null || true
systemctl disable e2scrub_reap 2>/dev/null || true
systemctl disable dphys-swapfile 2>/dev/null || true

# ── 8. Done ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  Installation complete!${NC}"
echo -e "${GREEN}========================================${NC}"
echo "Camera will start automatically on next boot."
echo "Hotspot: connect to 'Optocam Zero' — password: 0026opto"
echo "Gallery: open 192.168.4.1 in a browser while connected to the hotspot"
echo ""
echo -e "${YELLOW}Rebooting in 5 seconds... (Ctrl+C to cancel)${NC}"
sleep 5
reboot
