#!/bin/sh

set -u
set -e

# Optocam: mount points for the FAT boot partition (WiFi settings, read by
# S90wifi) and the photo partition. The rootfs is read-only, so they must exist
# in the image; empty overlay directories don't survive git, so create them here.
mkdir -p ${TARGET_DIR}/boot ${TARGET_DIR}/data

# Add a console on tty1
if [ -e ${TARGET_DIR}/etc/inittab ]; then
    grep -qE '^tty1::' ${TARGET_DIR}/etc/inittab || \
	sed -i '/GENERIC_SERIAL/a\
tty1::respawn:/sbin/getty -L  tty1 0 vt100 # HDMI console' ${TARGET_DIR}/etc/inittab
# systemd doesn't use /etc/inittab, enable getty.tty1.service instead
elif [ -d ${TARGET_DIR}/etc/systemd ]; then
    mkdir -p "${TARGET_DIR}/etc/systemd/system/getty.target.wants"
    ln -sf /lib/systemd/system/getty@.service \
       "${TARGET_DIR}/etc/systemd/system/getty.target.wants/getty@tty1.service"
fi
