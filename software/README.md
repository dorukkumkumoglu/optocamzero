# Optocam Zero Installation Guide

## Requirements

- Completed Optocam Zero with the correct components.
- MicroSD card (16GB or larger, A2 type recommended for best performance).
- A computer with internet access

<br>

## Flashing the SD Card

Download Raspberry Pi Imager from [raspberrypi.com/software](https://www.raspberrypi.com/software/) and install it. Select **Raspberry Pi Zero 2W** as the device and **Raspberry Pi OS Lite (32-bit) Bookworm** as the OS — you can find it under Raspberry Pi OS (other).

Before flashing, click **Edit Settings** and fill in your hostname, username, password, and WiFi credentials (remember to take note of this info). Go to the **Services** tab and enable SSH. Click Save, then flash the card.

<br>

## First Boot

Insert the SD card into the Pi and power it on. Wait about 1-2 minutes for it to boot and connect to your WiFi.

<br>

## Connecting via SSH

Open Terminal on your computer and run:

```
ssh your-username@your-hostname.local
```

Type `yes` when asked about the fingerprint, then enter your password.

<br>

## Installing

Run the following commands:

```
sudo apt-get update
```

```
sudo apt-get install -y git
```

```
git clone https://github.com/dorukkumkumoglu/optocamzero.git && sudo bash optocamzero/software/install.sh
```

Installation takes about 10-15 minutes. The Pi reboots automatically when done and the camera starts immediately.

<br>

## Interface/ Controls

The device is turned on and off using the power switch on the right side. Reaching the camera preview after turning on the device takes 22 seconds.

Camera focus is set to continous auto and can't be adjusted manually.
Camera shutter speed or iso is set to auto and can't be adjusted manually.
Currently there are 8 different photo filters included. You can switch between them. Color temp can also be changed.

Main camera preview screen controls:
-Top left corner displays current color temp (left-right joystick toggles between different color temp modes).
-Top right corner displays current photo filter (up-down joystick toggles between different photo filters).
-Bottom left corner displays current iso.
-Bottom right corner displays current shutter speed.
-Loading circle appears in bottom center when an image is being saved. Wait for it to disappear before turning off the camera.

Gallery controls:
-Center joystick button opens gallery. 
-The numbers on the buttom left corner displays photo count and the currently displayed photo number.
-Left-right joystick moves between photos in the gallery. 
-Up joystick for photo deletion. A confirmation overlay will show, move up once more to confirm deletion. Press any button on device to exit deletion.
-To exit gallery, press center joystick or shutter button, this will open the main camera preview immediately.

Transfer mode (Hotspot):
Optocam has included hotspot mode and photo transfer interface optimized for both mobile and desktop use.
To activate, long press the center joystick button.

-





Long-press the joystick to activate transfer mode. Connect your phone or computer to the WiFi network called **Optocam Zero** (find password on the screen) and open **192.168.4.1** in a browser. The green dot in the transfer screen and the number next to it indicate how many devices are currently connected to the hotspot.

<br>

## Troubleshooting

**SSH shows "host key changed" error:**
```
ssh-keygen -R your-hostname.local
```

**Camera does not start after reboot:**
```
sudo systemctl status camera-auto.service
```
