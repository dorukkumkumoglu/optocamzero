#!/usr/bin/env python3
import sys
import time
_script_start = time.time()
def log(msg):
    sys.stderr.write(f"[{time.time() - _script_start:.2f}s] {msg}\n")
    sys.stderr.flush()
log("Script file started")
import RPi.GPIO as GPIO
log("GPIO imported")
import pigpio
log("pigpio imported")
import spidev
log("spidev imported")
import threading
log("threading imported")
import os
log("os imported")
log("datetime skipped")
from PIL import Image
log("PIL imported")
import numpy as np
log("numpy imported")
import gc
import subprocess
log("All imports done")
# Force clean GPIO
GPIO.setwarnings(False)
GPIO.setmode(GPIO.BCM)
for pin in [21, 20, 5, 26, 13, 6, 19]:
    try:
        GPIO.remove_event_detect(pin)
    except:
        pass
GPIO.cleanup()
time.sleep(0.2)
# Pins
RST_PIN = 27
DC_PIN = 25
BL_PIN = 24
BUTTON_CAPTURE = 21
BUTTON_PREVIEW = 20
JOYSTICK_LEFT = 5
JOYSTICK_RIGHT = 26
JOYSTICK_PRESS = 13
JOYSTICK_UP = 6
JOYSTICK_DOWN = 19
# Setup GPIO
GPIO.setmode(GPIO.BCM)
GPIO.setup(RST_PIN, GPIO.OUT)
GPIO.setup(DC_PIN, GPIO.OUT)
GPIO.setup(BL_PIN, GPIO.OUT)
_pi = pigpio.pi()
_pi.set_PWM_frequency(BL_PIN, 1000)
_pi.set_PWM_dutycycle(BL_PIN, 255)
GPIO.setup(BUTTON_PREVIEW, GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(BUTTON_CAPTURE, GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(JOYSTICK_LEFT, GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(JOYSTICK_RIGHT, GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(JOYSTICK_PRESS, GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(JOYSTICK_UP, GPIO.IN, pull_up_down=GPIO.PUD_UP)
GPIO.setup(JOYSTICK_DOWN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
# SPI
spi = spidev.SpiDev()
spi.open(0, 0)
spi.max_speed_hz = 100000000
spi.mode = 0
spi.bits_per_word = 8
# Locks
camera_lock = threading.RLock()
display_lock = threading.Lock()
# Camera configuration cache
class CameraConfigCache:
    def __init__(self, picam2):
        self.preview_config = picam2.create_preview_configuration(
            main={"size": (240, 240), "format": "RGB888"},
            buffer_count=3,
            queue=False,
            controls={"AfMode": 2, "AfSpeed": 1, "FrameDurationLimits": (100, 25000)}
        )
        self.capture_config = picam2.create_still_configuration(
            main={"size": (2592, 2592), "format": "RGB888"},
            buffer_count=2
        )
config_cache = None
FONT_PATH = "/home/dkumkum/cmunvt.ttf"

# Shadow cache for preview overlays — only regenerated when value changes
_shadow_cache = {}

def make_text_shadow(text, x, y, font):
    from PIL import ImageDraw, ImageFilter
    shadow = Image.new("RGBA", (240, 240), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).text((x, y), text, font=font, fill=(0, 0, 0, 200))
    return shadow.filter(ImageFilter.GaussianBlur(radius=4))

_STANDARD_ISO = [100, 125, 160, 200, 250, 320, 400, 500, 640, 800, 1000, 1250, 1600]

AWB_MODES = [
    (1, "Tungsten",    "TNG"),
    (2, "Fluorescent", "FLR"),
    (3, "Indoor",      "IND"),
    (4, "Daylight",    "DAY"),
    (5, "Cloudy",      "CLD"),
]

# (microseconds, display string)
_STANDARD_SHUTTERS = [
    (125, "1/8000"), (156, "1/6400"), (200, "1/5000"), (250, "1/4000"),
    (313, "1/3200"), (400, "1/2500"), (500, "1/2000"), (625, "1/1600"),
    (800, "1/1250"), (1000, "1/1000"), (1250, "1/800"), (1563, "1/640"),
    (2000, "1/500"), (2500, "1/400"), (3125, "1/320"), (4000, "1/250"),
    (5000, "1/200"), (6250, "1/160"), (8000, "1/125"), (10000, "1/100"),
    (12500, "1/80"), (16667, "1/60"), (20000, "1/50"), (25000, "1/40"),
    (33333, "1/30"), (40000, "1/25"), (50000, "1/20"), (66667, "1/15"),
    (100000, "1/10"), (125000, "1/8"), (166667, "1/6"), (200000, "1/5"),
    (250000, "1/4"), (333333, "1/3"), (500000, "1/2"), (1000000, "1\""),
]

def nearest_standard_iso(gain):
    iso = gain * 100
    return min(_STANDARD_ISO, key=lambda s: abs(s - iso))

def nearest_standard_shutter(exp_us):
    return min(_STANDARD_SHUTTERS, key=lambda s: abs(s[0] - exp_us))[1]

def get_cached_shadow(key, text, x, y, font):
    if _shadow_cache.get(key) != text:
        _shadow_cache[key] = text
        _shadow_cache[key + "_img"] = make_text_shadow(text, x, y, font)
    return _shadow_cache[key + "_img"]

# Filter indicator circle — cached per filter name, built once on first use
_indicator_cache = {}

def get_filter_indicator(filter_name):
    if filter_name in _indicator_cache:
        return _indicator_cache[filter_name]
    from PIL import ImageDraw, ImageFilter
    pad = 10
    r   = 17
    cx  = 240 - pad - r
    cy  = pad + r
    font = load_font(24)

    # Build shadow and white layers separately, then composite — same approach as HUD text
    shadow = Image.new("RGBA", (240, 240), (0, 0, 0, 0))
    white  = Image.new("RGBA", (240, 240), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    wd = ImageDraw.Draw(white)

    # Filters with multi-char labels use a pill; single-char filters use a circle
    _PILL_LABELS = {"B&W": "B&W", "TRI-X": "TX", "Film Standard": "FS"}

    if filter_name in _PILL_LABELS:
        label  = _PILL_LABELS[filter_name]
        tb     = wd.textbbox((0, 0), label, font=font)
        h_pad  = 10
        pill_h = r * 2
        pill_w = (tb[2] - tb[0]) + h_pad * 2
        x1, y0 = 240 - pad, pad
        x0, y1 = x1 - pill_w, y0 + pill_h
        cr  = pill_h // 2
        pcx = (x0 + x1) // 2
        pcy = (y0 + y1) // 2
        tx  = pcx - (tb[0] + tb[2]) // 2
        ty  = pcy - (tb[1] + tb[3]) // 2
        sd.rounded_rectangle([x0, y0, x1, y1], radius=cr, outline=(0, 0, 0, 200), width=2)
        sd.text((tx, ty), label, font=font, fill=(0, 0, 0, 200))
        wd.rounded_rectangle([x0, y0, x1, y1], radius=cr, outline=(255, 255, 255, 255), width=1)
        wd.text((tx, ty), label, font=font, fill=(255, 255, 255, 255))
    else:
        label = filter_name[0]
        tb = wd.textbbox((0, 0), label, font=font)
        tx = cx - (tb[0] + tb[2]) // 2
        ty = cy - (tb[1] + tb[3]) // 2
        if label in ("D", "P", "L", "N"):
            tx += 1
        sd.ellipse([cx-r, cy-r, cx+r, cy+r], outline=(0, 0, 0, 200), width=2)
        sd.text((tx, ty), label, font=font, fill=(0, 0, 0, 200))
        wd.ellipse([cx-r, cy-r, cx+r, cy+r], outline=(255, 255, 255, 255), width=1)
        wd.text((tx, ty), label, font=font, fill=(255, 255, 255, 255))

    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=4))
    layer  = Image.alpha_composite(shadow, white)
    _indicator_cache[filter_name] = layer
    return layer

_font_cache = {}  # v2: cache loaded fonts so TTF isn't re-opened every frame
def load_font(size):
    if size in _font_cache:
        return _font_cache[size]
    try:
        from PIL import ImageFont
        _font_cache[size] = ImageFont.truetype(FONT_PATH, size)
    except:
        from PIL import ImageFont
        _font_cache[size] = ImageFont.load_default()
    return _font_cache[size]

# ── Filters ────────────────────────────────────────────────────────────────
FILTERS = ["Film Standard", "Punch", "B&W", "Deep", "Sand", "Eterna", "TRI-X", "Cutout", "No Filter"]

def _make_lut(points):
    x = [p[0] for p in points]
    y = [p[1] for p in points]
    return np.interp(np.arange(256), x, y).clip(0, 255).astype(np.uint8)

# Base tone curves
_BASE_CURVES = {
    "B&W":        _make_lut([(0,0),(64,16),(128,160),(192,242),(255,255)]),   # steeper S — deeper blacks, brighter whites
    "Punch":      _make_lut([(0,0),(64,52),(128,148),(192,212),(255,242)]),   # pure blacks, highlights pulled down
    "Sand":       _make_lut([(0,0),(64,50),(128,132),(192,205),(255,255)]),   # gentle S for sepia
    "Deep":       _make_lut([(0,30),(64,70),(128,152),(192,222),(255,255)]),
    "Eterna":        _make_lut([(0,30),(64,78),(128,128),(192,172),(255,215)]),  # very flat, heavy lifted blacks, compressed highlights
    "Film Standard": _make_lut([(0,18),(64,55),(128,140),(192,210),(255,252)]),  # Classic Neg: lifted blacks, strong S, compressed highlights
}

# Channel LUTs — built manually per filter
_CHANNEL_LUTS = {}
_v = np.arange(256, dtype=np.float32)

# B&W — all channels identical, ISP handles grayscale via Saturation=0
_CHANNEL_LUTS["B&W"] = (
    _BASE_CURVES["B&W"],
    _BASE_CURVES["B&W"],
    _BASE_CURVES["B&W"],
)

# Punch — pure blacks, R/G slight boost, B shadow blue fades to zero at midtones
_pc = _BASE_CURVES["Punch"].astype(np.float32)
_punch_shadow_blue = np.maximum(0.0, 65.0 * (1.0 - _v / 105.0))
_CHANNEL_LUTS["Punch"] = (
    np.clip(_pc * 1.05, 0, 255).astype(np.uint8),
    np.clip(_pc * 1.02, 0, 255).astype(np.uint8),
    np.clip(_pc + _punch_shadow_blue, 0, 255).astype(np.uint8),
)

# Sand — toned-down sepia: warm brown tint, image structure preserved
_sc = _BASE_CURVES["Sand"].astype(np.float32)
_CHANNEL_LUTS["Sand"] = (
    np.clip(_sc * 1.08, 0, 255).astype(np.uint8),   # R: subtle warm boost
    np.clip(_sc * 0.92, 0, 255).astype(np.uint8),   # G: slight pullback
    np.clip(_sc * 0.55, 0, 255).astype(np.uint8),   # B: reduced but not crushed → softer sepia
)

# Deep — Matrix-style blue tint: image stays recognisable, blue pervades everything
# Colours desaturated via ISP, blue channel boosted, R/G pulled back
_dc = _BASE_CURVES["Deep"].astype(np.float32)
_CHANNEL_LUTS["Deep"] = (
    np.clip(_dc * 0.55, 0, 255).astype(np.uint8),   # R: heavily pulled back
    np.clip(_dc * 0.70, 0, 255).astype(np.uint8),   # G: pulled back
    np.clip(_dc * 1.35, 0, 255).astype(np.uint8),   # B: strongly boosted
)

# Eterna — Fuji cinema film: flat, cool, muted — maximum latitude look
_et = _BASE_CURVES["Eterna"].astype(np.float32)

_CHANNEL_LUTS["Eterna"] = (
    np.clip(_et * 0.96, 0, 255).astype(np.uint8),   # R: slightly pulled — cool cast
    np.clip(_et * 1.00, 0, 255).astype(np.uint8),   # G: neutral
    np.clip(_et * 1.05, 0, 255).astype(np.uint8),   # B: slight boost — cinematic cool
)

# Film Standard — Fuji Classic Neg: muted reds, teal shadows, cool-neutral character
_fs = _BASE_CURVES["Film Standard"].astype(np.float32)
_CHANNEL_LUTS["Film Standard"] = (
    np.clip(_fs * 0.95, 0, 255).astype(np.uint8),   # R: pulled — muted warm tones
    np.clip(_fs * 1.02, 0, 255).astype(np.uint8),   # G: slight boost — natural greens
    np.clip(_fs * 1.08, 0, 255).astype(np.uint8),   # B: boosted — teal/cool shadow character
)

# Cutout — hard 3-level posterisation: black / mid-grey / white, no smooth gradients
_co = np.zeros(256, dtype=np.uint8)
_co[65:130] = 128    # mid-grey band
_co[130:]   = 255    # white
_CHANNEL_LUTS["Cutout"] = (_co, _co, _co)


# TRI-X — Kodak tritone: black shadows → golden-yellow mids → green highlights
# Colours sampled from the Kodak TRI-X Pan 135-36 box
_TRITON_SHADOW    = np.array([0,   0,   0],   dtype=np.float32)
_TRITON_MID       = np.array([242, 183,  8],  dtype=np.float32)  # Kodak yellow
_TRITON_HIGHLIGHT = np.array([35,  155, 60],  dtype=np.float32)  # Kodak green
_trix_lut = np.zeros((256, 3), dtype=np.float32)
_TRIX_BLACK = 0.18   # hold pure black below this luma
_TRIX_SPLIT = 0.38   # below split: black→yellow   above: yellow→green
for _i in range(256):
    _t = _i / 255.0
    if _t <= _TRIX_BLACK:
        _trix_lut[_i] = _TRITON_SHADOW
    elif _t <= _TRIX_SPLIT:
        _t2 = (_t - _TRIX_BLACK) / (_TRIX_SPLIT - _TRIX_BLACK)
        _trix_lut[_i] = _TRITON_SHADOW + (_TRITON_MID - _TRITON_SHADOW) * _t2
    else:
        _t2 = (_t - _TRIX_SPLIT) / (1.0 - _TRIX_SPLIT)
        _trix_lut[_i] = _TRITON_MID + (_TRITON_HIGHLIGHT - _TRITON_MID) * _t2
_TRIX_LUT = np.clip(_trix_lut, 0, 255).astype(np.uint8)   # shape (256, 3)

# ── Grain tables ──────────────────────────────────────────────────────────────
# Pre-generate one 1024×1024 int16 table per intensity at startup.
# Per-apply cost: two arange calls + modulo indexing — no RNG at save time.
_GRAIN_TABLE_SIZE = 1024
_grain_tables: dict = {}
_grain_rng = np.random.default_rng(0)  # fixed seed → reproducible tables

def _get_grain_table(intensity: int) -> np.ndarray:
    if intensity not in _grain_tables:
        _grain_tables[intensity] = _grain_rng.integers(
            -intensity, intensity + 1,
            (_GRAIN_TABLE_SIZE, _GRAIN_TABLE_SIZE),
            dtype=np.int16,
        )
    return _grain_tables[intensity]

def _apply_grain(arr: np.ndarray, intensity: int) -> np.ndarray:
    """Apply tiled grain to a uint8 H×W×3 array at any resolution."""
    h, w = arr.shape[:2]
    table = _get_grain_table(intensity)
    dy = np.random.randint(0, _GRAIN_TABLE_SIZE)
    dx = np.random.randint(0, _GRAIN_TABLE_SIZE)
    rows = (np.arange(dy, dy + h) % _GRAIN_TABLE_SIZE)[:, np.newaxis]  # (H,1)
    cols = (np.arange(dx, dx + w) % _GRAIN_TABLE_SIZE)[np.newaxis, :]  # (1,W)
    grain = table[rows, cols]                                           # (H,W)
    arr16 = arr.astype(np.int16)
    arr16 += grain[:, :, np.newaxis]   # broadcast same grain to all channels
    return np.clip(arr16, 0, 255).astype(np.uint8)
# ──────────────────────────────────────────────────────────────────────────────

# Grain — all filters at 22, Normal has none
_GRAIN = {"B&W": 27, "Punch": 22, "Sand": 22, "Deep": 22, "Cutout": 22, "TRI-X": 15, "Eterna": 15, "Film Standard": 18}

# ISP saturation — applied once on filter change, free every frame
_FILM_ISP = {
    "No Filter": {"Saturation": 1.0, "Contrast": 1.0, "Brightness": 0.0},
    "B&W":    {"Saturation": 0.0, "Contrast": 1.0, "Brightness": 0.0},
    "Punch":  {"Saturation": 1.3, "Contrast": 1.0, "Brightness": -0.05},
    "Sand":   {"Saturation": 0.45,"Contrast": 1.0, "Brightness": 0.0},
    "Deep":   {"Saturation": 0.6, "Contrast": 1.0, "Brightness": 0.0},
    "Cutout": {"Saturation": 0.0, "Contrast": 1.0, "Brightness": 0.0},
    "TRI-X":       {"Saturation": 1.0, "Contrast": 1.0, "Brightness": 0.0},
    "Eterna":        {"Saturation": 0.75, "Contrast": 1.0, "Brightness": 0.0},
    "Film Standard": {"Saturation": 0.85, "Contrast": 1.0, "Brightness": 0.0},
}

def _apply_filter_by_name(image, name, apply_grain=True):
    """Tone curve + channel shifts in numpy. Saturation handled by ISP hardware."""
    if name == "No Filter":
        return image
    arr = np.array(image, dtype=np.uint8)
    if name == "TRI-X":
        # Luma-based tritone mapping
        luma = (arr[:, :, 0].astype(np.uint32) * 299 +
                arr[:, :, 1].astype(np.uint32) * 587 +
                arr[:, :, 2].astype(np.uint32) * 114 + 500) // 1000
        arr = _TRIX_LUT[luma.clip(0, 255).astype(np.uint8)]  # (H, W, 3)
    elif name == "Cutout":
        # PIL's convert('L') uses optimised C — avoids three large uint32 intermediate arrays
        p = _co[np.array(image.convert('L'), dtype=np.uint8)]
        arr = np.stack([p, p, p], axis=2)
    else:
        r_lut, g_lut, b_lut = _CHANNEL_LUTS[name]
        arr[:, :, 0] = r_lut[arr[:, :, 0]]
        arr[:, :, 1] = g_lut[arr[:, :, 1]]
        arr[:, :, 2] = b_lut[arr[:, :, 2]]
    grain = _GRAIN.get(name, 0) if apply_grain else 0
    if grain:
        arr = _apply_grain(arr, grain)
    return Image.fromarray(arr)

def apply_filter(image):
    return _apply_filter_by_name(image, FILTERS[filter_index], apply_grain=False)
# ──────────────────────────────────────────────────────────────────────────
def send_command(cmd):
    GPIO.output(DC_PIN, GPIO.LOW)
    spi.xfer([cmd])
def send_data(data):
    GPIO.output(DC_PIN, GPIO.HIGH)
    chunk_size = 65536  # v2: larger chunks = fewer SPI transactions = less tearing
    for i in range(0, len(data), chunk_size):
        chunk = data[i:i + chunk_size]
        try:
            spi.writebytes2(chunk)
        except AttributeError:
            spi.writebytes(chunk)
def init_display():
    """Display initialization with improved black levels and contrast"""
    print("Initializing display (enhanced blacks)...")
    GPIO.output(RST_PIN, GPIO.HIGH)
    time.sleep(0.05)
    GPIO.output(RST_PIN, GPIO.LOW)
    time.sleep(0.05)
    GPIO.output(RST_PIN, GPIO.HIGH)
    time.sleep(0.08)
    init_commands = [
        (0x36, [0x70]),
        (0x3A, [0x05]),
        (0xB2, [0x0C, 0x0C, 0x00, 0x33, 0x33]),
        (0xB7, [0x35]),
        (0xBB, [0x35]),
        (0xC0, [0x2C]),
        (0xC2, [0x01]),
        (0xC3, [0x13]),
        (0xC4, [0x20]),
        (0xC6, [0x0F]),
        (0xD0, [0xA4, 0xA1]),
        (0xE0, [0xF0, 0x00, 0x04, 0x04, 0x04, 0x05, 0x29, 0x33, 0x3E, 0x38, 0x12, 0x12, 0x28, 0x30]),
        (0xE1, [0xF0, 0x07, 0x0A, 0x0D, 0x0B, 0x07, 0x28, 0x33, 0x3E, 0x36, 0x14, 0x14, 0x29, 0x32]),
        (0x21, []),
        (0x11, []),
    ]
    for cmd, data in init_commands:
        send_command(cmd)
        if data:
            GPIO.output(DC_PIN, GPIO.HIGH)
            spi.xfer(data)
        if cmd == 0x11:
            time.sleep(0.08)
    send_command(0x29)
def set_backlight(state):
    _pi.set_PWM_dutycycle(BL_PIN, 255 if state else 0)
def set_backlight_brightness(pct):
    _pi.set_PWM_dutycycle(BL_PIN, int(pct * 2.55))
def clear_display():
    with display_lock:
        send_command(0x2A)
        GPIO.output(DC_PIN, GPIO.HIGH)
        spi.xfer([0x00, 0x00, 0x00, 0xEF])
        send_command(0x2B)
        GPIO.output(DC_PIN, GPIO.HIGH)
        spi.xfer([0x00, 0x00, 0x00, 0xEF])
        send_command(0x2C)
        send_data(bytearray(240 * 240 * 2))
# v2: precomputed LUT replaces per-frame float32 contrast math
# Equivalent to: (x - 128) * 1.15 + 123  (same as original: *1.15 + 128 - 5)
_CONTRAST_LUT = np.clip(
    (np.arange(256, dtype=np.float32) - 128) * 1.15 + 123,
    0, 255
).astype(np.uint8)

def convert_to_rgb565(image):
    """RGB565 conversion with enhanced contrast"""
    rgb_array = np.frombuffer(image.tobytes(), dtype=np.uint8).reshape((240, 240, 3))
    rgb_array = _CONTRAST_LUT[rgb_array]  # v2: LUT lookup, no float32 intermediate
    r = rgb_array[:, :, 0].astype(np.uint16) & 0xF8
    g = rgb_array[:, :, 1].astype(np.uint16) & 0xFC
    b = rgb_array[:, :, 2].astype(np.uint16) & 0xF8
    rgb565 = (r << 8) | (g << 3) | (b >> 3)
    return rgb565.astype('>u2').tobytes()
def display_image(image):
    with display_lock:
        send_command(0x2A)
        GPIO.output(DC_PIN, GPIO.HIGH)
        spi.writebytes([0x00, 0x00, 0x00, 0xEF])
        send_command(0x2B)
        GPIO.output(DC_PIN, GPIO.HIGH)
        spi.writebytes([0x00, 0x00, 0x00, 0xEF])
        send_command(0x2C)
        send_data(convert_to_rgb565(image))
def overlay_capture_dot(base_image):
    """Capture indicator dot"""
    img_array = np.array(base_image)
    dot_center = (220, 20)
    dot_radius = 8
    y, x = np.ogrid[:240, :240]
    mask = (x - dot_center[0])**2 + (y - dot_center[1])**2 <= dot_radius**2
    img_array[mask] = [0, 255, 0]
    border = ((x - dot_center[0])**2 + (y - dot_center[1])**2 <= (dot_radius+1)**2) & \
             ((x - dot_center[0])**2 + (y - dot_center[1])**2 > (dot_radius-1)**2)
    img_array[border] = [255, 255, 255]
    return Image.fromarray(img_array)
def show_splash():
    """Display pre-converted RGB565 splash image directly to display"""
    splash_path = "/home/dkumkum/splash.raw"
    if not os.path.exists(splash_path):
        return
    with open(splash_path, "rb") as f:
        data = f.read()
    with display_lock:
        send_command(0x2A)
        GPIO.output(DC_PIN, GPIO.HIGH)
        spi.writebytes([0x00, 0x00, 0x00, 0xEF])
        send_command(0x2B)
        GPIO.output(DC_PIN, GPIO.HIGH)
        spi.writebytes([0x00, 0x00, 0x00, 0xEF])
        send_command(0x2C)
        send_data(data)

def show_transfer_mode_screen():
    """Display transfer mode info screen on device"""
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (240, 240), (0, 0, 0))
    draw = ImageDraw.Draw(img)
    font_title = load_font(17)
    font_label = load_font(16)
    font_value = load_font(20)
    font_hint  = load_font(18)

    # Check how many devices are connected to the hotspot
    try:
        result = subprocess.run(["iw", "dev", "uap0", "station", "dump"],
                                capture_output=True, text=True, timeout=1)
        device_count = result.stdout.count("Station ")
        device_connected = device_count > 0
    except Exception:
        device_count = 0
        device_connected = False

    # Blink when no device connected (0.5s on/off), solid when connected
    dot_visible = device_connected or (int(time.time() * 2) % 2 == 0)
    dot_color   = (60, 200, 80) if device_connected else (90, 90, 90)
    dot_r = 6

    # Title — left-aligned, dot on right aligned with text
    title = "Transfer Mode"
    tb = draw.textbbox((0, 0), title, font=font_title)
    title_y = 13
    draw.text((20, title_y), title, font=font_title, fill=(160, 160, 160))
    dot_cy = title_y + (tb[1] + tb[3]) // 2
    dot_cx = 214
    if dot_visible:
        draw.ellipse([dot_cx - dot_r, dot_cy - dot_r, dot_cx + dot_r, dot_cy + dot_r], fill=dot_color)

    # Device count — shown only when devices are connected, left of the dot
    if device_connected:
        count_str = str(device_count)
        cb = draw.textbbox((0, 0), count_str, font=font_title)
        count_w = cb[2] - cb[0]
        count_x = dot_cx - dot_r - count_w - 8
        count_y = dot_cy - (cb[3] - cb[1]) // 2 - cb[1]
        draw.text((count_x, count_y), count_str, font=font_title, fill=dot_color)

    draw.line([(8, 43), (232, 43)], fill=(40, 40, 40), width=1)

    # WiFi
    draw.text((20, 51),  "WiFi",         font=font_label, fill=(100, 100, 100))
    draw.text((20, 69),  "Optocam Zero", font=font_value, fill=(255, 255, 255))

    # Password
    draw.text((20, 99),  "Password",     font=font_label, fill=(100, 100, 100))
    draw.text((20, 117), "0026opto",     font=font_value, fill=(255, 255, 255))

    # Browser
    draw.text((20, 147), "Browser",      font=font_label, fill=(100, 100, 100))
    draw.text((20, 165), "192.168.4.1",  font=font_value, fill=(255, 255, 255))

    draw.line([(8, 197), (232, 197)], fill=(40, 40, 40), width=1)

    # Hint — properly centred in bottom gap (197→240)
    hint = "Hold center to exit"
    hb = draw.textbbox((0, 0), hint, font=font_hint)
    hint_h = hb[3] - hb[1]
    hint_y = 197 + (43 - hint_h) // 2 - hb[1]
    draw.text(((240 - (hb[2] - hb[0])) // 2, hint_y), hint, font=font_hint, fill=(60, 60, 60))

    display_image(img)

GALLERY_DIR = "/home/dkumkum/photos"

_capture_counter = None
_capture_counter_lock = threading.Lock()

def get_next_capture_number():
    """Return next available number using an in-memory counter.
    Scans filesystem only once on first call — rapid shots never collide."""
    global _capture_counter
    with _capture_counter_lock:
        if _capture_counter is None:
            try:
                numbers = [
                    int(f[len("Optocamzero_"):-len(".jpg")])
                    for f in os.listdir(GALLERY_DIR)
                    if f.startswith("Optocamzero_") and f.endswith(".jpg")
                    and f[len("Optocamzero_"):-len(".jpg")].isdigit()
                ] if os.path.exists(GALLERY_DIR) else []
                _capture_counter = max(numbers) + 1 if numbers else 1
            except:
                _capture_counter = 1
        num = _capture_counter
        _capture_counter += 1
        return num

def get_gallery_images():
    """Return list of captured image paths sorted by number"""
    try:
        if not os.path.exists(GALLERY_DIR):
            print("Gallery dir not found")
            return []
        files = [
            os.path.join(GALLERY_DIR, f)
            for f in os.listdir(GALLERY_DIR)
            if f.startswith("Optocamzero_") and f.endswith(".jpg")
        ]
        files.sort(key=lambda f: int(os.path.basename(f)[len("Optocamzero_"):-len(".jpg")]))
        print(f"Found {len(files)} images")
        return files
    except Exception as e:
        print(f"Gallery scan error: {e}")
        return []
def display_gallery_image(filepath, index, total, confirm_delete=False):
    """Load and display a gallery image using draft mode for fast decoding"""
    try:
        from PIL import ImageDraw, ImageFilter
        img = Image.open(filepath)
        img.draft("RGB", (240, 240))
        img = img.convert("RGB")
        img = img.resize((240, 240), Image.BILINEAR)

        # Counter bottom-left with 15px padding
        font = load_font(25)
        text = f"{index}/{total}"
        draw = ImageDraw.Draw(img)
        bbox_t = draw.textbbox((0, 0), text, font=font)
        text_h = bbox_t[3] - bbox_t[1]
        x, y = 15, 240 - 15 - text_h
        shadow = Image.new("RGBA", (240, 240), (0, 0, 0, 0))
        ImageDraw.Draw(shadow).text((x, y), text, font=font, fill=(0, 0, 0, 200))
        shadow = shadow.filter(ImageFilter.GaussianBlur(radius=4))
        img = img.convert("RGBA")
        img = Image.alpha_composite(img, shadow)
        img = img.convert("RGB")
        draw = ImageDraw.Draw(img)
        draw.text((x, y), text, font=font, fill=(255, 255, 255))

        # Delete confirmation dialog
        if confirm_delete:
            overlay = Image.new("RGBA", (240, 240), (0, 0, 0, 160))
            img = img.convert("RGBA")
            img = Image.alpha_composite(img, overlay)
            img = img.convert("RGB")
            draw = ImageDraw.Draw(img)
            font_dialog = load_font(25)
            font_yes = font_dialog
            # "Delete?" centered
            t1 = "Delete?"
            b1 = draw.textbbox((0, 0), t1, font=font_dialog)
            draw.text(((240 - (b1[2] - b1[0])) // 2, 80), t1, font=font_dialog, fill=(255, 255, 255))
            # "YES: [arrow]" centered as a block
            t2 = "YES: "
            b2 = draw.textbbox((0, 0), t2, font=font_yes)
            text_w = b2[2] - b2[0]
            text_h = b2[3] - b2[1]
            arrow_w = 14
            bx = (240 - text_w - arrow_w) // 2
            by = 118
            draw.text((bx, by), t2, font=font_yes, fill=(255, 255, 255))
            ax = bx + text_w
            mid_y = by + (b2[1] + b2[3]) // 2
            draw.polygon([(ax + 7, mid_y - 8), (ax, mid_y + 6), (ax + 14, mid_y + 6)], fill=(255, 255, 255))
            # "NO: Any Button" centered below in gray
            t3 = "NO: Any Button"
            b3 = draw.textbbox((0, 0), t3, font=font_yes)
            draw.text(((240 - (b3[2] - b3[0])) // 2, by + text_h + 10), t3, font=font_yes, fill=(180, 180, 180))

        display_image(img)
    except Exception as e:
        print(f"Gallery load error: {e}")
def _save_image_async(captured_image, filepath, filename, film_name="No Filter"):
    """Save and verify image in background — runs after preview has already resumed"""
    global saving_active
    try:
        start = time.time()
        captured_image = _apply_filter_by_name(captured_image, film_name)
        captured_image.save(filepath, "JPEG", quality=98, optimize=True)
        # fsync only this file, not the whole filesystem like os.sync()
        with open(filepath, "rb") as f:
            os.fsync(f.fileno())

        if not os.path.exists(filepath):
            print("✗ File not created")
            return

        file_size = os.path.getsize(filepath)
        if file_size < 100000:
            print(f"✗ File too small ({file_size} bytes) - DELETING")
            try:
                os.remove(filepath)
            except:
                pass
            return

        try:
            test_img = Image.open(filepath)
            test_img.verify()
            test_img.close()
        except Exception as e:
            print(f"✗ Corrupted: {e} - DELETING")
            try:
                os.remove(filepath)
            except:
                pass
            return

        print(f"✓ Saved {filename} ({file_size/1024/1024:.2f} MB) in {time.time()-start:.2f}s")
    except Exception as e:
        print(f"✗ Save error: {e}")
    finally:
        with _save_active_lock:
            saving_active -= 1

def capture_full_res(picam2):
    """
    Capture full-res image. Camera operations run under lock.
    Preview resumes immediately after camera is free — save happens in background.
    """
    global capturing, camera_started, show_focus, config_cache, saving_active
    captured_image = None
    filepath = None
    filename = None

    try:
        with camera_lock:
            capturing = True
            show_focus = True
            print("\n=== CAPTURE ===")

            os.makedirs(GALLERY_DIR, exist_ok=True)
            number = get_next_capture_number()
            filename = f"Optocamzero_{number}.jpg"
            filepath = os.path.join(GALLERY_DIR, filename)

            # Stop preview
            if camera_started:
                picam2.stop()
                camera_started = False
                time.sleep(0.05)

            # Configure and start capture
            picam2.configure(config_cache.capture_config)
            picam2.start()
            time.sleep(0.12)

            # Autofocus
            picam2.set_controls({"AfMode": 1, "AfTrigger": 0})
            focus_start = time.time()
            focused = False
            while time.time() - focus_start < 1.0:
                try:
                    metadata = picam2.capture_metadata()
                    af_state = metadata.get("AfState", 0)
                    if af_state == 2:
                        focused = True
                        print(f"✓ Focus: {time.time() - focus_start:.2f}s")
                        break
                    elif af_state == 3:
                        break
                except:
                    pass
                time.sleep(0.03)
            if not focused:
                print("⚠ AF timeout")
            show_focus = False

            # Capture
            for attempt in range(2):
                try:
                    captured_image = picam2.capture_image()
                    if captured_image and captured_image.size[0] > 0:
                        print(f"✓ Captured: {captured_image.size}")
                        break
                    captured_image = None
                    time.sleep(0.08)
                except Exception as e:
                    print(f"⚠ Attempt {attempt + 1}: {e}")
                    captured_image = None
                    time.sleep(0.08)

            time.sleep(0.05)
            picam2.stop()
            time.sleep(0.05)

            if captured_image is None:
                print("✗ Capture failed")
                return None

            # Rotate in memory (fast)
            captured_image = captured_image.transpose(Image.ROTATE_90)

        # === Camera lock released — preview restarts now ===
        capturing = False
        print("✓ Preview resuming...")

        # Save in background — filter applied inside thread, doesn't block preview
        with _save_active_lock:
            saving_active += 1
        threading.Thread(
            target=_save_image_async,
            args=(captured_image, filepath, filename, FILTERS[filter_index]),
            daemon=True
        ).start()

        return filepath

    except Exception as e:
        print(f"✗ ERROR: {e}")
        import traceback
        traceback.print_exc()
        capturing = False
        return None
def button_handler():
    """Button and joystick polling"""
    global preview_active, capture_requested, exit_requested
    global gallery_active, gallery_index, gallery_images, gallery_needs_update, gallery_confirm_delete, gallery_empty_message_time
    global awb_mode_index, awb_mode_changed, awb_changed_time, gallery_empty_message_time, gallery_confirm_delete, no_space_message_time, splash_active
    global filter_index, filter_label_time, isp_changed
    global transfer_mode, transfer_screen_shown, _transfer_last_activity, _transfer_dimmed
    global _idle_last_activity, _idle_dimmed

    last_capture = 0
    last_preview = 0
    last_joy_press = 0
    last_joy_up = 0
    last_joy_down = 0
    debounce = 0.3

    left_held_since = 0
    right_held_since = 0
    last_scroll_time = 0
    HOLD_THRESHOLD = 0.5
    FAST_INTERVAL = 0.15

    joy_press_times = []
    TRIPLE_PRESS_WINDOW = 0.8
    joy_press_was_down = False
    joy_press_down_time = 0
    joy_long_press_fired = False

    print("✓ Buttons ready")

    while not exit_requested:
        try:
            now = time.time()

            # --- Splash screen: any button closes it ---
            if splash_active:
                any_pressed = (
                    not GPIO.input(BUTTON_CAPTURE) or
                    not GPIO.input(BUTTON_PREVIEW) or
                    not GPIO.input(JOYSTICK_UP) or
                    not GPIO.input(JOYSTICK_DOWN) or
                    not GPIO.input(JOYSTICK_LEFT) or
                    not GPIO.input(JOYSTICK_RIGHT) or
                    not GPIO.input(JOYSTICK_PRESS)
                )
                if any_pressed:
                    splash_active = False
                    joy_press_times.clear()
                    print("Splash closed")
                time.sleep(0.05)
                continue

            # --- Idle activity tracking (camera mode only) ---
            if not transfer_mode:
                any_input = (
                    not GPIO.input(BUTTON_CAPTURE) or
                    not GPIO.input(BUTTON_PREVIEW) or
                    not GPIO.input(JOYSTICK_UP) or
                    not GPIO.input(JOYSTICK_DOWN) or
                    not GPIO.input(JOYSTICK_LEFT) or
                    not GPIO.input(JOYSTICK_RIGHT) or
                    not GPIO.input(JOYSTICK_PRESS)
                )
                if any_input:
                    _idle_last_activity = time.time()
                    if _idle_dimmed:
                        _idle_dimmed = False
                        set_backlight(True)
                        time.sleep(0.3)
                        continue

            # --- Joystick Up: delete confirmation (gallery only) ---
            if not GPIO.input(JOYSTICK_UP):
                if now - last_joy_up > debounce:
                    last_joy_up = now
                    if gallery_active and gallery_images:
                        if gallery_confirm_delete:
                            # Confirmed — delete image
                            filepath = gallery_images[gallery_index]
                            try:
                                os.remove(filepath)
                                print(f"✓ Deleted: {os.path.basename(filepath)}")
                                # Remove any cached thumbnails
                                fname = os.path.basename(filepath)
                                thumb_dir = os.path.join(GALLERY_DIR, ".thumbs")
                                for f in os.listdir(thumb_dir) if os.path.exists(thumb_dir) else []:
                                    if f.startswith(fname + "_"):
                                        try: os.remove(os.path.join(thumb_dir, f))
                                        except: pass
                            except Exception as e:
                                print(f"✗ Delete error: {e}")
                            gallery_images.pop(gallery_index)
                            gallery_confirm_delete = False
                            if not gallery_images:
                                gallery_active = False
                                preview_active = True
                                print("Gallery empty, closing")
                            else:
                                gallery_index = min(gallery_index, len(gallery_images) - 1)
                                gallery_needs_update = True
                        else:
                            # Show confirm dialog
                            gallery_confirm_delete = True
                            gallery_needs_update = True
                    elif preview_active and not capturing:
                        filter_index = (filter_index - 1) % len(FILTERS)
                        filter_label_time = now
                        isp_changed = True
                        print(f"Filter: {FILTERS[filter_index]}")

            # --- Joystick Down: dismiss delete dialog / cycle filter forward ---
            if not GPIO.input(JOYSTICK_DOWN):
                if now - last_joy_down > debounce:
                    last_joy_down = now
                    if gallery_active and gallery_confirm_delete:
                        gallery_confirm_delete = False
                        gallery_needs_update = True
                    elif preview_active and not capturing:
                        filter_index = (filter_index + 1) % len(FILTERS)
                        filter_label_time = now
                        isp_changed = True
                        print(f"Filter: {FILTERS[filter_index]}")

            # --- Joystick Press: long press = transfer mode, triple = splash, short = gallery ---
            joy_is_down = not GPIO.input(JOYSTICK_PRESS)

            if joy_is_down and not joy_press_was_down:
                # Button just pressed
                joy_press_down_time = now
                joy_long_press_fired = False
                joy_press_was_down = True

            elif joy_is_down and joy_press_was_down:
                # Still held — check for long press (1.5s)
                if not joy_long_press_fired and now - joy_press_down_time >= 1.5:
                    joy_long_press_fired = True
                    joy_press_times.clear()
                    transfer_mode = not transfer_mode
                    transfer_screen_shown = False
                    if transfer_mode:
                        gallery_active = False
                        splash_active = False
                        preview_active = False
                        print("Transfer mode ON")
                        _transfer_last_activity = time.time()
                        _transfer_dimmed = False
                        subprocess.Popen(["sudo", "systemctl", "start", "optocam-hotspot.service"])
                        subprocess.Popen(["sudo", "systemctl", "start", "optocam-gallery.service"])
                    else:
                        preview_active = True
                        print("Transfer mode OFF")
                        if _transfer_dimmed:
                            set_backlight(True)
                        _transfer_dimmed = False
                        _idle_last_activity = time.time()
                        _idle_dimmed = False
                        subprocess.Popen(["sudo", "systemctl", "stop", "optocam-hotspot.service"])
                        subprocess.Popen(["sudo", "systemctl", "stop", "optocam-gallery.service"])

            elif not joy_is_down and joy_press_was_down:
                # Button just released — handle short press
                joy_press_was_down = False
                if not joy_long_press_fired and now - joy_press_down_time > 0.02:
                    joy_press_times.append(now)
                    joy_press_times[:] = [t for t in joy_press_times if now - t < TRIPLE_PRESS_WINDOW]
                    if len(joy_press_times) >= 3:
                        joy_press_times.clear()
                        gallery_active = False
                        gallery_confirm_delete = False
                        transfer_mode = False
                        splash_active = True
                        print("Splash activated")
                    elif transfer_mode:
                        pass  # short press does nothing in transfer mode
                    elif gallery_active:
                        if gallery_confirm_delete:
                            gallery_confirm_delete = False
                            gallery_needs_update = True
                        else:
                            gallery_active = False
                            preview_active = True
                            print("Gallery closed")
                    else:
                        gallery_images = get_gallery_images()
                        if gallery_images:
                            gallery_index = len(gallery_images) - 1
                            gallery_active = True
                            preview_active = False
                            gallery_needs_update = True
                            print(f"Gallery opened ({len(gallery_images)} images)")
                        else:
                            gallery_empty_message_time = time.time()
                            print("Gallery empty")

            # --- Skip everything else in transfer mode ---
            if transfer_mode:
                time.sleep(0.05)
                continue

            # --- Capture button ---
            if not GPIO.input(BUTTON_CAPTURE):
                if now - last_capture > debounce:
                    last_capture = now
                    if gallery_active:
                        if gallery_confirm_delete:
                            gallery_confirm_delete = False
                            gallery_needs_update = True
                        else:
                            gallery_active = False
                            preview_active = True
                            print("Gallery closed")
                    elif preview_active and not capturing:
                        try:
                            check_path = GALLERY_DIR if os.path.exists(GALLERY_DIR) else os.path.dirname(GALLERY_DIR)
                            stat = os.statvfs(check_path)
                            free_bytes = stat.f_bavail * stat.f_bsize
                            if free_bytes < 20 * 1024 * 1024:
                                no_space_message_time = time.time()
                                print("✗ No space in card")
                            else:
                                capture_requested = True
                                print("📸 CAPTURE")
                        except:
                            capture_requested = True
                            print("📸 CAPTURE")

            # --- Preview toggle button ---
            if not GPIO.input(BUTTON_PREVIEW):
                if now - last_preview > debounce:
                    if gallery_active and gallery_confirm_delete:
                        gallery_confirm_delete = False
                        gallery_needs_update = True
                        last_preview = now
                    elif not gallery_active:
                        preview_active = not preview_active
                        last_preview = now
                        print(f"👁 {'ON' if preview_active else 'OFF'}")

            # --- Joystick Left / Right (gallery navigation) ---
            if gallery_active and gallery_images:
                left_pressed = not GPIO.input(JOYSTICK_LEFT)
                right_pressed = not GPIO.input(JOYSTICK_RIGHT)

                if left_pressed:
                    if gallery_confirm_delete:
                        gallery_confirm_delete = False
                        gallery_needs_update = True
                        left_held_since = now  # prevent immediate nav
                    elif left_held_since == 0:
                        left_held_since = now
                        gallery_index = (gallery_index - 1) % len(gallery_images)
                        gallery_needs_update = True
                        last_scroll_time = now
                    elif now - left_held_since > HOLD_THRESHOLD:
                        if now - last_scroll_time > FAST_INTERVAL:
                            gallery_index = (gallery_index - 1) % len(gallery_images)
                            gallery_needs_update = True
                            last_scroll_time = now
                else:
                    left_held_since = 0

                if right_pressed:
                    if gallery_confirm_delete:
                        gallery_confirm_delete = False
                        gallery_needs_update = True
                        right_held_since = now
                    elif right_held_since == 0:
                        right_held_since = now
                        gallery_index = (gallery_index + 1) % len(gallery_images)
                        gallery_needs_update = True
                        last_scroll_time = now
                    elif now - right_held_since > HOLD_THRESHOLD:
                        if now - last_scroll_time > FAST_INTERVAL:
                            gallery_index = (gallery_index + 1) % len(gallery_images)
                            gallery_needs_update = True
                            last_scroll_time = now
                else:
                    right_held_since = 0

            # --- Joystick Left / Right (AWB mode in preview) ---
            elif preview_active and not capturing:
                if not GPIO.input(JOYSTICK_LEFT):
                    if now - last_scroll_time > debounce:
                        last_scroll_time = now
                        awb_mode_index = (awb_mode_index - 1) % len(AWB_MODES)
                        awb_mode_changed = True
                        awb_changed_time = now
                        print(f"AWB: {AWB_MODES[awb_mode_index][1]}")
                if not GPIO.input(JOYSTICK_RIGHT):
                    if now - last_scroll_time > debounce:
                        last_scroll_time = now
                        awb_mode_index = (awb_mode_index + 1) % len(AWB_MODES)
                        awb_mode_changed = True
                        awb_changed_time = now
                        print(f"AWB: {AWB_MODES[awb_mode_index][1]}")

            time.sleep(0.02)

        except Exception as e:
            print(f"Button error: {e}")
            time.sleep(0.1)
# Global state
preview_active = True
capture_requested = False
exit_requested = False
camera_started = False
capturing = False
show_focus = False
capture_dot_time = 0
gallery_active = False
gallery_index = 0
gallery_images = []
gallery_needs_update = False
gallery_confirm_delete = False
gallery_empty_message_time = 0
no_space_message_time = 0
splash_active = False
awb_mode_index = AWB_MODES.index(next(m for m in AWB_MODES if m[1] == "Daylight"))
awb_mode_changed = False
awb_changed_time = 0
filter_index = FILTERS.index("Film Standard")
saving_active = 0
_save_active_lock = threading.Lock()
filter_label_time = 0
isp_changed = False
transfer_mode = False
transfer_screen_shown = False
_transfer_last_refresh = 0
_transfer_last_activity = 0.0
_transfer_dimmed = False
_idle_last_activity = 0.0
_idle_dimmed = False
IDLE_DIM_TIMEOUT = 90.0
def main():
    log("main() called")
    global preview_active, capture_requested, exit_requested, camera_started
    global capturing, show_focus, capture_dot_time, config_cache
    global gallery_active, gallery_index, gallery_images, gallery_needs_update, gallery_confirm_delete, gallery_empty_message_time
    global awb_mode_index, awb_mode_changed, awb_changed_time, no_space_message_time, splash_active
    global filter_index, filter_label_time, isp_changed, saving_active
    global transfer_mode, transfer_screen_shown, _transfer_last_refresh, _transfer_last_activity, _transfer_dimmed
    global _idle_last_activity, _idle_dimmed

    gc.disable()

    print("=" * 50)
    print("CAMERA - FAST & ROBUST")
    print("=" * 50)

    log("Initializing display...")
    init_display()
    set_backlight(True)
    show_splash()
    log("Display ready - showing splash screen")

    log("Importing Picamera2 (this may take 10 seconds)...")
    print("Initializing camera...")
    from picamera2 import Picamera2
    log("Picamera2 imported")

    picam2 = Picamera2()
    clear_display()
    config_cache = CameraConfigCache(picam2)

    print("\n" + "=" * 50)
    print("FEATURES:")
    print("✓ Enhanced black levels")
    print("✓ FAST capture (1.5-2s freeze)")
    print("✓ Robust verification (no 0 KB files)")
    print("✓ Auto-delete corrupted files")
    print("✓ Green dot = capture confirmation")
    print(f"✓ KEY1 (GPIO {BUTTON_CAPTURE}): Capture / Close gallery")
    print(f"✓ KEY2 (GPIO {BUTTON_PREVIEW}): Toggle preview")
    print(f"✓ Joystick press: Open/close gallery")
    print(f"✓ Joystick left/right: Navigate gallery")
    print("=" * 50 + "\n")

    button_thread = threading.Thread(target=button_handler, daemon=True)
    button_thread.start()

    frame_count = 0
    last_fps_report = time.time()
    _idle_last_activity = time.time()

    try:
        while not exit_requested:

            # === IDLE DIM CHECK (camera mode only) ===
            if not transfer_mode and not splash_active:
                if not _idle_dimmed and time.time() - _idle_last_activity > IDLE_DIM_TIMEOUT:
                    _idle_dimmed = True
                    set_backlight_brightness(8)

            # === SPLASH MODE ===
            if splash_active:
                if camera_started:
                    with camera_lock:
                        if camera_started:
                            picam2.stop()
                            camera_started = False
                            capture_dot_time = 0
                set_backlight(True)
                show_splash()
                time.sleep(0.05)

            # === TRANSFER MODE ===
            elif transfer_mode:
                if camera_started:
                    with camera_lock:
                        if camera_started:
                            picam2.stop()
                            camera_started = False
                            capture_dot_time = 0
                if not transfer_screen_shown:
                    transfer_screen_shown = True
                    _transfer_last_refresh = 0
                    _transfer_last_activity = time.time()
                    _transfer_dimmed = False
                    set_backlight(True)
                # Dim after 30s inactivity
                any_pressed = (
                    not GPIO.input(BUTTON_CAPTURE) or
                    not GPIO.input(BUTTON_PREVIEW) or
                    not GPIO.input(JOYSTICK_UP) or
                    not GPIO.input(JOYSTICK_DOWN) or
                    not GPIO.input(JOYSTICK_LEFT) or
                    not GPIO.input(JOYSTICK_RIGHT) or
                    not GPIO.input(JOYSTICK_PRESS)
                )
                if any_pressed:
                    if _transfer_dimmed:
                        _transfer_dimmed = False
                        set_backlight(True)
                        _transfer_last_refresh = 0
                    _transfer_last_activity = time.time()
                elif not _transfer_dimmed and time.time() - _transfer_last_activity > 30:
                    _transfer_dimmed = True
                    set_backlight_brightness(8)
                if time.time() - _transfer_last_refresh >= 0.5:
                    _transfer_last_refresh = time.time()
                    show_transfer_mode_screen()
                time.sleep(0.1)

            # === GALLERY MODE ===
            elif gallery_active:
                # Stop camera if running
                if camera_started:
                    with camera_lock:
                        if camera_started:
                            picam2.stop()
                            camera_started = False
                            capture_dot_time = 0

                # Display current gallery image when needed
                if gallery_needs_update and gallery_images:
                    gallery_needs_update = False
                    idx = gallery_index  # snapshot index before load
                    total = len(gallery_images)
                    set_backlight(True)
                    display_gallery_image(gallery_images[idx], idx + 1, total, gallery_confirm_delete)
                    print(f"Gallery: {idx + 1}/{total}")

                time.sleep(0.02)

            # === PREVIEW MODE ===
            elif preview_active and not capturing:
                if not camera_started:
                    with camera_lock:
                        set_backlight(True)
                        picam2.configure(config_cache.preview_config)
                        picam2.start()
                        camera_started = True
                        print("✓ Preview started\n")

                if capture_requested:
                    capture_requested = False
                    capture_dot_time = time.time()
                    threading.Thread(
                        target=capture_full_res,
                        args=(picam2,),
                        daemon=True
                    ).start()

                if camera_started and not capturing:
                    try:
                        with camera_lock:
                            if camera_started and not capturing:
                                if awb_mode_changed:
                                    awb_mode_changed = False
                                    picam2.set_controls({"AwbMode": AWB_MODES[awb_mode_index][0]})
                                if isp_changed:
                                    isp_changed = False
                                    picam2.set_controls(_FILM_ISP[FILTERS[filter_index]])
                                req = picam2.capture_request()
                                preview_image = req.make_image("main")
                                metadata = req.get_metadata()
                                req.release()
                                preview_image = preview_image.transpose(Image.ROTATE_90)
                                preview_image = apply_filter(preview_image)
                                if preview_image.size != (240, 240):
                                    preview_image = preview_image.resize((240, 240), Image.LANCZOS)
                                if capture_dot_time > 0 and (time.time() - capture_dot_time) >= 2.0:
                                    capture_dot_time = 0

                                # --- ISO (bottom left) and Shutter (bottom right) ---
                                from PIL import ImageDraw
                                font_hud = load_font(25)
                                font_awb_full  = load_font(25)
                                font_awb_abbr  = load_font(24)
                                tmp_draw = ImageDraw.Draw(preview_image)

                                iso_val = str(nearest_standard_iso(metadata.get("AnalogueGain", 1.0)))
                                exp = metadata.get("ExposureTime", 10000)
                                shutter_val = nearest_standard_shutter(exp) if exp > 0 else "?"
                                awb_switching = time.time() - awb_changed_time < 1.0
                                awb_label = (AWB_MODES[awb_mode_index][1] if awb_switching
                                             else AWB_MODES[awb_mode_index][2])
                                font_awb = font_awb_full if awb_switching else font_awb_abbr

                                # AWB position: top left — subtract top bearing so visible glyph
                                # sits exactly 15px from the edge, matching ISO/shutter bottom gap
                                b_awb = tmp_draw.textbbox((0, 0), awb_label, font=font_awb)
                                ax = 15
                                ay = 15 - b_awb[1]

                                # ISO position: bottom left
                                b_iso = tmp_draw.textbbox((0, 0), iso_val, font=font_hud)
                                ix = 15

                                # Shutter position: bottom right
                                b_sh = tmp_draw.textbbox((0, 0), shutter_val, font=font_hud)
                                sx = 240 - 15 - (b_sh[2] - b_sh[0])

                                # Shared bottom y — use max height so both sit on the same baseline
                                hud_bottom_y = 240 - 15 - max(b_iso[3] - b_iso[1], b_sh[3] - b_sh[1])
                                iy = hud_bottom_y
                                sy = hud_bottom_y

                                # v2: build a single combined RGBA overlay, do ONE convert/composite/convert
                                awb_shadow = get_cached_shadow("awb", awb_label, ax, ay, font_awb)
                                iso_shadow = get_cached_shadow("iso", iso_val, ix, iy, font_hud)
                                sh_shadow = get_cached_shadow("shutter", shutter_val, sx, sy, font_hud)

                                overlay = Image.alpha_composite(awb_shadow, iso_shadow)
                                overlay = Image.alpha_composite(overlay, sh_shadow)

                                # Transient centre messages (mutually exclusive)
                                centre_msg = None
                                centre_msg_key = None
                                if gallery_empty_message_time > 0 and time.time() - gallery_empty_message_time < 1.0:
                                    centre_msg = "No image in card"
                                    centre_msg_key = "empty_msg"
                                elif gallery_empty_message_time > 0:
                                    gallery_empty_message_time = 0
                                if centre_msg is None and no_space_message_time > 0 and time.time() - no_space_message_time < 1.0:
                                    centre_msg = "No space in card"
                                    centre_msg_key = "no_space_msg"
                                elif centre_msg is None and no_space_message_time > 0:
                                    no_space_message_time = 0
                                if (centre_msg is None and filter_label_time > 0
                                        and time.time() - filter_label_time < 1.5
                                        and gallery_empty_message_time == 0
                                        and no_space_message_time == 0):
                                    centre_msg = FILTERS[filter_index]
                                    centre_msg_key = "filter_label"
                                elif centre_msg is None and filter_label_time > 0:
                                    filter_label_time = 0

                                if centre_msg is not None:
                                    font_msg = load_font(25)
                                    _tmp = ImageDraw.Draw(preview_image)
                                    bbox = _tmp.textbbox((0, 0), centre_msg, font=font_msg)
                                    mx = (240 - (bbox[2] - bbox[0])) // 2
                                    my = (240 - (bbox[3] - bbox[1])) // 2
                                    msg_shadow = get_cached_shadow(centre_msg_key, centre_msg, mx, my, font_msg)
                                    overlay = Image.alpha_composite(overlay, msg_shadow)

                                # Filter indicator always on top
                                indicator = get_filter_indicator(FILTERS[filter_index])
                                overlay = Image.alpha_composite(overlay, indicator)

                                # Single convert → composite → convert for all shadows/overlays
                                preview_image = preview_image.convert("RGBA")
                                preview_image = Image.alpha_composite(preview_image, overlay)
                                preview_image = preview_image.convert("RGB")

                                draw_hud = ImageDraw.Draw(preview_image)
                                draw_hud.text((ax, ay), awb_label, font=font_awb, fill=(255, 255, 255))
                                draw_hud.text((ix, iy), iso_val, font=font_hud, fill=(255, 255, 255))
                                draw_hud.text((sx, sy), shutter_val, font=font_hud, fill=(255, 255, 255))

                                if centre_msg is not None:
                                    draw_hud.text((mx, my), centre_msg, font=font_msg, fill=(255, 255, 255))

                                # Saving spinner — bottom centre, vertically aligned with ISO/shutter
                                if saving_active > 0:
                                    sp_r  = 7
                                    sp_cx = 120
                                    sp_cy = hud_bottom_y + (b_iso[1] + b_iso[3]) // 2
                                    sp_a  = int(time.time() * 360) % 360
                                    sp_box   = [sp_cx-sp_r,   sp_cy-sp_r,   sp_cx+sp_r,   sp_cy+sp_r]
                                    sp_box_s = [sp_cx-sp_r+1, sp_cy-sp_r+1, sp_cx+sp_r+1, sp_cy+sp_r+1]
                                    draw_hud.arc(sp_box_s, start=sp_a, end=sp_a+270, fill=(0, 0, 0),       width=2)
                                    draw_hud.arc(sp_box,   start=sp_a, end=sp_a+270, fill=(255, 255, 255), width=2)

                                display_image(preview_image)
                                frame_count += 1
                                if time.time() - last_fps_report >= 5.0:
                                    elapsed = time.time() - last_fps_report
                                    fps = frame_count / elapsed
                                    print(f"📊 {fps:.1f} fps")
                                    frame_count = 0
                                    last_fps_report = time.time()
                    except Exception as e:
                        print(f"Preview error: {e}")
                        time.sleep(0.1)

                time.sleep(0.001)

            # === PREVIEW OFF ===
            else:
                if camera_started and not capturing:
                    with camera_lock:
                        if camera_started:
                            picam2.stop()
                            camera_started = False
                            set_backlight(False)
                            clear_display()
                            capture_dot_time = 0
                            print("✓ Preview stopped\n")
                time.sleep(0.05)

    except KeyboardInterrupt:
        print("\n\nShutting down...")
        exit_requested = True

    finally:
        with camera_lock:
            if camera_started:
                picam2.stop()
        set_backlight(False)
        GPIO.cleanup()
        spi.close()
        gc.enable()
        print("✓ Shutdown complete!")
if __name__ == "__main__":
    main()
