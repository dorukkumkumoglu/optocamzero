#!/bin/sh
# Leave transfer mode: tear down AP + gallery, rejoin home WiFi.
echo "=== transfer-stop $(cat /proc/uptime) ===" >> /data/transfer.log
[ -f /run/gallery-server.pid ] && kill $(cat /run/gallery-server.pid) 2>/dev/null
rm -f /run/gallery-server.pid
[ -f /run/dnsmasq-transfer.pid ] && kill $(cat /run/dnsmasq-transfer.pid) 2>/dev/null
killall hostapd 2>/dev/null
sleep 0.5
ifconfig wlan0 down 2>/dev/null
ifconfig wlan0 0.0.0.0 up 2>/dev/null
if [ -s /run/wpa_supplicant.conf ]; then
  wpa_supplicant -B -i wlan0 -c /run/wpa_supplicant.conf 2>/dev/null
  HN=$(hostname)
  udhcpc -b -i wlan0 -t 15 -T 2 ${HN:+-x hostname:$HN} >/dev/null 2>&1
fi
