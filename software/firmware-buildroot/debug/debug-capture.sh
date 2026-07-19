#!/bin/sh
# temp crash-forensics loop: mirrors volatile state to /data every 2s.
# Runs once per boot (started by S99debugcap). Before the loop starts, the
# previous boot's captures rotate to *.prev.txt — so after a freeze +
# power-cycle, the dead boot's last-2s snapshot survives the new boot.
# Installed on the dev camera at /data/debug-capture.sh; this is the tracked copy.
for f in applog dmesg sys; do
  mv "/data/debug-$f.txt" "/data/debug-$f.prev.txt" 2>/dev/null
done
sync
while true; do
  cp /tmp/optocam-boot.log /data/debug-applog.txt 2>/dev/null
  dmesg | tail -120 > /data/debug-dmesg.txt
  { date; cat /proc/uptime; free; echo ---; ps; } > /data/debug-sys.txt
  sync
  sleep 2
done
