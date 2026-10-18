# Optocam Zero Buildroot firmware

Current fast-boot firmware source for Optocam Zero. The older Raspberry Pi OS /
Python version is kept in [`../python-legacy/`](../python-legacy/).

## Contents

- `defconfig/` - Buildroot defconfig for the current firmware image.
- `src/` - native app sources plus its `Makefile`.
- `package/optocamzero/` - Buildroot package that cross-compiles and installs
  the native app (`optocam_app`, the `optocam_preview` fallback, the
  `optocam_splash` boot splash and the `st7789_test` panel test) to `/usr/bin`.
- `package/libcamera/` - replaces Buildroot's libcamera package with the
  Raspberry Pi fork (`v0.2.0+rpt20240418`); upstream gives photos a blue cast.
- `config/` - Raspberry Pi boot, genimage, BusyBox, and board scripts.
- `kernel/` - kernel config fragments.
- `overlay/` - root filesystem overlay.
- `debug/` - optional bring-up scripts.

## Build

These files drop into a **Buildroot 2024.02.9** checkout (other versions are
untested; the libcamera hash is tied to this version's download format):

```sh
git clone -b 2024.02.9 https://gitlab.com/buildroot.org/buildroot.git
```

The defconfig references in-tree paths, so copy each file to the location below:

| This repo | Buildroot checkout |
| --- | --- |
| `defconfig/optocam_zero_buildroot_defconfig` | `configs/` |
| `package/optocamzero/` | `package/optocamzero/` |
| `package/libcamera/` | `package/libcamera/` (overwrite `libcamera.mk` and `libcamera.hash`) |
| `src/` (incl. `Makefile`) | `package/optocamzero/src/` |
| `overlay/` | `board/raspberrypi/overlay/` |
| `kernel/*.fragment` | `board/raspberrypi/` |
| `config/busybox.fragment` | `board/raspberrypi/` |
| `config/post-build.sh`, `config/post-image.sh` | `board/raspberrypizero2w/` |
| `config/config_zero2w.txt`, `config/cmdline.txt`, `config/genimage.cfg.in` | `board/raspberrypizero2w/` |

Register the package by adding one line to Buildroot's `package/Config.in`
(e.g. under the "Hardware handling" or "Miscellaneous" menu):

```
source "package/optocamzero/Config.in"
```

Then build:

```sh
make optocam_zero_buildroot_defconfig
make
```

This produces `output/images/sdcard.img`. The `optocamzero` package compiles the
native app against the target's libcamera, libjpeg, and freetype and installs
`/usr/bin/optocam_app` and `/usr/bin/optocam_preview`.

## Runtime Defaults

- Target: Raspberry Pi Zero 2 W + IMX708 camera.
- Photos: `/data/photos`.
- Transfer mode: `Optocam Zero`, `192.168.4.1`, password `0026opto`.
