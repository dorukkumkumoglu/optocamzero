#!/usr/bin/env python3
"""OptoCam Gallery Server — serves photos over WiFi hotspot"""
import os
import io
import json
import time
import zipfile
import tempfile
from flask import Flask, send_from_directory, render_template_string, Response, request
from markupsafe import Markup

# Small counter-clockwise "reset" arrow shown next to each slider label. Markup
# so Jinja renders the SVG instead of escaping it.
RESET_ICON = Markup(
    '<svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" '
    'stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">'
    '<polyline points="1 4 1 10 7 10"/>'
    '<path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/></svg>')
# Desktop preview enlarge toggle: plain corner brackets, drawn here (no icon set).
# .z-out = corners out (enlarge), .z-in = corners in (shrink); CSS swaps them.
_ZOOM_SVG = ('<svg class="%s" viewBox="0 0 24 24" width="16" height="16" fill="none" '
             'stroke="currentColor" stroke-width="2.2" stroke-linecap="round" '
             'stroke-linejoin="round">%s</svg>')
ZOOM_ICONS = Markup(
    _ZOOM_SVG % ('z-out', '<path d="M3 9V3h6"/><path d="M15 3h6v6"/>'
                          '<path d="M21 15v6h-6"/><path d="M9 21H3v-6"/>') +
    _ZOOM_SVG % ('z-in',  '<path d="M9 3v6H3"/><path d="M21 9h-6V3"/>'
                          '<path d="M15 21v-6h6"/><path d="M3 15h6v6"/>'))

# Device paths, overridable via env only so this same file can be run on a
# laptop for UI work (the defaults are the on-device locations).
PHOTOS_DIR = os.environ.get("OPTOCAM_PHOTOS", "/home/dkumkum/photos")
HOME_DIR   = os.environ.get("OPTOCAM_HOME", "/home/dkumkum")   # logo + font
SHARE_DIR  = os.environ.get("OPTOCAM_SHARE", "/usr/share/optocam")  # baked assets
DATA_DIR   = os.environ.get("OPTOCAM_DATA", "/data")           # persistent settings

MEDIA_EXTS = (".jpg", ".gif")

app = Flask(__name__)

# token -> {"sent": int, "total": int, "ts": float} — live zip download progress.
DOWNLOAD_PROGRESS = {}
# token -> {"done": int, "total": int, "active": bool} — Format-data progress.
FORMAT_PROGRESS = {}

# ── Settings persisted to /data (read by the C++ firmware at boot / capture) ──
# These filenames are a contract with optocam_app.cpp — keep them in sync.
DISPCAL      = os.path.join(DATA_DIR, ".dispcal")      # "R G B [SAT]" saved white point
CALIB_LIVE   = os.path.join(DATA_DIR, ".calib")        # live gains while dragging sliders
CALIB_ACTIVE = os.path.join(DATA_DIR, ".calib_active") # flag: show calib pattern on LCD
# Filter Maker live view: same heartbeat pattern as calibration. While the flag
# stays fresh the firmware runs the camera (640x640 GIF stream) and drops
# unfiltered JPEG frames into tmpfs; /live-frame just serves the newest one.
LIVE_ACTIVE = os.path.join(DATA_DIR, ".live_active")
LIVE_FRAME  = os.environ.get("OPTOCAM_LIVE_FRAME", "/run/optocam-live.jpg")
LIVE_SIM    = os.environ.get("OPTOCAM_LIVE_SIM") == "1"   # laptop dev: synthesize frames
CALIB_IMG    = os.path.join(DATA_DIR, ".calib_img")    # pattern choice, one of CALIB_PATTERNS
# Single source of truth for the calibration pattern names — every route and
# the page's JS render from this. (Three separate copies of this tuple is how
# the CONTRAST pattern once flipped back to color.) The firmware's
# CALIB_IMG_NAMES in optocam_app.cpp is the one remaining manual sync point.
CALIB_PATTERNS = ("color", "gray", "photo")
# The synthetic ramps are tiny PNGs; the photo reference is a real photo and
# ships as a 720px JPEG (the LCD copy is the separate 240px calib_photo.rgb).
def _calib_asset_file(p):
    return "calib_photo.jpg" if p == "photo" else "calib_%s.png" % p
# Cache-buster for the pattern art: derived from the newest pattern file's
# mtime at startup, so changed art busts the day-long asset cache by itself.
def _calib_asset_version():
    try:
        return str(int(max(os.path.getmtime(os.path.join(SHARE_DIR, _calib_asset_file(p)))
                           for p in CALIB_PATTERNS)))
    except OSError:
        return "0"
CALIB_ASSET_V = _calib_asset_version()
CALIB_UI     = os.path.join(DATA_DIR, ".calib_ui")     # slider positions (six ints)
BAKED_DISPCAL = os.path.join(SHARE_DIR, "dispcal")     # per-batch factory white point
GIFCFG   = os.path.join(DATA_DIR, ".gifcfg")           # "FRAMES INTERVAL"
DEFAULTS = os.path.join(DATA_DIR, ".defaults")         # "FILTER_IDX AWB_IDX"
AE_LIMITS = os.path.join(DATA_DIR, ".aelimits")        # "SHUTTER_DEN METERING_IDX"
# Custom filters: five slots, each its own file. Line 1 is the firmware params
# ("y0..y4 mr mg mb sat grain"), line 2 the user-given name. A slot exists iff
# its file does — the camera only lists customs that exist.
MAX_EFFECT_SLOTS = 5
EFFECT_NAME_MAX = 14                                   # fits the HUD filter pill
LEGACY_EFFECT = os.path.join(DATA_DIR, ".effect")      # pre-slot single custom
def effect_slot_path(i):
    return os.path.join(DATA_DIR, ".effect%d" % i)
THEME    = os.path.join(DATA_DIR, ".theme")             # "dark" | "light" | "yellow"
THEMES = ("dark", "light", "yellow")


def read_theme():
    try:
        with open(THEME) as f:
            t = f.read().strip()
        return t if t in THEMES else "dark"
    except Exception:
        return "dark"

# Factory defaults (mirror optocam_app.cpp / optocam_hud.h).
GIF_FRAMES_DEFAULT = 10
GIF_INTERVAL_DEFAULT = 0.5
FILTER_DEFAULT = 0   # "Film Standard"
WB_DEFAULT = 0       # "Daylight"

# Device/firmware identity for the Data & Device info panel.
FIRMWARE_VERSION_FALLBACK = "1.2.0"      # used if /etc/optocam-version is absent
LICENSE_NAME = "CC BY-SA 4.0"
LICENSE_URL = "https://creativecommons.org/licenses/by-sa/4.0/"
COPYRIGHT = "2026 Doruk Kumkumoğlu"
GITHUB_URL = "https://github.com/dorukkumkumoglu/optocamzero"
# Index order MUST match FILTERS[] and AWB_MODES[] in optocam_hud.h.
# "Custom" (index 9) is the user's Effect-Maker filter.
# The nine baked-in filters. Customs are appended dynamically (camera indices
# 9..13) in slot order, only for slots that exist.
FILTER_OPTIONS = ["Film Standard", "Punch", "B&W", "Deep", "Sand",
                  "Eterna", "TRI-X", "Cutout", "No Filter"]
WB_OPTIONS = ["Daylight", "Cloudy", "Indoor", "Tungsten", "Auto"]

# Screen-calibration slider ranges (all integer slider units, default 100 unless
# noted). Channel gains are absolute percent gains; their defaults are the baked
# per-batch panel white point, so the factory calibration is visible in the UI
# and reset returns to it (never to a blue-tinted flat 1.00).
#   brightness 20..100 (backlight duty %, NOT a pixel gain — real luminance)
#   contrast 55..175 (absolute ÷100; factory default 115)  gamma 50..200 (÷100)
#   saturation 0..200 (÷100)          temp -100..100 (0, warm..cool R/B seesaw)
#   tint -100..100 (0, green..magenta G seesaw)
#   red/green/blue 50..150
BASE_CONTRAST = 1.15
TEMP_MAX_DELTA = 0.30   # temperature ±100 -> this per-channel R/B swing (wide)
TINT_MAX_DELTA = 0.30   # tint ±100 -> this green-channel swing (magenta = G down)
CALIB_RANGES = {
    "brightness": (20, 100), "contrast": (55, 175), "gamma": (50, 200),
    "saturation": (0, 200), "temp": (-100, 100),
    "red": (50, 150), "green": (50, 150), "blue": (50, 150),
    "tint": (-100, 100),
}
# NOTE: .calib_ui is positional — "tint" must stay LAST so files saved before
# the tint slider existed still parse (missing trailing value -> default).
CALIB_KEYS = ["brightness", "contrast", "gamma", "saturation", "temp",
              "red", "green", "blue", "tint"]


def _clamp(v, lo, hi):
    return lo if v < lo else hi if v > hi else v


def read_baked_panel():
    """Factory per-batch white-point gains (R, G, B). These seed the channel
    sliders' defaults so 'reset' returns to the panel's factory neutral."""
    try:
        with open(BAKED_DISPCAL) as f:
            parts = f.read().split()
        r, g, b = (float(parts[0]), float(parts[1]), float(parts[2]))
        return r, g, b
    except Exception:
        return 1.0, 1.0, 1.0


def calib_defaults():
    """All sliders neutral; temperature/tint centered. Channel gains default to
    the baked panel white point — factory calibration shows in the UI."""
    d = {k: 100 for k in CALIB_KEYS}
    d["temp"] = 0
    d["tint"] = 0
    d["contrast"] = int(round(BASE_CONTRAST * 100))   # factory 115, visible
    p_r, p_g, p_b = read_baked_panel()
    for k, v in (("red", p_r), ("green", p_g), ("blue", p_b)):
        d[k] = _clamp(int(round(v * 100)), *CALIB_RANGES[k])
    return d


def calib_params(d):
    """Pull + clamp the eight calibration sliders from a request body."""
    fallback = calib_defaults()
    out = {}
    for k in CALIB_KEYS:
        lo, hi = CALIB_RANGES[k]
        try:
            out[k] = _clamp(int(round(float(d.get(k, fallback[k])))), lo, hi)
        except (TypeError, ValueError):
            out[k] = fallback[k]
    return out


def calib_line(p):
    """Seven-float firmware line: 'GR GG GB SAT GAMMA CONTRAST BACKLIGHT'.
    Channel sliders are absolute gains (their DEFAULTS carry the baked panel
    white point); colour temperature/tint fold in on top. Contrast is likewise
    absolute — the slider defaults to the device's baseline 115. Brightness is
    NOT a pixel gain — it rides separately as backlight duty, so it can't clip
    highlights or cost tonal resolution."""
    t = (p["temp"] / 100.0) * TEMP_MAX_DELTA
    tn = (p["tint"] / 100.0) * TINT_MAX_DELTA
    gr = round(_clamp((1 - t) * p["red"] / 100.0, 0.2, 2.5), 3)
    gg = round(_clamp((1 - tn) * p["green"] / 100.0, 0.2, 2.5), 3)
    gb = round(_clamp((1 + t) * p["blue"] / 100.0, 0.2, 2.5), 3)
    sat = round(_clamp(p["saturation"] / 100.0, 0.0, 2.5), 3)
    gamma = round(_clamp(p["gamma"] / 100.0, 0.3, 3.0), 3)
    contrast = round(_clamp(p["contrast"] / 100.0, 0.2, 2.5), 3)
    backlight = round(_clamp(p["brightness"] / 100.0, 0.2, 1.0), 3)
    return "%.3f %.3f %.3f %.3f %.3f %.3f %.3f\n" % (
        gr, gg, gb, sat, gamma, contrast, backlight)


def _atomic_write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(text)
    os.replace(tmp, path)


def read_calib_ui():
    """Saved slider positions (all eight). Defaults if unsaved."""
    d = calib_defaults()
    try:
        with open(CALIB_UI) as f:
            vals = f.read().split()
        for k, v in zip(CALIB_KEYS, vals):
            lo, hi = CALIB_RANGES[k]
            d[k] = _clamp(int(round(float(v))), lo, hi)
    except Exception:
        pass
    return d


def read_gifcfg():
    try:
        with open(GIFCFG) as f:
            fr, iv = f.read().split()[:2]
        return int(fr), float(iv)
    except Exception:
        return GIF_FRAMES_DEFAULT, GIF_INTERVAL_DEFAULT


# Factory = today's effective behaviour: the app has always capped frames at
# 1/50 s, and with shutter clamped the AGC pushes analogue gain to the
# sensor's real maximum (~16x = ISO 1600) in low light — the HUD's ISO
# readout tops out there.
# Minimum shutter 1/x s. Ends at 125: the IMX708's binned preview mode runs at
# most 120fps, so ~1/120 is the shortest frame the sensor can produce — faster
# caps were silently clamped by the pipeline and lied to the user.
SHUTTER_OPTIONS = (30, 40, 50, 60, 80, 100, 125)
SHUTTER_DEFAULT = 50                       # the preview AE cap the camera ships with
# AeMeteringMode by index. Index 2 is libcamera's "Matrix", but the RPi tuning
# gives it uniform zone weights — whole-frame averaging — and the Pi docs call
# it "average", so the UI does too.
METERING_OPTIONS = ("CENTER", "SPOT", "AVERAGE")
METERING_DEFAULT = 0                              # centre-weighted (tuning default)
DGAIN_OPTIONS = ("OFF", "ON")
DGAIN_DEFAULT = 0                                 # sensor's native 16x analog ceiling


def read_ae_limits():
    """Exposure settings for the camera: (shutter_den, metering_idx, dgain).
    Values are snapped to the offered options so a hand-edited file can't
    render a pill state the UI doesn't have; a missing file means factory."""
    try:
        with open(AE_LIMITS) as f:
            parts = f.read().split()
        den = int(parts[0])
        met = int(parts[1]) if len(parts) > 1 else METERING_DEFAULT
        dgn = int(parts[2]) if len(parts) > 2 else DGAIN_DEFAULT
        if den not in SHUTTER_OPTIONS:   # e.g. a stale 160/200/250 file: snap
            den = min(SHUTTER_OPTIONS, key=lambda o: abs(o - den))
        return (den,
                met if 0 <= met < len(METERING_OPTIONS) else METERING_DEFAULT,
                dgn if dgn in (0, 1) else DGAIN_DEFAULT)
    except Exception:
        return SHUTTER_DEFAULT, METERING_DEFAULT, DGAIN_DEFAULT


def read_defaults():
    """Startup (filter_idx, wb_idx). File order is 'FILTER WB' (C++ fscanf).
    The filter index may reach into the custom range (9..13); callers that
    need it valid clamp against the currently existing customs."""
    try:
        with open(DEFAULTS) as f:
            fi, ai = f.read().split()[:2]
        return int(fi), int(ai)
    except Exception:
        return FILTER_DEFAULT, WB_DEFAULT


def _rm(path):
    try:
        os.remove(path)
    except OSError:
        pass


# ── Effect Maker (custom filters, camera indices 9..13). Each slot file's
# first line is "y0 y1 y2 y3 y4  mr mg mb  sat grain": a 5-point tone curve
# (0-255 at x=0,64,128,192,255), R/G/B tint multipliers, ISP saturation and
# grain. Second line is the display name. ──
EFFECT_FIELDS = ("curve", "red", "green", "blue", "saturation", "grain", "smooth")


def effect_defaults():
    return {"curve": [0, 64, 128, 192, 255],
            "red": 100, "green": 100, "blue": 100, "saturation": 100, "grain": 0,
            "smooth": 0}   # 0 = linear segments, 1 = monotone-cubic curve


def sanitize_effect_name(name, slot):
    """Printable-ASCII subset the HUD font is known to cover; falls back to the
    slot's default name rather than ever writing an empty line."""
    out = "".join(c for c in str(name or "")
                  if c.isalnum() or c in " ._-")[:EFFECT_NAME_MAX].strip()
    return out or ("Custom %d" % (slot + 1))


def _parse_effect_text(text, slot):
    """File text -> UI dict, or None if the params line is unusable. The 11th
    number (curve interpolation, 0 linear / 1 smooth) is optional — files from
    before the toggle existed have ten and read as linear."""
    lines = text.splitlines()
    try:
        v = [float(x) for x in lines[0].split()[:11]]
    except (IndexError, ValueError):
        return None
    if len(v) < 10:
        return None
    d = effect_defaults()
    d["curve"] = [int(_clamp(round(v[i]), 0, 255)) for i in range(5)]
    d["red"] = int(_clamp(round(v[5] * 100), 50, 150))
    d["green"] = int(_clamp(round(v[6] * 100), 50, 150))
    d["blue"] = int(_clamp(round(v[7] * 100), 50, 150))
    d["saturation"] = int(_clamp(round(v[8] * 100), 0, 200))
    d["grain"] = int(_clamp(round(v[9]), 0, 40))
    d["smooth"] = 1 if len(v) > 10 and v[10] >= 0.5 else 0
    d["name"] = sanitize_effect_name(lines[1] if len(lines) > 1 else "", slot)
    return d


def read_effects():
    """All four slots as UI dicts (None where the slot doesn't exist)."""
    out = []
    for i in range(MAX_EFFECT_SLOTS):
        try:
            with open(effect_slot_path(i)) as f:
                out.append(_parse_effect_text(f.read(), i))
        except OSError:
            out.append(None)
    return out


def migrate_legacy_effect():
    """One-time move of the pre-slot /data/.effect into slot 0."""
    if not os.path.exists(LEGACY_EFFECT) or os.path.exists(effect_slot_path(0)):
        _rm(LEGACY_EFFECT)          # slot 0 already taken: legacy copy is stale
        return
    try:
        with open(LEGACY_EFFECT) as f:
            d = _parse_effect_text(f.read(), 0)
        if d is not None:
            _atomic_write(effect_slot_path(0), effect_line(d) + d["name"] + "\n")
    except OSError:
        pass
    _rm(LEGACY_EFFECT)


def effect_camera_index(effects, slot):
    """Camera filter index of a slot: 9 + its position among existing slots."""
    return len(FILTER_OPTIONS) + sum(1 for j in range(slot) if effects[j] is not None)


def effect_params(d):
    """Clamp an incoming effect body to valid UI values."""
    fb = effect_defaults()
    cv = d.get("curve") or fb["curve"]
    out = {"curve": [int(_clamp(int(round(float(cv[i]))), 0, 255))
                     if i < len(cv) else fb["curve"][i] for i in range(5)]}
    for k, (lo, hi) in {"red": (50, 150), "green": (50, 150), "blue": (50, 150),
                        "saturation": (0, 200), "grain": (0, 40),
                        "smooth": (0, 1)}.items():
        try:
            out[k] = _clamp(int(round(float(d.get(k, fb[k])))), lo, hi)
        except (TypeError, ValueError):
            out[k] = fb[k]
    return out


def effect_line(p):
    """UI values -> the 11-number firmware line (last = curve interpolation)."""
    c = p["curve"]
    return "%d %d %d %d %d %.3f %.3f %.3f %.3f %d %d\n" % (
        c[0], c[1], c[2], c[3], c[4],
        p["red"] / 100.0, p["green"] / 100.0, p["blue"] / 100.0,
        p["saturation"] / 100.0, p["grain"], p.get("smooth", 0))


def capture_number_of(filename):
    """Numeric id from Optocamzero_<n>.<ext>, or None. Shared ordering across
    photos (.jpg) and GIFs (.gif)."""
    if not filename.startswith("Optocamzero_"):
        return None
    stem = filename[len("Optocamzero_"):]
    dot = stem.rfind(".")
    if dot == -1:
        return None
    num, ext = stem[:dot], stem[dot:].lower()
    if ext in MEDIA_EXTS and num.isdigit():
        return int(num)
    return None


def list_media():
    """All media files (photos + GIFs) newest-first."""
    if not os.path.exists(PHOTOS_DIR):
        return []
    files = [f for f in os.listdir(PHOTOS_DIR) if capture_number_of(f) is not None]
    files.sort(key=lambda f: capture_number_of(f), reverse=True)
    return files

HTML = """<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1">
<title>Optocam Zero</title>
<style>
@font-face {
    font-family: 'CamFont';
    /* v2: this copy's vertical metrics are rebalanced (ascent 1910 / descent
       625, sum unchanged) so all-caps labels centre optically in pills and
       buttons; CSS ascent-override can't do it because Safari ignores it.
       The firmware's HUD copy at /usr/share/optocam keeps the stock metrics —
       optocam_render.h anchors text by ascender, so never "fix" that one. */
    src: url('/font/cmunvt.ttf?v=2') format('truetype');
}
/* ── Theme palette. Dark is the default; Light and Yellow override the same
   variables so every element re-colours coherently. ── */
:root {
    --bg: #080808; --card: #0d0d0d; --surface: #101010; --raised: #161616;
    --line: #1e1e1e; --divider: #242424; --line2: #2a2a2a; --line3: #3a3a3a;
    --text: #d0d0d0; --text-hi: #ffffff; --muted: #888888; --muted2: #555555;
    --track: #333333; --thumb: #e7e4dc;
    --danger: #e8554f; --danger-line: #3a1a1a; --ok: #6ec06e; --ok-line: #234a23;
    --link: #7aa6e0; --black: #000000;
}
body.theme-light {
    /* near-neutral grays with a trace of warmth (R+1 B-2 per tone — luma
       change ≈0.07, so tonal values match the neutral set exactly); the page
       ground is a hair off white so the pure-white cards still lift from it */
    --bg: #fbfaf8; --card: #ffffff; --surface: #edecea; --raised: #e1e0de;
    --line: #dad9d7; --divider: #cecdcb; --line2: #bdbcba; --line3: #a3a2a0;
    --text: #292826; --text-hi: #12110f; --muted: #686765; --muted2: #959492;
    --track: #cac9c7; --thumb: #31302e;
    --danger: #c23a33; --danger-line: #e3b6b2; --ok: #3c8a48; --ok-line: #bcdcc1;
    --link: #8a5a24; --black: #dedddb;   /* light warm-tinged gray — viewer frame tone */
}
body.theme-yellow {
    --bg: #ffdb4a; --card: #ffe270; --surface: #ffd52e; --raised: #009966;
    --line: #e0bd2e; --divider: #cba91c; --line2: #b3941a; --line3: #008558;
    --text: #1c1a06; --text-hi: #000000; --muted: #6f5f00; --muted2: #927e00;
    --track: #cba91c; --thumb: #008558;
    --danger: #b8241a; --danger-line: #7a1a12; --ok: #00704b; --ok-line: #005236;
    --link: #00704b; --black: #201d05;
}
* { margin: 0; padding: 0; box-sizing: border-box; }
body {
    background: var(--bg);
    color: var(--text);
    font-family: 'CamFont', 'Courier New', monospace;
    padding: 14px;
    -webkit-tap-highlight-color: transparent;
    overscroll-behavior: none;
}
/* While the viewer is open, make the page background black too, so no gray
   (body color) shows in any strip the fixed viewer doesn't cover on iOS. */
body:has(#viewer.open) { background: #000000; }
header {
    border-bottom: 1px solid var(--line);
    padding-top: 11px;    /* less top than bottom to offset the 14px body padding above */
    padding-bottom: 25px;
    margin-bottom: 14px;
    /* Stretch past the body's 14px padding so the divider spans the full width. */
    margin-left: -14px;
    margin-right: -14px;
    display: flex;
    flex-direction: column;
    align-items: center;
}
.logo { height: 30px; width: auto; display: block; }
/* the wordmark SVG is light; darken it on the light/yellow themes */
body.theme-light .logo, body.theme-yellow .logo { filter: brightness(0); }
@media (min-width: 768px) {
    header { padding-top: 16px; padding-bottom: 30px; }
}
.meta {
    display: flex;
    gap: 18px;
    font-size: 12px;
    color: var(--muted2);
    letter-spacing: 1px;
}
/* Counter and free-space share one size (the counter's). */
.meta span { font-size: 12px; }
/* Row holding the grouped meta (left) and the grid-density toggle (right). */
.meta-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
}
.grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 8px;
    padding-bottom: 50px;
}
@media (min-width: 768px) {
    .grid { grid-template-columns: repeat(5, 1fr); }   /* desktop default */
    /* Gap tightens as density increases: 5-col 8px, 6-col 7px, 7-col 6px. */
    .grid.dcols-5 { grid-template-columns: repeat(5, 1fr); }
    .grid.dcols-6 { grid-template-columns: repeat(6, 1fr); gap: 7px; }
    .grid.dcols-7 { grid-template-columns: repeat(7, 1fr); gap: 6px; }
    /* Uniform control sizing across all desktop densities:
       21px selection ring, 22px download icon. */
    .grid .sel-circle::before { width: 21px; height: 21px; }
    .grid .item.sel .sel-circle::after { top: 17.5px; left: 17.5px; }
    .grid .dl-icon { width: 22px; height: 22px; }
}
@media (min-width: 1200px) {
    .grid {
        max-width: 1400px;
        margin: 0 auto;
    }
}
/* Stop iOS Safari from hijacking long-press on thumbnails (the image
   callout/"Save Image" menu, magnified preview and image drag) — otherwise it
   cancels the touch and the glide-to-select can't run. */
.grid, .grid * {
    -webkit-touch-callout: none;
    -webkit-user-select: none;
    user-select: none;
}
.img-btn img { -webkit-user-drag: none; }
.item {
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 3px;
    overflow: hidden;
    cursor: pointer;
}
.item.sel { border-color: var(--line); }
.img-wrap { position: relative; }
/* Tint selected thumbnails — overlay sits above the image but below the
   dot / download / GIF-badge controls (z-index 2). */
.item.sel .img-wrap::after {
    content: '';
    position: absolute;
    inset: 0;
    background: rgba(0, 0, 0, 0.4);
    pointer-events: none;
    z-index: 1;
}
.img-btn {
    display: block;
    width: 100%;
    padding: 0;
    border: none;
    background: none;
    cursor: pointer;
}
.img-btn img {
    width: 100%;
    aspect-ratio: 1 / 1;
    object-fit: cover;
    display: block;
}
.sel-circle {
    position: absolute;
    /* Anchored to the very corner so there's no dead margin around the dot;
       the ring (::before) is offset inward to keep its visual position. */
    top: 0;
    left: 0;
    /* Visible ring stays 23px (the ::before); the button itself is larger to
       give a bigger, easier-to-hit tap target without changing the look. */
    width: 54px;
    height: 54px;
    border-radius: 50%;
    border: none;
    background: transparent;
    cursor: pointer;
    z-index: 2;
    transition: none;
    touch-action: pan-y;   /* vertical drags still scroll; horizontal glide selects */
}
.sel-circle::before {
    content: '';
    position: absolute;   /* offset inward from the corner-anchored button */
    top: 7px;
    left: 7px;
    width: 23px;
    height: 23px;
    border-radius: 50%;
    border: 1.5px solid rgba(255,255,255,0.45);
    background: rgba(0,0,0,0.45);
    transition: background 0.12s, border-color 0.12s;
    box-sizing: border-box;
}
.item.sel .sel-circle::before {
    background: var(--text-hi);
    border-color: var(--text-hi);
}
.item.sel .sel-circle::after {
    content: '';
    position: absolute;
    /* Centered on the 23px ring (offset 7px inward from the button corner). */
    top: 18.5px;
    left: 18.5px;
    width: 5px;
    height: 9px;
    border: 2px solid var(--black);
    border-top: none;
    border-left: none;
    transform: translate(-60%, -65%) rotate(45deg);
}
.dl-icon {
    position: absolute;
    top: 7px;
    right: 7px;
    width: 24px;
    height: 24px;
    background: rgba(0,0,0,0.6);
    border: none;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    text-decoration: none;
    z-index: 2;
}
.dl-icon svg { width: 13px; height: 13px; }
.gif-badge {
    position: absolute;
    bottom: 7px;
    left: 7px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 4px 8px 2px;        /* extra top padding nudges the text down */
    font-family: 'CamFont', monospace;
    font-size: 10px;
    letter-spacing: 1px;
    color: #ffffff;              /* pill bg is always dark → white in every theme */
    background: rgba(0,0,0,0.6);
    border: none;
    border-radius: 999px;        /* fully rounded ends → pill */
    z-index: 2;
    pointer-events: none;
}
/* Spinner over a GIF poster whose animation is still loading in the
   background. Hidden until the poster JPG has actually loaded (gets `.show`
   in JS), and removed the moment the live GIF swaps in. Sits above the image
   but below the badge/controls, and ignores clicks. */
.grid-spin {
    position: absolute;
    top: 50%;
    left: 50%;
    width: 30px;
    height: 30px;
    margin: -15px 0 0 -15px;
    border: 2px solid rgba(255,255,255,0.35);
    border-top-color: var(--text-hi);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
    z-index: 1;
    pointer-events: none;
    filter: drop-shadow(0 0 1px rgba(0,0,0,0.65));
    display: none;
}
.grid-spin.show { display: block; }

/* ── Toolbar (meta + filter/order controls), below the header divider ── */
.toolbar {
    display: flex;
    flex-direction: column;
    gap: 12px;
    margin-bottom: 28px;
}
.controls {
    display: flex;
    justify-content: space-between;
    gap: 22px;
    flex-wrap: wrap;
}
@media (min-width: 768px) {
    /* Desktop: meta on the left, controls on the right, one row. The gear is
       pulled out to the toolbar's far-right edge (past the controls). */
    .toolbar {
        flex-direction: row;
        align-items: center;
        justify-content: space-between;
        position: relative;
        padding-right: 52px;   /* room for the 32px circled gear + pill gap */
    }
    #density-group { display: none; }   /* density toggle is mobile-only */
    #settings-btn {
        position: absolute;
        right: 0;
        top: 50%;
        transform: translateY(-50%);
    }
}
@media (min-width: 1200px) {
    /* Match the grid's centered max width so the toolbar's left and right
       edges line up with the image block. */
    .toolbar {
        max-width: 1400px;
        margin-left: auto;
        margin-right: auto;
    }
}
.density-group button {
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 6px 10px;
}
.density-group svg { display: block; fill: currentColor; }
/* Grid density (mobile only — desktop keeps its 4/5 columns). */
@media (max-width: 767px) {
    /* Gap tightens as density increases: 2-col 8px, 3-col 7px, 4-col 6px. */
    .grid.cols-2 { grid-template-columns: repeat(2, 1fr); }
    .grid.cols-3 { grid-template-columns: repeat(3, 1fr); gap: 7px; }
    .grid.cols-4 { grid-template-columns: repeat(4, 1fr); gap: 6px; }
    /* 4-col also drops the per-thumb download icon. */
    .grid.cols-4 .dl-icon { display: none; }
    #desktop-density-group { display: none; }   /* desktop-only selector */
    /* Denser grids: smaller selection ring (20px) and, on 3-col, download icon. */
    .grid.cols-3 .sel-circle::before,
    .grid.cols-4 .sel-circle::before { width: 20px; height: 20px; }
    .grid.cols-3 .item.sel .sel-circle::after,
    .grid.cols-4 .item.sel .sel-circle::after { top: 17px; left: 17px; }
    .grid.cols-3 .dl-icon { width: 21px; height: 21px; }
}
.ctrl-group {
    display: inline-flex;
    border: 1px solid var(--line);
    border-radius: 11px;
    overflow: hidden;
}
.ctrl-group button {
    font-family: 'CamFont', monospace;
    font-size: 11px;
    letter-spacing: 1px;
    color: var(--muted2);
    background: var(--bg);
    border: none;
    padding: 7px 12px;
    cursor: pointer;
    transition: color 0.12s, background 0.12s;
}
.ctrl-group button + button { border-left: 1px solid var(--line); }
.ctrl-group button.active { color: var(--text-hi); background: var(--raised); }

/* ── Viewer ── */
#viewer {
    /* The lightbox is always dark, in every theme — Light/Yellow's pale
       "black" made the photo frame look washed out. Re-declaring the dark
       palette here (inherited by every viewer control) keeps it black while
       preserving the dark theme's accent colours (delete red, links, etc). */
    --bg: #080808; --card: #0d0d0d; --surface: #101010; --raised: #161616;
    --line: #1e1e1e; --divider: #242424; --line2: #2a2a2a; --line3: #3a3a3a;
    --text: #d0d0d0; --text-hi: #ffffff; --muted: #888888; --muted2: #555555;
    --track: #333333; --thumb: #e7e4dc;
    --danger: #e8554f; --danger-line: #3a1a1a; --ok: #6ec06e; --ok-line: #234a23;
    --link: #7aa6e0; --black: #000000;   /* lightbox background — black */
    position: fixed;
    inset: 0;
    background: var(--black);
    z-index: 100;
    flex-direction: column;
    display: none;
    pointer-events: none;
    transform: translateZ(0);   /* own compositing layer — avoids iOS paint bleed */
}
/* Carry the active theme into the (otherwise dark) lightbox. The Yellow theme
   goes full monochrome-yellow-on-black: arrows/counter/controls (--accent),
   every text tone and the divider lines all become tones of the theme yellow,
   over the same black frame. Only the delete button keeps its danger red. Dark
   and Light define none of these, so their controls fall back to the neutral
   defaults below (var(--accent, <default>)) and stay a plain dark lightbox. */
body.theme-yellow #viewer {
    /* Muted olive-gold, sitting near the theme's line colour (#e0bd2e) rather
       than a pure bright yellow — softer against the deep-green frame. */
    --accent: #e0c246;      /* arrows, counter, download ring/icon, HQ active */
    --text: #d3b63f;        /* BACK label + chevron */
    --text-hi: #ecd873;     /* active nav-btn (a touch brighter) */
    --muted: #a08a30;       /* filename */
    --muted2: #7c6b26;      /* HQ (inactive) label */
    --line: #3a2f08;        /* header / nav / info divider lines — dark yellow */
    --line2: #4d3f0c;       /* round-control borders */
    --divider: #2e2607;
}
#viewer.open { display: flex; pointer-events: auto; }
.viewer-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 14px;
    border-bottom: 1px solid var(--line);
    flex-shrink: 0;
}
/* Counter + filename live in a bottom bar (below the image) on all sizes now,
   so the header's center copy is unused. */
.viewer-info { display: none; }
.viewer-controls { display: flex; align-items: center; gap: 8px; }
.viewer-fname {
    font-size: 12px;
    color: var(--muted);
    letter-spacing: 0.5px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    min-width: 0;
}
/* Bottom bar under the image: filename left-aligned, counter right-aligned
   (set by markup order). Full-width divider continuous with the nav line. */
.viewer-info-m {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    padding: 11px 14px;
    border-top: 1px solid var(--line);
    flex-shrink: 0;
}
@media (min-width: 768px) {
    /* Match the bottom bar's height to the top header (57px: 32px controls +
       12px*2 padding + 1px border), and group filename + counter together in
       the center rather than pushing them to the edges. */
    .viewer-info-m {
        min-height: 57px;
        padding-top: 12px;
        padding-bottom: 12px;
        justify-content: center;
        gap: 40px;
    }
}
.viewer-back {
    font-family: 'CamFont', monospace;
    font-size: 13px;
    color: var(--text);
    background: none;
    border: none;
    cursor: pointer;
    letter-spacing: 1px;
    padding: 4px 0;
    display: flex;
    align-items: center;
    gap: 5px;
}
.viewer-pos {
    font-size: 16px;
    color: var(--accent, var(--text));
    letter-spacing: 1px;
}
.viewer-dl {
    width: 32px;
    height: 32px;
    background: var(--raised);
    border: 1px solid var(--line2);
    border-radius: 50%;
    color: var(--accent, #ffffff);
    display: flex;
    align-items: center;
    justify-content: center;
    text-decoration: none;
}
.viewer-dl svg { width: 14px; height: 14px; }
.viewer-del {
    width: 32px;
    height: 32px;
    background: var(--raised);
    border: 1px solid var(--danger-line);
    border-radius: 50%;
    color: var(--danger);
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    padding: 0;
}
.viewer-del svg { width: 14px; height: 14px; }
.viewer-hq {
    width: 32px;
    height: 32px;
    border-radius: 50%;
    border: 1px solid var(--line2);
    background: var(--raised);
    color: var(--muted2);
    font-family: 'CamFont', monospace;
    font-size: 11px;
    letter-spacing: 0.5px;
    cursor: pointer;
    display: flex;
    align-items: center;
    justify-content: center;
    padding-left: 1px;
    transition: border-color 0.15s, color 0.15s;
}
.viewer-hq.active { border-color: var(--accent, var(--muted)); color: var(--accent, var(--text-hi)); }
.viewer-body {
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
    position: relative;
}
.viewer-body img {
    max-width: 100%;
    max-height: 100%;
    object-fit: contain;
}
.spinner {
    position: absolute;
    width: 32px;
    height: 32px;
    border: 2px solid var(--line);
    border-top-color: var(--muted);
    border-radius: 50%;
    animation: spin 0.8s linear infinite;
    display: none;
}
.spinner.active { display: block; }
@keyframes spin { to { transform: rotate(360deg); } }
.viewer-nav {
    display: flex;
    border-top: 1px solid var(--line);
    flex-shrink: 0;
    background: var(--black);
}
.nav-btn {
    flex: 1;
    padding: 14px;
    background: none;
    border: none;
    color: var(--accent, var(--muted));
    font-family: 'CamFont', monospace;
    font-size: 18px;
    cursor: pointer;
    letter-spacing: 1px;
}
.nav-btn:active { background: var(--surface); color: var(--text-hi); }
.nav-btn:first-child { border-right: 1px solid var(--line); }
.side-nav { display: none; }
@media (min-width: 768px) {
    .viewer-nav { display: none; }
    .side-nav {
        display: flex;
        position: absolute;
        top: 50%;
        transform: translateY(-50%);
        width: 44px;
        height: 80px;
        align-items: center;
        justify-content: center;
        background: none;
        border: none;
        color: var(--accent, var(--muted2));
        font-size: 22px;
        cursor: pointer;
        transition: color 0.15s;
    }
    @media (hover: hover) { .side-nav:hover { color: var(--text); } }
    /* 12px inside the constrained viewer-body, i.e. at the image column edge. */
    .side-nav.left-nav { left: 12px; }
    .side-nav.right-nav { right: 12px; }
}
@media (min-width: 1200px) {
    .side-nav svg { width: 19px; height: 19px; }
}
@media (min-width: 768px) {
    /* Header + bottom bar stay full-width so their divider lines span the whole
       screen, with their content constrained to the gallery column via padding.
       The image area (viewer-body) IS constrained to the column so the side-nav
       arrows, positioned inside it, sit at the column edge. */
    .viewer-header {
        padding-left: max(14px, calc((100% - 1400px) / 2));
        padding-right: max(14px, calc((100% - 1400px) / 2));
    }
    .viewer-body {
        width: 100%;
        max-width: 1428px;
        margin-left: auto;
        margin-right: auto;
        padding: 0 14px;
    }
}

/* ── Selection bar ── */
#sel-bar {
    position: fixed;
    bottom: 0; left: 0; right: 0;
    background: var(--bg);
    border-top: 1px solid var(--line2);
    padding: 12px 16px;
    padding-top: 14px;
    padding-bottom: calc(12px + env(safe-area-inset-bottom));
    display: none;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    z-index: 50;
}
/* Short background skirt below the bar to cover the few-px gap that can flash
   during a fast scroll while Safari's URL bar collapses. */
#sel-bar::after {
    content: '';
    position: absolute;
    top: 100%;
    left: 0;
    right: 0;
    height: 24px;
    background: var(--bg);
    pointer-events: none;
}
@media (min-width: 768px) {
    /* Desktop: group the content in the center rather than spreading it edge-to-edge. */
    #sel-bar { padding-top: 26px; padding-bottom: 34px; justify-content: center; gap: 28px; }
}
#sel-bar.open { display: flex; animation: sel-bar-up 0.18s ease-out; }
@keyframes sel-bar-up {
    from { transform: translateY(100%); }
    to { transform: translateY(0); }
}
.sel-left { display: flex; align-items: center; gap: 9px; }
.sel-info { display: flex; flex-direction: column; gap: 2px; }
#sel-count { font-size: 12px; color: var(--muted); letter-spacing: 1px; line-height: 1; }
#sel-size { font-size: 12px; color: var(--muted2); letter-spacing: 1px; line-height: 1; }
#desel-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    background: none;
    border: 1px solid var(--line2);
    border-radius: 50%;
    cursor: pointer;
    padding: 0;
    width: 30px;
    height: 30px;
    color: var(--text-hi);
}
#desel-btn svg { display: block; }
.sel-bar-btns { display: flex; gap: 8px; }
#dl-all-btn, #del-btn {
    font-family: 'CamFont', monospace;
    font-size: 12px;
    letter-spacing: 1px;
    background: var(--bg);
    border: 1px solid var(--track);
    border-radius: 11px;
    padding: 8px 14px;
    cursor: pointer;
    display: flex;
    align-items: center;
    gap: 6px;
}
#dl-all-btn { color: var(--text-hi); }
#del-btn { color: var(--danger); border-color: var(--danger-line); }

/* ── Confirm popup ── */
#confirm-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.7);
    z-index: 200;
    display: none;
    pointer-events: none;
    align-items: center;
    justify-content: center;
}
#confirm-overlay.open { display: flex; pointer-events: auto; }
#confirm-box {
    background: var(--bg);
    border: 1px solid var(--line2);
    border-radius: 11px;
    padding: 24px 20px;
    width: 80%;
    max-width: 280px;
    text-align: center;
}
#confirm-box p {
    font-size: 12px;
    color: var(--text);
    letter-spacing: 1px;
    margin-bottom: 20px;
    line-height: 1.6;
}
.confirm-btns { display: flex; gap: 10px; }
.confirm-btns button {
    flex: 1;
    font-family: 'CamFont', monospace;
    font-size: 12px;
    letter-spacing: 1.5px;
    padding: 8px 6px;
    border-radius: 11px;
    cursor: pointer;
    background: var(--bg);
    border: 1px solid var(--track);
    white-space: nowrap;    /* "DELETE ALL" must stay on one line */
}
#confirm-yes { color: var(--danger); border-color: var(--danger-line); }
#confirm-no  { color: var(--text); }
.empty {
    text-align: center;
    color: var(--track);
    font-size: 13px;
    letter-spacing: 2px;
    margin-top: 60px;
}
#top-btn {
    position: fixed;
    bottom: 20px;
    right: 22px;
    width: 38px;
    height: 38px;
    background: var(--bg);
    border: 1px solid var(--line2);
    border-radius: 50%;
    color: var(--text-hi);
    display: none;
    align-items: center;
    justify-content: center;
    cursor: pointer;
    z-index: 40;
    transition: bottom 0.18s ease;
}
#top-btn.visible { display: flex; }
/* Lift it above the selection bar when that bar is open. */
body:has(#sel-bar.open) #top-btn { bottom: calc(76px + env(safe-area-inset-bottom)); }
@media (min-width: 768px) {
    body:has(#sel-bar.open) #top-btn { bottom: 104px; }
}
#drag-select {
    position: fixed;
    border: 1px solid rgba(255,255,255,0.35);
    background: rgba(255,255,255,0.05);
    pointer-events: none;
    z-index: 30;
    display: none;
}
#dl-progress {
    position: fixed;
    left: 50%;
    transform: translateX(-50%);
    width: 60%;                  /* centered, short enough to clear the go-up circle */
    max-width: 360px;
    bottom: 35px;                 /* resting — tracks the go-up button */
    height: 10px;
    background: var(--bg);          /* track — matches the selection overlay */
    border: 1px solid var(--line2);
    border-radius: 999px;
    overflow: hidden;
    z-index: 300;
    display: none;
    pointer-events: none;
    transition: bottom 0.18s ease;
}
/* Rise with the go-up button when the selection bar is open. */
body:has(#sel-bar.open) #dl-progress {
    bottom: calc(91px + env(safe-area-inset-bottom));
}
@media (min-width: 768px) {
    body:has(#sel-bar.open) #dl-progress { bottom: 119px; }
}
#dl-progress.active { display: block; }
#dl-progress-fill {
    height: 100%;
    width: 0;
    background: var(--text-hi);
    border-radius: 999px;
    transition: width 0.18s linear;
}

/* ── Settings gear (in the meta row, next to the grid selector) ── */
#settings-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    /* a hair over the 29px pills — circles read smaller than rects */
    height: 32px;
    width: 32px;
    flex-shrink: 0;         /* a squeezed circle is an oval */
    padding: 0;
    color: var(--muted);
    background: var(--bg);
    border: 1px solid var(--line);
    border-radius: 50%;
    cursor: pointer;
    transition: color 0.12s;
}
@media (hover: hover) { #settings-btn:hover { color: var(--text); } }
/* 20px inside the 32px circle: whole-pixel 5px margins — a half-pixel margin
   antialiases unevenly and reads off-centre. */
#settings-btn svg { display: block; width: 20px; height: 20px; }

/* ── Settings panel (full-screen overlay, styled like the viewer) ── */
#settings {
    position: fixed;
    inset: 0;
    background: var(--bg);
    z-index: 150;
    display: none;
    flex-direction: column;
}
#settings.open { display: flex; }
.settings-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 12px 14px;
    /* match the lightbox header: its 32px round controls make it 57px tall,
       while this one only holds text — pin it so the two line up */
    min-height: 57px;
    border-bottom: 1px solid var(--line);
    flex-shrink: 0;
}
.settings-title { font-size: 14px; color: var(--text); letter-spacing: 2px; }
.settings-body {
    flex: 1;
    overflow-y: auto;
    -webkit-overflow-scrolling: touch;
    padding: 16px 14px 60px;
}
@media (min-width: 768px) {
    .settings-header { padding-left: max(14px, calc((100% - 720px) / 2)); padding-right: max(14px, calc((100% - 720px) / 2)); }
    /* the scroll container stays full-width — capping it left dead zones either
     * side where the wheel had nothing to scroll — and the cards centre instead */
    .settings-body > * { max-width: 720px; margin-left: auto; margin-right: auto; }
}
/* Each setting group is its own card. */
.settings-sec {
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 14px;
    padding: 15px 18px 22px;
    margin-bottom: 14px;
}
/* Section header. A real <button> so the accordion is reachable by keyboard —
   the browser gives us Enter/Space and focus for free. */
.sec-title {
    display: block;
    /* full-bleed divider: the old div stretched via auto width + negative
       margins; a button needs the width spelled out or its right edge (and the
       chevron anchored to it) stops 18px short of the card edge */
    width: calc(100% + 36px);
    background: none;
    border: none;
    font-family: 'CamFont', monospace;
    font-size: 13px;
    color: var(--text);
    letter-spacing: 2px;
    text-align: left;
    position: relative;
    cursor: pointer;
    user-select: none;
    -webkit-user-select: none;
    margin: 0 -18px 14px;
    padding: 0 18px 9px;
    border-bottom: 1px solid var(--divider);
}
.sec-title::before {
    content: '';
    position: absolute;
    left: 0;
    right: 0;
    top: -15px;      /* reach up over the card's top padding */
    bottom: -8px;
}
.settings-sec.collapsed > .sec-title::before { bottom: -15px; }  /* whole pill */
.sec-title:focus-visible {
    outline: 1px solid var(--muted);
    outline-offset: 2px;
    border-radius: 3px;
}
/* Auto-save status. Sections have no SAVE button — they persist on change — so
   this is the only report that the camera took the edit. It sits in the main
   settings header, opposite the BACK button, and so is always visible. It also
   doubles as the title-centring spacer: 52px wide like BACK, right edge pinned
   (flex-end), so a wider "NOT SAVED" overflows leftward without moving it. */
.hdr-status {
    width: 52px;
    display: flex;
    justify-content: flex-end;
    white-space: nowrap;
    font-size: 13px;
    letter-spacing: 1px;
    color: var(--muted2);
    opacity: 0;
    transition: opacity 0.2s;
}
.hdr-status[data-state] { opacity: 1; }
/* keep the ok/danger hues but soften them so the badge reads as a status
   whisper, not a header-level control */
.hdr-status[data-state="saved"]  { color: var(--ok); opacity: 0.7; }
.hdr-status[data-state="failed"] { color: var(--danger); opacity: 0.7; }
/* Failure needs an action, not just a colour — the retry row appears under the
   section's controls and stays until the write succeeds. */
.sec-retry {
    display: none;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    margin-top: 16px;
    padding: 10px 12px;
    border: 1px solid var(--danger-line);
    border-radius: 8px;
    font-size: 11px;
    letter-spacing: 1px;
    color: var(--danger);
}
.settings-sec[data-state="failed"] .sec-retry { display: flex; }
.sec-retry button {
    font-family: 'CamFont', monospace;
    font-size: 11px;
    letter-spacing: 2px;
    padding: 5px 14px;
    border-radius: 8px;
    cursor: pointer;
    background: var(--bg);
    color: var(--text);
    border: 1px solid var(--track);
}
/* accordion chevron on the right of every section header */
.sec-title::after {
    content: '';
    position: absolute;
    right: 20px;
    top: 3px;
    width: 7px;
    height: 7px;
    border-right: 1.5px solid var(--muted);
    border-bottom: 1.5px solid var(--muted);
    transform: rotate(45deg);           /* down = collapsed */
    transition: transform 0.2s;
}
.settings-sec:not(.collapsed) > .sec-title::after {
    transform: rotate(-135deg);         /* up = expanded */
    top: 7px;
}
/* Collapsed sections show only their header (compact, no divider). */
.settings-sec.collapsed { padding-top: 15px; padding-bottom: 15px; }
.settings-sec.collapsed > .sec-title {
    margin-bottom: 0;
    padding-bottom: 0;
    border-bottom: none;
}
.settings-sec.collapsed > :not(.sec-title) { display: none; }
.sec-hint {
    font-size: 13px;
    color: var(--muted);
    letter-spacing: 0.5px;
    line-height: 1.6;
    margin-bottom: 18px;
}
/* Effect Maker — filter slots (list of created customs), then the editor. */
.fx-slot-row {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 5px 8px 5px 12px;
    border: 1px solid var(--line);
    border-radius: 9px;
    margin-bottom: 8px;
    cursor: pointer;
    color: var(--text);
}
.fx-slot-row.active { border-color: var(--muted); color: var(--text-hi); }
/* the name is edited right in the pill */
.fx-slot-row .fx-slot-name-input {
    /* the editable zone spans the pill's left half (incl. the 12px padding,
       bled over via the negative margin): clicking anywhere left of the
       midpoint places the caret; right of it, clicks select/deselect the row */
    flex: 0 0 calc(50% + 12px);
    min-width: 0;
    font-family: 'CamFont', monospace;
    font-size: 12px;
    letter-spacing: 1.5px;
    color: inherit;
    background: none;
    border: none;
    padding: 4px 0 4px 12px;
    margin-left: -12px;
}
.fx-slot-row .fx-slot-tag { margin-left: auto; }   /* keep SLOT n pinned right */
.fx-slot-row .fx-slot-name-input:focus { outline: none; color: var(--text-hi); }
.fx-slot-row .fx-slot-tag { font-size: 10px; color: var(--muted2); letter-spacing: 1px; }
.fx-slot-row .fx-slot-del {
    background: none;
    border: none;
    cursor: pointer;
    color: var(--muted);
    font-family: 'CamFont', monospace;
    font-size: 13px;
    line-height: 1;
    padding: 6px 8px;
    border-radius: 6px;
}
@media (hover: hover) { .fx-slot-row .fx-slot-del:hover { color: var(--danger); } }
.fx-slot-row .fx-slot-del:focus-visible { outline: 1px solid var(--muted); outline-offset: 1px; }
/* Row under the curve graph: interpolation toggle left, curve reset right.
   Padded to the drawn plot area's edges (FX_PAD=9 inside the canvas; the
   right side subtracts the reset icon's own 6px padding so the GLYPH lands
   on the grid edge). */
.fx-curve-tools {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin: -14px 0 18px;
    padding: 0 3px 0 9px;
}
/* neutralise the pill group's auto margins inside the flex row; anchor the
   scale on the left edge so the toggle sits flush with the graph's left side */
.fx-curve-tools .fx-interp-group { margin: 0; transform: scale(0.85); transform-origin: left center; }
.fx-curve-reset { margin-left: 0; padding: 5px; }
.fx-curve-reset svg { width: 14px; height: 14px; }   /* a touch larger than the 12px slider resets */
/* Effect Maker — live preview canvas + draggable tone-curve canvas. */
.fx-preview-wrap {
    margin: 4px 0 16px;
    border: 1px solid var(--line);
    border-radius: 4px;
    overflow: hidden;
    background: var(--black);
    display: flex;
    justify-content: center;
}
#fx-preview {
    display: block; width: 100%; height: auto;
    /* hold-to-peek: keep vertical scroll, suppress iOS callout/selection */
    touch-action: pan-y;
    -webkit-touch-callout: none;
    -webkit-user-select: none;
    user-select: none;
}
@media (min-width: 768px) { #fx-preview { max-width: 320px; } }
.fx-curve {
    display: block;
    width: 100%;
    height: 150px;
    background: var(--card);
    border-radius: 6px;
    margin-bottom: 18px;
    /* pan-y lets the page scroll past the curve; the drag handler only blocks
       scrolling (preventDefault) when a touch actually starts on a point. */
    touch-action: pan-y;
    cursor: pointer;
}

/* Data & Device info rows (label left, value right). */
.info-loading { font-size: 12px; color: var(--muted2); letter-spacing: 1px; padding: 6px 0; }
.info-row {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 14px;
    padding: 8px 0;
    border-bottom: 1px solid var(--line);
}
.info-row:last-child { border-bottom: none; }
/* first row under a subtitle: trim its internal padding so the subtitle gap
 * reads close to the Camera Settings sections (16px there, 24px untrimmed) */
#device-info .sub-group + .info-row { padding-top: 4px; }
.info-k { font-size: 11px; color: var(--muted); letter-spacing: 1px; white-space: nowrap; }
.info-v { font-size: 12px; color: var(--text); letter-spacing: 0.5px; text-align: right; }
.info-v .sub { display: block; font-size: 10px; color: var(--muted2); margin-top: 2px; }
.info-v a { color: var(--link); text-decoration: none; }
@media (hover: hover) { .info-v a:hover { text-decoration: underline; } }
.info-copyright {
    font-size: 10px;
    color: var(--muted2);
    letter-spacing: 0.5px;
    text-align: center;
    margin-top: 16px;
    line-height: 1.6;
}
/* Sub-category label (POWER-ON DEFAULTS / EXPOSURE / STORAGE …) inside a section. */
.sub-group {
    /* subsection heading: section-title size, muted ink, wide tracking */
    font-size: 13px;
    color: var(--muted);
    letter-spacing: 2px;
    margin: 22px 0 16px;
}
/* One label+slider group. Label hugs its slider; gap goes below the group. */
.slider-block { margin-bottom: 20px; }
.slider-block:last-of-type { margin-bottom: 0; }
/* end labels inside a block: the block's own margin provides the gap below */
.slider-block .slider-ends { margin-bottom: 0; }
.set-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 14px;
    margin-bottom: 4px;
}
.set-label { font-size: 12px; color: var(--muted); letter-spacing: 1px; }
.set-val { font-size: 12px; color: var(--text-hi); letter-spacing: 1px; min-width: 62px; text-align: right; }

/* Sliders — dark track, small white thumb with a shadow so it reads clearly
   against the track. The input box is a 28px-tall invisible touch target (a
   real finger can land anywhere on the row); the visible 4px track is drawn
   by the track pseudo-element, and negative margins keep the flow height
   identical to the old 4px input so nothing shifts. touch-action pan-y:
   horizontal drags belong to the slider, vertical drags still scroll. */
input[type=range].set-slider {
    -webkit-appearance: none;
    appearance: none;
    width: 100%;
    height: 28px;
    background: transparent;
    outline: none;
    margin: -10px 0 -12px;
    touch-action: pan-y;
}
input[type=range].set-slider::-webkit-slider-runnable-track {
    height: 4px;
    background: var(--track);
    border-radius: 999px;
}
input[type=range].set-slider::-moz-range-track {
    height: 4px;
    background: var(--track);
    border-radius: 999px;
}
/* Solid off-white knob — no transparent halo/hit-ring. */
input[type=range].set-slider::-webkit-slider-thumb {
    -webkit-appearance: none;
    appearance: none;
    width: 16px;
    height: 16px;
    border: none;
    border-radius: 50%;
    background: var(--thumb);
    cursor: pointer;
    margin-top: -6px;          /* centre the 16px thumb on the 4px track */
}
input[type=range].set-slider::-moz-range-thumb {
    width: 16px;
    height: 16px;
    border: none;
    border-radius: 50%;
    background: var(--thumb);
    cursor: pointer;
}

/* Per-slider reset icon (counter-clockwise arrow) next to the label. */
.mini-reset {
    display: inline-flex;
    align-items: center;
    background: none;
    border: none;
    padding: 3px;
    margin-left: 4px;
    color: var(--muted2);
    cursor: pointer;
    vertical-align: middle;
    line-height: 0;
    position: relative;
    top: -1px;                 /* optical align with the label text */
}
@media (hover: hover) { .mini-reset:hover { color: var(--text); } }
.mini-reset:active { color: var(--text); }
/* a parameter off its default gets a pronounced icon — changed values read at a glance;
 * the light/yellow --text tones sit too close to the muted base, so go full --text-hi there */
.mini-reset.changed { color: var(--text); }
@media (hover: hover) { .mini-reset.changed:hover { color: var(--text-hi); } }
body.theme-light .mini-reset.changed, body.theme-yellow .mini-reset.changed { color: var(--text-hi); }
.mini-reset svg { display: block; }
.slider-ends {
    display: flex;
    justify-content: space-between;
    font-size: 10px;
    color: var(--line3);
    letter-spacing: 1px;
    margin: 4px 0 16px;
}
/* Reference-pattern picker, styled as a centered segmented control. */
.calib-img-group {
    display: flex;
    width: fit-content;
    margin: 2px auto 16px;
}
.calib-img-group button { flex: 1; }

/* Calibration reference image + on-camera note. */
.calib-img-wrap {
    margin: 4px 0 18px;
    border: 1px solid var(--line);
    border-radius: 4px;
    overflow: hidden;
    background: var(--black);
    display: flex;
    justify-content: center;
}
body.theme-yellow .calib-img-wrap { background: #bfa748; }
.calib-img-wrap img {
    display: block;
    width: 100%;                  /* full width of its container (mobile) */
    image-rendering: pixelated;   /* keep the ramp/steps crisp */
}
/* the photo reference is served at 720px — scale it like a photo, not a ramp */
.calib-img-wrap img.smooth { image-rendering: auto; }
/* Desktop enlarge toggle: mobile already fills the width, so the button only
 * exists >=768px, where the previews are capped. It rides in a .pv-row beside
 * the section's view pill, circled like the settings gear, on the card's
 * right edge (= the image wrap's right edge). The row takes over the pill's
 * vertical margins so the 50% centering lands on the pill's true middle. */
.pv-row { position: relative; display: flex; justify-content: center; margin: 2px 0 16px; }
.pv-row > .calib-img-group { margin: 0; }
.pv-zoom {
    position: absolute;
    right: 0; top: 50%;
    transform: translateY(-50%);
    display: none;
    align-items: center;
    justify-content: center;
    height: 32px;
    width: 32px;
    padding: 0;
    color: var(--muted);
    background: var(--bg);
    border: 1px solid var(--line);
    border-radius: 50%;
    cursor: pointer;
    transition: color 0.12s;
}
@media (hover: hover) { .pv-zoom:hover { color: var(--text); } }
.pv-zoom svg { display: block; }
.pv-zoom .z-in { display: none; }
.pv-zoom.on .z-out { display: none; }
.pv-zoom.on .z-in { display: block; }
@media (min-width: 768px) {
    .pv-zoom { display: flex; }
    .fx-preview-wrap.expanded #fx-preview { max-width: none; }
}
@media (min-width: 768px) {
    .calib-img-wrap img { max-width: 340px; }   /* don't blow up on desktop */
}
.calib-note {
    font-size: 10px;
    color: var(--muted2);
    letter-spacing: 0.6px;
    text-align: center;
    margin: -12px 0 18px;
    line-height: 1.5;
}

/* Segmented option pickers (WB / filter), reuse ctrl-group look but wrap. */
.opt-group {
    display: flex;
    flex-wrap: wrap;
    gap: 7px;
    margin-bottom: 6px;
}
.opt-group button {
    font-family: 'CamFont', monospace;
    font-size: 11px;
    letter-spacing: 1px;
    color: var(--muted);
    background: var(--card);
    border: 1px solid var(--line);
    border-radius: 9px;
    padding: 7px 12px;
    cursor: pointer;
    transition: color 0.12s, border-color 0.12s, background 0.12s;
}
.opt-group button.active { color: var(--text-hi); background: var(--raised); border-color: var(--line3); }
/* separates the subsections of Camera Settings; bleeds through the card's
   18px side padding so the line spans the full card width */
.def-divider { border-top: 1px solid var(--divider); margin: 22px -18px 4px; }

/* Save / Reset action row — buttons share the full width equally. */
.set-actions { display: flex; gap: 10px; margin-top: 22px; }
.set-btn {
    flex: 1;
    font-family: 'CamFont', monospace;
    font-size: 11px;
    letter-spacing: 1.5px;
    padding: 11px 20px;
    border-radius: 10px;
    cursor: pointer;
    background: var(--bg);
    border: 1px solid var(--track);
    color: var(--text);
    transition: color 0.12s, border-color 0.12s, background 0.12s;
}
.set-btn.errored { color: var(--danger); border-color: var(--danger-line); }
.set-btn:focus-visible { outline: 1px solid var(--muted); outline-offset: 2px; }
/* transition:none so the highlight appears the instant of the tap (the base
   .set-btn 0.12s transition otherwise fades it in and reads as a delay). */
.set-btn.pulse { background: var(--line3); border-color: var(--muted); color: var(--text-hi); transition: none; }
.set-btn.danger { color: var(--danger); border-color: var(--danger-line); }
.set-btn:disabled { opacity: 0.5; cursor: default; }


/* Format-data progress inside the panel. */
#format-progress {
    height: 8px;
    background: var(--bg);
    border: 1px solid var(--line2);
    border-radius: 999px;
    overflow: hidden;
    margin-top: 16px;
    display: none;
}
#format-progress.active { display: block; }
#format-progress-fill {
    height: 100%;
    width: 0;
    background: var(--danger);
    border-radius: 999px;
    transition: width 0.2s linear;
}
#set-confirm-overlay {
    position: fixed;
    inset: 0;
    background: rgba(0,0,0,0.7);
    z-index: 200;
    display: none;
    align-items: center;
    justify-content: center;
}
#set-confirm-overlay.open { display: flex; }
#set-confirm-box {
    background: var(--bg);
    border: 1px solid var(--line2);
    border-radius: 11px;
    padding: 24px 20px;
    width: 84%;
    max-width: 310px;
    text-align: center;
}
#set-confirm-box p {
    font-size: 12px;
    color: var(--text);
    letter-spacing: 1px;
    margin-bottom: 20px;
    line-height: 1.6;
}
#set-confirm-no  { color: var(--text); }
#set-confirm-yes { color: var(--danger); border-color: var(--danger-line); }
.confirm-btns button:focus-visible { outline: 1px solid var(--muted); outline-offset: 2px; }
</style>
</head>
<body class="theme-{{ theme }}">

<header>
  <img class="logo" src="/logo" alt="OptoCam">
</header>

<div class="toolbar">
  <div class="meta-row">
    <div class="meta">
      <span>{{ count }} IMAGE{% if count != 1 %}S{% endif %}</span>
      <span>{{ free_space }} FREE</span>
    </div>
    <div style="display:flex; align-items:center; gap:20px;">
    {% if has_media %}
    <div class="ctrl-group density-group" id="density-group">
      <button data-val="2" class="active" onclick="setDensity('2')" aria-label="2 columns">
        <svg viewBox="0 0 14 14" width="13" height="13" xmlns="http://www.w3.org/2000/svg">
          <rect x="1" y="1" width="5" height="5"/><rect x="8" y="1" width="5" height="5"/>
          <rect x="1" y="8" width="5" height="5"/><rect x="8" y="8" width="5" height="5"/>
        </svg>
      </button>
      <button data-val="3" onclick="setDensity('3')" aria-label="3 columns">
        <svg viewBox="0 0 14 14" width="13" height="13" xmlns="http://www.w3.org/2000/svg">
          <rect x="1" y="1" width="3" height="3"/><rect x="5.5" y="1" width="3" height="3"/><rect x="10" y="1" width="3" height="3"/>
          <rect x="1" y="5.5" width="3" height="3"/><rect x="5.5" y="5.5" width="3" height="3"/><rect x="10" y="5.5" width="3" height="3"/>
          <rect x="1" y="10" width="3" height="3"/><rect x="5.5" y="10" width="3" height="3"/><rect x="10" y="10" width="3" height="3"/>
        </svg>
      </button>
      <button data-val="4" onclick="setDensity('4')" aria-label="4 columns">
        <svg viewBox="0 0 14 14" width="13" height="13" xmlns="http://www.w3.org/2000/svg">
          <rect x="1" y="1" width="2.25" height="2.25"/><rect x="4.25" y="1" width="2.25" height="2.25"/><rect x="7.5" y="1" width="2.25" height="2.25"/><rect x="10.75" y="1" width="2.25" height="2.25"/>
          <rect x="1" y="4.25" width="2.25" height="2.25"/><rect x="4.25" y="4.25" width="2.25" height="2.25"/><rect x="7.5" y="4.25" width="2.25" height="2.25"/><rect x="10.75" y="4.25" width="2.25" height="2.25"/>
          <rect x="1" y="7.5" width="2.25" height="2.25"/><rect x="4.25" y="7.5" width="2.25" height="2.25"/><rect x="7.5" y="7.5" width="2.25" height="2.25"/><rect x="10.75" y="7.5" width="2.25" height="2.25"/>
          <rect x="1" y="10.75" width="2.25" height="2.25"/><rect x="4.25" y="10.75" width="2.25" height="2.25"/><rect x="7.5" y="10.75" width="2.25" height="2.25"/><rect x="10.75" y="10.75" width="2.25" height="2.25"/>
        </svg>
      </button>
    </div>
    {% endif %}
    <button id="settings-btn" onclick="openSettings()" aria-label="Settings">
      <!-- 8-tooth gear, drawn in-house (no third-party licence) -->
      <svg viewBox="-112 -112 224 224" fill="currentColor" xmlns="http://www.w3.org/2000/svg">
        <path fill-rule="evenodd" d="M-73,0a73,73 0 1,0 146,0a73,73 0 1,0 -146,0ZM-37,0a37,37 0 1,0 74,0a37,37 0 1,0 -74,0Z"/>
        <rect x="-17" y="-100" width="34" height="41" rx="7"/>
        <rect x="-17" y="-100" width="34" height="41" rx="7" transform="rotate(45)"/>
        <rect x="-17" y="-100" width="34" height="41" rx="7" transform="rotate(90)"/>
        <rect x="-17" y="-100" width="34" height="41" rx="7" transform="rotate(135)"/>
        <rect x="-17" y="-100" width="34" height="41" rx="7" transform="rotate(180)"/>
        <rect x="-17" y="-100" width="34" height="41" rx="7" transform="rotate(225)"/>
        <rect x="-17" y="-100" width="34" height="41" rx="7" transform="rotate(270)"/>
        <rect x="-17" y="-100" width="34" height="41" rx="7" transform="rotate(315)"/>
      </svg>
    </button>
    </div>
  </div>
  {% if has_media %}
  <div class="controls">
    <div class="ctrl-group" id="filter-group">
      <button data-val="all" class="active" onclick="setFilter('all')">ALL</button>
      <button data-val="photos" onclick="setFilter('photos')">PHOTOS</button>
      <button data-val="gifs" onclick="setFilter('gifs')">GIFS</button>
    </div>
    <div class="ctrl-group" id="order-group">
      <button data-val="newest" class="active" onclick="setOrder('newest')">NEWEST</button>
      <button data-val="oldest" onclick="setOrder('oldest')">OLDEST</button>
    </div>
    <div class="ctrl-group density-group" id="desktop-density-group">
      <button data-val="5" class="active" onclick="setDesktopDensity('5')" aria-label="5 columns">
        <svg viewBox="0 0 14 14" width="13" height="13" xmlns="http://www.w3.org/2000/svg">
          <rect x="1" y="1" width="5" height="5"/><rect x="8" y="1" width="5" height="5"/>
          <rect x="1" y="8" width="5" height="5"/><rect x="8" y="8" width="5" height="5"/>
        </svg>
      </button>
      <button data-val="6" onclick="setDesktopDensity('6')" aria-label="6 columns">
        <svg viewBox="0 0 14 14" width="13" height="13" xmlns="http://www.w3.org/2000/svg">
          <rect x="1" y="1" width="3" height="3"/><rect x="5.5" y="1" width="3" height="3"/><rect x="10" y="1" width="3" height="3"/>
          <rect x="1" y="5.5" width="3" height="3"/><rect x="5.5" y="5.5" width="3" height="3"/><rect x="10" y="5.5" width="3" height="3"/>
          <rect x="1" y="10" width="3" height="3"/><rect x="5.5" y="10" width="3" height="3"/><rect x="10" y="10" width="3" height="3"/>
        </svg>
      </button>
      <button data-val="7" onclick="setDesktopDensity('7')" aria-label="7 columns">
        <svg viewBox="0 0 14 14" width="13" height="13" xmlns="http://www.w3.org/2000/svg">
          <rect x="1" y="1" width="2.25" height="2.25"/><rect x="4.25" y="1" width="2.25" height="2.25"/><rect x="7.5" y="1" width="2.25" height="2.25"/><rect x="10.75" y="1" width="2.25" height="2.25"/>
          <rect x="1" y="4.25" width="2.25" height="2.25"/><rect x="4.25" y="4.25" width="2.25" height="2.25"/><rect x="7.5" y="4.25" width="2.25" height="2.25"/><rect x="10.75" y="4.25" width="2.25" height="2.25"/>
          <rect x="1" y="7.5" width="2.25" height="2.25"/><rect x="4.25" y="7.5" width="2.25" height="2.25"/><rect x="7.5" y="7.5" width="2.25" height="2.25"/><rect x="10.75" y="7.5" width="2.25" height="2.25"/>
          <rect x="1" y="10.75" width="2.25" height="2.25"/><rect x="4.25" y="10.75" width="2.25" height="2.25"/><rect x="7.5" y="10.75" width="2.25" height="2.25"/><rect x="10.75" y="10.75" width="2.25" height="2.25"/>
        </svg>
      </button>
    </div>
  </div>
  {% endif %}
</div>

{% if files %}
<div class="grid" id="grid">
  {% for f in files %}
  <div class="item" data-file="{{ f }}" data-idx="{{ loop.index0 }}">
    <div class="img-wrap">
      <button class="img-btn" onclick="openViewer(this)">
        <img src="/thumb/{{ f }}?v={{ vers[f] }}" data-src="/thumb/{{ f }}?v={{ vers[f] }}" loading="lazy" decoding="async" alt="{{ f }}" draggable="false" onload="thumbLoaded(this)" onerror="thumbFailed(this)">
      </button>
      {% if f.lower().endswith('.gif') %}<span class="gif-badge">GIF</span><div class="grid-spin"></div>{% endif %}
      <button class="sel-circle" onclick="toggleSel(event, this)"></button>
      <a class="dl-icon" href="/photo/{{ f }}" download="{{ f }}" onclick="event.stopPropagation()">
        <svg viewBox="0 0 16 16" xmlns="http://www.w3.org/2000/svg">
          <path d="M8 1v9M4 7l4 4 4-4M2 14h12" stroke="#fff" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/>
        </svg>
      </a>
    </div>
  </div>
  {% endfor %}
</div>
<div class="empty" id="empty-msg" style="display:none">NO IMAGE</div>
{% else %}
<div class="empty">NO IMAGE</div>
{% endif %}

<!-- Viewer -->
<div id="viewer">
  <div class="viewer-header" onclick="event.stopPropagation()">
    <button class="viewer-back" onclick="closeViewer()"><svg viewBox="0 0 16 16" width="13" height="13" xmlns="http://www.w3.org/2000/svg" style="position:relative;top:-1px;"><path d="M10 3L5 8l5 5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>BACK</button>
    <div class="viewer-info">
      <span class="viewer-fname"></span>
      <span class="viewer-pos"></span>
    </div>
    <div class="viewer-controls">
      <button class="viewer-del" id="viewer-del" onclick="confirmDeleteCurrent()" aria-label="Delete">
        <svg viewBox="0 0 16 16" xmlns="http://www.w3.org/2000/svg">
          <path d="M2 3.5h12M5.5 3.5V1.5h5v2M3.5 3.5l.8 10h7.4l.8-10" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" fill="none"/>
        </svg>
      </button>
      <button class="viewer-hq" id="viewer-hq" onclick="toggleHQ()">HQ</button>
      <a class="viewer-dl" id="viewer-dl" href="#" download>
        <svg viewBox="0 0 16 16" xmlns="http://www.w3.org/2000/svg">
          <path d="M8 1v9M4 7l4 4 4-4M2 14h12" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/>
        </svg>
      </a>
    </div>
  </div>
  <div class="viewer-body" id="viewer-body" onclick="if(window.innerWidth>=1200)closeViewer()">
    <div class="spinner" id="spinner"></div>
    <img id="viewer-img" src="" alt="" onload="imgLoaded()" onerror="imgFailed()" onclick="event.stopPropagation()" style="display:none;">
    <button class="side-nav left-nav" onclick="event.stopPropagation();stepViewer(-1)"><svg viewBox="0 0 16 16" width="16" height="16" xmlns="http://www.w3.org/2000/svg"><path d="M10 3L5 8l5 5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg></button>
    <button class="side-nav right-nav" onclick="event.stopPropagation();stepViewer(1)"><svg viewBox="0 0 16 16" width="16" height="16" xmlns="http://www.w3.org/2000/svg"><path d="M6 3l5 5-5 5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg></button>
  </div>
  <div class="viewer-info-m">
    <span class="viewer-fname"></span>
    <span class="viewer-pos"></span>
  </div>
  <div class="viewer-nav">
    <button class="nav-btn" onclick="stepViewer(-1)"><svg viewBox="0 0 16 16" width="14" height="14" xmlns="http://www.w3.org/2000/svg"><path d="M10 3L5 8l5 5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg></button>
    <button class="nav-btn" onclick="stepViewer(1)"><svg viewBox="0 0 16 16" width="14" height="14" xmlns="http://www.w3.org/2000/svg"><path d="M6 3l5 5-5 5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg></button>
  </div>
</div>

<!-- Footer -->
<footer style="text-align:center; padding: 28px 0 20px; font-size:11px; color:#444; letter-spacing:1px;">Doruk Kumkumo&#287;lu 2026</footer>

<!-- Drag selection box -->
<div id="drag-select"></div>

<!-- Back to top -->
<button id="top-btn" onclick="window.scrollTo({top:0,behavior:'smooth'})"><svg viewBox="0 0 16 16" width="14" height="14" xmlns="http://www.w3.org/2000/svg"><path d="M3 10l5-5 5 5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg></button>

<!-- Selection bar -->
<div id="sel-bar">
  <div class="sel-left">
    <button id="desel-btn" onclick="deselectAll()" aria-label="Clear selection">
      <svg viewBox="0 0 16 16" width="14" height="14" xmlns="http://www.w3.org/2000/svg">
        <path d="M3 3l10 10M13 3L3 13" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>
      </svg>
    </button>
    <div class="sel-info">
      <span id="sel-count"></span>
      <span id="sel-size"></span>
    </div>
  </div>
  <div class="sel-bar-btns">
    <button id="dl-all-btn" onclick="downloadSelected()">
      <svg viewBox="0 0 16 16" width="14" height="14" xmlns="http://www.w3.org/2000/svg">
        <path d="M8 1v9M4 7l4 4 4-4M2 14h12" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/>
      </svg>SAVE</button>
    <button id="del-btn" onclick="confirmDelete()">
      <svg viewBox="0 0 16 16" width="14" height="14" xmlns="http://www.w3.org/2000/svg">
        <path d="M2 3.5h12M5.5 3.5V1.5h5v2M3.5 3.5l.8 10h7.4l.8-10" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" fill="none"/>
      </svg>DELETE</button>
  </div>
</div>

<!-- Confirm delete popup -->
<div id="confirm-overlay" style="display:none">
  <div id="confirm-box">
    <p id="confirm-msg"></p>
    <div class="confirm-btns">
      <button id="confirm-yes" onclick="confirmYes()">DELETE</button>
      <button id="confirm-no" onclick="closeConfirm()">CANCEL</button>
    </div>
  </div>
</div>

<!-- Download progress pill -->
<div id="dl-progress"><div id="dl-progress-fill"></div></div>
<iframe id="download-frame" name="download-frame" style="display:none"></iframe>

<!-- Settings panel -->
<div id="settings">
  <div class="settings-header">
    <button class="viewer-back" onclick="closeSettings()"><svg viewBox="0 0 16 16" width="13" height="13" xmlns="http://www.w3.org/2000/svg" style="position:relative;top:-1px;"><path d="M10 3L5 8l5 5" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/></svg>BACK</button>
    <span class="settings-title">SETTINGS</span>
    <span id="hdr-status" class="hdr-status"></span>
  </div>
  <div class="settings-body">

    <!-- 1 · Camera Settings -->
    <div class="settings-sec collapsed" data-group="defaults">
      <button type="button" class="sec-title" aria-expanded="false" onclick="toggleSection(this)">CAMERA SETTINGS</button>

      <div class="sub-group">POWER-ON DEFAULTS</div>
      <div class="sec-hint">The white balance and filter the camera starts with in each time it powers on.</div>

      <div class="set-label" style="margin-bottom:10px">WHITE BALANCE</div>
      <div class="opt-group" id="wb-group">
        {% for name in settings.wb_options %}
        <button data-val="{{ loop.index0 }}" class="{{ 'active' if loop.index0 == settings.wb else '' }}" onclick="pickWb({{ loop.index0 }})">{{ name | upper }}</button>
        {% endfor %}
      </div>

      <div class="set-label" style="margin:18px 0 10px">FILTER</div>
      <div class="opt-group" id="filter-group-def">
        {% for name in settings.filter_options %}
        <button data-val="{{ loop.index0 }}" class="{{ 'active' if loop.index0 == settings.filter else '' }}" onclick="pickFilter({{ loop.index0 }})">{{ name | upper }}</button>
        {% endfor %}
      </div>

      <div class="set-actions">
        <button class="set-btn" id="def-reset" onclick="resetDefaults()">RESET TO DEFAULT</button>
      </div>

      <div class="def-divider"></div>

      <div class="sub-group">EXPOSURE</div>
      <div class="sec-hint">A faster minimum shutter avoids motion blur; metering sets which part of the frame is read for brightness; digital gain lets dark shots pass ISO 1600, up to 3200, with extra noise.</div>

      <div class="set-label" style="margin:0 0 10px">MINIMUM SHUTTER SPEED</div>
      <div class="opt-group" id="shutter-group">
        {% for d in settings.shutter_options %}
        <button data-val="{{ d }}" class="{{ 'active' if d == settings.shutter else '' }}" onclick="pickShutter({{ d }})">1/{{ d }}</button>
        {% endfor %}
      </div>

      <div class="set-label" style="margin:18px 0 10px">METERING</div>
      <div class="opt-group" id="metering-group">
        {% for m in settings.metering_options %}
        <button data-val="{{ loop.index0 }}" class="{{ 'active' if loop.index0 == settings.metering else '' }}" onclick="pickMetering({{ loop.index0 }})">{{ m }}</button>
        {% endfor %}
      </div>

      <div class="set-label" style="margin:18px 0 10px">DIGITAL GAIN</div>
      <div class="opt-group" id="dgain-group">
        {% for m in settings.dgain_options %}
        <button data-val="{{ loop.index0 }}" class="{{ 'active' if loop.index0 == settings.dgain else '' }}" onclick="pickDgain({{ loop.index0 }})">{{ m }}</button>
        {% endfor %}
      </div>

      <div class="set-actions">
        <button class="set-btn" id="ae-reset" onclick="resetAeLimits()">RESET TO DEFAULT</button>
      </div>

      <div class="def-divider"></div>

      <div class="sub-group">GIF RECORDING</div>
      <div class="sec-hint">How many frames a GIF captures and the duration gap between them.</div>

      <div class="set-row">
        <span class="set-label">FRAME COUNT<button class="mini-reset" onclick="miniReset('gif-frames',10,'gif')" aria-label="Reset frame count">{{ RESET_ICON }}</button></span>
        <span class="set-val" id="gif-frames-val">10</span>
      </div>
      <input type="range" class="set-slider" id="gif-frames" aria-label="GIF frame count" min="2" max="30" step="1"
             value="{{ settings.gif_frames }}" oninput="onGifInput()">
      <div class="slider-ends"><span>2</span><span>30</span></div>

      <div class="set-row">
        <span class="set-label">INTERVAL<button class="mini-reset" onclick="miniReset('gif-interval',5,'gif')" aria-label="Reset interval">{{ RESET_ICON }}</button></span>
        <span class="set-val" id="gif-interval-val">0.5s</span>
      </div>
      <input type="range" class="set-slider" id="gif-interval" aria-label="GIF frame interval" min="1" max="30" step="1"
             value="{{ (settings.gif_interval * 10) | round | int }}" oninput="onGifInput()">
      <div class="slider-ends"><span>0.1s</span><span>3.0s</span></div>

      <div class="set-actions">
        <button class="set-btn" id="gif-reset" onclick="resetGif()">RESET TO DEFAULT</button>
      </div>
      <div class="sec-retry"><span>COULDN'T SAVE TO CAMERA</span><button type="button" onclick="retrySave('defaults'); retrySave('gif')">RETRY</button></div>
    </div>

    <!-- 2 · Effect Maker -->
    <div class="settings-sec collapsed" id="sec-effect" data-group="effect">
      <button type="button" class="sec-title" aria-expanded="false" onclick="toggleSection(this)">FILTER MAKER</button>
      <div class="sec-hint">Design custom filters. Each appears under its own name in the camera's filters list. Up to {{ max_effect_slots }} filters.</div>

      <div id="fx-slot-list"></div>
      <div class="set-actions" style="margin-top:14px">
        <button class="set-btn" id="fx-create" onclick="createFx()">+ NEW FILTER</button>
      </div>

      <div id="fx-editor" hidden>
      <div class="ctrl-group calib-img-group" id="fx-src-group" style="margin-top:20px">
        <button data-val="photo" class="active" onclick="setFxSrc('photo')">PHOTO</button>
        <button data-val="live" onclick="setFxSrc('live')">LIVE</button>
      </div>
      <div class="fx-preview-wrap"><canvas id="fx-preview" width="720" height="720"></canvas></div>

      <div class="pv-row">
      <div class="ctrl-group calib-img-group" id="fx-view-group">
        <button data-val="curve" class="active" onclick="setFxView('curve')">CURVE</button>
        <button data-val="color" onclick="setFxView('color')">COLOR</button>
      </div>
      <button type="button" class="pv-zoom" onclick="toggleZoom(this)" aria-label="Enlarge preview">{{ ZOOM_ICONS }}</button>
      </div>

      <div id="fx-curve-view">
        <canvas id="fx-curve" width="300" height="150" class="fx-curve"></canvas>
        <div class="fx-curve-tools">
          <div class="ctrl-group calib-img-group fx-interp-group" id="fx-interp-group">
            <button data-val="0" class="active" onclick="setFxInterp(0)">LINEAR</button>
            <button data-val="1" onclick="setFxInterp(1)">SMOOTH</button>
          </div>
          <button class="mini-reset fx-curve-reset" onclick="resetFxCurve()" aria-label="Reset curve">{{ RESET_ICON }}</button>
        </div>
      </div>

      <div id="fx-color-view" hidden>
        <div class="slider-block">
          <div class="set-row"><span class="set-label">RED TINT<button class="mini-reset" onclick="miniReset('fx-red',100,'effect')" aria-label="Reset red tint">{{ RESET_ICON }}</button></span><span class="set-val" id="fx-red-val"></span></div>
          <input type="range" class="set-slider" id="fx-red" aria-label="Red tint" min="50" max="150" step="1" value="{{ settings.effect.red }}" oninput="onEffectInput()">
        <div class="slider-ends"><span>0.50</span><span>1.50</span></div>
        </div>
        <div class="slider-block">
          <div class="set-row"><span class="set-label">GREEN TINT<button class="mini-reset" onclick="miniReset('fx-green',100,'effect')" aria-label="Reset green tint">{{ RESET_ICON }}</button></span><span class="set-val" id="fx-green-val"></span></div>
          <input type="range" class="set-slider" id="fx-green" aria-label="Green tint" min="50" max="150" step="1" value="{{ settings.effect.green }}" oninput="onEffectInput()">
        <div class="slider-ends"><span>0.50</span><span>1.50</span></div>
        </div>
        <div class="slider-block">
          <div class="set-row"><span class="set-label">BLUE TINT<button class="mini-reset" onclick="miniReset('fx-blue',100,'effect')" aria-label="Reset blue tint">{{ RESET_ICON }}</button></span><span class="set-val" id="fx-blue-val"></span></div>
          <input type="range" class="set-slider" id="fx-blue" aria-label="Blue tint" min="50" max="150" step="1" value="{{ settings.effect.blue }}" oninput="onEffectInput()">
        <div class="slider-ends"><span>0.50</span><span>1.50</span></div>
        </div>
        <div class="slider-block">
          <div class="set-row"><span class="set-label">SATURATION<button class="mini-reset" onclick="miniReset('fx-saturation',100,'effect')" aria-label="Reset saturation">{{ RESET_ICON }}</button></span><span class="set-val" id="fx-saturation-val"></span></div>
          <input type="range" class="set-slider" id="fx-saturation" aria-label="Effect saturation" min="0" max="200" step="1" value="{{ settings.effect.saturation }}" oninput="onEffectInput()">
        <div class="slider-ends"><span>0%</span><span>200%</span></div>
        </div>
        <div class="slider-block">
          <div class="set-row"><span class="set-label">GRAIN<button class="mini-reset" onclick="miniReset('fx-grain',0,'effect')" aria-label="Reset grain">{{ RESET_ICON }}</button></span><span class="set-val" id="fx-grain-val"></span></div>
          <input type="range" class="set-slider" id="fx-grain" aria-label="Grain" min="0" max="40" step="1" value="{{ settings.effect.grain }}" oninput="onEffectInput()">
        <div class="slider-ends"><span>0</span><span>40</span></div>
        </div>
      </div>

      <div class="set-actions">
        <button class="set-btn danger" id="fx-delete" onclick="deleteFx()">DELETE FILTER</button>
      </div>
      </div>
      <div class="sec-retry"><span>COULDN'T SAVE TO CAMERA</span><button type="button" onclick="retrySave('effect')">RETRY</button></div>
    </div>

    <!-- 3 · Screen Calibration -->
    <div class="settings-sec collapsed" id="sec-calib" data-group="calib">
      <button type="button" class="sec-title" aria-expanded="false" onclick="toggleSection(this)">SCREEN CALIBRATION</button>
      <div class="sec-hint">Adjust the camera's display until this reference looks right on the LCD, comparing against the same image below. Changes save to the camera as you make them.</div>

      <div class="ctrl-group calib-img-group" id="calib-img-group">
        <button data-val="color" class="active" onclick="pickCalibImg('color')">COLOR</button>
        <button data-val="gray" onclick="pickCalibImg('gray')">GRAY</button>
        <button data-val="photo" onclick="pickCalibImg('photo')">PHOTO</button>
      </div>

      <div id="calib-controls">
      <div class="calib-img-wrap"><img id="calib-img" src="/calib-image?img=color&v={{ calib_asset_v }}" alt="Calibration reference"></div>
      <div class="calib-note" id="calib-note">Match the camera's LCD to this reference.</div>

      <div class="ctrl-group calib-img-group" id="cal-view-group" style="margin:22px auto 16px">
        <button data-val="display" class="active" onclick="setCalView('display')">DISPLAY</button>
        <button data-val="color" onclick="setCalView('color')">COLOR</button>
        <button data-val="advanced" onclick="setCalView('advanced')">ADVANCED</button>
      </div>

      <div id="cal-display-view">
      <div class="slider-block">
        <div class="set-row"><span class="set-label">SCREEN BRIGHTNESS<button class="mini-reset" onclick="miniReset('cal-brightness',100,'calib')" aria-label="Reset screen brightness">{{ RESET_ICON }}</button></span><span class="set-val" id="cal-brightness-val"></span></div>
        <input type="range" class="set-slider" id="cal-brightness" aria-label="Screen brightness" min="20" max="100" step="1" value="{{ settings.cal.brightness }}" oninput="onCalibInput()">
        <div class="slider-ends"><span>20%</span><span>100%</span></div>
      </div>
      <div class="slider-block">
        <div class="set-row"><span class="set-label">CONTRAST<button class="mini-reset" onclick="miniReset('cal-contrast',{{ cal_def.contrast }},'calib')" aria-label="Reset contrast">{{ RESET_ICON }}</button></span><span class="set-val" id="cal-contrast-val"></span></div>
        <input type="range" class="set-slider" id="cal-contrast" aria-label="Contrast" min="55" max="175" step="1" value="{{ settings.cal.contrast }}" oninput="onCalibInput()">
        <div class="slider-ends"><span>55%</span><span>175%</span></div>
      </div>
      <div class="slider-block">
        <div class="set-row"><span class="set-label">GAMMA<button class="mini-reset" onclick="miniReset('cal-gamma',100,'calib')" aria-label="Reset gamma">{{ RESET_ICON }}</button></span><span class="set-val" id="cal-gamma-val"></span></div>
        <input type="range" class="set-slider" id="cal-gamma" aria-label="Gamma" min="50" max="200" step="1" value="{{ settings.cal.gamma }}" oninput="onCalibInput()">
        <div class="slider-ends"><span>0.50</span><span>2.00</span></div>
      </div>
      </div>

      <div id="cal-color-view" hidden>
      <div class="slider-block">
        <div class="set-row"><span class="set-label">TEMPERATURE<button class="mini-reset" onclick="miniReset('cal-temp',0,'calib')" aria-label="Reset temperature">{{ RESET_ICON }}</button></span><span class="set-val" id="cal-temp-val"></span></div>
        <input type="range" class="set-slider" id="cal-temp" aria-label="Colour temperature" min="-100" max="100" step="1" value="{{ settings.cal.temp }}" oninput="onCalibInput()">
        <div class="slider-ends"><span>WARM</span><span>COOL</span></div>
      </div>
      <div class="slider-block">
        <div class="set-row"><span class="set-label">TINT<button class="mini-reset" onclick="miniReset('cal-tint',0,'calib')" aria-label="Reset tint">{{ RESET_ICON }}</button></span><span class="set-val" id="cal-tint-val"></span></div>
        <input type="range" class="set-slider" id="cal-tint" aria-label="Tint" min="-100" max="100" step="1" value="{{ settings.cal.tint }}" oninput="onCalibInput()">
        <div class="slider-ends"><span>GREEN</span><span>MAGENTA</span></div>
      </div>
      <div class="slider-block">
        <div class="set-row"><span class="set-label">SATURATION<button class="mini-reset" onclick="miniReset('cal-saturation',100,'calib')" aria-label="Reset saturation">{{ RESET_ICON }}</button></span><span class="set-val" id="cal-saturation-val"></span></div>
        <input type="range" class="set-slider" id="cal-saturation" aria-label="Saturation" min="0" max="200" step="1" value="{{ settings.cal.saturation }}" oninput="onCalibInput()">
        <div class="slider-ends"><span>0%</span><span>200%</span></div>
      </div>
      </div>

      <div id="cal-adv-view" hidden>
      <div class="slider-block">
        <div class="set-row"><span class="set-label">RED GAIN<button class="mini-reset" onclick="miniReset('cal-red',{{ cal_def.red }},'calib')" aria-label="Reset red">{{ RESET_ICON }}</button></span><span class="set-val" id="cal-red-val"></span></div>
        <input type="range" class="set-slider" id="cal-red" aria-label="Red gain" min="50" max="150" step="1" value="{{ settings.cal.red }}" oninput="onCalibInput()">
        <div class="slider-ends"><span>0.50</span><span>1.50</span></div>
      </div>
      <div class="slider-block">
        <div class="set-row"><span class="set-label">GREEN GAIN<button class="mini-reset" onclick="miniReset('cal-green',{{ cal_def.green }},'calib')" aria-label="Reset green">{{ RESET_ICON }}</button></span><span class="set-val" id="cal-green-val"></span></div>
        <input type="range" class="set-slider" id="cal-green" aria-label="Green gain" min="50" max="150" step="1" value="{{ settings.cal.green }}" oninput="onCalibInput()">
        <div class="slider-ends"><span>0.50</span><span>1.50</span></div>
      </div>
      <div class="slider-block">
        <div class="set-row"><span class="set-label">BLUE GAIN<button class="mini-reset" onclick="miniReset('cal-blue',{{ cal_def.blue }},'calib')" aria-label="Reset blue">{{ RESET_ICON }}</button></span><span class="set-val" id="cal-blue-val"></span></div>
        <input type="range" class="set-slider" id="cal-blue" aria-label="Blue gain" min="50" max="150" step="1" value="{{ settings.cal.blue }}" oninput="onCalibInput()">
        <div class="slider-ends"><span>0.50</span><span>1.50</span></div>
      </div>
      </div>

      <div class="set-actions">
        <button class="set-btn" id="cal-reset" onclick="resetCalib()">RESET TO DEFAULT</button>
      </div>
      <div class="sec-retry"><span>COULDN'T SAVE TO CAMERA</span><button type="button" onclick="retrySave('calib')">RETRY</button></div>
      </div>
    </div>

    <!-- 4 · Hotspot Theme -->
    <div class="settings-sec collapsed" data-group="theme">
      <button type="button" class="sec-title" aria-expanded="false" onclick="toggleSection(this)">THEME</button>
      <div class="sec-hint">Colour scheme for this web interface.</div>
      <div class="ctrl-group calib-img-group" id="theme-group">
        <button data-val="dark" class="{{ 'active' if theme == 'dark' else '' }}" onclick="setTheme('dark')">DARK</button>
        <button data-val="light" class="{{ 'active' if theme == 'light' else '' }}" onclick="setTheme('light')">LIGHT</button>
        <button data-val="yellow" class="{{ 'active' if theme == 'yellow' else '' }}" onclick="setTheme('yellow')">YELLOW</button>
      </div>
      <div class="sec-retry"><span>COULDN'T SAVE TO CAMERA</span><button type="button" onclick="retrySave('theme')">RETRY</button></div>
    </div>

    <!-- 5 · Data & Device -->
    <div class="settings-sec collapsed" id="sec-data">
      <button type="button" class="sec-title" aria-expanded="false" onclick="toggleSection(this)">DATA &amp; DEVICE</button>
      <div class="sec-hint">Storage, firmware, and licence information.</div>
      <div id="device-info" class="info-rows"><div class="info-loading">Loading…</div></div>
    </div>

    <!-- 6 · Format Data -->
    <div class="settings-sec collapsed">
      <button type="button" class="sec-title" aria-expanded="false" onclick="toggleSection(this)">FORMAT DATA</button>
      <div class="sec-hint">Permanently delete every photo and GIF on the camera. This cannot be undone.</div>
      <div class="set-actions">
        <button class="set-btn danger" id="format-btn" onclick="confirmFormat()">DELETE ALL PHOTOS</button>
      </div>
      <div id="format-progress"><div id="format-progress-fill"></div></div>
    </div>
  </div>
</div>

<!-- Confirm popup for the settings panel (resets, format). Cancel comes first
     and holds focus: everything routed through here is irreversible. -->
<div id="set-confirm-overlay">
  <div id="set-confirm-box">
    <p id="set-confirm-msg"></p>
    <div class="confirm-btns">
      <button id="set-confirm-no" onclick="setConfirmAnswer(false)">CANCEL</button>
      <button id="set-confirm-yes" onclick="setConfirmAnswer(true)"></button>
    </div>
  </div>
</div>

<script>
// allFiles: every media file, newest-first (the order the grid is rendered in).
// files: the currently visible subset, in display order — what the viewer walks.
let allFiles = {{ files_json | safe }};
let files = allFiles.slice();
const gifs = new Set({{ gifs_json | safe }});
const sizes = {{ sizes_json | safe }};   // filename -> bytes
const VERS = {{ vers_json | safe }};     // filename -> cache-bust key (size-mtime)
const vv = f => VERS[f] || 0;            // versioned-URL param value
const selected = new Set();

function formatSize(bytes) {
    // Decimal units (1 MB = 1,000,000 bytes) to match how macOS/iOS report
    // sizes. One decimal on GB (the rows where a whole-unit round hides half a
    // gigabyte); MB and below stay whole — tenths of a MB are noise.
    if (bytes >= 999.95e6) return (bytes / 1e9).toFixed(1) + ' GB';
    if (bytes >= 1e6) return Math.round(bytes / 1e6) + ' MB';
    if (bytes >= 1e3) return Math.round(bytes / 1e3) + ' KB';
    return bytes + ' B';
}
let viewerIdx = 0;
let hqMode = false;
let viewerScrollY = 0;   // page scroll position saved while the viewer is open

let curFilter = 'all';   // all | photos | gifs
let curOrder = 'newest'; // newest | oldest
let curDensity = '2';          // mobile grid columns: 2 | 3 | 4
let curDesktopDensity = '5';   // desktop grid columns: 5 | 6 | 7

const grid = document.getElementById('grid');
// filename -> grid item element, so we can show/hide and reorder in place.
const itemEls = {};
if (grid) document.querySelectorAll('.item').forEach(el => { itemEls[el.dataset.file] = el; });

function isGif(f) { return gifs.has(f); }

function matchesFilter(f) {
    if (curFilter === 'photos') return !isGif(f);
    if (curFilter === 'gifs') return isGif(f);
    return true;
}

// Recompute the visible subset, reorder the grid to match, and keep `files`
// (used by the viewer) in lockstep so navigation stays correct.
function applyView() {
    let view = allFiles.filter(matchesFilter);
    if (curOrder === 'oldest') view = view.slice().reverse();
    files = view;
    const viewSet = new Set(view);
    if (grid) {
        // Append visible items in view order; appendChild moves existing nodes,
        // so the grid ends up in exactly this order.
        view.forEach(f => { const el = itemEls[f]; if (el) { el.style.display = ''; grid.appendChild(el); } });
        allFiles.forEach(f => { if (!viewSet.has(f)) { const el = itemEls[f]; if (el) el.style.display = 'none'; } });
        grid.style.display = view.length === 0 ? 'none' : '';
    }
    const span = document.querySelector('.meta span');
    if (span) span.textContent = view.length + ' IMAGE' + (view.length !== 1 ? 'S' : '');
    const empty = document.getElementById('empty-msg');
    if (empty) empty.style.display = view.length === 0 ? 'block' : 'none';
}

function setActive(groupId, val) {
    document.querySelectorAll('#' + groupId + ' button')
        .forEach(b => b.classList.toggle('active', b.dataset.val === val));
}

function setFilter(v) {
    if (v === curFilter) return;
    curFilter = v;
    setActive('filter-group', v);
    applyView();
}

function setOrder(v) {
    if (v === curOrder) return;
    curOrder = v;
    setActive('order-group', v);
    applyView();
}

// Grid density: just toggles a CSS class (6-col layout applies on mobile only).
// Independent of filtering/ordering, so it never touches the files arrays.
function setDensity(v) {
    if (v === curDensity) return;
    curDensity = v;
    setActive('density-group', v);
    if (grid) {
        grid.classList.remove('cols-2', 'cols-3', 'cols-4');
        grid.classList.add('cols-' + v);
    }
    try { localStorage.setItem('optocam_density', v); } catch (e) {}
}

// Restore the saved density on load.
try {
    const d = localStorage.getItem('optocam_density');
    if (d && ['2', '3', '4'].includes(d)) setDensity(d);
} catch (e) {}

function setDesktopDensity(v) {
    if (v === curDesktopDensity) return;
    curDesktopDensity = v;
    setActive('desktop-density-group', v);
    if (grid) {
        grid.classList.remove('dcols-5', 'dcols-6', 'dcols-7');
        grid.classList.add('dcols-' + v);
    }
    try { localStorage.setItem('optocam_density_desktop', v); } catch (e) {}
}

// Restore the saved desktop density on load.
try {
    const d = localStorage.getItem('optocam_density_desktop');
    if (d && ['5', '6', '7'].includes(d)) setDesktopDensity(d);
} catch (e) {}


function setItemSelected(item, sel) {
    const f = item.dataset.file;
    if (sel) { selected.add(f); item.classList.add('sel'); }
    else { selected.delete(f); item.classList.remove('sel'); }
}

function refreshSelBar() {
    const bar = document.getElementById('sel-bar');
    if (selected.size > 0) {
        bar.classList.add('open');
        document.getElementById('sel-count').textContent = selected.size + ' SELECTED';
        let bytes = 0;
        selected.forEach(f => { bytes += sizes[f] || 0; });
        document.getElementById('sel-size').textContent = formatSize(bytes);
    } else {
        bar.classList.remove('open');
    }
}

function toggleSel(e, circle) {
    e.stopPropagation();
    const item = circle.closest('.item');
    setItemSelected(item, !item.classList.contains('sel'));
    refreshSelBar();
}

function thumbLoaded(img) {
    img.dataset.loaded = '1';
    img.dataset.retrying = '0';
}

function thumbFailed(img) {
    if (!img || img.dataset.loaded === '1') return;
    const tries = parseInt(img.dataset.retries || '0', 10);
    if (tries >= 8) return;
    img.dataset.retries = String(tries + 1);
    img.dataset.retrying = '1';
    const delay = Math.min(5000, 250 * Math.pow(1.7, tries));
    setTimeout(() => {
        if (!document.body.contains(img) || img.dataset.loaded === '1') return;
        const base = img.dataset.src || img.src.split('?')[0];
        img.src = base + (base.includes('?') ? '&' : '?') + 'retry=' + (tries + 1) + '-' + Date.now();
    }, delay);
}

function preloadAhead(idx) {
    const ahead = files.slice(idx + 1, idx + 6);
    if (ahead.length === 0) return;
    fetch('/preload-ahead', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({files: ahead})
    });
}

function openViewer(el) {
    // Resolve the index from the item's filename against the live `files`
    // array so selection stays correct after deletions reindex the grid.
    const f = el.closest('.item').dataset.file;
    const idx = files.indexOf(f);
    if (idx === -1) return;
    viewerIdx = idx;
    hqMode = false;
    document.getElementById('viewer-hq').classList.remove('active');
    updateViewer();
    preloadAhead(idx);
    document.getElementById('viewer').classList.add('open');
    // Fully lock the background so the scrolled grid can't bleed through the
    // overlay on iOS Safari (overflow:hidden alone doesn't lock it).
    viewerScrollY = window.scrollY;
    document.body.style.position = 'fixed';
    document.body.style.top = (-viewerScrollY) + 'px';
    document.body.style.left = '0';
    document.body.style.right = '0';
    document.body.style.overflow = 'hidden';
}

function closeViewer() {
    document.getElementById('viewer').classList.remove('open');
    document.body.style.position = '';
    document.body.style.top = '';
    document.body.style.left = '';
    document.body.style.right = '';
    document.body.style.overflow = '';
    window.scrollTo(0, viewerScrollY);
    hqMode = false;
    document.getElementById('viewer-hq').classList.remove('active');
}

function toggleHQ() {
    if (isGif(files[viewerIdx])) return;   // GIFs have no HQ variant
    hqMode = !hqMode;
    document.getElementById('viewer-hq').classList.toggle('active', hqMode);
    const img = document.getElementById('viewer-img');
    const spinner = document.getElementById('spinner');
    const f = files[viewerIdx];
    img.style.display = 'none';
    spinner.classList.add('active');
    img.src = hqMode ? '/photo/' + f : '/thumb/' + f + '?size=1200&v=' + vv(f);
}

function updateViewer() {
    const img = document.getElementById('viewer-img');
    const spinner = document.getElementById('spinner');
    const f = files[viewerIdx];
    const gif = isGif(f);
    resetZoom(false);   // each image opens unzoomed
    img.style.display = 'none';
    spinner.classList.add('active');
    // GIFs always play their full animated original; photos use the sized thumb / HQ
    img.src = gif ? '/gif/' + f + '?v=' + vv(f)
                  : (hqMode ? '/photo/' + f : '/thumb/' + f + '?size=1200&v=' + vv(f));
    document.getElementById('viewer-hq').style.display = gif ? 'none' : 'flex';
    const posText = (viewerIdx + 1) + ' / ' + files.length;
    document.querySelectorAll('.viewer-pos').forEach(e => e.textContent = posText);
    document.querySelectorAll('.viewer-fname').forEach(e => e.textContent = f);
    const dl = document.getElementById('viewer-dl');
    dl.href = '/photo/' + f;
    dl.download = f;
}

function imgLoaded() {
    document.getElementById('spinner').classList.remove('active');
    document.getElementById('viewer-img').style.display = 'block';
}

function imgFailed() {
    // Don't leave the viewer spinning forever if a load fails.
    document.getElementById('spinner').classList.remove('active');
}

// --- Progressive GIF previews in the grid ------------------------------
// Grid cells first show a tiny static first-frame JPEG (fast, reliable, never
// a broken "?"). Then, only for GIFs scrolled near the viewport, we fetch the
// full animated original in the background — just a couple at a time so we
// never stampede the Pi the way loading every GIF at once did — and swap it in
// once it has decoded. If the animated fetch fails, the static poster stays.
const GIF_CONCURRENCY = 2;
const gifQueue = [];
const gifQueued = new Set();
let gifActive = 0;

function pumpGifQueue() {
    while (gifActive < GIF_CONCURRENCY && gifQueue.length) {
        const item = gifQueue.shift();
        const gridImg = item.querySelector('img');
        if (!gridImg) continue;
        gifActive++;
        const stopSpin = () => { const sp = item.querySelector('.grid-spin'); if (sp) sp.remove(); };
        const loader = new Image();
        // Animation ready → swap poster for the live GIF and drop the spinner.
        loader.onload = () => { gridImg.src = loader.src; stopSpin(); gifActive--; pumpGifQueue(); };
        // Failed → keep the static poster, but stop spinning (don't hang).
        loader.onerror = () => { stopSpin(); gifActive--; pumpGifQueue(); };
        loader.src = '/gif/' + encodeURIComponent(item.dataset.file) + '?v=' + vv(item.dataset.file);
    }
}

function enqueueGif(item) {
    const f = item.dataset.file;
    if (gifQueued.has(f)) return;
    gifQueued.add(f);
    gifQueue.push(item);
    pumpGifQueue();
}

const gifObserver = ('IntersectionObserver' in window)
    ? new IntersectionObserver((entries, obs) => {
        entries.forEach(e => {
            if (!e.isIntersecting) return;
            obs.unobserve(e.target);   // load once, then leave it animating
            enqueueGif(e.target);
        });
      }, { rootMargin: '300px' })
    : null;

// Show the spinner only once the static poster is actually on screen (its
// JPG has loaded); until then there's nothing to overlay. It's removed later
// when the animated GIF swaps in (or fails) via stopSpin().
function revealSpinnerWhenPosterLoads(item) {
    const sp = item.querySelector('.grid-spin');
    const img = item.querySelector('img');
    if (!sp || !img) return;
    if (img.complete && img.naturalWidth > 0) sp.classList.add('show');
    else img.addEventListener('load', () => sp.classList.add('show'), { once: true });
}

(function observeGifs() {
    Object.values(itemEls).forEach(item => {
        if (!isGif(item.dataset.file)) return;
        revealSpinnerWhenPosterLoads(item);
        if (gifObserver) gifObserver.observe(item);   // hidden/filtered items fire when shown
        else enqueueGif(item);                        // no observer: just load all (throttled)
    });
})();

window.addEventListener('load', () => fetch('/preload'));

window.addEventListener('scroll', () => {
    const btn = document.getElementById('top-btn');
    btn.classList.toggle('visible', window.scrollY > 300);
});

function stepViewer(dir) {
    const next = viewerIdx + dir;
    if (next >= 0 && next < files.length) {
        viewerIdx = next;
        hqMode = false;
        document.getElementById('viewer-hq').classList.remove('active');
        updateViewer();
        preloadAhead(viewerIdx);
    }
}

// Keyboard navigation in viewer
document.addEventListener('keydown', e => {
    // Never hijack keys from a form field — Backspace in the filter-name pill
    // must edit the name, not trigger the gallery's delete shortcut.
    if (/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName) || e.target.isContentEditable) return;
    const isDesktop = window.innerWidth >= 1200;
    const viewerOpen = document.getElementById('viewer').classList.contains('open');
    if (viewerOpen) {
        if (e.key === 'ArrowLeft') stepViewer(-1);
        else if (e.key === 'ArrowRight') stepViewer(1);
        else if (e.key === 'Escape') closeViewer();
    } else if (isDesktop) {
        if (e.key === 'Escape' && selected.size > 0) deselectAll();
        else if (e.key === 'Backspace' && selected.size > 0) { e.preventDefault(); confirmDelete(); }
    }
});

// Viewer: double-tap to zoom, drag to pan while zoomed, swipe to nav/close otherwise.
let touchStartX = 0, touchStartY = 0, multiTouch = false;
let zoomScale = 1, panX = 0, panY = 0;
let panOrigX = 0, panOrigY = 0, panning = false;
let lastTapTime = 0, lastTapX = 0, lastTapY = 0;
let pinching = false, pinchStartDist = 0, pinchStartScale = 1;
let pinchContentX = 0, pinchContentY = 0, pinchEndTime = 0;
const ZOOM = 2.5, MAX_ZOOM = 5;
const vb = document.getElementById('viewer-body');

function applyTransform(animate) {
    const img = document.getElementById('viewer-img');
    img.style.transition = animate ? 'transform 0.2s ease' : 'none';
    img.style.transform = 'translate(' + panX + 'px,' + panY + 'px) scale(' + zoomScale + ')';
    img.style.cursor = zoomScale > 1 ? 'grab' : '';
}

function resetZoom(animate) {
    zoomScale = 1; panX = 0; panY = 0; panning = false;
    applyTransform(animate);
}

function clampPan() {
    const img = document.getElementById('viewer-img');
    const maxX = Math.max(0, (img.offsetWidth * zoomScale - vb.clientWidth) / 2);
    const maxY = Math.max(0, (img.offsetHeight * zoomScale - vb.clientHeight) / 2);
    panX = Math.max(-maxX, Math.min(maxX, panX));
    panY = Math.max(-maxY, Math.min(maxY, panY));
}

// Zoom toward the tapped point; if already zoomed, zoom back out.
function toggleZoomAt(clientX, clientY) {
    if (zoomScale > 1) { resetZoom(true); return; }
    const img = document.getElementById('viewer-img');
    const r = vb.getBoundingClientRect();
    let px = (clientX - r.left) - r.width / 2;
    let py = (clientY - r.top) - r.height / 2;
    px = Math.max(-img.offsetWidth / 2, Math.min(img.offsetWidth / 2, px));
    py = Math.max(-img.offsetHeight / 2, Math.min(img.offsetHeight / 2, py));
    zoomScale = ZOOM;
    panX = -px * (ZOOM - 1);
    panY = -py * (ZOOM - 1);
    clampPan();
    applyTransform(true);
}

vb.addEventListener('touchstart', e => {
    if (e.touches.length >= 2) {           // begin pinch
        multiTouch = true; pinching = true; panning = false;
        const t0 = e.touches[0], t1 = e.touches[1];
        pinchStartDist = Math.hypot(t1.clientX - t0.clientX, t1.clientY - t0.clientY) || 1;
        pinchStartScale = zoomScale;
        const r = vb.getBoundingClientRect();
        const mx = (t0.clientX + t1.clientX) / 2 - r.left - r.width / 2;
        const my = (t0.clientY + t1.clientY) / 2 - r.top - r.height / 2;
        pinchContentX = (mx - panX) / zoomScale;   // content point under the pinch centre
        pinchContentY = (my - panY) / zoomScale;
        return;
    }
    multiTouch = false; pinching = false; panning = false;
    touchStartX = e.touches[0].clientX;
    touchStartY = e.touches[0].clientY;
    panOrigX = panX; panOrigY = panY;
}, {passive: true});

vb.addEventListener('touchmove', e => {
    if (pinching && e.touches.length >= 2) {
        e.preventDefault();               // our pinch — not the page's
        const t0 = e.touches[0], t1 = e.touches[1];
        const dist = Math.hypot(t1.clientX - t0.clientX, t1.clientY - t0.clientY);
        zoomScale = Math.max(1, Math.min(MAX_ZOOM, pinchStartScale * (dist / pinchStartDist)));
        const r = vb.getBoundingClientRect();
        const mx = (t0.clientX + t1.clientX) / 2 - r.left - r.width / 2;
        const my = (t0.clientY + t1.clientY) / 2 - r.top - r.height / 2;
        panX = mx - pinchContentX * zoomScale;     // keep that content point under the fingers
        panY = my - pinchContentY * zoomScale;
        clampPan();
        applyTransform(false);
        return;
    }
    if (pinching) return;
    if (zoomScale > 1) {
        const t = e.touches[0];
        const dx = t.clientX - touchStartX, dy = t.clientY - touchStartY;
        if (panning || Math.abs(dx) > 6 || Math.abs(dy) > 6) {
            panning = true;
            e.preventDefault();           // pan the image, don't scroll/swipe
            panX = panOrigX + dx;
            panY = panOrigY + dy;
            clampPan();
            applyTransform(false);
        }
    }
}, {passive: false});

vb.addEventListener('touchend', e => {
    if (pinching) {
        if (e.touches.length >= 2) return;
        pinching = false;
        pinchEndTime = Date.now();
        if (zoomScale <= 1.01) resetZoom(true);
        else { clampPan(); applyTransform(true); }
        if (e.touches.length === 1) {     // a finger remains → continue as a pan
            touchStartX = e.touches[0].clientX;
            touchStartY = e.touches[0].clientY;
            panOrigX = panX; panOrigY = panY;
            multiTouch = false; panning = false;
        }
        return;
    }
    if (multiTouch) { multiTouch = false; panning = false; return; }
    if (panning) { panning = false; return; }
    const ex = e.changedTouches[0].clientX, ey = e.changedTouches[0].clientY;
    const dx = ex - touchStartX, dy = ey - touchStartY;
    if (Math.abs(dx) < 10 && Math.abs(dy) < 10) {
        if (Date.now() - pinchEndTime < 400) return;   // ignore stray taps right after a pinch
        const now = Date.now();
        if (now - lastTapTime < 300 &&
            Math.abs(ex - lastTapX) < 40 && Math.abs(ey - lastTapY) < 40) {
            toggleZoomAt(ex, ey);
            lastTapTime = 0;
            return;
        }
        lastTapTime = now; lastTapX = ex; lastTapY = ey;
        return;
    }
    if (zoomScale > 1) return;             // while zoomed, drags pan — no nav/close
    if (Math.abs(dy) > Math.abs(dx) && dy > 60) { closeViewer(); return; }
    if (Math.abs(dx) > 50) {
        if (dx < 0) stepViewer(1);
        else stepViewer(-1);
    }
}, {passive: true});

// Block Safari's page pinch-zoom in the viewer and over the grid (we handle pinch there).
['gesturestart', 'gesturechange', 'gestureend'].forEach(function (evt) {
    document.addEventListener(evt, function (ev) {
        const viewerOpen = document.getElementById('viewer').classList.contains('open');
        const inGrid = ev.target && ev.target.closest && ev.target.closest('#grid');
        if (viewerOpen || inGrid) ev.preventDefault();
    }, {passive: false});
});

function deselectAll() {
    selected.forEach(f => {
        const el = document.querySelector(`.item[data-file="${f}"]`);
        if (el) el.classList.remove('sel');
    });
    selected.clear();
    document.getElementById('sel-bar').classList.remove('open');
}

function downloadSelected() {
    // A single selection downloads the image itself, not a one-file zip.
    if (selected.size === 1) {
        const f = [...selected][0];
        const a = document.createElement('a');
        a.href = '/photo/' + encodeURIComponent(f);
        a.download = f;
        document.body.appendChild(a);
        a.click();
        a.remove();
        return;
    }
    const token = progressToken();
    const form = document.createElement('form');
    form.method = 'POST';
    form.action = '/download-zip';
    selected.forEach(f => {
        const inp = document.createElement('input');
        inp.type = 'hidden';
        inp.name = 'files';
        inp.value = f;
        form.appendChild(inp);
    });
    const tok = document.createElement('input');
    tok.type = 'hidden';
    tok.name = 'download_token';
    tok.value = token;
    form.appendChild(tok);
    form.target = 'download-frame';
    document.body.appendChild(form);
    trackDownload(token);
    form.submit();
    form.remove();
}

function progressToken() {
    return Date.now().toString(36) + Math.random().toString(36).slice(2);
}

// Poll the Pi for how much of the zip it has streamed, and fill the bottom line.
// (Reflects bytes the Pi has sent — runs slightly ahead of bytes received.)
function trackDownload(token) {
    const started = Date.now();
    let shown = false, lastSent = -1, lastChange = Date.now();
    const poll = setInterval(async () => {
        let total = 0, sent = 0, active = false, ok = false;
        try {
            const d = await (await getJSON('/download-progress?token=' + token)).json();
            total = d.total; sent = d.sent; active = !!d.active; ok = true;
        } catch (e) { /* network blip — keep polling */ }
        if (sent !== lastSent) { lastSent = sent; lastChange = Date.now(); }

        // Reveal once the server has accepted the download, then advance as
        // bytes stream. Using the hidden iframe keeps this page alive while the
        // browser handles the file response.
        if (total > 0 && active) {
            shown = true;
            showProgress(sent / total);
        }

        const now = Date.now();
        const complete = shown && total > 0 && sent >= total;
        const ended = shown && ok && total > 0 && !active;   // server finished/aborted
        // No time cap: keep the bar up as long as the Pi reports the stream is
        // active, however long the download takes.
        if (complete) {
            clearInterval(poll);
            showProgress(1);
            setTimeout(hideProgress, 400);
        } else if (ended) {
            clearInterval(poll);                              // aborted before finishing
            hideProgress();
        } else if ((!shown && now - started > 30000) ||      // server never accepted it
                   (shown && !active && sent === 0) ||        // prompt/download aborted before bytes
                   (shown && now - lastChange > 30000)) {     // hard-freeze backstop
            clearInterval(poll);
            hideProgress();
        }
    }, 250);
}

function showProgress(frac) {
    document.getElementById('dl-progress').classList.add('active');
    document.getElementById('dl-progress-fill').style.width =
        (Math.max(0, Math.min(1, frac)) * 100) + '%';
}

function hideProgress() {
    document.getElementById('dl-progress').classList.remove('active');
    document.getElementById('dl-progress-fill').style.width = '0';
}

// null → confirming a selection delete; a filename → confirming that one image
// (from the viewer). The shared confirm popup dispatches on this in confirmYes().
let pendingDeleteFile = null;

function openConfirm(msgHtml) {
    document.getElementById('confirm-msg').innerHTML = msgHtml;
    const ov = document.getElementById('confirm-overlay');
    ov.style.display = 'flex';
    ov.style.pointerEvents = 'auto';
}

function confirmDelete() {
    pendingDeleteFile = null;
    const n = selected.size;
    openConfirm('Delete ' + n + ' image' + (n !== 1 ? 's' : '') + '?<br>This cannot be undone.');
}

function confirmDeleteCurrent() {
    pendingDeleteFile = files[viewerIdx];
    openConfirm('Delete this image?<br>This cannot be undone.');
}

function confirmYes() {
    if (pendingDeleteFile !== null) deleteCurrent();
    else deleteSelected();
}

function closeConfirm() {
    const ov = document.getElementById('confirm-overlay');
    ov.style.display = 'none';
    ov.style.pointerEvents = 'none';
}

async function deleteCurrent() {
    closeConfirm();
    const f = pendingDeleteFile;
    pendingDeleteFile = null;
    if (!f) return;
    const res = await fetch('/delete', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({files: [f]})
    });
    if (!res.ok) return;
    const wasIdx = files.indexOf(f);   // slot to land on after removal
    const el = itemEls[f];
    if (el) el.remove();
    delete itemEls[f];
    selected.delete(f);
    const aidx = allFiles.indexOf(f);
    if (aidx !== -1) allFiles.splice(aidx, 1);
    refreshSelBar();
    applyView();   // rebuilds `files` (and count / empty state)
    if (files.length === 0) {
        closeViewer();
    } else {
        // Show whatever now occupies the deleted slot, or the new last image.
        viewerIdx = Math.min(Math.max(wasIdx, 0), files.length - 1);
        updateViewer();
    }
}

async function deleteSelected() {
    closeConfirm();
    const filesToDelete = [...selected];
    const res = await fetch('/delete', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({files: filesToDelete})
    });
    if (res.ok) {
        filesToDelete.forEach(f => {
            const el = itemEls[f];
            if (el) el.remove();
            delete itemEls[f];
            selected.delete(f);
            let idx = allFiles.indexOf(f);
            if (idx !== -1) allFiles.splice(idx, 1);
        });
        document.getElementById('sel-bar').classList.remove('open');
        // Rebuild the visible view (also refreshes `files`, count and empty state).
        applyView();
    }
}

// Drag-to-select (desktop only)
let dragStart = null, dragMoved = false;
const dragBox = document.getElementById('drag-select');

document.addEventListener('mousedown', e => {
    if (window.innerWidth < 1200) return;
    if (document.getElementById('viewer').classList.contains('open')) return;
    const blocked = e.target.closest('.img-btn,.sel-circle,.dl-icon,#sel-bar,#top-btn,header');
    if (blocked) return;
    dragStart = { x: e.clientX, y: e.clientY };
    dragMoved = false;
    document.body.style.userSelect = 'none';
});

document.addEventListener('mousemove', e => {
    if (!dragStart) return;
    const dx = e.clientX - dragStart.x, dy = e.clientY - dragStart.y;
    if (!dragMoved && Math.abs(dx) < 5 && Math.abs(dy) < 5) return;
    dragMoved = true;
    const x1 = Math.min(dragStart.x, e.clientX);
    const y1 = Math.min(dragStart.y, e.clientY);
    const x2 = Math.max(dragStart.x, e.clientX);
    const y2 = Math.max(dragStart.y, e.clientY);
    dragBox.style.cssText = `display:block;left:${x1}px;top:${y1}px;width:${x2-x1}px;height:${y2-y1}px;`;
});

document.addEventListener('mouseup', e => {
    if (!dragStart) return;
    document.body.style.userSelect = '';
    dragBox.style.display = 'none';
    if (dragMoved) {
        const x1 = Math.min(dragStart.x, e.clientX);
        const y1 = Math.min(dragStart.y, e.clientY);
        const x2 = Math.max(dragStart.x, e.clientX);
        const y2 = Math.max(dragStart.y, e.clientY);
        document.querySelectorAll('.item').forEach(item => {
            const r = item.getBoundingClientRect();
            if (r.left < x2 && r.right > x1 && r.top < y2 && r.bottom > y1) {
                const f = item.dataset.file;
                if (!selected.has(f)) { selected.add(f); item.classList.add('sel'); }
            }
        });
        refreshSelBar();
    }
    dragStart = null;
    dragMoved = false;
});

// ── Touch multi-select (mobile/tablet) ──
// Tap a thumbnail's dot to select it. Once something is selected, glide
// sideways across thumbnails to "paint" more (a glide started on a dot works
// in any direction). Plain taps open the viewer; vertical drags still scroll.
(function () {
    if (!grid) return;
    const INTENT = 10;        // px of movement before we decide scroll vs. paint
    let painting = false;
    let decided = false;      // have we classified this gesture yet?
    let paintMode = true;     // true = select swept items, false = deselect
    let startItem = null;
    let startOnDot = false;
    let startX = 0, startY = 0;
    let suppressClick = false;
    const handled = new Set();

    function paint(item) {
        if (!item) return;
        const f = item.dataset.file;
        if (handled.has(f)) return;
        handled.add(f);
        setItemSelected(item, paintMode);
        refreshSelBar();
    }

    function beginPaint() {
        painting = true;
        suppressClick = true;          // swallow the click that ends the glide
        handled.clear();
        // Glide off an unselected thumb selects the swath; off a selected one
        // deselects it.
        paintMode = !startItem.classList.contains('sel');
        paint(startItem);
        if (navigator.vibrate) { try { navigator.vibrate(10); } catch (e) {} }
    }

    grid.addEventListener('touchstart', e => {
        if (window.innerWidth >= 1200) return;   // desktop uses mouse drag-select
        if (document.getElementById('viewer').classList.contains('open')) return;
        painting = false;
        decided = false;
        if (e.touches.length > 1) { startItem = null; return; }
        startItem = e.target.closest('.item');
        startOnDot = !!e.target.closest('.sel-circle');
        const t = e.touches[0];
        startX = t.clientX;
        startY = t.clientY;
        handled.clear();
    }, {passive: true});

    grid.addEventListener('touchmove', e => {
        if (!startItem) return;
        const t = e.touches[0];
        const dx = t.clientX - startX, dy = t.clientY - startY;
        if (!painting) {
            if (!decided) {
                if (Math.abs(dx) < INTENT && Math.abs(dy) < INTENT) return;
                decided = true;
                const horizontal = Math.abs(dx) > Math.abs(dy);
                const selectionMode = selected.size > 0;
                // Only a sideways glide starts painting — from a dot, or anywhere
                // once a selection exists. A vertical drag always scrolls, even when
                // it begins on a dot, so scrolling is never hijacked.
                if (horizontal && (startOnDot || selectionMode)) {
                    beginPaint();
                } else {
                    startItem = null;      // vertical / not eligible → let it scroll
                    return;
                }
            }
            if (!painting) return;
        }
        e.preventDefault();                // selecting — don't scroll the page
        const el = document.elementFromPoint(t.clientX, t.clientY);
        paint(el ? el.closest('.item') : null);
    }, {passive: false});

    function end() {
        painting = false;
        decided = false;
        startItem = null;
        startOnDot = false;
        setTimeout(() => { suppressClick = false; }, 350);  // fallback reset
    }
    grid.addEventListener('touchend', end);
    grid.addEventListener('touchcancel', end);

    // Swallow the click that fires right after a paint gesture so it doesn't
    // double-toggle the start thumbnail.
    document.addEventListener('click', e => {
        if (suppressClick) {
            e.preventDefault();
            e.stopPropagation();
            suppressClick = false;
        }
    }, true);
})();

// ── Pinch the grid to change density (mobile only): spread = fewer/larger,
// pinch = more/smaller columns. Steps through 2 → 3 → 4. ──
(function () {
    if (!grid) return;
    const STEP = 1.25;          // distance ratio that triggers one density step
    const COLS = ['2', '3', '4'];
    let pinching = false, baseDist = 0;

    function dist(e) {
        const a = e.touches[0], b = e.touches[1];
        return Math.hypot(b.clientX - a.clientX, b.clientY - a.clientY);
    }

    function changeDensity(delta) {   // -1 = fewer columns, +1 = more columns
        let i = COLS.indexOf(curDensity);
        if (i === -1) i = 0;
        const ni = Math.max(0, Math.min(COLS.length - 1, i + delta));
        if (COLS[ni] !== curDensity) {
            setDensity(COLS[ni]);
            if (navigator.vibrate) { try { navigator.vibrate(8); } catch (e) {} }
        }
    }

    grid.addEventListener('touchstart', e => {
        if (window.innerWidth >= 768) return;   // density is a mobile feature
        if (e.touches.length === 2) { pinching = true; baseDist = dist(e); }
    }, {passive: true});

    grid.addEventListener('touchmove', e => {
        if (!pinching || e.touches.length < 2) return;
        e.preventDefault();                      // don't scroll/zoom the page
        const ratio = dist(e) / baseDist;
        if (ratio > STEP) { changeDensity(-1); baseDist = dist(e); }       // spread → fewer cols
        else if (ratio < 1 / STEP) { changeDensity(1); baseDist = dist(e); } // pinch → more cols
    }, {passive: false});

    grid.addEventListener('touchend', e => {
        if (e.touches.length < 2) pinching = false;
    }, {passive: true});
})();

// ══════════════════════ Settings panel ══════════════════════
// Factory defaults injected from the server so RESET matches the firmware.
const SETTINGS_DEFAULTS = {{ settings_defaults_json | safe }};
let defWb = {{ settings.wb }};
let defFilter = {{ settings.filter }};
let defShutter = {{ settings.shutter }};
let defMetering = {{ settings.metering }};
let defDgain = {{ settings.dgain }};

function flashBtn(btn, label, cls) {
    if (!btn) return;
    if (btn.dataset.orig === undefined) btn.dataset.orig = btn.textContent;
    btn.textContent = label;
    btn.classList.remove('saved', 'errored');
    btn.classList.add(cls);
    clearTimeout(btn._t);
    btn._t = setTimeout(() => {
        btn.textContent = btn.dataset.orig;
        btn.classList.remove('saved', 'errored');
    }, 1300);
}
function flashError(btn) { flashBtn(btn, 'FAILED', 'errored'); }
// Instant neutral (light-gray) highlight confirming a RESET click. Fired
// immediately so the feedback isn't gated on the network round-trip. Kept
// short — at 400ms the button read as stuck in a pressed state.
function flashPulse(btn) {
    if (!btn) return;
    btn.classList.add('pulse');
    clearTimeout(btn._t);
    btn._t = setTimeout(() => btn.classList.remove('pulse'), 180);
}

// A hotspot link to a Pi Zero can stall rather than fail outright, so every
// write is bounded — a request still in flight after this long is treated as a
// failure the viewer can retry, instead of a spinner that never resolves.
const SAVE_TIMEOUT_MS = 6000;

function postJSON(url, body) {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), SAVE_TIMEOUT_MS);
    return fetch(url, {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body || {}),
        signal: ctl.signal
    }).finally(() => clearTimeout(t));
}
// Bounded GET for the same reason: a stalled hotspot link must surface as a
// failure, not a spinner that never resolves (device info, heartbeats, polls).
function getJSON(url) {
    const ctl = new AbortController();
    const t = setTimeout(() => ctl.abort(), SAVE_TIMEOUT_MS);
    return fetch(url, {signal: ctl.signal}).finally(() => clearTimeout(t));
}

// ---- Auto-save ----
// Sections persist on change rather than behind a SAVE button, so the status
// dot in the section header is the only report the viewer gets. It must never
// claim success before the camera has actually acknowledged the write.
const SAVE_ENDPOINT = {
    calib: '/calib-save', gif: '/gif-save', effect: '/effect-save',
    defaults: '/defaults-save', theme: '/theme-save'
};
const SAVE_DEBOUNCE_MS = {calib: 500, gif: 400, effect: 500, defaults: 0, theme: 0};
const saveTimers = {};
// Last payload sent per group, kept so RETRY can resend the edit that failed.
const savePending = {};

function sectionOf(group) {
    if (group === 'gif') group = 'defaults';   // GIF settings live inside Camera Settings
    return document.querySelector('[data-group="' + group + '"]');
}

// Groups can save concurrently; the single header badge shows the aggregate,
// worst first (a failure must never be masked by a later success elsewhere).
const saveStates = {};
function renderHdrStatus() {
    const all = Object.values(saveStates);
    const st = all.indexOf('failed') >= 0 ? 'failed' :
               all.indexOf('saving') >= 0 ? 'saving' :
               all.indexOf('saved')  >= 0 ? 'saved'  : null;
    const el = document.getElementById('hdr-status');
    el.textContent = st === 'saving' ? 'SAVING' :
                     st === 'saved'  ? 'SAVED'  :
                     st === 'failed' ? 'NOT SAVED' : '';
    if (st) el.dataset.state = st; else delete el.dataset.state;
}
function setSaveState(group, state) {
    const sec = sectionOf(group);
    if (!sec) return;
    // data-state stays on the section: it drives that section's RETRY row.
    if (state) sec.dataset.state = state; else delete sec.dataset.state;
    if (state) saveStates[group] = state; else delete saveStates[group];
    renderHdrStatus();
    // Clear a success badge after a beat; a failure stays until it's resolved.
    clearTimeout(sec._statusT);
    if (state === 'saved') {
        sec._statusT = setTimeout(() => {
            if (sec.dataset.state === 'saved') setSaveState(group, null);
        }, 1600);
    }
}

// Coalesce a drag into one write, then report what actually happened.
// Payloads within the window merge (a rename and a slider move are one write);
// a payload for a DIFFERENT effect slot flushes the old one first so switching
// slots mid-debounce can't cross-apply edits.
function autoSave(group, values) {
    const prev = savePending[group];
    if (prev && prev.slot !== undefined && values.slot !== undefined
             && prev.slot !== values.slot) {
        clearTimeout(saveTimers[group]);
        flushSave(group);
        savePending[group] = values;
    } else {
        savePending[group] = prev ? Object.assign({}, prev, values) : values;
    }
    clearTimeout(saveTimers[group]);
    saveTimers[group] = setTimeout(() => flushSave(group), SAVE_DEBOUNCE_MS[group] || 0);
}

function flushSave(group) {
    const body = savePending[group];
    if (body === undefined) return;
    setSaveState(group, 'saving');
    postJSON(SAVE_ENDPOINT[group], body).then(r => {
        if (r.ok) {
            // Only drop the pending payload once the camera has confirmed it,
            // so a later RETRY always has something to resend.
            if (savePending[group] === body) delete savePending[group];
            setSaveState(group, 'saved');
        } else if (r.status >= 400 && r.status < 500) {
            // Permanently invalid — resending identical bytes can never work
            // (typically: the filter was deleted from another device). Drop the
            // payload and resync instead of offering a futile RETRY.
            if (savePending[group] === body) delete savePending[group];
            setSaveState(group, null);
            if (group === 'effect') resyncEffects();
        } else {
            setSaveState(group, 'failed');   // transient: server error / timeout
        }
    }).catch(() => setSaveState(group, 'failed'));
}

// Pull the slot list fresh from the camera and rebuild everything that shows
// it. Heals the page after another device created/renamed/deleted filters.
function resyncEffects() {
    getJSON('/effects').then(r => { if (!r.ok) throw 0; return r.json(); }).then(list => {
        EFFECTS = list;
        const keep = fxSlot;
        fxSlot = null;
        if (keep !== null && EFFECTS[keep]) selectFxSlot(keep);   // reload values
        else document.getElementById('fx-editor').hidden = true;
        renderFxSlots(); rebuildFilterGroup();
    }).catch(() => {});
}

function retrySave(group) {
    if (savePending[group] === undefined) { setSaveState(group, null); return; }
    flushSave(group);
}

// ---- Confirm dialog ----
// Shared by every irreversible settings action (the RESETs, format). Resolves
// true only on an explicit confirm; Cancel takes focus so a stray Enter or a
// dismissed dialog can never be the destructive answer.
let confirmResolve = null;
function askConfirm(msgHtml, yesLabel) {
    const overlay = document.getElementById('set-confirm-overlay');
    document.getElementById('set-confirm-msg').innerHTML = msgHtml;
    document.getElementById('set-confirm-yes').textContent = yesLabel;
    overlay.classList.add('open');
    document.getElementById('set-confirm-no').focus();
    return new Promise(resolve => { confirmResolve = resolve; });
}
function setConfirmAnswer(ok) {
    document.getElementById('set-confirm-overlay').classList.remove('open');
    const r = confirmResolve;
    confirmResolve = null;
    if (r) r(ok);
}
document.addEventListener('keydown', e => {
    if (e.key === 'Escape' && confirmResolve) { e.stopPropagation(); setConfirmAnswer(false); }
}, true);

function openSettings() {
    // Collapse every section *before* the panel is shown — while it's still
    // display:none, so re-collapsing a section left open from a prior visit
    // snaps shut instead of visibly rotating its chevron on open.
    document.querySelectorAll('.settings-sec').forEach(s => s.classList.add('collapsed'));
    document.querySelectorAll('.sec-title').forEach(t => t.setAttribute('aria-expanded', 'false'));
    document.getElementById('settings').classList.add('open');
    document.body.style.overflow = 'hidden';
    document.querySelector('.settings-body').scrollTop = 0;
    syncCalibLabels();
    syncGifLabels();
    syncEffectLabels();
    deactivateCalib();   // LCD shows the hotspot screen until calibration is expanded
    fxSrcReset();        // sections were force-collapsed above — no orphan live stream
}

function closeSettings() {
    document.getElementById('settings').classList.remove('open');
    document.body.style.overflow = '';
    deactivateCalib();
    fxSrcReset();
}

// ---- Screen calibration ----
const CAL_KEYS = {{ calib_keys | tojson }};
let curCalibImg = 'color';
function calibValues() {
    const v = {};
    CAL_KEYS.forEach(k => { v[k] = parseInt(document.getElementById('cal-' + k).value, 10); });
    return v;
}
function fmtCal(k, v) {
    if (k === 'brightness' || k === 'contrast' || k === 'saturation') return v + '%';
    if (k === 'gamma') return (v / 100).toFixed(2);
    if (k === 'temp' || k === 'tint') return v === 0 ? '0' : (v > 0 ? '+' + v : '' + v);
    return (v / 100).toFixed(2);   // red/green/blue gain
}
function syncCalibLabels() {
    const v = calibValues();
    CAL_KEYS.forEach(k => { document.getElementById('cal-' + k + '-val').textContent = fmtCal(k, v[k]); });
    updateResetHints();
}
let calibTimer = null, calibPending = false;
function onCalibInput() {
    syncCalibLabels();
    // Throttle the live push so a fast drag doesn't flood the Pi, but always
    // send a final update after the last move so the camera lands on the
    // released value.
    calibPending = true;
    // The live push only previews on the LCD; persist the profile too, so
    // collapsing the section no longer throws the adjustment away.
    autoSave('calib', calibValues());
    if (calibTimer) return;
    const flush = () => {
        if (!calibPending) { calibTimer = null; return; }
        calibPending = false;
        postJSON('/calib-live', calibValues());
        calibTimer = setTimeout(flush, 70);
    };
    flush();
}
function resetCalib() {
    const btn = document.getElementById('cal-reset');
    askConfirm('Discard the saved display profile?<br>The camera returns to its factory calibration.',
               'DISCARD').then(ok => {
        if (!ok) return;
        flashPulse(btn);
        postJSON('/calib-reset', {}).then(r => { if (!r.ok) throw 0; return r.json(); }).then(d => {
            CAL_KEYS.forEach(k => { document.getElementById('cal-' + k).value = d[k]; });
            syncCalibLabels();
            delete savePending['calib'];   // the reset is now the saved state
            setSaveState('calib', null);
        }).catch(() => flashError(btn));
    });
}
/* Emphasize a reset icon while its parameter sits off the default, so changed
 * values read at a glance. Slider defaults come from each button's own
 * miniReset(...) onclick args — the one place they're already declared; the
 * curve-reset button is the special case (identity curve). Called from the
 * label syncs + drawCurve, which every value change already goes through. */
function updateResetHints() {
    document.querySelectorAll('.mini-reset').forEach(btn => {
        const m = /miniReset\('([^']+)'\s*,\s*(-?[\d.]+)/.exec(btn.getAttribute('onclick') || '');
        let changed = false;
        if (m) {
            const el = document.getElementById(m[1]);
            changed = !!el && parseFloat(el.value) !== parseFloat(m[2]);
        } else if (btn.classList.contains('fx-curve-reset')) {
            changed = [0, 64, 128, 192, 255].some((y, i) => fxCurve[i] !== y);
        }
        btn.classList.toggle('changed', changed);
    });
}
// Per-slider reset icon: sets one slider back to its default and fires the
// group's live update (calibration/gif/effect).
function miniReset(id, def, group) {
    const el = document.getElementById(id);
    if (!el) return;
    el.value = def;
    if (group === 'calib') onCalibInput();
    else if (group === 'gif') onGifInput();
    else if (group === 'effect') onEffectInput();
}

// Screen-calibration reference picker (COLOR/GRAY/PHOTO). The LCD calibration is
// tied to the section being expanded: activateCalib()/deactivateCalib() are
// called by toggleSection().
const CALIB_IMGS = {{ calib_patterns | tojson }};
// Derived server-side from the pattern files' mtimes — stale-cache busting
// needs no manual bump.
const CALIB_ASSET_V = '{{ calib_asset_v }}';
// Each pattern teaches its own move — the caption says which knob it serves.
const CALIB_NOTES = {
    color: 'Match the camera\\'s LCD to this reference.',
    gray:  'Fix colour cast with TEMPERATURE and TINT.',
    photo: 'Match the camera\\'s LCD to this reference.'
};
function setCalibNote(name) {
    const el = document.getElementById('calib-note');
    if (el) el.textContent = CALIB_NOTES[name] || CALIB_NOTES.photo;
}
function setCalibImgEl(name) {
    const el = document.getElementById('calib-img');
    el.classList.toggle('smooth', name === 'photo');
    el.src = '/calib-image?img=' + name + '&v=' + CALIB_ASSET_V;
}
/* Desktop-only enlarge/shrink of the filter-maker preview. The button lives
 * beside the CURVE/COLOR pill, not in the wrap, so find the wrap through the
 * enclosing settings section. */
function toggleZoom(btn) {
    const wrap = btn.closest('.settings-sec').querySelector('.fx-preview-wrap');
    const on = wrap.classList.toggle('expanded');
    btn.classList.toggle('on', on);
    btn.setAttribute('aria-label', on ? 'Shrink preview' : 'Enlarge preview');
}
function activateCalib() {
    fxSrcReset();     /* calibration owns the LCD + CPU: kick Filter Maker's
                         LIVE stream back to PHOTO so the two never overlap */
    setCalibImgEl(curCalibImg);
    setCalibNote(curCalibImg);
    postJSON('/calib-live', calibValues());
    postJSON('/calib-img', {img: curCalibImg});   // sets pattern + activates flag
    startCalibHeartbeat();
}
function deactivateCalib() {
    stopCalibHeartbeat();
    /* a throttled slider flush still in flight would POST /calib-live right
     * after the close and resurrect the active flag — cancel it first */
    calibPending = false;
    if (calibTimer) { clearTimeout(calibTimer); calibTimer = null; }
    postJSON('/calib-close', {});
}
function calibIsOpen() {
    const s = document.getElementById('sec-calib');
    return s && !s.classList.contains('collapsed');
}
function pickCalibImg(name) {
    if (CALIB_IMGS.indexOf(name) < 0) name = 'color';
    curCalibImg = name;
    setOptActive('calib-img-group', name);
    setCalibImgEl(name);
    setCalibNote(name);
    if (calibIsOpen()) { postJSON('/calib-img', {img: name}); startCalibHeartbeat(); }
}
function cycleCalibImg(dir) {   // +1 next, -1 previous (color/gray/contrast/photo)
    let i = CALIB_IMGS.indexOf(curCalibImg);
    if (i < 0) i = 0;
    pickCalibImg(CALIB_IMGS[(i + dir + CALIB_IMGS.length) % CALIB_IMGS.length]);
}
// Hotspot theme: swap the body theme class (all colours are CSS variables) and
// persist so the next page load renders in the same theme.
function setTheme(name) {
    document.body.classList.remove('theme-dark', 'theme-light', 'theme-yellow');
    document.body.classList.add('theme-' + name);
    setOptActive('theme-group', name);
    autoSave('theme', {theme: name});
    if (typeof drawCurve === 'function') drawCurve();   // recolour the JS-drawn curve
}

// Expand/collapse a section. The calibration LCD, the effect canvas and the
// device-info fetch are driven lazily off the section's expansion.
function toggleSection(titleEl) {
    const sec = titleEl.closest('.settings-sec');
    if (!sec) return;
    const expanding = sec.classList.contains('collapsed');
    sec.classList.toggle('collapsed');
    titleEl.setAttribute('aria-expanded', expanding ? 'true' : 'false');
    if (sec.id === 'sec-calib') { if (expanding) activateCalib(); else deactivateCalib(); }
    else if (sec.id === 'sec-effect') {
        if (expanding) { renderFxSlots(); initEffectCanvas(); }
        else fxSrcReset();                     /* collapsing kills the live stream */
    }
    else if (sec.id === 'sec-data' && expanding) loadDeviceInfo();
}
// Swipe the reference image left/right to change pattern (mirrors the joystick).
(function () {
    const wrap = document.querySelector('.calib-img-wrap');
    if (!wrap) return;
    let sx = 0, sy = 0, tracking = false;
    wrap.addEventListener('touchstart', e => {
        if (e.touches.length !== 1) { tracking = false; return; }
        tracking = true; sx = e.touches[0].clientX; sy = e.touches[0].clientY;
    }, {passive: true});
    wrap.addEventListener('touchend', e => {
        if (!tracking) return;
        tracking = false;
        const t = e.changedTouches[0];
        const dx = t.clientX - sx, dy = t.clientY - sy;
        if (Math.abs(dx) > 40 && Math.abs(dx) > Math.abs(dy)) cycleCalibImg(dx < 0 ? 1 : -1);
    }, {passive: true});
})();
// While calibration is on, heartbeat the active flag (~every 1.5s, well under
// the firmware's 8s stale timeout) so a dropped phone lets the LCD recover, and
// follow the camera's joystick pattern changes.
let calibHb = null;
function startCalibHeartbeat() {
    stopCalibHeartbeat();
    calibHb = setInterval(async () => {
        postJSON('/calib-ping', {});
        try {
            const d = await (await getJSON('/calib-img-get')).json();
            if (d.img && d.img !== 'off' && d.img !== curCalibImg) {
                curCalibImg = d.img;
                setOptActive('calib-img-group', d.img);
                setCalibImgEl(d.img);
                setCalibNote(d.img);
            }
        } catch (e) {}
    }, 1500);
}
function stopCalibHeartbeat() { if (calibHb) { clearInterval(calibHb); calibHb = null; } }

function setCalView(view) {
    setOptActive('cal-view-group', view);
    const views = {display: 'cal-display-view', color: 'cal-color-view', advanced: 'cal-adv-view'};
    for (const [k, id] of Object.entries(views)) {
        const el = document.getElementById(id);
        if (k === view) el.removeAttribute('hidden'); else el.setAttribute('hidden', '');
    }
}

// ---- Data & Device info ----
function loadDeviceInfo() {
    const el = document.getElementById('device-info');
    if (!el) return;
    getJSON('/device-info').then(r => r.json()).then(d => renderDeviceInfo(d))
        .catch(() => { el.innerHTML = '<div class="info-loading">Unavailable</div>'; });
}
function renderDeviceInfo(d) {
    const el = document.getElementById('device-info');
    if (!el) return;
    const row = (k, v, sub) => '<div class="info-row"><span class="info-k">' + k +
        '</span><span class="info-v">' + v + (sub ? '<span class="sub">' + sub + '</span>' : '') +
        '</span></div>';
    const s = d.storage || {};
    /* how many more stills fit: average of what's already stored, or a 3.5MB
     * q98 full-res estimate when the card is still fresh */
    const avgPhoto = (s.photo_count >= 5) ? s.photo_bytes / s.photo_count : 3.5 * 1024 * 1024;
    const fitCount = Math.floor((s.free || 0) / avgPhoto);
    let h = '<div class="sub-group">STORAGE</div>';
    h += row('SD CARD CAPACITY', formatSize(s.disk_total || 0));
    h += row('SYSTEM FILES', formatSize(s.system || 0));
    h += row('AVAILABLE SPACE', '≈ ' + fitCount.toLocaleString() + ' photos · ' + formatSize(s.free || 0));
    h += row('PHOTOS STORED', (s.photo_count || 0) + ' files · ' + formatSize(s.photo_bytes || 0));
    h += row('GIFS STORED', (s.gif_count || 0) + ' files · ' + formatSize(s.gif_bytes || 0));
    h += '<div class="sub-group" style="margin-top:30px">DEVICE</div>';
    h += row('FIRMWARE VERSION', d.version || '—');
    h += row('LICENSE', '<a href="' + d.license_url + '" target="_blank" rel="noopener">' + d.license + '</a>');
    h += row('SOURCE', '<a href="' + d.github + '" target="_blank" rel="noopener">GITHUB</a>');
    h += '<div class="info-copyright">' + (d.copyright || '') + '<br>Licensed under ' + (d.license || '') + '</div>';
    el.innerHTML = h;
}

// ---- GIF recording ----
function gifValues() {
    return {
        frames: parseInt(document.getElementById('gif-frames').value, 10),
        interval: parseInt(document.getElementById('gif-interval').value, 10) / 10
    };
}
function syncGifLabels() {
    const v = gifValues();
    document.getElementById('gif-frames-val').textContent = v.frames;
    document.getElementById('gif-interval-val').textContent = v.interval.toFixed(1) + 's';
    updateResetHints();
}
function onGifInput() { syncGifLabels(); autoSave('gif', gifValues()); }
function resetGif() {
    const btn = document.getElementById('gif-reset');
    flashPulse(btn);
    postJSON('/gif-reset', {}).then(r => { if (!r.ok) throw 0; return r.json(); }).then(d => {
        document.getElementById('gif-frames').value = d.frames;
        document.getElementById('gif-interval').value = Math.round(d.interval * 10);
        syncGifLabels();
        delete savePending['gif'];
        setSaveState('gif', null);
    }).catch(() => flashError(btn));
}

// ---- Effect Maker (custom filter) ----
// Preview matches the firmware EXACTLY for the tone curve + R/G/B tint (a
// per-channel LUT); saturation and grain are close approximations of the
// on-sensor ISP + grain overlay. Curve X positions are fixed at 0,64,128,192,255.
const FX_XS = [0, 64, 128, 192, 255];
const FX_PV = 720;   // preview render size — 3x the LCD so it stays sharp on retina
let fxCurve = {{ settings.effect.curve | tojson }};   // 5 Y values (0-255)
let fxSample = null, fxCtx = null, fxLoaded = false, fxPeeking = false;
const FX_KEYS = ['red', 'green', 'blue', 'saturation', 'grain'];

// Custom-filter slots. EFFECTS mirrors the four /data/.effectN files (null =
// slot free); fxSlot is the slot open in the editor, or null.
let EFFECTS = {{ effects_json | safe }};
const MAX_FX = {{ max_effect_slots }};
const FX_NAME_MAX = {{ effect_name_max }};
const PRESET_COUNT = {{ preset_count }};
const FILTER_PRESETS = {{ settings.filter_options | tojson }}.slice(0, PRESET_COUNT);
let fxSlot = null;

function escHtml(s) {
    return String(s).replace(/[&<>"']/g,
        c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

// Full rebuild of the slot list. Never call this from handlers that live
// INSIDE a pill (rename, focus): replacing the DOM would destroy the input
// mid-keystroke — those paths use updateFxActive()/direct value sync instead.
function renderFxSlots() {
    const list = document.getElementById('fx-slot-list');
    let h = '';
    EFFECTS.forEach((e, i) => {
        if (!e) return;
        h += '<div class="fx-slot-row' + (i === fxSlot ? ' active' : '') +
             '" data-slot="' + i + '" onclick="toggleFxSlot(' + i + ')">' +
             '<input class="fx-slot-name-input" value="' + escHtml(e.name) + '"' +
             ' maxlength="' + FX_NAME_MAX + '" autocomplete="off" spellcheck="false"' +
             ' aria-label="Filter name"' +
             /* click AND focus both select: focus alone covers keyboard tabbing,
                click covers pointers even when the focus event is swallowed */
             ' onclick="event.stopPropagation(); selectFxSlot(' + i + ')"' +
             ' onfocus="selectFxSlot(' + i + ')"' +
             ' oninput="renameFx(' + i + ', this.value)">' +
             '<span class="fx-slot-tag">SLOT ' + (i + 1) + '</span>' +
             '<button type="button" class="fx-slot-del" aria-label="Delete filter"' +
             ' onclick="event.stopPropagation(); deleteFxSlot(' + i + ')">&#10005;</button></div>';
    });
    list.innerHTML = h;
    const create = document.getElementById('fx-create');
    const free = EFFECTS.some(e => !e);
    create.disabled = !free;
    create.textContent = free ? '+ NEW FILTER' : 'ALL ' + MAX_FX + ' SLOTS USED';
}

function updateFxActive() {
    document.querySelectorAll('#fx-slot-list .fx-slot-row').forEach(r =>
        r.classList.toggle('active', +r.dataset.slot === fxSlot));
}

function fxPillInput(slot) {
    const row = document.querySelector('#fx-slot-list .fx-slot-row[data-slot="' + slot + '"]');
    return row ? row.querySelector('.fx-slot-name-input') : null;
}

// Rename from the pill input — the only place a filter is named.
function renameFx(slot, value) {
    if (!EFFECTS[slot]) return;
    EFFECTS[slot].name = value.trim() || ('Custom ' + (slot + 1));
    autoSave('effect', {slot: slot, name: value});
    rebuildFilterGroup();
}

// The camera's filter list is the nine presets plus existing customs in slot
// order; the CAMERA SETTINGS group must always mirror that numbering.
function rebuildFilterGroup() {
    const names = FILTER_PRESETS.concat(
        EFFECTS.filter(e => e).map(e => e.name));
    if (defFilter >= names.length) defFilter = 0;
    const g = document.getElementById('filter-group-def');
    g.innerHTML = names.map((n, i) =>
        '<button data-val="' + i + '" class="' + (i === defFilter ? 'active' : '') +
        '" onclick="pickFilter(' + i + ')">' + escHtml(n.toUpperCase()) + '</button>').join('');
}

function toggleFxSlot(i) {
    if (fxSlot === i) deselectFxSlot();
    else selectFxSlot(i);
}
function deselectFxSlot() {
    fxSlot = null;
    fxSrcReset();                              /* editor gone — stop any live stream */
    document.getElementById('fx-editor').hidden = true;
    document.activeElement && document.activeElement.blur();
    updateFxActive();
}
function selectFxSlot(i) {
    if (!EFFECTS[i] || fxSlot === i) return;
    fxSlot = i;
    const e = EFFECTS[i];
    fxCurve = e.curve.slice();
    fxSmooth = e.smooth || 0;
    setOptActive('fx-interp-group', fxSmooth);
    FX_KEYS.forEach(k => { document.getElementById('fx-' + k).value = e[k]; });
    document.getElementById('fx-editor').hidden = false;
    updateFxActive();       // no rebuild: selection can come from a pill input's focus
    syncEffectLabels(); drawCurve(); renderPreview();
}

function createFx() {
    const btn = document.getElementById('fx-create');
    postJSON('/effect-create', {}).then(r => { if (!r.ok) throw 0; return r.json(); }).then(d => {
        EFFECTS[d.slot] = d.effect;
        rebuildFilterGroup();
        renderFxSlots();            // the new pill must exist before selection
        selectFxSlot(d.slot);
        const pill = fxPillInput(d.slot);   // name it right away, in the pill
        if (pill) { pill.focus(); pill.select(); }
    }).catch(() => flashError(btn));
}

function deleteFxSlot(slot) {
    if (!EFFECTS[slot]) return;
    askConfirm('Delete “' + escHtml(EFFECTS[slot].name) + '”?<br>The camera loses this filter.',
               'DELETE').then(ok => {
        if (!ok) return;
        postJSON('/effect-delete', {slot: slot})
            .then(r => { if (!r.ok) throw 0; return r.json(); }).then(d => {
                EFFECTS[slot] = null;
                if (fxSlot === slot) {
                    fxSlot = null;
                    document.getElementById('fx-editor').hidden = true;
                }
                delete savePending['effect'];
                setSaveState('effect', null);
                defFilter = d.filter;
                renderFxSlots(); rebuildFilterGroup();
            }).catch(() => flashError(document.getElementById('fx-delete')));
    });
}
function deleteFx() { if (fxSlot !== null) deleteFxSlot(fxSlot); }

function clamp255(v) { return v < 0 ? 0 : v > 255 ? 255 : v | 0; }
// Curve interpolation for the ACTIVE custom filter: 0 = straight segments
// (the presets' native math), 1 = monotone cubic (Lightroom-style smooth).
// Per-filter, persisted as the 11th number of the slot file.
let fxSmooth = 0;
function fxValues() {
    const v = {curve: fxCurve.slice(), smooth: fxSmooth};
    FX_KEYS.forEach(k => { v[k] = parseInt(document.getElementById('fx-' + k).value, 10); });
    return v;
}
function syncEffectLabels() {
    const v = fxValues();
    document.getElementById('fx-red-val').textContent = (v.red / 100).toFixed(2);
    document.getElementById('fx-green-val').textContent = (v.green / 100).toFixed(2);
    document.getElementById('fx-blue-val').textContent = (v.blue / 100).toFixed(2);
    document.getElementById('fx-saturation-val').textContent = v.saturation + '%';
    document.getElementById('fx-grain-val').textContent = v.grain;
    updateResetHints();
}
// Monotone-cubic (Fritsch–Carlson) tangents through the 5 fixed-X points.
// Monotone variant on purpose: plain Catmull-Rom overshoots past 0/255 between
// close points and the clamp would flat-top the curve.
function fxTangents(ys) {
    const d = [], m = [];
    for (let i = 0; i < 4; i++) d.push((ys[i + 1] - ys[i]) / (FX_XS[i + 1] - FX_XS[i]));
    m[0] = d[0]; m[4] = d[3];
    for (let i = 1; i < 4; i++) m[i] = (d[i - 1] * d[i] <= 0) ? 0 : (d[i - 1] + d[i]) / 2;
    for (let i = 0; i < 4; i++) {
        if (d[i] === 0) { m[i] = 0; m[i + 1] = 0; continue; }
        const a = m[i] / d[i], b = m[i + 1] / d[i], s = a * a + b * b;
        if (s > 9) { const t = 3 / Math.sqrt(s); m[i] = t * a * d[i]; m[i + 1] = t * b * d[i]; }
    }
    return m;
}
function fxEvalSmooth(ys, m, x) {   // Hermite eval at input x (0..255)
    let i = 0; while (i < 3 && x > FX_XS[i + 1]) i++;
    const h = FX_XS[i + 1] - FX_XS[i], t = (x - FX_XS[i]) / h, t2 = t * t, t3 = t2 * t;
    return ys[i] * (2 * t3 - 3 * t2 + 1) + h * m[i] * (t3 - 2 * t2 + t) +
           ys[i + 1] * (-2 * t3 + 3 * t2) + h * m[i + 1] * (t3 - t2);
}
// LUT for the active interpolation mode — mirrors the firmware byte-for-byte
// in both modes (linear = _make_curve, smooth = _make_curve_smooth).
function fxCurveLUT(pts, smooth) {
    if (smooth === undefined) smooth = fxSmooth;
    const lut = new Uint8Array(256);
    if (smooth) {
        const m = fxTangents(pts);
        for (let x = 0; x < 256; x++) lut[x] = clamp255(Math.round(fxEvalSmooth(pts, m, x)));
        return lut;
    }
    for (let x = 0; x < 256; x++) {
        let i = 1; while (i < 4 && x > FX_XS[i]) i++;
        const t = (x - FX_XS[i - 1]) / (FX_XS[i] - FX_XS[i - 1]);
        lut[x] = clamp255(Math.round(pts[i - 1] + t * (pts[i] - pts[i - 1])));
    }
    return lut;
}
function renderPreview() {
    if (!fxLoaded || !fxSample || !fxCtx) return;
    /* canvas tracks the sample: 720 for the static photo, the camera's native
     * size (640) on the LIVE tab — no resample between filter and screen */
    const pv = fxCtx.canvas;
    if (pv.width !== fxSample.width || pv.height !== fxSample.height) {
        pv.width = fxSample.width; pv.height = fxSample.height;
    }
    if (fxPeeking) { fxCtx.putImageData(fxSample, 0, 0); return; }   /* hold-to-peek: raw, live-updating */
    const v = fxValues();
    const cur = fxCurveLUT(v.curve);
    const mr = v.red / 100, mg = v.green / 100, mb = v.blue / 100;
    const sat = v.saturation / 100, grain = v.grain;
    const lr = new Uint8Array(256), lg = new Uint8Array(256), lb = new Uint8Array(256);
    for (let i = 0; i < 256; i++) { lr[i] = clamp255(cur[i] * mr); lg[i] = clamp255(cur[i] * mg); lb[i] = clamp255(cur[i] * mb); }
    const src = fxSample.data;
    const out = fxCtx.createImageData(fxSample.width, fxSample.height);
    const d = out.data;
    for (let p = 0; p < src.length; p += 4) {
        let r = lr[src[p]], g = lg[src[p + 1]], b = lb[src[p + 2]];
        if (Math.abs(sat - 1) > 0.001) {
            const y = (r * 299 + g * 587 + b * 114) / 1000;
            r = clamp255(y + sat * (r - y)); g = clamp255(y + sat * (g - y)); b = clamp255(y + sat * (b - y));
        }
        if (grain > 0) { const n = (Math.random() * 2 - 1) * grain; r = clamp255(r + n); g = clamp255(g + n); b = clamp255(b + n); }
        d[p] = r; d[p + 1] = g; d[p + 2] = b; d[p + 3] = 255;
    }
    fxCtx.putImageData(out, 0, 0);
}
const FX_PAD = 9;   // inset so control-point circles never touch the graph edges
// Plot mapping (CSS px). t = x/255 (0..1), v = level/255 (0..1).
function fxPX(t, W) { return FX_PAD + t * (W - 2 * FX_PAD); }
function fxPY(v, H) { return FX_PAD + (1 - v) * (H - 2 * FX_PAD); }
function drawCurve() {
    const cv = document.getElementById('fx-curve');
    if (!cv) return;
    const rect = cv.getBoundingClientRect();
    const W = rect.width || 300, H = rect.height || 150;   // CSS pixels
    const dpr = window.devicePixelRatio || 1;
    const bw = Math.round(W * dpr), bh = Math.round(H * dpr);
    if (cv.width !== bw || cv.height !== bh) { cv.width = bw; cv.height = bh; }  // crisp on retina
    const ctx = cv.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, W, H);
    // Pull colours from the active theme so the curve stays visible on light/yellow.
    const cs = getComputedStyle(document.body);
    const cGrid = (cs.getPropertyValue('--line2') || '#2a2a2a').trim();
    const cLine = (cs.getPropertyValue('--text') || '#d2d2d2').trim();
    const cPt = (cs.getPropertyValue('--text-hi') || '#fff').trim();
    // grid at quarters, within the padded plot area
    ctx.strokeStyle = cGrid; ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
        const x = fxPX(i / 4, W), y = fxPY(i / 4, H);
        ctx.beginPath(); ctx.moveTo(x, fxPY(0, H)); ctx.lineTo(x, fxPY(1, H)); ctx.stroke();
        ctx.beginPath(); ctx.moveTo(fxPX(0, W), y); ctx.lineTo(fxPX(1, W), y); ctx.stroke();
    }
    // curve: drawn with whatever interpolation the filter actually uses, so the
    // graph never lies — straight segments for linear, sampled Hermite for smooth.
    ctx.strokeStyle = cLine; ctx.lineWidth = 2; ctx.lineJoin = 'round'; ctx.lineCap = 'round';
    ctx.beginPath();
    if (fxSmooth) {
        const m = fxTangents(fxCurve);
        for (let s = 0; s <= 96; s++) {
            const x = s * 255 / 96;
            const y = Math.max(0, Math.min(255, fxEvalSmooth(fxCurve, m, x)));
            const px = fxPX(x / 255, W), py = fxPY(y / 255, H);
            s === 0 ? ctx.moveTo(px, py) : ctx.lineTo(px, py);
        }
    } else {
        for (let i = 0; i < 5; i++) {
            const px = fxPX(FX_XS[i] / 255, W), py = fxPY(fxCurve[i] / 255, H);
            i === 0 ? ctx.moveTo(px, py) : ctx.lineTo(px, py);
        }
    }
    ctx.stroke();
    ctx.fillStyle = cPt;
    for (let i = 0; i < 5; i++) {
        const px = fxPX(FX_XS[i] / 255, W), py = fxPY(fxCurve[i] / 255, H);
        ctx.beginPath(); ctx.arc(px, py, 5.5, 0, 2 * Math.PI); ctx.fill();
    }
    updateResetHints();   // curve edits arrive here (drag/reset/interp/slot)
}
// Curve back to identity. Only the curve — sliders, tint and the LINEAR/SMOOTH
// choice stay. Instant like the per-slider mini-resets (no confirm).
function resetFxCurve() {
    if (fxSlot === null) return;
    fxCurve = [0, 64, 128, 192, 255];
    EFFECTS[fxSlot].curve = fxCurve.slice();
    drawCurve(); renderPreview();
    autoSave('effect', {slot: fxSlot, curve: fxCurve.slice()});
}
function setFxInterp(v) {
    if (fxSlot === null) return;
    fxSmooth = v;
    EFFECTS[fxSlot].smooth = v;
    setOptActive('fx-interp-group', v);
    drawCurve(); renderPreview();
    autoSave('effect', {slot: fxSlot, smooth: v});
}
function setFxView(view) {
    setOptActive('fx-view-group', view);
    const curveV = document.getElementById('fx-curve-view');
    const colorV = document.getElementById('fx-color-view');
    if (view === 'color') { curveV.setAttribute('hidden', ''); colorV.removeAttribute('hidden'); }
    else { colorV.setAttribute('hidden', ''); curveV.removeAttribute('hidden'); drawCurve(); }
}
function initEffectCanvas() {
    const pv = document.getElementById('fx-preview');
    if (!pv || fxLoaded) { renderPreview(); drawCurve(); return; }
    fxCtx = pv.getContext('2d');
    const img = new Image();
    img.onload = () => {
        const oc = document.createElement('canvas'); oc.width = FX_PV; oc.height = FX_PV;
        const octx = oc.getContext('2d');
        const s = Math.min(img.width, img.height), sx = (img.width - s) / 2, sy = (img.height - s) / 2;
        octx.drawImage(img, sx, sy, s, s, 0, 0, FX_PV, FX_PV);
        fxSample = octx.getImageData(0, 0, FX_PV, FX_PV);
        fxPhotoSample = fxSample;     /* LIVE keeps this for instant switch-back */
        fxLoaded = true;
        renderPreview(); drawCurve();
    };
    img.src = '/calib-image?img=photo&v=' + CALIB_ASSET_V;   // sample image (same origin)
    drawCurve();
}
/* ---- LIVE tab: poll camera frames, filter them client-side. The firmware
 * streams unfiltered 640px JPEGs while /data/.live_active stays fresh; every
 * frame runs through the same renderPreview() the static photo uses, so
 * slider moves apply instantly with zero extra traffic. */
let fxSrc = 'photo', liveTimer = null, livePinger = null, liveCv = null, liveAutoStop = null;
const LIVE_MAX_MS = 120000;            /* auto-stop: camera + hotspot are the two big power draws */
let fxPhotoSample = null;              /* static sample kept for instant switch-back */
function setFxSrc(name) {
    if (name === fxSrc) return;
    if (name === 'live') {
        /* LIVE and screen calibration must never overlap (they fight over the
           LCD and the CPU) — switching to LIVE collapses an open calib section */
        const cs = document.getElementById('sec-calib');
        if (cs && !cs.classList.contains('collapsed'))
            toggleSection(cs.querySelector('.sec-title'));
    }
    fxSrc = name;
    setOptActive('fx-src-group', name);
    if (name === 'live') startLive(); else stopLive();
}
/* Return the pill to PHOTO and stop streaming — used by every "leave" path
 * (tab switch handles itself via setFxSrc). */
function fxSrcReset() {
    if (fxSrc === 'photo') return;
    fxSrc = 'photo';
    setOptActive('fx-src-group', 'photo');
    stopLive();
}
function startLive() {
    postJSON('/live-open', {});
    livePinger = setInterval(() => postJSON('/live-ping', {}), 1500);
    if (liveAutoStop) clearTimeout(liveAutoStop);
    liveAutoStop = setTimeout(fxSrcReset, LIVE_MAX_MS);   /* back to PHOTO after 2 min */
    liveTick();
}
function stopLive() {
    if (livePinger) { clearInterval(livePinger); livePinger = null; }
    if (liveTimer) { clearTimeout(liveTimer); liveTimer = null; }
    if (liveAutoStop) { clearTimeout(liveAutoStop); liveAutoStop = null; }
    postJSON('/live-close', {});
    if (fxPhotoSample) { fxSample = fxPhotoSample; renderPreview(); }
}
/* Backgrounded page (phone app-switch, locked screen): stop the camera and
 * the pings instead of letting throttled 1Hz timers keep it warm for nothing;
 * pick the stream back up when the page returns. */
document.addEventListener('visibilitychange', () => {
    if (fxSrc !== 'live') return;
    if (document.hidden) {
        if (livePinger) { clearInterval(livePinger); livePinger = null; }
        if (liveTimer) { clearTimeout(liveTimer); liveTimer = null; }
        postJSON('/live-close', {});
    } else startLive();
});
async function liveTick() {
    if (fxSrc !== 'live') return;
    const t0 = performance.now();
    try {
        const r = await fetch('/live-frame', {cache: 'no-store'});
        if (r.ok) {
            const bmp = await createImageBitmap(await r.blob());
            if (!liveCv) liveCv = document.createElement('canvas');
            if (liveCv.width !== bmp.width || liveCv.height !== bmp.height) {
                liveCv.width = bmp.width; liveCv.height = bmp.height;
            }
            const c = liveCv.getContext('2d', {willReadFrequently: true});
            c.drawImage(bmp, 0, 0);
            bmp.close();
            if (fxSrc === 'live') {            /* may have stopped mid-await */
                fxSample = c.getImageData(0, 0, liveCv.width, liveCv.height);
                fxLoaded = true;               /* live works even if the photo 404'd */
                renderPreview();
            }
        }
    } catch (e) {}                             /* 503 while camera warms up, drops */
    /* pace to ~12fps net of the work just done; floor keeps the loop yielding */
    const spent = performance.now() - t0;
    liveTimer = setTimeout(liveTick, Math.max(20, 83 - spent));
}
window.addEventListener('pagehide', () => {
    if (fxSrc === 'live') navigator.sendBeacon('/live-close');
});
/* Press-and-hold the preview to peek at the unfiltered original. fxPeeking is
 * honoured inside renderPreview so the LIVE tab keeps streaming raw frames
 * while held, instead of the next frame stomping the peek. */
(() => {
    const pv = document.getElementById('fx-preview');
    if (!pv) return;
    // Hold-gated: the peek arms after a short beat, so a scroll flick passing
    // over the image never flashes the original — a real scroll either moves
    // >8px (cancels the timer) or fires pointercancel before it elapses.
    let t = 0, down = false, sx = 0, sy = 0;
    const peek = e => {
        if (!fxLoaded || !fxSample || !fxCtx) return;
        fxPeeking = true;
        pv.setPointerCapture && pv.setPointerCapture(e.pointerId);
        renderPreview();
    };
    const unpeek = () => {
        clearTimeout(t); down = false;
        if (!fxPeeking) return;
        fxPeeking = false; renderPreview();
    };
    pv.addEventListener('pointerdown', e => {
        down = true; sx = e.clientX; sy = e.clientY;
        clearTimeout(t);
        t = setTimeout(() => { if (down) peek(e); }, 130);
    });
    pv.addEventListener('pointermove', e => {
        if (!down || fxPeeking) return;
        if (Math.abs(e.clientX - sx) > 8 || Math.abs(e.clientY - sy) > 8)
            clearTimeout(t);                        // it's a scroll, not a hold
    });
    pv.addEventListener('pointerup', unpeek);
    pv.addEventListener('pointercancel', unpeek);   // scroll takes over → restore
    pv.addEventListener('contextmenu', e => e.preventDefault());
})();
function onEffectInput() {
    if (fxSlot === null) return;
    syncEffectLabels(); renderPreview();
    Object.assign(EFFECTS[fxSlot], fxValues());        // keep the mirror honest
    autoSave('effect', Object.assign({slot: fxSlot}, fxValues()));
}
// Draggable tone-curve control points (vertical only; X positions fixed).
// A touch only grabs a point when it STARTS within HIT px of one — otherwise the
// gesture is left alone so the page scrolls normally past the curve.
(function () {
    const cv = document.getElementById('fx-curve');
    if (!cv) return;
    let drag = -1, grabOff = 0;
    /* finger-shaped hit ellipse: X stays tight (the gaps between columns keep
       scrolling), Y is generous — misjudging a point's height is the common
       miss. grabOff makes touch drags relative (the point follows the finger's
       movement instead of teleporting to the touch position). */
    const HX = 30, HY = 60;
    const pointClient = i => {
        const r = cv.getBoundingClientRect();
        return { x: r.left + fxPX(FX_XS[i] / 255, r.width), y: r.top + fxPY(fxCurve[i] / 255, r.height) };
    };
    const nearest = (cx, cy, strict) => {
        let bi = -1, bd = 1e9;
        for (let i = 0; i < 5; i++) {
            const pt = pointClient(i);
            const nx = (cx - pt.x) / HX, ny = (cy - pt.y) / HY;
            const d = nx * nx + ny * ny;
            if (d < bd) { bd = d; bi = i; }
        }
        return (!strict || bd <= 1) ? bi : -1;
    };
    const setY = clientY => {
        const r = cv.getBoundingClientRect();
        const v = 1 - (clientY + grabOff - r.top - FX_PAD) / (r.height - 2 * FX_PAD);   // invert padded mapping
        fxCurve[drag] = clamp255(Math.round(v * 255));
        drawCurve(); renderPreview();
        if (fxSlot !== null) {            // curve drags persist like the sliders
            EFFECTS[fxSlot].curve = fxCurve.slice();
            autoSave('effect', Object.assign({slot: fxSlot}, fxValues()));
        }
    };
    // Desktop mouse: grab the nearest point on click (classic click-sets-Y).
    cv.addEventListener('mousedown', e => { drag = nearest(e.clientX, e.clientY, false); grabOff = 0; setY(e.clientY); e.preventDefault(); });
    window.addEventListener('mousemove', e => { if (drag >= 0) setY(e.clientY); });
    window.addEventListener('mouseup', () => { drag = -1; });
    // Touch: only intercept (and block scroll) when starting on a point.
    cv.addEventListener('touchstart', e => {
        const t = e.touches[0];
        drag = nearest(t.clientX, t.clientY, true);
        if (drag >= 0) { e.preventDefault(); grabOff = pointClient(drag).y - t.clientY; setY(t.clientY); }
    }, { passive: false });
    cv.addEventListener('touchmove', e => {
        if (drag < 0) return;      // not on a point → let the page scroll
        e.preventDefault();
        setY(e.touches[0].clientY);
    }, { passive: false });
    cv.addEventListener('touchend', () => { drag = -1; });
    cv.addEventListener('touchcancel', () => { drag = -1; });
})();

// ---- Slider touch feel ----
// Native range inputs on iOS only respond to grabbing the 16px thumb. Make
// every slider behave like a native iPhone slider: touching anywhere on the
// row jumps the value there and starts a captured drag. Values are stepped
// and dispatched as a normal 'input' event, so the existing oninput handlers
// (auto-save, live preview) fire unchanged. Vertical swipes still scroll the
// page (touch-action: pan-y cancels the pointer stream for them).
// Intent gating: a touch landing on a slider says nothing yet — it may be a
// scroll passing through. The value only moves once the gesture proves
// horizontal (>6px, more x than y), or on a clean tap (release with no
// movement). Vertical swipes fall through to pan-y scrolling untouched, so
// scrolling the page can never nudge a value.
document.querySelectorAll('input.set-slider').forEach(sl => {
    const setFromX = e => {
        const r = sl.getBoundingClientRect(), pad = 8;   // half thumb width
        const frac = Math.min(1, Math.max(0, (e.clientX - r.left - pad) / (r.width - 2 * pad)));
        const min = +sl.min, max = +sl.max, step = +sl.step || 1;
        const v = Math.round((min + frac * (max - min)) / step) * step;
        if (String(v) !== sl.value) {
            sl.value = v;
            sl.dispatchEvent(new Event('input', { bubbles: true }));
        }
    };
    let sx = 0, sy = 0, armed = false, pid = -1;
    sl.addEventListener('pointerdown', e => {
        if (e.pointerType === 'mouse' && e.button !== 0) return;
        pid = e.pointerId; sx = e.clientX; sy = e.clientY;
        armed = e.pointerType === 'mouse';   // a mouse can't be scrolling: jump now
        try { sl.setPointerCapture(pid); } catch (_) {}
        if (armed) { setFromX(e); e.preventDefault(); }
    });
    sl.addEventListener('pointermove', e => {
        if (e.pointerId !== pid) return;
        if (!armed) {
            const dx = Math.abs(e.clientX - sx), dy = Math.abs(e.clientY - sy);
            if (dx > 6 && dx > dy) armed = true;   // horizontal intent proven
            else return;                            // undecided/vertical: scroll wins
        }
        setFromX(e);
    });
    sl.addEventListener('pointerup', e => {
        if (e.pointerId !== pid) return;
        if (!armed && Math.abs(e.clientX - sx) < 6 && Math.abs(e.clientY - sy) < 6)
            setFromX(e);                            // clean tap still jumps
        armed = false; pid = -1;
    });
    sl.addEventListener('pointercancel', () => { armed = false; pid = -1; });
});

// ---- Default options (WB / filter) ----
// String compare so it works for both integer picks (WB/filter) and named
// picks (the calibration image group).
function setOptActive(groupId, val) {
    document.querySelectorAll('#' + groupId + ' button')
        .forEach(b => b.classList.toggle('active', b.dataset.val === String(val)));
}
function defaultsValues() { return {wb: defWb, filter: defFilter, shutter: defShutter, metering: defMetering, dgain: defDgain}; }
function pickWb(i) { defWb = i; setOptActive('wb-group', i); autoSave('defaults', defaultsValues()); }
function pickFilter(i) { defFilter = i; setOptActive('filter-group-def', i); autoSave('defaults', defaultsValues()); }
function pickShutter(d) { defShutter = d; setOptActive('shutter-group', d); autoSave('defaults', defaultsValues()); }
function pickMetering(i) { defMetering = i; setOptActive('metering-group', i); autoSave('defaults', defaultsValues()); }
function pickDgain(i) { defDgain = i; setOptActive('dgain-group', i); autoSave('defaults', defaultsValues()); }
function resetDefaults() {
    const btn = document.getElementById('def-reset');
    flashPulse(btn);
    postJSON('/defaults-reset', {}).then(r => { if (!r.ok) throw 0; return r.json(); }).then(d => {
        // Set state directly — pickWb/pickFilter would queue another save.
        defWb = d.wb; defFilter = d.filter;
        setOptActive('wb-group', d.wb); setOptActive('filter-group-def', d.filter);
        delete savePending['defaults'];
        setSaveState('defaults', null);
    }).catch(() => flashError(btn));
}
function resetAeLimits() {
    const btn = document.getElementById('ae-reset');
    flashPulse(btn);
    postJSON('/aelimits-reset', {}).then(r => { if (!r.ok) throw 0; return r.json(); }).then(d => {
        defShutter = d.shutter; defMetering = d.metering; defDgain = d.dgain;
        setOptActive('shutter-group', d.shutter); setOptActive('metering-group', d.metering);
        setOptActive('dgain-group', d.dgain);
        delete savePending['defaults'];
        setSaveState('defaults', null);
    }).catch(() => flashError(btn));
}

// ---- Format data ----
function confirmFormat() {
    askConfirm('Delete all photos?<br>This cannot be undone.', 'DELETE ALL')
        .then(ok => { if (ok) doFormat(); });
}
function doFormat() {
    const btn = document.getElementById('format-btn');
    btn.disabled = true;
    const bar = document.getElementById('format-progress');
    const fill = document.getElementById('format-progress-fill');
    bar.classList.add('active');
    fill.style.width = '0';
    const token = Date.now().toString(36) + Math.random().toString(36).slice(2);
    postJSON('/format-data', {token: token});
    const poll = setInterval(async () => {
        let d;
        try { d = await (await getJSON('/format-progress?token=' + token)).json(); }
        catch (e) { return; }
        const frac = d.total > 0 ? d.done / d.total : (d.active ? 0 : 1);
        fill.style.width = (Math.max(0, Math.min(1, frac)) * 100) + '%';
        if (!d.active) {
            clearInterval(poll);
            fill.style.width = '100%';
            setTimeout(() => location.reload(), 500);
        }
    }, 250);
}
</script>
</body>
</html>"""


def get_free_space():
    try:
        path = PHOTOS_DIR if os.path.exists(PHOTOS_DIR) else "/home/dkumkum"
        stat = os.statvfs(path)
        free = stat.f_bavail * stat.f_bsize
        if free >= 1024 ** 3:
            return f"{free / 1024**3:.1f} GB"
        return f"{free / 1024**2:.0f} MB"
    except:
        return "?"


def _statvfs_info(path):
    """total/free/used bytes for the filesystem holding `path`, or None."""
    try:
        s = os.statvfs(path)
        bs = s.f_frsize or s.f_bsize
        total = s.f_blocks * bs
        free = s.f_bavail * bs                 # available to unprivileged users
        used = total - s.f_bfree * bs
        return {"total": total, "free": free, "used": used}
    except OSError:
        return None


def _firmware_version():
    try:
        with open("/etc/optocam-version") as f:
            v = f.read().strip()
        return v or FIRMWARE_VERSION_FALLBACK
    except OSError:
        return FIRMWARE_VERSION_FALLBACK


def _volumes():
    """SD-card partitions from /proc/mounts, with per-mount usage."""
    seen, vols = set(), []
    labels = {"/boot": "Boot", "/data": "Photos", "/": "Firmware"}
    try:
        with open("/proc/mounts") as f:
            for line in f:
                p = line.split()
                if len(p) < 3:
                    continue
                dev, mount, fs = p[0], p[1], p[2]
                if not (dev.startswith("/dev/mmcblk") or dev == "/dev/root"):
                    continue
                if mount in seen:
                    continue
                seen.add(mount)
                info = _statvfs_info(mount) or {}
                vols.append({
                    "label": labels.get(mount, mount),
                    "dev": dev, "mount": mount, "fs": fs,
                    "total": info.get("total", 0), "used": info.get("used", 0),
                    "ro": (",ro," in ("," + p[3] + ",")) if len(p) > 3 else False,
                })
    except OSError:
        pass
    # order Photos, Firmware, Boot, then anything else
    order = {"/data": 0, "/": 1, "/boot": 2}
    vols.sort(key=lambda v: order.get(v["mount"], 9))
    return vols


@app.route("/")
def index():
    # All media, newest-first. Filtering and ordering happen client-side so
    # changing a selector never reloads the page.
    files = list_media()
    gif_set = [f for f in files if f.lower().endswith(".gif")]
    sizes = {}
    # Per-file cache-bust key (size-mtime): media URLs are cached hard
    # ("immutable" — filenames never change within one camera lifetime), but a
    # re-flash or FORMAT DATA restarts the numbering, and a returning browser
    # would then show the OLD lifetime's cached media under the same URL.
    # Versioned URLs make each file instance unique; same idiom as the
    # calibration assets' ?v=.
    vers = {}
    for f in files:
        try:
            st = os.stat(os.path.join(PHOTOS_DIR, f))
            sizes[f] = st.st_size
            vers[f] = "%d-%d" % (st.st_size, int(st.st_mtime))
        except OSError:
            sizes[f] = 0
            vers[f] = "0"
    cal = read_calib_ui()
    gif_frames, gif_iv = read_gifcfg()
    filt, wb = read_defaults()
    ae_den, ae_met, ae_dg = read_ae_limits()
    effects = read_effects()
    # Defaults list = presets + existing customs; a stale saved index (custom
    # deleted outside this UI) falls back to the default filter.
    filter_options = FILTER_OPTIONS + [e["name"] for e in effects if e is not None]
    if not 0 <= filt < len(filter_options):
        filt = FILTER_DEFAULT
    settings = {
        "cal": cal,
        "gif_frames": gif_frames, "gif_interval": gif_iv,
        "filter": filt, "wb": wb,
        "filter_options": filter_options, "wb_options": WB_OPTIONS,
        "shutter": ae_den, "metering": ae_met, "dgain": ae_dg,
        "shutter_options": SHUTTER_OPTIONS, "metering_options": METERING_OPTIONS,
        "dgain_options": DGAIN_OPTIONS,
        "effect": effect_defaults(),          # editor loads a slot client-side
    }
    settings_defaults = {
        "cal": calib_defaults(),
        "frames": GIF_FRAMES_DEFAULT, "interval": GIF_INTERVAL_DEFAULT,
        "wb": WB_DEFAULT, "filter": FILTER_DEFAULT,
        "shutter": SHUTTER_DEFAULT, "metering": METERING_DEFAULT,
        "dgain": DGAIN_DEFAULT,
    }
    html = render_template_string(
        HTML, files=files, count=len(files), has_media=bool(files),
        free_space=get_free_space(),
        files_json=json.dumps(files), gifs_json=json.dumps(gif_set),
        sizes_json=json.dumps(sizes), vers=vers, vers_json=json.dumps(vers),
        settings=settings, settings_defaults_json=json.dumps(settings_defaults),
        cal_def=settings_defaults["cal"],
        effects_json=json.dumps(effects), preset_count=len(FILTER_OPTIONS),
        calib_patterns=list(CALIB_PATTERNS), calib_asset_v=CALIB_ASSET_V,
        calib_keys=CALIB_KEYS,
        max_effect_slots=MAX_EFFECT_SLOTS, effect_name_max=EFFECT_NAME_MAX,
        RESET_ICON=RESET_ICON, ZOOM_ICONS=ZOOM_ICONS, theme=read_theme()
    )
    # The page embeds all CSS/JS inline, so never let the browser serve a stale
    # copy — otherwise UI changes appear not to take effect after a redeploy.
    # (Thumbnails/GIFs keep their own immutable caching, so this costs nothing.)
    resp = Response(html)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/logo")
def logo():
    return send_from_directory(HOME_DIR, "optocamlogo.svg", mimetype="image/svg+xml")


@app.route("/font/<filename>")
def font(filename):
    return send_from_directory(HOME_DIR, filename)


@app.route("/photo/<filename>")
def photo(filename):
    return send_from_directory(PHOTOS_DIR, filename, as_attachment=True)


THUMB_DIR = os.path.join(PHOTOS_DIR, ".thumbs")

def get_thumb_path(filename, size):
    os.makedirs(THUMB_DIR, exist_ok=True)
    return os.path.join(THUMB_DIR, f"{filename}_{size}.jpg")

def _gif_complete(path):
    """A fully-written GIF ends with the trailer byte 0x3B. The camera encodes
    GIFs in place at their final name, so a request can briefly land mid-write;
    this rejects those partial files instead of serving truncated bytes."""
    try:
        if os.path.getsize(path) < 1000:
            return False
        with open(path, "rb") as f:
            f.seek(-1, os.SEEK_END)
            return f.read(1) == b"\x3b"
    except OSError:
        return False

def _retryable_error(msg, status=503):
    resp = Response(msg, status=status, mimetype="text/plain")
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Retry-After"] = "1"
    return resp

@app.route("/thumb/<filename>")
def thumb(filename):
    # Static preview for the grid. For GIFs this is a JPEG of the first frame —
    # the full animation is served separately by /gif so the heavy multi-frame
    # file isn't pulled for every grid cell at once (the cause of dropped
    # transfers / broken "?" previews under load).
    path = os.path.join(PHOTOS_DIR, filename)
    if not os.path.exists(path):
        return "Not found", 404
    is_gif = filename.lower().endswith(".gif")
    if is_gif and not _gif_complete(path):
        return _retryable_error("GIF not ready")
    size = min(request.args.get('size', 400, type=int), 1200)
    cache_path = get_thumb_path(filename, size)
    if not os.path.exists(cache_path) or os.path.getsize(cache_path) == 0:
        tmp = None
        try:
            from PIL import Image
            img = Image.open(path)
            if is_gif:
                img.seek(0)             # poster = first frame
            img = img.convert("RGB")    # GIFs are mode "P"; JPEG needs RGB
            img.thumbnail((size, size))
            tmp = tempfile.NamedTemporaryFile(dir=THUMB_DIR, delete=False, suffix='.tmp')
            img.save(tmp, "JPEG", quality=75)
            tmp.close()
            os.chmod(tmp.name, 0o644)
            os.replace(tmp.name, cache_path)
        except:
            if tmp is not None:
                try:
                    tmp.close()
                    os.unlink(tmp.name)
                except:
                    pass
            return _retryable_error("Thumbnail not ready")
    with open(cache_path, "rb") as f:
        resp = Response(f.read(), mimetype="image/jpeg")
        resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp


@app.route("/gif/<filename>")
def gif_full(filename):
    """Full animated GIF, served inline. Used by the viewer and by the grid's
    background loader to swap a static poster for the live animation. Filenames
    are unique and never rewritten once complete, so it caches hard."""
    if not filename.lower().endswith(".gif"):
        return "Not found", 404
    path = os.path.join(PHOTOS_DIR, filename)
    if not os.path.exists(path):
        return "Not found", 404
    if not _gif_complete(path):
        resp = Response("GIF not ready", status=503, mimetype="text/plain")
        resp.headers["Cache-Control"] = "no-store"
        resp.headers["Retry-After"] = "1"
        return resp
    resp = send_from_directory(PHOTOS_DIR, filename, mimetype="image/gif")
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


@app.route("/preload")
def preload():
    import threading
    from PIL import Image as PILImage
    def generate_all():
        if not os.path.exists(PHOTOS_DIR):
            return
        # Only photos get pre-generated thumbnails; GIFs are served as-is.
        files = [f for f in list_media() if f.lower().endswith(".jpg")][:10]
        for filename in files:
            cache_path = get_thumb_path(filename, 1200)
            if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
                continue
            try:
                img = PILImage.open(os.path.join(PHOTOS_DIR, filename))
                img.thumbnail((1200, 1200))
                tmp = tempfile.NamedTemporaryFile(dir=THUMB_DIR, delete=False, suffix='.tmp')
                img.save(tmp, "JPEG", quality=75)
                tmp.close()
                os.chmod(tmp.name, 0o644)
                os.replace(tmp.name, cache_path)
            except:
                pass
            time.sleep(0.2)  # keep server responsive between generations
    threading.Thread(target=generate_all, daemon=True).start()
    return "", 204


@app.route("/preload-ahead", methods=["POST"])
def preload_ahead():
    import threading
    from PIL import Image as PILImage
    data = request.get_json()
    filenames = [f for f in data.get("files", []) if not f.lower().endswith(".gif")]
    def generate():
        for filename in filenames:
            cache_path = get_thumb_path(filename, 1200)
            if os.path.exists(cache_path) and os.path.getsize(cache_path) > 0:
                continue
            try:
                path = os.path.join(PHOTOS_DIR, filename)
                if not os.path.exists(path):
                    continue
                img = PILImage.open(path)
                img.thumbnail((1200, 1200))
                tmp = tempfile.NamedTemporaryFile(dir=THUMB_DIR, delete=False, suffix='.tmp')
                img.save(tmp, "JPEG", quality=75)
                tmp.close()
                os.chmod(tmp.name, 0o644)
                os.replace(tmp.name, cache_path)
            except:
                pass
    threading.Thread(target=generate, daemon=True).start()
    return "", 204


@app.route("/delete", methods=["POST"])
def delete_photos():
    data = request.get_json()
    filenames = data.get("files", [])
    for f in filenames:
        path = os.path.join(PHOTOS_DIR, os.path.basename(f))
        if os.path.exists(path):
            os.remove(path)
        for size in [400, 1200]:
            cache = get_thumb_path(os.path.basename(f), size)
            if os.path.exists(cache):
                os.remove(cache)
    return "", 204


@app.route("/download-zip", methods=["POST"])
def download_zip():
    filenames = request.form.getlist("files")
    token = request.form.get("download_token", "")
    paths = []
    for f in filenames:
        base = os.path.basename(f)
        p = os.path.join(PHOTOS_DIR, base)
        if os.path.exists(p):
            paths.append((p, base))

    # Approximate the final ZIP_STORED size as the progress denominator.
    total = 22
    for p, arcname in paths:
        total += 92 + 2 * len(arcname.encode("utf-8")) + os.path.getsize(p)

    if token:
        now = time.time()
        for k, v in list(DOWNLOAD_PROGRESS.items()):   # prune old, finished entries
            if not v.get("active") and now - v["ts"] > 60:
                DOWNLOAD_PROGRESS.pop(k, None)
        DOWNLOAD_PROGRESS[token] = {"sent": 0, "total": total, "ts": now, "active": True}

    class _Stream:
        """Write-only sink. With no seek/tell, zipfile streams using data
        descriptors, so we never buffer more than one file at a time."""
        def __init__(self):
            self.buf = bytearray()
        def write(self, data):
            self.buf.extend(data)
            return len(data)
        def flush(self):
            pass
        def drain(self):
            data = bytes(self.buf)
            del self.buf[:]
            return data

    def generate():
        sink = _Stream()
        sent = 0
        completed = False
        try:
            with zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED) as zf:
                for p, arcname in paths:
                    zinfo = zipfile.ZipInfo.from_file(p, arcname)
                    zinfo.compress_type = zipfile.ZIP_STORED
                    # Copy each file in small chunks so progress updates continuously
                    # (not in one big jump per file).
                    with zf.open(zinfo, "w") as dest, open(p, "rb") as src:
                        while True:
                            buf = src.read(262144)
                            if not buf:
                                break
                            dest.write(buf)
                            chunk = sink.drain()
                            if chunk:
                                yield chunk
                                sent += len(chunk)
                                if token:
                                    DOWNLOAD_PROGRESS[token] = {"sent": sent, "total": total, "ts": time.time(), "active": True}
                    chunk = sink.drain()       # data descriptor written when the entry closes
                    if chunk:
                        yield chunk
                        sent += len(chunk)
                        if token:
                            DOWNLOAD_PROGRESS[token] = {"sent": sent, "total": total, "ts": time.time(), "active": True}
            tail = sink.drain()                # central directory, written on close
            if tail:
                yield tail
                sent += len(tail)
            completed = True
        finally:
            # Always mark the stream ended (completed or aborted) so the client
            # can stop waiting. A completed download reports the full size.
            if token:
                DOWNLOAD_PROGRESS[token] = {
                    "sent": total if completed else sent,
                    "total": total,
                    "ts": time.time(),
                    "active": False,
                }

    return Response(
        generate(),
        mimetype="application/zip",
        headers={"Content-Disposition": "attachment; filename=optocam_photos.zip"}
    )


@app.route("/download-progress")
def download_progress():
    p = DOWNLOAD_PROGRESS.get(request.args.get("token", ""))
    if not p:
        return Response('{"sent":0,"total":0,"active":false}', mimetype="application/json")
    return Response(
        json.dumps({"sent": p["sent"], "total": p["total"], "active": p.get("active", False)}),
        mimetype="application/json")


# ════════════════════════ Settings routes ════════════════════════

@app.route("/calib-image")
def calib_image():
    """The screen-calibration reference pattern shown on the phone/PC so the
    user can compare it against the same image rendered on the camera LCD.
    ?img=color|gray|photo selects which pattern."""
    img = request.args.get("img", "color")
    if img not in CALIB_PATTERNS:
        img = "color"
    resp = send_from_directory(SHARE_DIR, _calib_asset_file(img))  # ?v= busts the day-long cache
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


def _write_calib_live(p):
    _atomic_write(CALIB_LIVE, calib_line(p))


@app.route("/calib-open", methods=["POST"])
def calib_open():
    """Enter live calibration: show the pattern on the LCD at the current
    (saved) white point. The firmware watches /data/.calib_active."""
    _write_calib_live(calib_params(request.get_json(silent=True) or {}))
    _atomic_write(CALIB_ACTIVE, "1")
    return "", 204


@app.route("/calib-live", methods=["POST"])
def calib_live():
    _write_calib_live(calib_params(request.get_json(silent=True) or {}))
    _atomic_write(CALIB_ACTIVE, "1")   # keep it active even if /open was missed
    return "", 204


@app.route("/calib-img", methods=["POST"])
def calib_img():
    """Switch the reference pattern shown on the LCD (and the phone image)."""
    d = request.get_json(silent=True) or {}
    img = d.get("img", "color")
    if img not in CALIB_PATTERNS:
        img = "color"
    _atomic_write(CALIB_IMG, img + "\n")
    _atomic_write(CALIB_ACTIVE, "1")
    return "", 204


@app.route("/calib-img-get")
def calib_img_get():
    """Current reference pattern — lets the web selector follow the camera's
    joystick, which writes the same /data/.calib_img file."""
    img = "color"
    try:
        with open(CALIB_IMG) as f:
            v = f.read().split()[0]
        if v in CALIB_PATTERNS:
            img = v
    except Exception:
        pass
    return Response(json.dumps({"img": img}), mimetype="application/json")


@app.route("/calib-save", methods=["POST"])
def calib_save():
    """Persist the profile: the firmware reads /data/.dispcal at boot, so this
    becomes the default white point on every power-up."""
    p = calib_params(request.get_json(silent=True) or {})
    _atomic_write(DISPCAL, calib_line(p))
    _atomic_write(CALIB_UI, " ".join(str(p[k]) for k in CALIB_KEYS) + "\n")
    _write_calib_live(p)
    return "", 204


@app.route("/calib-reset", methods=["POST"])
def calib_reset():
    """Discard the saved profile — the firmware falls back to the baked
    per-batch default. Push that default to the live LCD too."""
    _rm(DISPCAL)
    _rm(CALIB_UI)
    d = calib_defaults()
    _write_calib_live(d)               # baked panel white point
    return json.dumps(d), 200, {"Content-Type": "application/json"}


@app.route("/calib-close", methods=["POST"])
def calib_close():
    _rm(CALIB_ACTIVE)
    _rm(CALIB_LIVE)
    _rm(CALIB_IMG)
    return "", 204


@app.route("/calib-ping", methods=["POST"])
def calib_ping():
    """Heartbeat: refresh the active flag's mtime so the firmware keeps showing
    the calibration pattern. Without it the LCD reverts after ~8s (recovers from
    a dropped phone)."""
    if os.path.exists(CALIB_ACTIVE):
        _atomic_write(CALIB_ACTIVE, "1")
    return "", 204


# ---- Filter Maker live view ----
@app.route("/live-open", methods=["POST"])
def live_open():
    _atomic_write(LIVE_ACTIVE, "1")
    return "", 204


@app.route("/live-ping", methods=["POST"])
def live_ping():
    """Heartbeat: keep the flag's mtime fresh so the firmware keeps the camera
    running. Goes stale ~5s after the phone drops without a clean /live-close."""
    _atomic_write(LIVE_ACTIVE, "1")
    return "", 204


@app.route("/live-close", methods=["POST"])
def live_close():
    _rm(LIVE_ACTIVE)
    return "", 204


_live_sim = {"t": 0.0, "jpg": b"", "src": None}
def _sim_live_frame():
    """Laptop-dev stand-in for the camera: a 640px crop drifting over the photo
    reference plus a moving dot, so the live pipeline can be built and profiled
    without hardware. Lazy PIL import — the device server never needs it."""
    import math
    from PIL import Image, ImageDraw
    now = time.time()
    if now - _live_sim["t"] < 0.08 and _live_sim["jpg"]:
        return _live_sim["jpg"]
    if _live_sim["src"] is None:
        try:
            _live_sim["src"] = Image.open(os.path.join(SHARE_DIR, "calib_photo.jpg")).convert("RGB")
        except OSError:
            _live_sim["src"] = Image.new("RGB", (1080, 1080), (120, 120, 120))
    src = _live_sim["src"]
    m = max(1, min(src.width, src.height) - 640)
    x = int((math.sin(now * 0.35) * 0.5 + 0.5) * m)
    y = int((math.cos(now * 0.27) * 0.5 + 0.5) * m)
    im = src.crop((x, y, x + 640, y + 640))
    d = ImageDraw.Draw(im)
    cx = 320 + int(math.cos(now * 2.0) * 260)
    cy = 320 + int(math.sin(now * 2.0) * 260)
    d.ellipse((cx - 10, cy - 10, cx + 10, cy + 10), fill=(255, 60, 60))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    _live_sim.update(t=now, jpg=buf.getvalue())
    return _live_sim["jpg"]


@app.route("/live-frame")
def live_frame():
    """Newest camera frame (unfiltered, WB applied). 503 while the camera is
    still spinning up — the client just tries again."""
    if LIVE_SIM:
        data = _sim_live_frame()
    else:
        try:
            with open(LIVE_FRAME, "rb") as f:
                data = f.read()
        except OSError:
            return "", 503
    resp = Response(data, mimetype="image/jpeg")
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/gif-save", methods=["POST"])
def gif_save():
    d = request.get_json(silent=True) or {}
    frames = _clamp(int(d.get("frames", GIF_FRAMES_DEFAULT)), 2, 60)
    interval = _clamp(float(d.get("interval", GIF_INTERVAL_DEFAULT)), 0.1, 5.0)
    _atomic_write(GIFCFG, "%d %.2f\n" % (frames, interval))
    return "", 204


@app.route("/gif-reset", methods=["POST"])
def gif_reset():
    _rm(GIFCFG)
    return json.dumps({"frames": GIF_FRAMES_DEFAULT, "interval": GIF_INTERVAL_DEFAULT}), \
        200, {"Content-Type": "application/json"}


@app.route("/defaults-save", methods=["POST"])
def defaults_save():
    d = request.get_json(silent=True) or {}
    n_customs = sum(1 for e in read_effects() if e is not None)
    filt = _clamp(int(d.get("filter", FILTER_DEFAULT)), 0,
                  len(FILTER_OPTIONS) + n_customs - 1)
    wb = _clamp(int(d.get("wb", WB_DEFAULT)), 0, len(WB_OPTIONS) - 1)
    _atomic_write(DEFAULTS, "%d %d\n" % (filt, wb))   # order: FILTER WB (C++ fscanf)
    den = int(d.get("shutter", SHUTTER_DEFAULT)); met = int(d.get("metering", METERING_DEFAULT))
    dgn = int(d.get("dgain", DGAIN_DEFAULT))
    if den not in SHUTTER_OPTIONS: den = SHUTTER_DEFAULT
    if not 0 <= met < len(METERING_OPTIONS): met = METERING_DEFAULT
    if dgn not in (0, 1): dgn = DGAIN_DEFAULT
    if den == SHUTTER_DEFAULT and met == METERING_DEFAULT and dgn == DGAIN_DEFAULT:
        _rm(AE_LIMITS)                                 # factory — no file
    else:
        _atomic_write(AE_LIMITS, "%d %d %d\n" % (den, met, dgn))
    return "", 204


@app.route("/defaults-reset", methods=["POST"])
def defaults_reset():
    _rm(DEFAULTS)
    return json.dumps({"wb": WB_DEFAULT, "filter": FILTER_DEFAULT}), \
        200, {"Content-Type": "application/json"}


@app.route("/aelimits-reset", methods=["POST"])
def aelimits_reset():
    _rm(AE_LIMITS)
    return json.dumps({"shutter": SHUTTER_DEFAULT, "metering": METERING_DEFAULT,
                       "dgain": DGAIN_DEFAULT}), \
        200, {"Content-Type": "application/json"}


def _valid_slot(d):
    try:
        s = int(d.get("slot"))
    except (TypeError, ValueError):
        return None
    return s if 0 <= s < MAX_EFFECT_SLOTS else None


def _write_effect_slot(slot, p, name):
    _atomic_write(effect_slot_path(slot), effect_line(p) + name + "\n")


@app.route("/effects")
def effects_list():
    return Response(json.dumps(read_effects()), mimetype="application/json")


@app.route("/effect-create", methods=["POST"])
def effect_create():
    effects = read_effects()
    free = [i for i, e in enumerate(effects) if e is None]
    if not free:
        return json.dumps({"error": "all slots used"}), 409, \
            {"Content-Type": "application/json"}
    slot = free[0]
    d = effect_defaults()
    d["name"] = sanitize_effect_name("", slot)
    _write_effect_slot(slot, d, d["name"])
    return Response(json.dumps({"slot": slot, "effect": d}),
                    mimetype="application/json")


@app.route("/effect-save", methods=["POST"])
def effect_save():
    """Update a slot's params and/or name. The slot must already exist —
    creation is explicit (/effect-create), never a side effect of an edit."""
    d = request.get_json(silent=True) or {}
    slot = _valid_slot(d)
    if slot is None:
        return "Bad slot", 400
    cur = read_effects()[slot]
    if cur is None:
        return "No such filter", 404
    name = cur["name"]
    if any(k in d for k in EFFECT_FIELDS):
        merged = dict(cur)
        merged.update({k: d[k] for k in EFFECT_FIELDS if k in d})
        cur = effect_params(merged)
    if "name" in d:
        name = sanitize_effect_name(d["name"], slot)
    cur["name"] = name
    _write_effect_slot(slot, cur, cur["name"])
    return "", 204


@app.route("/effect-delete", methods=["POST"])
def effect_delete():
    """Remove a slot. If the startup-default filter pointed at a custom, remap
    it — camera indices above the deleted slot all shift down by one."""
    d = request.get_json(silent=True) or {}
    slot = _valid_slot(d)
    if slot is None:
        return "Bad slot", 400
    before = read_effects()
    if before[slot] is None:
        return "No such filter", 404
    old_idx = effect_camera_index(before, slot)
    _rm(effect_slot_path(slot))
    filt, wb = read_defaults()
    if filt == old_idx:
        filt = FILTER_DEFAULT                       # its target is gone
    elif filt > old_idx:
        filt -= 1                                   # later customs shifted down
    _atomic_write(DEFAULTS, "%d %d\n" % (filt, wb))
    return Response(json.dumps({"filter": filt}), mimetype="application/json")


@app.route("/theme-save", methods=["POST"])
def theme_save():
    d = request.get_json(silent=True) or {}
    t = d.get("theme", "dark")
    if t not in THEMES:
        t = "dark"
    _atomic_write(THEME, t + "\n")
    return "", 204


@app.route("/format-data", methods=["POST"])
def format_data():
    """Delete every photo/GIF (and its cached thumbnails) in the background,
    reporting progress by token so the panel can animate a bar."""
    import threading
    d = request.get_json(silent=True) or {}
    token = str(d.get("token", "")) or (str(time.time()))

    def run():
        files = list_media()
        total = len(files)
        FORMAT_PROGRESS[token] = {"done": 0, "total": total, "active": True}
        for i, f in enumerate(files):
            base = os.path.basename(f)
            _rm(os.path.join(PHOTOS_DIR, base))
            for size in (400, 1200):
                _rm(get_thumb_path(base, size))
            FORMAT_PROGRESS[token] = {"done": i + 1, "total": total, "active": True}
            time.sleep(0.01)          # keep the server responsive + bar visible
        # sweep any stragglers left in the thumb cache
        try:
            for t in os.listdir(THUMB_DIR):
                _rm(os.path.join(THUMB_DIR, t))
        except OSError:
            pass
        FORMAT_PROGRESS[token] = {"done": total, "total": total, "active": False}
        # prune old finished tokens
        now = time.time()
        for k in [k for k, v in FORMAT_PROGRESS.items()
                  if not v.get("active") and k != token]:
            FORMAT_PROGRESS.pop(k, None)

    threading.Thread(target=run, daemon=True).start()
    return "", 204


@app.route("/format-progress")
def format_progress():
    p = FORMAT_PROGRESS.get(request.args.get("token", ""))
    if not p:
        return Response('{"done":0,"total":0,"active":false}', mimetype="application/json")
    return Response(json.dumps(p), mimetype="application/json")


@app.route("/device-info")
def device_info():
    """Live storage + firmware + license info for the Data & Device panel."""
    photos = _statvfs_info(PHOTOS_DIR if os.path.exists(PHOTOS_DIR) else "/") or \
        {"total": 0, "free": 0, "used": 0}
    # media tallies, split by kind
    photo_count = photo_bytes = gif_count = gif_bytes = 0
    for f in list_media():
        try:
            size = os.path.getsize(os.path.join(PHOTOS_DIR, f))
        except OSError:
            continue
        if f.lower().endswith(".gif"):
            gif_count += 1; gif_bytes += size
        else:
            photo_count += 1; photo_bytes += size
    # whole SD card: the block device when we're on it, else the partitions
    vols = _volumes()
    try:
        with open("/sys/block/mmcblk0/size") as f:
            disk_total = int(f.read()) * 512
    except (OSError, ValueError):
        disk_total = sum(v["total"] for v in vols) or photos["total"]
    # "system files" = everything occupied that isn't the user's media
    used = sum(v["used"] for v in vols) or photos["used"]
    system_bytes = max(0, used - photo_bytes - gif_bytes)
    info = {
        "storage": {
            "disk_total": disk_total, "system": system_bytes, "free": photos["free"],
            "photo_count": photo_count, "photo_bytes": photo_bytes,
            "gif_count": gif_count, "gif_bytes": gif_bytes,
        },
        "version": _firmware_version(),
        "license": LICENSE_NAME, "license_url": LICENSE_URL,
        "copyright": COPYRIGHT, "github": GITHUB_URL,
    }
    return Response(json.dumps(info), mimetype="application/json")


if __name__ == "__main__":
    migrate_legacy_effect()      # pre-slot /data/.effect -> slot 0
    app.run(host="0.0.0.0", port=80, debug=False, threaded=True)
