# Optocam Zero — Installation Guide

## Requirements

- Completed Optocam Zero with the correct components.
- MicroSD card (16GB or larger, A2 type recommended for best performance). 
- A computer with internet access

---

## Flashing the SD Card

Download Raspberry Pi Imager from [raspberrypi.com/software](https://www.raspberrypi.com/software/) and install it. Select **Raspberry Pi Zero 2W** as the device and **Raspberry Pi OS Lite (32-bit)** as the OS — you can find it under Raspberry Pi OS (other).

Before flashing, click **Edit Settings** and fill in your hostname, username, password, and WiFi credentials (remember to take note of this info). Go to the **Services** tab and enable SSH. Click Save, then flash the card.

---

## First Boot

Insert the SD card into the Pi and power it on. Wait about 1-2 minutes for it to boot and connect to your WiFi.

---

## Connecting via SSH

Open Terminal on your computer and run:

```
ssh your-username@your-hostname.local
```

Type `yes` when asked about the fingerprint, then enter your password.

---

## Installing

Run the following commands one by one:

```
sudo apt-get update
```

```
sudo apt-get install -y git
```

```
git clone https://github.com/dorukkumkumoglu/optocamzero.git && sudo bash optocamzero/software/installer/install.sh
```

Installation takes about 10-15 minutes. The Pi reboots automatically when done and the camera starts immediately.

---

## Transferring Photos

Long-press the joystick to activate transfer mode. Connect your phone or computer to the WiFi network called **Optocam Zero** (find password on the screen) and open **192.168.4.1** in a browser. The green dot in the transfer screen and the number next to it indicate how many devices are currently connected to the hotspot.

---

## Troubleshooting

**SSH shows "host key changed" error:**
```
ssh-keygen -R your-hostname.local
```

**Camera does not start after reboot:**
```
sudo systemctl status camera-auto.service
```
