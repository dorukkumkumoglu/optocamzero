# Optocam Zero Software Installation and Interface Controls

<br>

## Requirements

- Completed Optocam Zero with the correct components.
- Micro sd card (16GB or larger, A2 type recommended for best performance).
- A computer with internet access.

<br>

## Installation

**1. Flash the SD card**

Download Raspberry Pi Imager from [raspberrypi.com/software](https://www.raspberrypi.com/software/) and install it. Select **Raspberry Pi Zero 2W** as the device and **Raspberry Pi OS Lite (32-bit) Bookworm** as the OS. You can find it under Raspberry Pi OS (other).

Before flashing, click **Edit Settings** and fill in your hostname, username, password, and WiFi credentials (remember to take note of this info). Go to the Services tab and enable SSH. Click Save, then flash the card.

**2. First boot**

Insert the SD card into the Pi and power it on. Wait about 1-2 minutes for it to boot and connect to your WiFi.

**3. Connect via SSH**

Open Terminal on your computer and run:

```
ssh your-username@your-hostname.local
```

Type `yes` when asked about the fingerprint, then enter your password.

**4. Run the installer**

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

## Troubleshooting

**SSH shows "host key changed" error:**
```
ssh-keygen -R your-hostname.local
```

**Camera does not start after reboot:**
```
sudo systemctl status camera-auto.service
```

<br>

## Interface/ Controls

The device is turned on and off using the power switch on the right side. Reaching the camera preview after turning on the device takes 22 seconds.

Camera focus is set to continuous auto and cannot be adjusted manually. Camera shutter speed and ISO are set to auto and cannot be adjusted manually.

Currently, 8 different photo filters are included. You can switch between them. Color temperature can also be changed.

### Main camera preview screen controls
- Top left corner displays current color temperature (left-right joystick toggles between different color temperature modes).
- Top right corner displays current photo filter (up-down joystick toggles between different photo filters).
- Bottom left corner displays current ISO.
- Bottom right corner displays current shutter speed.
- A loading circle appears in the bottom center when an image is being saved. Wait for it to disappear before turning off the camera.

### Gallery controls
- Center joystick button opens the gallery.
- The numbers in the bottom left corner display the photo count and the currently displayed photo number.
- Left-right joystick navigates between photos in the gallery.
- Push joystick up for photo deletion. A confirmation overlay will appear. Push up once more to confirm deletion. Press any other button on the device to cancel.
- To exit the gallery, press the center joystick button or the shutter button. This will return to the main camera preview immediately.

### Transfer mode (Hotspot)
Optocam Zero includes a hotspot mode and photo transfer interface optimized for both mobile and desktop use.
- To activate transfer mode, long press the center joystick button.
- Long press the center joystick button again to exit transfer mode.
- To transfer images, connect your phone or computer to the Wi-Fi network called **Optocam Zero** (find the password on the screen) and open **192.168.4.1** in a browser.
- The dot in the top right corner and the number next to it indicate how many devices are currently connected to the hotspot. The dot turns green when a device is connected.

### Transfer interface (External Device)
After opening the address in a browser, the transfer interface will appear. All photos in your gallery are visible here and can be scrolled and browsed.

Below the header logo, the total image count and available free space are displayed.
- Photos can be downloaded individually by clicking the download icon in the top right corner of the photo.
- Multiple photos can be selected using the selectors in the top left corner of each image. After selection, you may batch download or delete the selected photos.
- For single image view, click on the image. Use the back button on screen to return, or swipe down on touch devices.
- In single image view, use the left-right arrow keys or swipe left-right to browse between photos.
- To view the full resolution image, click the HQ button in single image view.
