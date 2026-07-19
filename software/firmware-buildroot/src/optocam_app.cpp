/* optocam_app.cpp — OptoCam Buildroot Stage-2 app, step 1.
 * Everything optocam_preview.cpp does (5s boot preview), plus:
 *   - buttons via gpiochip ioctls (capture GPIO21, preview GPIO20,
 *     joystick L5/R26/PRESS13/U6/D19 — all pull-up, active-low)
 *   - shutter: switch to 2592x2592 still, JPEG q92 (libjpeg-turbo),
 *     rotated 90° to match the display, saved to /data/photos/
 *     Optocamzero_N.jpg with fsync (power-cut costs at most this file)
 *   - white shutter-flash on the display for feedback
 * Boot-critical path is unchanged: display first, cache-warm, entity wait.
 */
#include <cstdio>
#include <cstdlib>
#include <cstdint>
#include <cstring>
#include <unistd.h>
#include <fcntl.h>
#include <dirent.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/mount.h>
#include <sys/stat.h>
#include <map>
#include <vector>
#include <memory>
#include <atomic>
#include <mutex>
#include <chrono>
#include <condition_variable>
#include <thread>
#include <linux/spi/spidev.h>
#include <linux/gpio.h>
#include <linux/media.h>
#include <libcamera/libcamera.h>
#include <jpeglib.h>
#include <setjmp.h>
#include <csignal>
#include "optocam_hud.h"
#include "optocam_gif.h"

using namespace libcamera;

/* ---------- ST7789 display (validated in Stage-1) ---------- */
static bool g_madctl_legacy = false;        /* A/B: original 0x70 panel mode */
static int spifd, rst_fd, dc_fd, bl_fd;
static int gpio_out(int chipfd, unsigned off, int val) {
    struct gpiohandle_request r; memset(&r, 0, sizeof r);
    r.lineoffsets[0] = off; r.lines = 1; r.flags = GPIOHANDLE_REQUEST_OUTPUT;
    r.default_values[0] = val; strcpy(r.consumer_label, "optocam");
    if (ioctl(chipfd, GPIO_GET_LINEHANDLE_IOCTL, &r) < 0) { perror("linehandle"); return -1; }
    return r.fd;
}
static void gpio_set(int fd, int v) { struct gpiohandle_data d; d.values[0] = v; ioctl(fd, GPIOHANDLE_SET_LINE_VALUES_IOCTL, &d); }
/* Memory-mapped fast path for the backlight pin (BCM 24): at the dim duty the
 * PWM pulse is 63µs, and the gpiochip ioctl's kernel-crossing latency under
 * IRQ load lands on the pulse edges as faint brightness wander. A register
 * store makes the edges nanosecond-exact regardless of load. The line stays
 * requested as output via gpiochip; both paths reach the same pad. */
static volatile uint32_t *g_gpioreg = nullptr;
static void bl_fast_init(void) {
    int fd = open("/dev/gpiomem", O_RDWR | O_SYNC);
    if (fd < 0) return;                     /* no gpiomem: ioctl fallback */
    void *m = mmap(nullptr, 4096, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
    close(fd);
    if (m != MAP_FAILED) g_gpioreg = (volatile uint32_t *)m;
}
static inline void bl_set(int v) {
    if (g_gpioreg) g_gpioreg[v ? 7 : 10] = 1u << 24;   /* GPSET0 / GPCLR0 */
    else gpio_set(bl_fd, v);
}
static void spi_write(const uint8_t *b, size_t len) {
    size_t off = 0;
    while (off < len) { size_t n = len - off; if (n > 65536) n = 65536;
        ssize_t w = write(spifd, b + off, n); if (w <= 0) { perror("spi"); return; } off += (size_t)w; }
}
static void dcmd(uint8_t c) { gpio_set(dc_fd, 0); spi_write(&c, 1); }
static void ddata(const uint8_t *d, size_t n) { gpio_set(dc_fd, 1); spi_write(d, n); }

static void display_init(bool soft) {
    /* soft: the early-splash binary already initialised the panel and is
     * holding the GPIO lines — kill it, re-grab the lines WITHOUT resetting
     * the panel (its RAM keeps showing the splash), and just take over. */
    if (soft) { system("killall optocam_splash 2>/dev/null"); usleep(50000); }
    /* at boot we race spi-bcm2835's async probe — retry until the nodes exist
     * (the "backlight on, nothing else" boot failure) */
    int chip = -1;
    for (int t = 0; t < 500 && (chip = open("/dev/gpiochip0", O_RDONLY)) < 0; t++) usleep(20000);
    rst_fd = gpio_out(chip, 27, 1); dc_fd = gpio_out(chip, 25, 0);
    bl_fd = gpio_out(chip, 24, soft ? 1 : 0);          /* keep backlight lit on takeover */
    close(chip);
    spifd = -1;
    for (int t = 0; t < 500 && (spifd = open("/dev/spidev0.0", O_RDWR)) < 0; t++) usleep(20000);
    if (spifd < 0) printf("optocam: SPIDEV OPEN FAILED\n");
    uint8_t mode = SPI_MODE_0, bits = 8; uint32_t sp = 100000000;
    ioctl(spifd, SPI_IOC_WR_MODE, &mode); ioctl(spifd, SPI_IOC_WR_BITS_PER_WORD, &bits);
    ioctl(spifd, SPI_IOC_WR_MAX_SPEED_HZ, &sp);
    uint8_t mode2 = SPI_MODE_0, bits2 = 8; uint32_t sp2 = 100000000;
    ioctl(spifd, SPI_IOC_WR_MODE, &mode2); ioctl(spifd, SPI_IOC_WR_BITS_PER_WORD, &bits2);
    ioctl(spifd, SPI_IOC_WR_MAX_SPEED_HZ, &sp2);
    if (soft) return;                                  /* panel already showing splash */
    gpio_set(rst_fd, 1); usleep(50000); gpio_set(rst_fd, 0); usleep(50000); gpio_set(rst_fd, 1); usleep(80000);
    struct { uint8_t c, n, d[16]; } in[] = {
        {(uint8_t)0x36,(uint8_t)1,{g_madctl_legacy ? (uint8_t)0x70 : (uint8_t)0x00}},{0x3A,1,{0x05}},{0xB2,5,{0x0C,0x0C,0x00,0x33,0x33}},{0xB7,1,{0x35}},{0xBB,1,{0x35}},
        {0xC0,1,{0x2C}},{0xC2,1,{0x01}},{0xC3,1,{0x13}},{0xC4,1,{0x20}},{0xC6,1,{0x0F}},{0xD0,2,{0xA4,0xA1}},
        {0xE0,14,{0xF0,0x00,0x04,0x04,0x04,0x05,0x29,0x33,0x3E,0x38,0x12,0x12,0x28,0x30}},
        {0xE1,14,{0xF0,0x07,0x0A,0x0D,0x0B,0x07,0x28,0x33,0x3E,0x36,0x14,0x14,0x29,0x32}},
        {0x21,0,{0}},{0x11,0,{0}},
    };
    for (unsigned i = 0; i < sizeof(in)/sizeof(in[0]); i++) { dcmd(in[i].c); if (in[i].n) ddata(in[i].d, in[i].n); if (in[i].c == 0x11) usleep(80000); }
    dcmd(0x29);   /* backlight stays OFF until the splash is drawn (like the app) */
}
static uint8_t fb565[240*240*2];
static unsigned FW, FH, FSTRIDE;
/* fused contrast + RGB565 LUTs, exactly the Python app's display path:
 * (x - 128) * 1.15 + 123, then masked/shifted per channel */
static uint16_t R565[256], G565[256], B565[256];
/* optional saturation stage: 4th dispcal float ≠ 1.0 activates a 3x3 colour
 * matrix (white-point gains folded in) baked into 9 int16 LUTs — per pixel:
 * 9 loads + 6 adds + clamps. sat==1.0 keeps the original 3-LUT path. */
static int16_t M9[3][3][256];
static bool g_sat_on = false;
static inline uint16_t px565(uint8_t r, uint8_t g, uint8_t b) {
    if (!g_sat_on) return R565[r] | G565[g] | B565[b];
    int rv = M9[0][0][r] + M9[0][1][g] + M9[0][2][b];
    int gv = M9[1][0][r] + M9[1][1][g] + M9[1][2][b];
    int bv = M9[2][0][r] + M9[2][1][g] + M9[2][2][b];
    rv = rv < 0 ? 0 : rv > 255 ? 255 : rv;
    gv = gv < 0 ? 0 : gv > 255 ? 255 : gv;
    bv = bv < 0 ? 0 : bv > 255 ? 255 : bv;
    return (uint16_t)((rv & 0xF8) << 8) | (uint16_t)((gv & 0xFC) << 3) | (uint16_t)((bv & 0xF8) >> 3);
}
/* camera WB shift — the amber/blue fine-tune real cameras offer: a fixed RGB
 * trim applied to CAMERA pixels only (live preview, photos, gifs), never the
 * UI. Predictable and AWB-independent, unlike tuning-file knobs where the WB
 * gains and the CT-dependent colour matrix partially cancel each other.
 * /data/.wbtrim (or baked /usr/share/optocam/wbtrim): "R G B" floats. */
static uint8_t WBL[3][256];
static bool g_wb_on = false;
static void build_wb_trim(void) {
    float tr = 1, tg = 1, tb = 1;
    FILE *f = fopen("/data/.wbtrim", "r");
    if (!f) f = fopen("/usr/share/optocam/wbtrim", "r");
    if (f) {
        float r, g, b;
        if (fscanf(f, "%f %f %f", &r, &g, &b) == 3 &&
            r > 0.5f && r <= 1.5f && g > 0.5f && g <= 1.5f && b > 0.5f && b <= 1.5f) {
            tr = r; tg = g; tb = b;
        }
        fclose(f);
    }
    for (int i = 0; i < 256; i++) {
        auto q = [](float v) { return (uint8_t)(v < 0 ? 0 : v > 255 ? 255 : v); };
        WBL[0][i] = q(i * tr); WBL[1][i] = q(i * tg); WBL[2][i] = q(i * tb);
    }
    g_wb_on = tr != 1.0f || tg != 1.0f || tb != 1.0f;
}
static void wb_apply(uint8_t *p, size_t npx, bool bgr) {
    if (!g_wb_on) return;
    int ri = bgr ? 2 : 0, bi = bgr ? 0 : 2;
    for (size_t i = 0; i < npx; i++, p += 3) {
        p[ri] = WBL[0][p[ri]]; p[1] = WBL[1][p[1]]; p[bi] = WBL[2][p[bi]];
    }
}
/* device baseline contrast: photos map i -> (i-128)*1.15 + 123. The calibration
 * "contrast" slider scales that 1.15 (default keeps the shipped look). */
static constexpr float BASE_CONTRAST = 1.15f;
/* Calibration transfer applied per display pixel, baked into the LUTs so it
 * costs nothing per frame. Order: contrast -> gamma -> per-channel gain (+ an
 * optional saturation matrix). Returns a normalised 0..1 tone; the per-channel
 * gain is applied by the caller. */
static inline float calib_tone(int i, float contrast, float inv_gamma) {
    float c = (i - 128) * contrast + 123.0f;
    c = c < 0 ? 0 : c > 255 ? 255 : c;
    float n = c / 255.0f;
    if (inv_gamma < 0.999f || inv_gamma > 1.001f) n = powf(n, inv_gamma);
    return n;
}
/* core LUT builder: per-channel white-point gains, saturation, gamma and
 * contrast. Split out so the transfer-mode screen calibrator can rebuild the
 * LUTs live from slider values (see /data/.calib) without re-reading a file. */
static void build_565_luts_from(float gr, float gg, float gb, float sat,
                                float gamma, float contrast) {
    float inv_g = (gamma > 0.05f) ? 1.0f / gamma : 1.0f;
    auto q = [](float v) -> uint16_t { return v < 0 ? 0 : v > 255 ? 255 : (uint16_t)v; };
    for (int i = 0; i < 256; i++) {
        float n = calib_tone(i, contrast, inv_g);
        R565[i] = (q(255.0f * n * gr) & 0xF8) << 8;
        G565[i] = (q(255.0f * n * gg) & 0xFC) << 3;
        B565[i] = (q(255.0f * n * gb) & 0xF8) >> 3;
    }
    if (sat < 0.99f || sat > 1.01f) {
        /* standard luma-preserving saturation matrix, white gains folded in */
        const float W[3] = {gr, gg, gb}, LR = 0.299f, LG = 0.587f, LB = 0.114f;
        const float S[3][3] = {
            {LR + (1-LR)*sat, LG*(1-sat),      LB*(1-sat)},
            {LR*(1-sat),      LG + (1-LG)*sat, LB*(1-sat)},
            {LR*(1-sat),      LG*(1-sat),      LB + (1-LB)*sat}};
        for (int i = 0; i < 3; i++)
            for (int j = 0; j < 3; j++)
                for (int v = 0; v < 256; v++) {
                    float base = 255.0f * calib_tone(v, contrast, inv_g);
                    M9[i][j][v] = (int16_t)lrintf(W[i] * S[i][j] * base);
                }
        g_sat_on = true;
    } else g_sat_on = false;
}
/* Calibrated backlight duty, 51..255 (255 = solid DC, no PWM). The "screen
 * brightness" calibration slider lands here — real luminance via the BL pin,
 * never a pixel gain, so it can't clip highlights or cost tonal bits. */
static std::atomic<int> g_bl_level(255);
/* panel white-point calibration: this unit's LCD batch runs bluer than the
 * original device's (identical init registers + identical conversion were
 * verified — the difference is the glass). /data/.dispcal holds up to seven
 * floats "GR GG GB [SAT GAMMA CONTRAST BACKLIGHT]" (written by the hotspot
 * Screen-Calibration panel on save); absent fields fall back to neutral / the
 * baked contrast / full backlight. The baked fallback file stays 3-float and
 * back-compatible. Gain bounds are INCLUSIVE of 0.2 — the server clamps to
 * exactly 0.2, and `>` here used to reject such a profile wholesale. */
static bool read_dispcal(const char *path, float &gr, float &gg, float &gb,
                         float &sat, float &gamma, float &contrast, float &bl) {
    FILE *cal = fopen(path, "r");
    if (!cal) return false;
    float r, g, b, s = 1.0f, ga = 1.0f, ct = BASE_CONTRAST, lv = 1.0f;
    int n = fscanf(cal, "%f %f %f %f %f %f %f", &r, &g, &b, &s, &ga, &ct, &lv);
    fclose(cal);
    if (n >= 3 && r >= 0.2f && r <= 2.5f && g >= 0.2f && g <= 2.5f && b >= 0.2f && b <= 2.5f) {
        gr = r; gg = g; gb = b;
        sat      = (n >= 4 && s  >= 0.0f && s  <= 2.5f) ? s  : 1.0f;
        gamma    = (n >= 5 && ga >= 0.3f && ga <= 3.0f) ? ga : 1.0f;
        contrast = (n >= 6 && ct >= 0.2f && ct <= 2.5f) ? ct : BASE_CONTRAST;
        bl       = (n >= 7 && lv >= 0.2f && lv <= 1.0f) ? lv : 1.0f;
        return true;
    }
    return false;
}
static void build_565_luts(void) {
    float gr = 1.0f, gg = 1.0f, gb = 1.0f, sat = 1.0f, gamma = 1.0f, contrast = BASE_CONTRAST, bl = 1.0f;
    if (!read_dispcal("/data/.dispcal", gr, gg, gb, sat, gamma, contrast, bl))   /* /data mounts late — */
        read_dispcal("/usr/share/optocam/dispcal", gr, gg, gb, sat, gamma, contrast, bl);  /* baked fallback */
    build_565_luts_from(gr, gg, gb, sat, gamma, contrast);
    g_bl_level.store((int)lrintf(bl * 255.0f));
}
/* Writes now happen in the panel's PHYSICAL scan order (MADCTL 0x00) and the
 * old hardware remap (0x70: MV|MX) is applied in software — same image, but
 * the write pointer moves WITH the scan instead of across it, which turns the
 * harsh diagonal tearing into occasional mild banding (esp-idf #15265 clue).
 * Variant selectable via /tmp/rot (0-7) for on-device axis calibration. */
static int fb_variant(void) { return g_madctl_legacy ? 1 : 2; }
static inline void fb_map(int variant, int i, int j, int &x, int &y) {
    switch (variant & 7) {
    case 0: x = 239 - i; y = j; break;        /* P70 guess: MV + MX */
    case 1: x = i;       y = j; break;
    case 2: x = i;       y = 239 - j; break;
    case 3: x = 239 - i; y = 239 - j; break;
    case 4: x = j;       y = i; break;
    case 5: x = 239 - j; y = i; break;
    case 6: x = j;       y = 239 - i; break;
    default: x = 239 - j; y = 239 - i; break;
    }
}
static void fb_store_565(const uint8_t *vis565) {       /* splash path (RGB565 in) */
    int v = fb_variant();
    for (int i = 0; i < 240; i++)
        for (int j = 0; j < 240; j++) {
            int x, y; fb_map(v, i, j, x, y);
            fb565[(i*240 + j)*2]     = vis565[(y*240 + x)*2];
            fb565[(i*240 + j)*2 + 1] = vis565[(y*240 + x)*2 + 1];
        }
}
static void fb_flush(void) {
    uint8_t win[4] = {0x00,0x00,0x00,0xEF};
    dcmd(0x2A); ddata(win, 4); dcmd(0x2B); ddata(win, 4); dcmd(0x2C); ddata(fb565, sizeof fb565);
}
static void display_blit(const uint8_t *rgb) {
    unsigned ox = FW > 240 ? (FW - 240) / 2 : 0, oy = FH > 240 ? (FH - 240) / 2 : 0;
    int v = fb_variant();
    for (int i = 0; i < 240; i++) {
        uint8_t *out = fb565 + i * 240 * 2;
        for (int j = 0; j < 240; j++) {
            int x, y; fb_map(v, i, j, x, y);
            int sx = 239 - y, sy = x;                 /* 90° to match panel mount */
            const uint8_t *px = rgb + (size_t)(oy + sy) * FSTRIDE + (size_t)(ox + sx) * 3;
            uint16_t c = px565(px[2], px[1], px[0]);
            out[j*2] = c >> 8; out[j*2+1] = c & 0xFF;
        }
    }
    fb_flush();
}
static void display_fill(uint16_t c) {
    for (int i = 0; i < 240*240; i++) { fb565[i*2] = c >> 8; fb565[i*2+1] = c & 0xFF; }
    fb_flush();
}
/* ---- screen calibration (transfer mode only, off the boot path) ----
 * Blit a straight 240x240 RGB buffer through px565() so the live white-point
 * LUTs apply — the panel shows the calibration pattern exactly as it will
 * render real photos. */
static void display_blit_240(const uint8_t *rgb) {
    int v = fb_variant();
    for (int i = 0; i < 240; i++) {
        uint8_t *out = fb565 + i * 240 * 2;
        for (int j = 0; j < 240; j++) {
            int x, y; fb_map(v, i, j, x, y);
            const uint8_t *px = rgb + ((size_t)y * 240 + x) * 3;
            uint16_t c = px565(px[0], px[1], px[2]);   /* calib.rgb is RGB order */
            out[j*2] = c >> 8; out[j*2+1] = c & 0xFF;
        }
    }
    fb_flush();
}
/* selectable reference pattern: /usr/share/optocam/calib_<name>.rgb, chosen by
 * /data/.calib_img ("color" | "gray" | "photo"). Always leaves a usable buffer:
 * on any read failure it falls back to the colour chart, then to a flat mid-gray
 * fill — so the calibration screen is never blank. Returns true always. */
static bool load_calib_image(uint8_t *rgb240, const char *name) {
    const size_t NB = (size_t)240 * 240 * 3;
    const char *tries[3];
    char path[64];
    snprintf(path, sizeof path, "/usr/share/optocam/calib_%s.rgb", name);
    tries[0] = path;
    tries[1] = "/usr/share/optocam/calib_color.rgb";
    tries[2] = "/usr/share/optocam/calib.rgb";        /* legacy single-file name */
    for (int t = 0; t < 3; t++) {
        FILE *f = fopen(tries[t], "rb");
        if (!f) continue;
        size_t n = fread(rgb240, 1, NB, f);
        fclose(f);
        if (n == NB) return true;
    }
    memset(rgb240, 128, NB);                           /* flat gray so it's never blank */
    return true;
}
static const char *CALIB_IMG_NAMES[3] = {"color", "gray", "photo"};
#define N_CALIB_IMGS 3
static void read_calib_img_name(char *out, size_t cap) {   /* -> one of CALIB_IMG_NAMES */
    snprintf(out, cap, "color");
    FILE *f = fopen("/data/.calib_img", "r");
    if (!f) return;
    char buf[16] = {0};
    if (fscanf(f, "%15s", buf) == 1)
        for (int k = 0; k < N_CALIB_IMGS; k++)
            if (!strcmp(buf, CALIB_IMG_NAMES[k])) { snprintf(out, cap, "%s", buf); break; }
    fclose(f);
}
/* joystick left/right on the camera cycles the pattern by writing the same
 * /data/.calib_img the hotspot reads, so LCD and phone stay in sync. */
static void cycle_calib_img(const char *cur, int dir) {
    int i = 0;
    for (int k = 0; k < N_CALIB_IMGS; k++) if (cur && !strcmp(cur, CALIB_IMG_NAMES[k])) { i = k; break; }
    i = (i + dir + N_CALIB_IMGS) % N_CALIB_IMGS;
    FILE *f = fopen("/data/.calib_img", "w");
    if (f) { fprintf(f, "%s\n", CALIB_IMG_NAMES[i]); fclose(f); }
}
/* live slider values the calibrator writes as the user drags:
 * "GR GG GB [SAT GAMMA CONTRAST BACKLIGHT]". Wider accepted range than the
 * saved profile since temperature/tint*channel can push a gain past 1.5.
 * Bounds inclusive of 0.2, matching read_dispcal. */
static bool read_calib_live(float &r, float &g, float &b, float &s,
                            float &gamma, float &contrast, float &bl) {
    FILE *f = fopen("/data/.calib", "r");
    if (!f) return false;
    float rr, gg, bb, ss = 1.0f, ga = 1.0f, ct = BASE_CONTRAST, lv = 1.0f;
    int n = fscanf(f, "%f %f %f %f %f %f %f", &rr, &gg, &bb, &ss, &ga, &ct, &lv);
    fclose(f);
    if (n >= 3 && rr >= 0.2f && rr <= 2.5f && gg >= 0.2f && gg <= 2.5f && bb >= 0.2f && bb <= 2.5f) {
        r = rr; g = gg; b = bb;
        s        = (n >= 4 && ss >= 0.0f && ss <= 2.5f) ? ss : 1.0f;
        gamma    = (n >= 5 && ga >= 0.3f && ga <= 3.0f) ? ga : 1.0f;
        contrast = (n >= 6 && ct >= 0.2f && ct <= 2.5f) ? ct : BASE_CONTRAST;
        bl       = (n >= 7 && lv >= 0.2f && lv <= 1.0f) ? lv : 1.0f;
        return true;
    }
    return false;
}

/* ---------- buttons (active-low, internal pull-ups) ---------- */
enum { BTN_CAPTURE, BTN_PREVIEW, JOY_LEFT, JOY_RIGHT, JOY_PRESS, JOY_UP, JOY_DOWN, NBTN };
static const unsigned btn_pins[NBTN] = { 21, 20, 5, 26, 13, 6, 19 };
static int btn_fd = -1;
static void buttons_init(void) {
    int chip = open("/dev/gpiochip0", O_RDONLY);
    struct gpiohandle_request r; memset(&r, 0, sizeof r);
    for (int i = 0; i < NBTN; i++) r.lineoffsets[i] = btn_pins[i];
    r.lines = NBTN;
    r.flags = GPIOHANDLE_REQUEST_INPUT | GPIOHANDLE_REQUEST_BIAS_PULL_UP;
    strcpy(r.consumer_label, "optocam-btn");
    if (ioctl(chip, GPIO_GET_LINEHANDLE_IOCTL, &r) < 0) perror("btn linehandle");
    else btn_fd = r.fd;
    close(chip);
}
static uint8_t buttons_read(void) {           /* bit i set = button i pressed */
    struct gpiohandle_data d;
    if (btn_fd < 0 || ioctl(btn_fd, GPIOHANDLE_GET_LINE_VALUES_IOCTL, &d) < 0) return 0;
    uint8_t m = 0;
    for (int i = 0; i < NBTN; i++) if (!d.values[i]) m |= 1 << i;
    return m;
}

/* ---------- libcamera ---------- */
static std::shared_ptr<Camera> camera;
static Stream *stream;
static std::map<FrameBuffer *, void *> memmap;
static std::vector<std::pair<void *, size_t>> mappings;   /* munmap on reconfigure */
static std::vector<std::unique_ptr<Request>> requests;
static FrameBufferAllocator *alloc_ = nullptr;

static std::atomic<int> g_mode(0);            /* 0 = preview, 1 = still pending */
static std::atomic<bool> g_streaming(false);  /* false while stopping/reconfiguring */
static std::mutex still_mtx;
static std::condition_variable still_cv;
static const uint8_t *still_data = nullptr;
static std::atomic<int> g_af_state(0);              /* AfState from metadata */
static std::atomic<bool> g_still_take(false), g_af_trig(false);

/* ---------- HUD state (button thread writes, render thread reads) ---------- */
static std::atomic<int> g_filter_idx(0);            /* "Film Standard" */
static uint8_t g_frame_copy[1536 * 1024];           /* fits 640x640 RGB (gif) + stride */
static std::atomic<uint64_t> g_frame_seq(0);
static std::atomic<bool> g_frame_wanted(true);
static std::mutex g_frame_mtx;
static std::mutex g_render_mtx;                     /* serializes sprite/text rendering */
static float g_meta_gain = 1.0f; static int32_t g_meta_exp = 10000;
/* AWB diagnostics (logged with the fps meter) */
static std::atomic<int> g_meta_ct(0);
static std::atomic<int> g_meta_cgr_x100(0), g_meta_cgb_x100(0);
static std::atomic<int> g_meta_dg_x100(100);   /* ISP digital gain — feeds the
                                                * preview WYSIWYG dim + diagnostics */
static std::atomic<bool> g_ae_locked(false);
static std::atomic<int32_t> g_ae_lock_exp(10000);
static std::atomic<float> g_ae_lock_gain(1.0f);
/* Exposure settings from the hotspot "Camera Settings" panel: /data/.aelimits
 * holds "SHUTTER_DEN METERING_IDX DGAIN_BOOST"; a missing file means factory
 * (50 0 0) — 1/50 s is the preview cap this app has always run, and
 * centre-weighted is the tuning default, so factory changes nothing. The
 * shutter cap applies natively via FrameDurationLimits; metering maps straight
 * onto AeMeteringMode (0 centre-weighted, 1 spot, 2 matrix). DGAIN_BOOST=1
 * lets captures ask for 32x total gain instead of the sensor's 16x analog
 * ceiling — the RPi IPA delivers the >16x remainder as ISP digital gain. */
static std::atomic<int64_t> g_max_exp_us(20000);  /* 1/50 s */
static std::atomic<int> g_metering(0);
static std::atomic<bool> g_dgain_boost(false);
/* capture gain ceiling, shared by stills and the GIF exposure lock */
static float capture_max_gain(void) { return g_dgain_boost.load() ? 32.0f : 16.0f; }
/* Stills run on MANUAL exposure copied from the preview's converged AE: the
 * still mode's ~70ms frames make FrameDurationLimits useless as a shutter cap
 * there (measured: a 1/125 cap still produced a 60ms still while the AGC
 * re-adapted), and the preview values are already capped — and WYSIWYG with
 * the screen. 0 = fall back to AE (no preview metadata yet). */
static std::atomic<int32_t> g_still_exp_ovr(0);
static std::atomic<float> g_still_gain_ovr(1.0f);
static constexpr int32_t GIF_MAX_EXP_US = 100000;     /* 1/10s */
static std::atomic<int> g_awb_idx(AWB_DEFAULT_IDX);
static std::atomic<bool> g_ctrl_dirty(true);
static std::atomic<bool> g_live_neutral(false);  /* LIVE tab: ship raw frames —
                                                  * the browser applies the filter,
                                                  * so the active filter's ISP
                                                  * (e.g. B&W sat=0) must not.
                                                  * Also forces auto-WB: filters
                                                  * are designed on a neutral,
                                                  * auto-balanced base. */
static int32_t meter_mode(void) {
    switch (g_metering.load()) {
        case 1:  return controls::MeteringSpot;
        case 2:  return controls::MeteringMatrix;
        default: return controls::MeteringCentreWeighted;
    }
}
static void load_ae_limits(void) {
    int den = 50, met = 0, dgb = 0;
    FILE *f = fopen("/data/.aelimits", "r");
    if (f) {
        int n = fscanf(f, "%d %d %d", &den, &met, &dgb);
        if (n < 1) den = 50;
        if (n < 2) met = 0;                   /* pre-metering files: den only */
        if (n < 3) dgb = 0;                   /* pre-boost files: den+met only */
        fclose(f);
    }
    if (den < 20) den = 50;                   /* sanity — UI offers 30..125 */
    if (den > 125) den = 125;                 /* sensor floor: binned mode is 120fps,
                                               * faster caps just get pipeline-clamped */
    if (met < 0 || met > 2) met = 0;
    int64_t exp_us = 1000000 / den;
    bool changed = g_max_exp_us.exchange(exp_us) != exp_us;
    if (g_metering.exchange(met) != met) changed = true;
    g_dgain_boost.store(dgb == 1);            /* captures only — no dirty needed */
    if (changed)
        g_ctrl_dirty.store(true);   /* boot loads this after preview starts —
                                     * the dirty path re-applies live */
}
/* Translate preview-mode AE values (live or locked) into the still mode's
 * manual exposure. THE one transform shared by do_capture and the HUD
 * readout, so what the screen shows at the shutter press is exactly what the
 * photo gets. Exposure scales by the mode sensitivity ratio (the binned
 * preview mode gathers ~2.5x the light of the un-binned still mode —
 * empirically tuned on-device by matching capture frame means: 4.0 was ~+18%
 * bright, 3.0 ~+15%, 2.5 lands it); overflow past the min-shutter cap moves
 * into gain (EV-preserving), clamped at the 16x sensor max, where genuinely
 * dim scenes take an honestly darker still. */
static constexpr double MODE_SENS_RATIO = 2.5;   /* binned preview vs still sensitivity */
static void still_exposure_from(int32_t pexp, float pgain, int32_t &exp_out, float &gain_out) {
    double exp2 = (double)pexp * MODE_SENS_RATIO, gain2 = pgain;
    int64_t ecap = g_max_exp_us.load();
    if (ecap > 0 && exp2 > (double)ecap) {
        gain2 = pgain * (exp2 / (double)ecap);
        exp2 = (double)ecap;
    }
    float maxg = capture_max_gain();
    if (gain2 > (double)maxg) gain2 = maxg;
    exp_out = (int32_t)exp2;
    gain_out = (float)gain2;
}
/* set the moment the user changes filter/WB from the joystick, so the late
 * /data-mounted startup defaults never yank a control out from under them. */
static std::atomic<bool> g_user_touched(false);
/* startup defaults written by the hotspot "Camera Settings" panel:
 * "FILTER_IDX AWB_IDX". Applied once, only if the user hasn't already acted. */
static void load_startup_defaults(void) {
    if (g_user_touched.load()) return;
    FILE *f = fopen("/data/.defaults", "r");
    if (!f) return;
    int fi = -1, ai = -1;
    int n = fscanf(f, "%d %d", &fi, &ai);
    fclose(f);
    if (g_user_touched.load()) return;                 /* re-check after the read */
    /* filter is read live per frame; bound is dynamic (presets + existing
     * customs), so build_custom_filters() must have run before this. */
    if (n >= 1 && fi >= 0 && fi < filter_count()) g_filter_idx.store(fi);
    if (n >= 2 && ai >= 0 && ai < 5) {
        g_awb_idx.store(ai);
        g_ctrl_dirty.store(true);       /* AWB mode only re-applies to the camera on a dirty flag */
    }
}
static std::atomic<double> g_filter_label_time(0), g_awb_changed_time(0);
static std::atomic<int> g_saving(0);
static std::atomic<double> g_capture_dot_time(0);
static std::atomic<bool> g_gif_mode(false);
static std::atomic<bool> g_frame_resync(false);
static std::atomic<int> g_toast_id(0);              /* 1 no-image 2 no-space 3 GIF 4 Photo */
static std::atomic<double> g_toast_time(0);

static void draw_spinner_arc_aa(Img &img, float cx, float cy, float rad, float width,
                                float start_deg, float sweep_deg,
                                uint8_t r, uint8_t g, uint8_t b, uint8_t a) {
    int lo_x = (int)std::floor(cx - rad - width - 1), hi_x = (int)std::ceil(cx + rad + width + 1);
    int lo_y = (int)std::floor(cy - rad - width - 1), hi_y = (int)std::ceil(cy + rad + width + 1);
    for (int y = lo_y; y <= hi_y; y++)
        for (int x = lo_x; x <= hi_x; x++) {
            float dx = (x + 0.5f) - cx, dy = (y + 0.5f) - cy;
            float dist = std::sqrt(dx * dx + dy * dy);
            float ring = std::clamp(width * 0.5f + 0.55f - std::fabs(dist - rad), 0.0f, 1.0f);
            if (ring <= 0.0f) continue;
            float ang = std::atan2(dy, dx) * 180.0f / (float)M_PI;
            if (ang < 0.0f) ang += 360.0f;
            float d = std::fmod(ang - start_deg + 720.0f, 360.0f);
            if (d > sweep_deg) continue;
            float end = std::min(d, sweep_deg - d) * rad * (float)M_PI / 180.0f;
            float cap = std::clamp(end + 0.55f, 0.0f, 1.0f);
            set_px(img, x, y, r, g, b, (uint8_t)(a * ring * cap));
        }
}
static const Img &saving_spinner_sprite(int phase) {
    static std::vector<Img> sprites;
    if (sprites.empty()) {
        sprites.reserve(24);
        for (int i = 0; i < 24; i++) {
            Img sp(22, 22);
            float a0 = i * 15.0f;
            draw_spinner_arc_aa(sp, 11.5f, 11.5f, 7.0f, 2.2f, a0, 270.0f, 0,0,0,255);
            draw_spinner_arc_aa(sp, 10.5f, 10.5f, 7.0f, 2.2f, a0, 270.0f, 255,255,255,255);
            sprites.push_back(std::move(sp));
        }
    }
    return sprites[phase % (int)sprites.size()];
}
/* render the full preview frame: rotate+filter, HUD overlay, centre msg */
static void render_preview(const uint8_t *rgb, float gain, int exp_us) {
    int fidx = g_filter_idx.load();
    unsigned ox = FW > 240 ? (FW - 240) / 2 : 0, oy = FH > 240 ? (FH - 240) / 2 : 0;
    static Img frame(240, 240);
    FilterLUTs &lut = g_luts[fidx];
    /* WYSIWYG in the dark: the preview AGC outruns what a capture can deliver
     * (analog 16x — 32x with boost — while the AGC also stacks its own digital
     * gain, which captures never get). Dim the preview by the exact EV the
     * photo will fall short, so the screen brightness IS the photo's. Frame
     * pixels are gamma-encoded, so the linear ratio applies as f^(1/2.2).
     * f≈1 whenever nothing clamps — the LUT pass is skipped entirely then. */
    static uint8_t dimlut[256]; static float dim_last = -1;
    bool dimming = false;
    if (exp_us > 0 && gain > 0) {
        int32_t se; float sg;
        still_exposure_from(exp_us, gain, se, sg);
        float pdg = g_meta_dg_x100.load() / 100.0f; if (pdg < 1.0f) pdg = 1.0f;
        float f = (float)(((double)se * sg) /
                          ((double)exp_us * gain * pdg * MODE_SENS_RATIO));
        if (f < 0.97f) {
            dimming = true;
            float fg = std::pow(std::max(f, 0.01f), 1.0f / 2.2f);
            if (std::fabs(fg - dim_last) > 0.005f) {
                dim_last = fg;
                for (int i = 0; i < 256; i++) dimlut[i] = (uint8_t)(i * fg + 0.5f);
            }
        }
    }
    for (int y = 0; y < 240; y++)
        for (int x = 0; x < 240; x++) {
            int sx = 239 - y, sy = x;
            const uint8_t *px = rgb + (size_t)(oy + sy) * FSTRIDE + (size_t)(ox + sx) * 3;
            uint8_t r = px[2], g = px[1], b = px[0];
            if (dimming) { r = dimlut[r]; g = dimlut[g]; b = dimlut[b]; }
            if (lut.trix) {
                unsigned lu = (r*299u + g*587u + b*114u + 500u) / 1000u;
                r = g_trix_lut[lu][0]; g = g_trix_lut[lu][1]; b = g_trix_lut[lu][2];
            } else if (lut.cutout) {
                r = g = b = g_cutout_lut[(r*299u + g*587u + b*114u) / 1000u];
            } else if (!lut.identity) {
                r = lut.r[r]; g = lut.g[g]; b = lut.b[b];
            }
            uint8_t *o = frame.at(x, y);
            o[0] = r; o[1] = g; o[2] = b; o[3] = 255;
        }

    double now = now_s();
    /* the readout predicts the CAPTURE: same transform as do_capture, so the
     * shutter/ISO on screen at the press are the numbers the photo gets */
    {
        int32_t de = exp_us; float dgain = gain;
        if (de > 0 && dgain > 0) still_exposure_from(de, dgain, de, dgain);
        exp_us = de; gain = dgain;
    }
    char isobuf[8];
    std::string iso = nearest_iso(gain, isobuf);
    if (gain > 16.01f) iso += "+";     /* capture will use ISP digital gain */
    std::string shutter = exp_us > 0 ? nearest_shutter(exp_us) : "?";
    int aidx = g_awb_idx.load();
    bool awb_switching = now - g_awb_changed_time.load() < 1.0;
    std::string awb_label = awb_switching ? AWB_MODES[aidx].full : AWB_MODES[aidx].abbr;
    int awb_fontsz = awb_switching ? 25 : 24;

    /* AWB pop on any label change */
    static std::string awb_last; static double awb_pop_start = -10;
    if (awb_label != awb_last) { awb_last = awb_label; awb_pop_start = now; }
    bool awb_popping = now - awb_pop_start < 0.30;

    double flt = g_filter_label_time.load();
    bool pill_popping = flt > 0 && now - flt < 0.30;

    Font *fhud = g_te.font(25);
    Font *fawb = g_te.font(awb_fontsz);
    int b0,b1,b2,b3;
    g_te.textbbox(fawb, awb_label, b0,b1,b2,b3);
    int ax = 15, ay = 15 - b1;
    g_te.textbbox(fhud, iso, b0,b1,b2,b3);
    int ih = b3 - b1, i_b1 = b1, i_b3 = b3;
    int sx0,sy0,sx1,sy1; g_te.textbbox(fhud, shutter, sx0,sy0,sx1,sy1);
    int hud_bottom_y = 240 - 15 - std::max(ih, sy1 - sy0);
    int ix = 15, iy = hud_bottom_y;
    int shx = 240 - 15 - (sx1 - sx0), shy = hud_bottom_y;

    /* cached assembled shadow overlay */
    static Img overlay; static std::string okey;
    int gp = g_gif_mode.load() ? (int)(now * 4) % 2 : -1;   /* march at 4Hz */
    std::string key = awb_label + "|" + iso + "|" + shutter + "|" + std::to_string(fidx)
                    + (pill_popping ? "P" : "p") + (awb_popping ? "A" : "a") + std::to_string(gp);
    if (key != okey) {
        okey = key;
        overlay = cached_shadow("iso", iso, 25, ix, iy);
        alpha_composite(overlay, cached_shadow("shutter", shutter, 25, shx, shy));
        if (!awb_popping)
            alpha_composite(overlay, cached_shadow("awb", awb_label, awb_fontsz, ax, ay));
        if (!pill_popping) alpha_composite(overlay, filter_indicator(fidx));
        if (g_gif_mode.load()) alpha_composite(overlay, gif_mode_indicator(gp));
    }
    alpha_composite(frame, overlay);
    if (!awb_popping) g_te.draw(frame, fawb, ax, ay, awb_label, 255,255,255);
    g_te.draw(frame, fhud, ix, iy, iso, 255,255,255);
    g_te.draw(frame, fhud, shx, shy, shutter, 255,255,255);

    /* saving spinner (bottom centre) */
    if (g_saving.load() > 0) {
        int sp_cy = hud_bottom_y + (i_b1 + i_b3) / 2;
        const Img &sp = saving_spinner_sprite((int)(now * 24.0));
        for (int y = 0; y < sp.h; y++)
            for (int x = 0; x < sp.w; x++) {
                const uint8_t *s = sp.at(x, y);
                if (!s[3]) continue;
                set_px(frame, 120 - sp.w / 2 + x, sp_cy - sp.h / 2 + y, s[0], s[1], s[2], s[3]);
            }
    }
    /* NOTE: overlay_capture_dot exists in the original but is never called —
     * dead code there, so no dot here either (the saving spinner is the
     * capture feedback). */

    /* pops on top */
    if (awb_popping) {
        Img sp(240,240);
        Img sh = text_shadow_sprite(awb_fontsz, awb_label, ax, ay);
        alpha_composite(sp, sh);
        g_te.draw(sp, g_te.font(awb_fontsz), ax, ay, awb_label, 255,255,255);
        float t = std::clamp((float)(now - awb_pop_start) / 0.30f, 0.f, 1.f);
        float e = ease_out_back(t);
        composite_pill_pop(frame, sp, awb_switching ? 0.94f + 0.06f*e : 0.90f + 0.10f*e);
    }
    if (pill_popping)
        composite_pill_pop(frame, filter_indicator(fidx), pill_pop_scale((float)(now - flt)));

    /* centre messages, exact priority: no-image, no-space, filter name, mode toast */
    int tid = g_toast_id.load(); double tt = g_toast_time.load();
    const char *tmsg = tid == 1 ? "No image in card" : tid == 2 ? "No space in card"
                     : tid == 3 ? "GIF Mode" : tid == 4 ? "Photo Mode" : nullptr;
    double tdur = (tid == 1 || tid == 2) ? 1.0 : 1.5;
    if (tmsg && (tid == 3 || tid == 4) && flt > 0 && now - flt < 1.5) tmsg = nullptr; /* filter label wins over mode toast */
    if (tmsg && now - tt < tdur) {
        float al, sc; ease_centre_msg((float)(now - tt), (float)tdur, al, sc);
        composite_scaled_centre(frame, centre_msg_sprite(tmsg), sc, al);
    } else if (flt > 0 && now - flt < 1.5) {
        float al, sc; ease_centre_msg((float)(now - flt), 1.5f, al, sc);
        composite_scaled_centre(frame, centre_msg_sprite(filter_name(fidx)), sc, al);
    } else if (flt > 0 && now - flt >= 1.5) g_filter_label_time.store(0);

    /* Img → RGB565 with contrast LUT, in panel scan order */
    int v = fb_variant();
    for (int i = 0; i < 240; i++) {
        uint8_t *out = fb565 + i * 240 * 2;
        for (int j = 0; j < 240; j++) {
            int x, y; fb_map(v, i, j, x, y);
            const uint8_t *o = frame.at(x, y);
            uint16_t c = px565(o[0], o[1], o[2]);
            out[j*2] = c >> 8; out[j*2+1] = c & 0xFF;
        }
    }
    fb_flush();
}

static void logstep(const char *m) {
    float up = 0; FILE *f = fopen("/proc/uptime", "r");
    if (f) { fscanf(f, "%f", &up); fclose(f); }
    printf("optocam: [%.2f] %s\n", up, m); fflush(stdout);
}

/* ---- signal forensics -------------------------------------------------------
 * The device was seen dying by SIGTERM (busybox "Terminated") with NO core and
 * no in-app or in-script sender we could find statically.  Catch every deadly
 * signal with SA_SIGINFO so si_pid/si_code names the actual sender, appended to
 * /data/sigcatch.log with async-signal-safe primitives only.  SIGTERM/HUP/INT/
 * QUIT/PIPE have no legitimate source in this always-on appliance (S03 respawns
 * us; power-off is a plug pull), so we log-and-SURVIVE them — except a genuine
 * init(pid 1) shutdown.  Real faults (SEGV/BUS/ABRT/ILL/FPE) log then re-raise
 * default, preserving the core dump + respawn. */
static void sc_str(int fd, const char *s) { if (s) { size_t n = 0; while (s[n]) n++; (void)!write(fd, s, n); } }
static void sc_num(int fd, long v) {
    char b[24]; int i = (int)sizeof b; bool neg = v < 0; unsigned long u = neg ? (unsigned long)(-(v + 1)) + 1UL : (unsigned long)v;
    if (u == 0) b[--i] = '0'; else while (u) { b[--i] = (char)('0' + u % 10); u /= 10; }
    if (neg) b[--i] = '-';
    (void)!write(fd, b + i, sizeof b - i);
}
/* Append "<label><contents of /proc/<pid>/<leaf>>" — read in the handler so the
 * sender is still alive (it just returned from kill()). NULs->spaces, cut at \n. */
static void sc_proc(int fd, long pid, const char *leaf, const char *label) {
    char path[48]; int n = 0;
    for (const char *c = "/proc/"; *c; c++) path[n++] = *c;
    char t[12]; int i = 12; long v = pid;
    if (v <= 0) t[--i] = '0'; else while (v > 0) { t[--i] = (char)('0' + v % 10); v /= 10; }
    while (i < 12) path[n++] = t[i++];
    for (const char *c = leaf; *c; c++) path[n++] = *c;
    path[n] = 0;
    sc_str(fd, label);
    int pf = open(path, O_RDONLY);
    if (pf < 0) { sc_str(fd, "(gone)"); return; }
    char buf[160]; ssize_t r = read(pf, buf, sizeof buf); close(pf);
    if (r <= 0) { sc_str(fd, "(empty)"); return; }
    for (ssize_t k = 0; k < r; k++) { if (buf[k] == '\n') { r = k; break; } if (buf[k] == '\0') buf[k] = ' '; }
    (void)!write(fd, buf, r);
}
static void crash_handler(int sig, siginfo_t *si, void *) {
    int fd = open("/data/sigcatch.log", O_WRONLY | O_CREAT | O_APPEND, 0644);
    if (fd < 0) fd = open("/tmp/sigcatch.log", O_WRONLY | O_CREAT | O_APPEND, 0644);
    struct timespec ts; ts.tv_sec = 0; ts.tv_nsec = 0; clock_gettime(CLOCK_MONOTONIC, &ts);
    bool from_init = si && si->si_pid == 1;
    bool survivable = (sig == SIGTERM || sig == SIGHUP || sig == SIGINT || sig == SIGQUIT || sig == SIGPIPE) && !from_init;
    if (fd >= 0) {
        sc_str(fd, "sig="); sc_num(fd, sig);
        sc_str(fd, " code="); sc_num(fd, si ? si->si_code : -999);
        sc_str(fd, " sender_pid="); sc_num(fd, si ? (long)si->si_pid : -1);
        sc_str(fd, " sender_uid="); sc_num(fd, si ? (long)si->si_uid : -1);
        sc_str(fd, " up_ms="); sc_num(fd, (long)(ts.tv_sec * 1000 + ts.tv_nsec / 1000000));
        if (si && si->si_code <= 0 && si->si_pid > 1) {   /* SI_USER/SI_QUEUE: a real kill() */
            sc_proc(fd, si->si_pid, "/comm",    " sender_comm=");
            sc_proc(fd, si->si_pid, "/cmdline", " sender_cmd=");
        }
        sc_str(fd, survivable ? " -> SURVIVED\n" : " -> reraise\n");
        close(fd);
    }
    if (survivable) return;
    signal(sig, SIG_DFL);
    raise(sig);
}
static void install_crash_handlers() {
    struct sigaction sa; memset(&sa, 0, sizeof sa);
    sa.sa_sigaction = crash_handler;
    sa.sa_flags = SA_SIGINFO | SA_RESTART;
    sigemptyset(&sa.sa_mask);
    for (int s : { SIGTERM, SIGHUP, SIGINT, SIGQUIT, SIGPIPE,
                   SIGSEGV, SIGBUS, SIGABRT, SIGILL, SIGFPE })
        sigaction(s, &sa, nullptr);
    int fd = open("/data/sigcatch.log", O_WRONLY | O_CREAT | O_APPEND, 0644);
    if (fd >= 0) { sc_str(fd, "--- start pid="); sc_num(fd, getpid());
                   sc_str(fd, " pgid="); sc_num(fd, getpgrp());
                   sc_str(fd, " sid="); sc_num(fd, getsid(0)); sc_str(fd, "\n"); close(fd); }
}

/* startup watchdog: a hang inside libcamera (cm.start()/camera->start(), the
 * same region as the intermittent stack-smash) leaves the splash up forever —
 * the S03 supervisor only catches exits, not hangs. SIGALRM fires if no frame
 * completes after an arm point; abort() leaves a core and gets us respawned. */
static std::atomic<bool> g_wd_armed(false);
static void wd_arm(unsigned s) { g_wd_armed.store(true); alarm(s); }
static void wd_timeout(int) {
    static const char m[] = "optocam: WATCHDOG timeout, aborting\n";
    ssize_t r = write(2, m, sizeof m - 1); (void)r;
    signal(SIGABRT, SIG_DFL);
    abort();
}

static void requestComplete(Request *req) {
    static int fc = 0;
    if (g_wd_armed.exchange(false)) alarm(0);   /* frames flowing — disarm */
    if (req->status() == Request::RequestCancelled) return;
    FrameBuffer *buf = req->buffers().begin()->second;
    if (g_mode.load() == 1) {
        /* Take only the FIRST still frame and never block this thread: the
         * callback runs on libcamera's camera thread, and camera->stop() joins
         * it — blocking here while the main thread encodes deadlocks stop(). */
        auto af = req->metadata().get(controls::AfState);
        if (af) g_af_state.store(*af);
        if (!g_still_take.load()) {           /* AF settling: keep frames flowing */
            req->reuse(Request::ReuseBuffers);
            if (g_af_trig.exchange(false)) {
                req->controls().set(controls::AfMode, controls::AfModeAuto);
                req->controls().set(controls::AfTrigger, controls::AfTriggerStart);
            }
            camera->queueRequest(req);
            return;
        }
        std::lock_guard<std::mutex> lk(still_mtx);
        if (!still_data) {
            still_data = static_cast<uint8_t *>(memmap[buf]);
            {   /* what the shot ACTUALLY used — ground truth for the
                 * min-shutter cap, which the still mode can't bind natively
                 * (its ~14fps frames dwarf the cap; the AE state carries the
                 * cap over from preview instead) */
                auto ev = req->metadata().get(controls::ExposureTime);
                auto gv = req->metadata().get(controls::AnalogueGain);
                auto dgv = req->metadata().get(controls::DigitalGain);
                auto cgv = req->metadata().get(controls::ColourGains);
                printf("optocam: STILL exp=%dus ag=%.2f dg=%.2f cg=%.2f/%.2f cap=%lldus\n",
                       ev ? *ev : -1, gv ? *gv : -1.0f, dgv ? *dgv : -1.0f,
                       cgv ? (*cgv)[0] : -1.0f, cgv ? (*cgv)[1] : -1.0f,
                       (long long)g_max_exp_us.load());
                fflush(stdout);
            }
            still_cv.notify_one();
        }
        return;                               /* hold the buffer until encoded */
    }
    if (fc++ == 0) { printf("optocam: FIRST FRAME\n"); fflush(stdout);
        /* cold start: one unfiltered fast frame, like the original */
        display_blit(static_cast<uint8_t *>(memmap[buf]));
        FILE *flag = fopen("/tmp/optocam-preview-up", "w");  /* unblocks S90 */
        if (flag) fclose(flag);
    } else {
        /* NEVER render here — camera thread. Copy + requeue only. The sensor
         * runs 100-200fps, the renderer ~30 — copy only when the last frame
         * was consumed (saves ~80% of the memcpys). */
        auto paf2 = req->metadata().get(controls::AfState);
        if (paf2) g_af_state.store(*paf2);
        if (g_frame_wanted.exchange(false)) {
            size_t len = std::min(sizeof g_frame_copy, (size_t)FH * FSTRIDE);
            {
                std::lock_guard<std::mutex> lk(g_frame_mtx);
                memcpy(g_frame_copy, memmap[buf], len);
                auto gv = req->metadata().get(controls::AnalogueGain);
                auto ev = req->metadata().get(controls::ExposureTime);
                if (gv) g_meta_gain = *gv;
                if (ev) g_meta_exp = *ev;
                auto ct = req->metadata().get(controls::ColourTemperature);
                auto cg = req->metadata().get(controls::ColourGains);
                if (ct) g_meta_ct.store(*ct);
                if (cg) { g_meta_cgr_x100.store((int)((*cg)[0]*100)); g_meta_cgb_x100.store((int)((*cg)[1]*100)); }
                auto dg = req->metadata().get(controls::DigitalGain);
                if (dg) g_meta_dg_x100.store((int)(*dg * 100));
            }
            g_frame_seq.fetch_add(1);
        }
    }
    if (!g_streaming.load()) return;          /* mode switch in progress — don't requeue */
    req->reuse(Request::ReuseBuffers);
    bool ctrl_dirty = g_ctrl_dirty.exchange(false);
    bool ae_locked = g_ae_locked.load();
    if (ctrl_dirty || ae_locked) {
        int fi = g_filter_idx.load();
        if (ctrl_dirty) {
            IspSet cisp = (fi >= CUSTOM_BASE) ? g_custom_isp[fi - CUSTOM_BASE] : FILM_ISP[fi];
            if (g_live_neutral.load()) cisp = {1.0f, 1.0f, 0.0f};
            req->controls().set(controls::Saturation, cisp.sat);
            req->controls().set(controls::Contrast, cisp.con);
            req->controls().set(controls::Brightness, cisp.bri);
            req->controls().set(controls::AwbMode, g_live_neutral.load()
                ? (int)controls::AwbAuto            /* LIVE tab: always auto-WB */
                : AWB_MODES[g_awb_idx.load()].mode);
            int64_t lim[2] = { 100, g_max_exp_us.load() };   /* min-shutter cap, may
                                                              * have loaded post-start */
            req->controls().set(controls::FrameDurationLimits, Span<const int64_t, 2>(lim, 2));
            req->controls().set(controls::AeMeteringMode, meter_mode());
            if (!ae_locked) req->controls().set(controls::AeEnable, true);
        }
        if (ae_locked) {
            req->controls().set(controls::AeEnable, false);
            req->controls().set(controls::ExposureTime, g_ae_lock_exp.load());
            req->controls().set(controls::AnalogueGain, g_ae_lock_gain.load());
        }
    }
    camera->queueRequest(req);
}

/* (re)configure the camera and build buffers/requests for the current config */
static bool setup_stream(CameraConfiguration *config, bool continuous_af) {
    StreamConfiguration &cfg = config->at(0);
    config->validate();
    logstep("setup: configuring");
    if (camera->configure(config) < 0) { printf("optocam: configure failed\n"); return false; }
    logstep("setup: configured");
    FW = cfg.size.width; FH = cfg.size.height; FSTRIDE = cfg.stride;
    stream = cfg.stream();
    /* munmap BEFORE freeing the allocator: a live mapping pins the dmabuf,
     * which pins its CMA memory — the leak that starved reconfiguration */
    for (auto &m : mappings) munmap(m.first, m.second);
    mappings.clear();
    delete alloc_;
    memmap.clear(); requests.clear();
    alloc_ = new FrameBufferAllocator(camera);
    if (alloc_->allocate(stream) < 0) { printf("optocam: allocate failed\n"); return false; }
    logstep("setup: allocated");
    for (const std::unique_ptr<FrameBuffer> &buffer : alloc_->buffers(stream)) {
        const FrameBuffer::Plane &p = buffer->planes()[0];
        void *mem = mmap(nullptr, p.length, PROT_READ, MAP_SHARED, p.fd.get(), p.offset);
        if (mem == MAP_FAILED) { printf("optocam: mmap failed\n"); return false; }
        memmap[buffer.get()] = mem;
        mappings.push_back({mem, p.length});
        std::unique_ptr<Request> req = camera->createRequest();
        req->addBuffer(stream, buffer.get());
        requests.push_back(std::move(req));
    }
    ControlList ctrls;
    int64_t exp_cap = g_max_exp_us.load();     /* user minimum shutter, 1/50 factory */
    if (continuous_af) {
        ctrls.set(controls::AfMode, controls::AfModeContinuous);
        ctrls.set(controls::AfSpeed, controls::AfSpeedFast);
    }
    {   /* the minimum-shutter cap binds AE natively — a frame can't be longer
         * than its duration. Preview/GIF and stills share the user's cap, so
         * 1/30 also buys a brighter (30 fps) live view. */
        int64_t lim[2] = { 100, exp_cap };
        ctrls.set(controls::FrameDurationLimits, Span<const int64_t, 2>(lim, 2));
    }
    ctrls.set(controls::AeMeteringMode, meter_mode());
    if (!continuous_af && g_still_exp_ovr.load() > 0) {
        /* still on translated preview values (covers AE lock too) — see
         * still_exposure_from() */
        ctrls.set(controls::AeEnable, false);
        ctrls.set(controls::ExposureTime, g_still_exp_ovr.load());
        ctrls.set(controls::AnalogueGain, g_still_gain_ovr.load());
    } else if (g_ae_locked.load()) {
        ctrls.set(controls::AeEnable, false);
        ctrls.set(controls::ExposureTime, g_ae_lock_exp.load());
        ctrls.set(controls::AnalogueGain, g_ae_lock_gain.load());
    } else {
        ctrls.set(controls::AeEnable, true);
    }
    logstep("setup: starting");
    wd_arm(20);                               /* camera->start() can hang */
    if (camera->start(&ctrls) < 0) { printf("optocam: start failed\n"); return false; }
    g_streaming.store(true);
    g_frame_wanted.store(true);               /* re-arm frame delivery */
    for (auto &r : requests) camera->queueRequest(r.get());
    logstep("setup: streaming");
    return true;
}

/* ---------- photo storage ---------- */
static bool data_ready(void) {
    struct stat st;
    if (stat("/data/photos", &st) == 0) return true;
    mount("/dev/mmcblk0p3", "/data", "ext4", MS_NOATIME, nullptr);  /* S90 may not have run yet */
    mkdir("/data/photos", 0755);
    return stat("/data/photos", &st) == 0;
}
static std::atomic<int> photo_counter(0);
static int scan_max_photo_number(void) {
    int maxn = 0;
    DIR *d = opendir("/data/photos");
    if (d) {
        struct dirent *e;
        while ((e = readdir(d))) {
            int n = 0;
            char ext_[8];
            if (sscanf(e->d_name, "Optocamzero_%d.%3s", &n, ext_) == 2 &&
                (!strcasecmp(ext_, "jpg") || !strcasecmp(ext_, "gif")) && n > maxn) maxn = n;
        }
        closedir(d);
    }
    return maxn;
}
static void seed_photo_counter_if_needed(void) {
    if (photo_counter.load() == 0) {
        int maxn = scan_max_photo_number();
        if (photo_counter.load() == 0) photo_counter.store(maxn);
    }
}
static int next_photo_number(void) {
    seed_photo_counter_if_needed();             /* first capture fallback */
    return photo_counter.fetch_add(1) + 1;
}
static void jpeg_save_thumb_240(const char *src_path, const Img &img);
static bool jpeg_decode_240(const char *path, Img &out);

/* JPEG-encode `rgb` (libcamera BGR memory order, FSTRIDE) rotated 90° to match
 * the display orientation, q92, then fsync file + directory. */
static bool save_jpeg(const uint8_t *rgb, unsigned W, unsigned H, unsigned stride, int number) {
    char tmp[64], fin[64];
    snprintf(tmp, sizeof tmp, "/data/photos/.tmp_%d.jpg", number);
    snprintf(fin, sizeof fin, "/data/photos/Optocamzero_%d.jpg", number);
    FILE *f = fopen(tmp, "wb");
    if (!f) { perror("fopen photo"); return false; }

    struct jpeg_compress_struct ci; struct jpeg_error_mgr jerr;
    ci.err = jpeg_std_error(&jerr);
    jpeg_create_compress(&ci);
    jpeg_stdio_dest(&ci, f);
    ci.image_width = H; ci.image_height = W;      /* rotated: WxH swap */
    ci.input_components = 3; ci.in_color_space = JCS_RGB;
    jpeg_set_defaults(&ci);
    jpeg_set_quality(&ci, 98, TRUE);
    jpeg_start_compress(&ci, TRUE);

    std::vector<uint8_t> row(H * 3);
    while (ci.next_scanline < ci.image_height) {
        unsigned y = ci.next_scanline;
        /* same mapping as display_blit: out(x,y) = src(row = x, col = W-1-y) */
        unsigned scol = W - 1 - y;
        for (unsigned x = 0; x < H; x++) {
            const uint8_t *px = rgb + (size_t)x * stride + (size_t)scol * 3;
            row[x*3] = px[0]; row[x*3+1] = px[1]; row[x*3+2] = px[2];  /* already RGB */
        }
        JSAMPROW rp = row.data();
        jpeg_write_scanlines(&ci, &rp, 1);
    }
    jpeg_finish_compress(&ci);
    jpeg_destroy_compress(&ci);
    fflush(f); fsync(fileno(f)); fclose(f);
    Img thumb;
    if (jpeg_decode_240(tmp, thumb)) jpeg_save_thumb_240(fin, thumb);
    rename(tmp, fin);
    int dfd = open("/data/photos", O_RDONLY);     /* persist the rename too */
    if (dfd >= 0) { fsync(dfd); close(dfd); }
    printf("optocam: saved %s\n", fin); fflush(stdout);
    return true;
}

static void do_capture(std::unique_ptr<CameraConfiguration> &previewCfg,
                       std::unique_ptr<CameraConfiguration> &stillCfg) {
    /* no flash — the original app freezes the last preview frame during capture */
    logstep("capture: begin");
    if (!data_ready()) printf("optocam: /data not available\n");
    g_streaming.store(false);
    logstep("capture: stopping preview");
    camera->stop();
    logstep("capture: preview stopped");

    g_mode.store(1);
    { std::lock_guard<std::mutex> lk(still_mtx); still_data = nullptr; }
    /* The preview already runs continuous AF. A one-shot AF re-sweep here adds
     * ~0.5s to the frozen-frame capture path right after boot, before preview
     * metadata has reported "focused". Take the first still frame instead. */
    bool af_ready = true;
    g_af_state.store(0);
    g_still_take.store(af_ready);
    g_af_trig.store(!af_ready);
    /* Manual still via still_exposure_from() — the same transform the HUD
     * readout displays, so the shot matches the numbers on screen. Applies to
     * the AE-locked case too (locked values are preview-mode values and need
     * the same mode-ratio translation). */
    g_still_exp_ovr.store(0);
    {
        int32_t pexp; float pgain;
        if (g_ae_locked.load()) {
            pexp = g_ae_lock_exp.load(); pgain = g_ae_lock_gain.load();
        } else {
            std::lock_guard<std::mutex> lk(g_frame_mtx);
            pexp = g_meta_exp; pgain = g_meta_gain;
        }
        if (pexp > 0 && pgain > 0) {
            int32_t e2; float g2;
            still_exposure_from(pexp, pgain, e2, g2);
            g_still_exp_ovr.store(e2);
            g_still_gain_ovr.store(g2);
        }
    }
    {   /* diagnostics: preview brightness at press, to compare with the still */
        std::lock_guard<std::mutex> lk(g_frame_mtx);
        uint64_t sum = 0; unsigned n = 0;
        for (size_t i = 0; i + 2 < (size_t)FH * FSTRIDE; i += 997 * 3) {   /* ~stride-blind sample */
            sum += g_frame_copy[i] + g_frame_copy[i+1] + g_frame_copy[i+2]; n += 3;
        }
        if (n) printf("optocam: PREVMEAN=%llu (exp=%d ag=%.2f)\n",
                      (unsigned long long)(sum / n), g_meta_exp, g_meta_gain);
        fflush(stdout);
    }
    if (setup_stream(stillCfg.get(), false)) {
        if (!af_ready) {
            double af_t0 = now_s();
            while (now_s() - af_t0 < 0.5) {
                int st_ = g_af_state.load();
                if (st_ == controls::AfStateFocused || st_ == controls::AfStateFailed) break;
                usleep(30000);
            }
            { std::lock_guard<std::mutex> lk(still_mtx); still_data = nullptr; }
            g_still_take.store(true);         /* next completed frame is the shot */
        }
        /* wait under the lock, but ENCODE and stop() outside it — the encode
         * takes seconds and stop() joins the callback thread */
        const uint8_t *frame = nullptr;
        {
            std::unique_lock<std::mutex> lk(still_mtx);
            if (still_cv.wait_for(lk, std::chrono::seconds(3), []{ return still_data != nullptr; }))
                frame = still_data;
        }
        logstep("capture: frame wait done");
        uint8_t *copy = nullptr;
        unsigned w = FW, h = FH, st = FSTRIDE;
        if (frame) {                     /* ~20MB heap copy (~0.1s) frees the CMA
                                          * buffer so the preview can restart NOW;
                                          * the slow JPEG encode runs detached —
                                          * same structure as the Python app */
            copy = new (std::nothrow) uint8_t[(size_t)h * st];
            if (copy) memcpy(copy, frame, (size_t)h * st);
            if (copy) {   /* diagnostics: still brightness vs PREVMEAN above */
                uint64_t sum = 0; unsigned n = 0;
                for (size_t i = 0; i + 2 < (size_t)h * st; i += 9973 * 3) {
                    sum += copy[i] + copy[i+1] + copy[i+2]; n += 3;
                }
                if (n) printf("optocam: STILLMEAN=%llu\n", (unsigned long long)(sum / n));
                fflush(stdout);
            }
        } else printf("optocam: still capture timed out\n");
        g_streaming.store(false);
        camera->stop();
        logstep("capture: still stopped");
        if (copy) {
            int num = next_photo_number();
            int fidx = g_filter_idx.load();       /* filter frozen at capture, like the app */
            g_saving.fetch_add(1);
            std::thread([copy, w, h, st, num, fidx] {
                /* convert BGR→RGB in place, filter WITH grain, then encode */
                for (size_t i = 0; i < (size_t)h * st; i += 3) std::swap(copy[i], copy[i + 2]);
                wb_apply(copy, (size_t)h * (st / 3), false);
                filter_apply(copy, (size_t)h * (st / 3), fidx);
                grain_apply(copy, st / 3, h, fidx >= CUSTOM_BASE ? g_custom_grain[fidx - CUSTOM_BASE] : GRAIN[fidx]);
                save_jpeg(copy, w, h, st, num);
                delete[] copy;
                g_saving.fetch_sub(1);
            }).detach();
        }
    }
    g_mode.store(0);
    g_still_exp_ovr.store(0);                      /* preview resumes on AE */
    setup_stream(previewCfg.get(), true);          /* back to live preview */
    logstep("capture: preview live again");
}

static void img_to_display(Img &frame) {
    int v = fb_variant();
    for (int i = 0; i < 240; i++) {
        uint8_t *out = fb565 + i * 240 * 2;
        for (int j = 0; j < 240; j++) {
            int x, y; fb_map(v, i, j, x, y);
            const uint8_t *o = frame.at(x, y);
            uint16_t c = px565(o[0], o[1], o[2]);
            out[j*2] = c >> 8; out[j*2+1] = c & 0xFF;
        }
    }
    fb_flush();
}

/* ---------- GIF recording (exact port of record_gif) ---------- */
static void jpeg_save_thumb_240(const char *src_path, const Img &img);
static Img &rec_number_sprite(const std::string &num) {   /* font 49, blur 5, cached */
    static std::map<std::string, Img> cache;
    auto it = cache.find(num);
    if (it != cache.end()) return it->second;
    Font *f = g_te.font(49);
    int b0,b1,b2,b3; g_te.textbbox(f, num, b0,b1,b2,b3);
    int nx = (240-(b2-b0))/2 - b0, ny = (240-(b3-b1))/2 - b1;
    Img sh(240,240), wh(240,240);
    g_te.draw(sh, f, nx, ny, num, 0,0,0,250);
    gaussian_blur_roi(sh, 5);
    g_te.draw(wh, f, nx, ny, num, 255,255,255,255);
    alpha_composite(sh, wh);
    return cache.emplace(num, std::move(sh)).first->second;
}
static void draw_rec_overlay(Img &img, const std::string &count, double age) {
    static const uint8_t RED[4] = {235,45,45,255};
    for (int t = 0; t < 3; t++)                        /* red border 3px (235,45,45) */
        for (int x = 0; x < 240; x++) {
            memcpy(img.at(x, t), RED, 4);
            memcpy(img.at(x, 239-t), RED, 4);
            memcpy(img.at(t, x), RED, 4);
            memcpy(img.at(239-t, x), RED, 4);
        }
    Img &layer = rec_number_sprite(count);
    if (age < 0.13) {                                  /* pop 0.95→1.0 */
        float t = (float)(age / 0.13), u = 1 - t;
        composite_scaled_centre(img, layer, 0.95f + 0.05f*(1 - u*u*u), 1.0f);
    } else alpha_composite(img, layer);
}
/* rotate90 + filter (no grain) a full 640 frame from the handoff buffer */
static void gif_grab_frame(const uint8_t *src, unsigned stride, uint8_t *dst, int fidx) {
    for (int y = 0; y < 640; y++)
        for (int x = 0; x < 640; x++) {
            const uint8_t *px = src + (size_t)x * stride + (size_t)(639 - y) * 3;
            uint8_t *o = dst + ((size_t)y * 640 + x) * 3;
            o[0] = px[2]; o[1] = px[1]; o[2] = px[0];
        }
    filter_apply(dst, 640*640, fidx);
}
static void record_gif(std::unique_ptr<CameraConfiguration> &previewCfg,
                       std::unique_ptr<CameraConfiguration> &gifCfg) {
    /* frame count + inter-frame interval, overridable by the hotspot "GIF
     * Recording" panel via /data/.gifcfg ("FRAMES INTERVAL"). Bounds keep a bad
     * file from producing a 0-frame or minutes-long recording. */
    int GIF_FRAMES = 10; double GIF_IV = 0.5;
    { FILE *gc = fopen("/data/.gifcfg", "r");
      if (gc) { int fr; float iv;
          if (fscanf(gc, "%d %f", &fr, &iv) == 2 &&
              fr >= 2 && fr <= 60 && iv >= 0.1f && iv <= 5.0f) { GIF_FRAMES = fr; GIF_IV = iv; }
          fclose(gc); } }
    int fidx = g_filter_idx.load();
    g_streaming.store(false);
    g_ae_locked.store(false);
    camera->stop();
    g_ctrl_dirty.store(true);                          /* re-apply ISP + AWB on the 640 stream */
    if (!setup_stream(gifCfg.get(), true)) { g_ae_locked.store(false); setup_stream(previewCfg.get(), true); return; }

    std::vector<std::vector<uint8_t>> frames;          /* full 640 RGB, filtered */
    bool cancelled = false;
    uint64_t seen = g_frame_seq.load();
    std::string rec_label; double rec_label_start = 0;
    static std::vector<uint8_t> local(sizeof g_frame_copy);
    int32_t local_exp = 0; float local_gain = 0;
    uint8_t bprev = buttons_read();
    char first_lbl[12]; snprintf(first_lbl, sizeof first_lbl, "1/%d", GIF_FRAMES);  /* pre-roll counter */

    auto live_view = [&](const char *lbl) {            /* rotate+scale+filter+overlay */
        double nw = now_s();
        if (rec_label != lbl) { rec_label = lbl; rec_label_start = nw; }
        Img img(240, 240);
        FilterLUTs &lut = g_luts[fidx];
        for (int y = 0; y < 240; y++)
            for (int x = 0; x < 240; x++) {
                int sx = (int)((239 - y) * 640 / 240), sy = (int)(x * 640 / 240);
                const uint8_t *px = local.data() + (size_t)sy * FSTRIDE + (size_t)sx * 3;
                uint8_t r = px[2], g = px[1], b = px[0];
                if (lut.trix) { unsigned lu=(r*299u+g*587u+b*114u+500u)/1000u; r=g_trix_lut[lu][0]; g=g_trix_lut[lu][1]; b=g_trix_lut[lu][2]; }
                else if (lut.cutout) { r=g=b=g_cutout_lut[(r*299u+g*587u+b*114u)/1000u]; }
                else if (!lut.identity) { r=lut.r[r]; g=lut.g[g]; b=lut.b[b]; }
                uint8_t *o = img.at(x, y); o[0]=r; o[1]=g; o[2]=b; o[3]=255;
            }
        std::lock_guard<std::mutex> rk(g_render_mtx);
        draw_rec_overlay(img, rec_label, nw - rec_label_start);
        img_to_display(img);
    };
    auto pump = [&]() -> bool {                        /* new frame into `local`? */
        uint64_t sq = g_frame_seq.load();
        if (sq == seen) return false;
        seen = sq;
        {
            std::lock_guard<std::mutex> lk(g_frame_mtx);
            memcpy(local.data(), g_frame_copy, sizeof g_frame_copy);
            local_exp = g_meta_exp;
            local_gain = g_meta_gain;
        }
        g_frame_wanted.store(true);           /* re-arm for the next frame */
        wb_apply(local.data(), (size_t)FH * (FSTRIDE / 3), true);
        return true;
    };
    auto check_cancel = [&]() {
        uint8_t c = buttons_read();
        if ((c & ~bprev) & (1 << BTN_CAPTURE)) cancelled = true;
        bprev = c;
    };

    double preroll_end = now_s() + 0.3;                /* pre-roll on "1/10" */
    while (now_s() < preroll_end && !cancelled) {
        check_cancel();
        if (pump()) live_view(first_lbl);
        usleep(10000);
    }
    if (!cancelled) {
        int32_t lock_exp;
        float lock_gain;
        {
            std::lock_guard<std::mutex> lk(g_frame_mtx);
            lock_exp = g_meta_exp;
            lock_gain = g_meta_gain;
        }
        if (lock_exp < 100) lock_exp = 100;
        if (lock_gain < 1.0f) lock_gain = 1.0f;
        float gcap = capture_max_gain();      /* same ISO ceiling as photos */
        if (lock_gain > gcap) {
            double scaled_exp = (double)lock_exp * ((double)lock_gain / gcap);
            lock_exp = (int32_t)std::min<double>(GIF_MAX_EXP_US, scaled_exp);
            lock_gain = gcap;
        }
        g_ae_lock_exp.store(lock_exp);
        g_ae_lock_gain.store(lock_gain);
        g_ae_locked.store(true);
        g_ctrl_dirty.store(true);

        int locked_frames = 0;
        double lock_wait_end = now_s() + 1.20;
        while (now_s() < lock_wait_end && locked_frames < 2 && !cancelled) {
            check_cancel();
            if (pump()) {
                int32_t ed = local_exp > lock_exp ? local_exp - lock_exp : lock_exp - local_exp;
                float gd = local_gain > lock_gain ? local_gain - lock_gain : lock_gain - local_gain;
                int32_t etol = std::max<int32_t>(200, lock_exp / 20);
                float gtol = std::max(0.05f, lock_gain * 0.05f);
                locked_frames = (ed <= etol && gd <= gtol) ? locked_frames + 1 : 0;
                live_view(first_lbl);
            }
            usleep(10000);
        }
    }
    double rec_start = now_s();
    for (int i = 0; i < GIF_FRAMES && !cancelled; i++) {
        double target = rec_start + i * GIF_IV;
        /* counter = frames captured so far, so each number holds for one full
         * interval. (Python showed the upcoming frame — at long intervals that
         * made 1→2 flip instantly while every later step took an interval.) */
        char lbl[12]; snprintf(lbl, sizeof lbl, "%d/%d", std::max(i, 1), GIF_FRAMES);
        while (!cancelled) {
            check_cancel();
            bool got = pump();
            double nw = now_s();
            if (got) live_view(lbl);
            if (nw >= target && got) break;             /* grab a fresh post-target frame */
            usleep(10000);
        }
        if (!cancelled) {                              /* grab the actual GIF frame */
            frames.emplace_back(640*640*3);
            gif_grab_frame(local.data(), FSTRIDE, frames.back().data(), fidx);
        }
    }
    if (!cancelled) {                                  /* let the counter land on N/N */
        char lbl[12]; snprintf(lbl, sizeof lbl, "%d/%d", GIF_FRAMES, GIF_FRAMES);
        double hold_end = now_s() + 0.35;
        while (now_s() < hold_end) {
            if (pump()) live_view(lbl);
            usleep(10000);
        }
    }
    g_streaming.store(false);
    g_ae_locked.store(false);
    g_ctrl_dirty.store(true);
    camera->stop();

    if (!cancelled && !frames.empty()) {
        int num = next_photo_number();
        g_saving.fetch_add(1);
        std::thread([fr = std::move(frames), num, iv_ms = (int)(GIF_IV * 1000 + 0.5)] {
            char path[64], tmp[64];
            snprintf(path, sizeof path, "/data/photos/Optocamzero_%d.gif", num);
            snprintf(tmp, sizeof tmp, "/data/photos/.tmp_%d.gif", num);
            GifPalette pal = gif_build_palette(fr[0].data(), 640*640);
            GifQuant q(pal);
            std::vector<std::vector<uint8_t>> qf;
            for (auto &f : fr) {
                qf.emplace_back(640*640);
                for (int i = 0; i < 640*640; i++)
                    qf.back()[i] = q.map(f[i*3], f[i*3+1], f[i*3+2]);
            }
            /* write to a temp name, rename when complete — a power cut mid-
             * encode can never leave a truncated file at the final name */
            gif_write(tmp, 640, 640, pal, qf, iv_ms);
            struct stat st_;
            if (stat(tmp, &st_) == 0 && st_.st_size < 1000) unlink(tmp);
            else {
                rename(tmp, path);
                Img first(640, 640);
                for (int i = 0; i < 640*640; i++) {
                    uint8_t *o = &first.px[(size_t)i*4];
                    o[0] = fr[0][i*3]; o[1] = fr[0][i*3+1]; o[2] = fr[0][i*3+2]; o[3] = 255;
                }
                Img thumb = resize_bilinear(first, 240, 240);
                jpeg_save_thumb_240(path, thumb);
            }
            printf("optocam: saved %s\n", path); fflush(stdout);
            g_saving.fetch_sub(1);
        }).detach();
    }
    g_mode.store(0);
    setup_stream(previewCfg.get(), true);              /* preview rebuilds at 240 */
}

/* ---------- gallery (exact port: counter overlay, delete confirm) ---------- */
#include <sys/statvfs.h>
static std::vector<std::string> gallery_files;
static int cap_number_of(const char *name) {
    int n; char ext[8];
    if (sscanf(name, "Optocamzero_%d.%4s", &n, ext) == 2 &&
        (!strcasecmp(ext, "jpg") || !strcasecmp(ext, "gif"))) return n;
    return -1;
}
static void gallery_scan() {
    gallery_files.clear();
    data_ready();                                      /* /data mounts in background at boot */
    DIR *d = opendir("/data/photos");
    if (!d) return;
    struct dirent *e;
    std::vector<std::pair<int,std::string>> v;
    while ((e = readdir(d))) {
        int n = cap_number_of(e->d_name);
        if (n >= 0) v.push_back({n, std::string("/data/photos/") + e->d_name});
    }
    closedir(d);
    std::sort(v.begin(), v.end());
    for (auto &pr : v) gallery_files.push_back(pr.second);
}
/* decode a JPEG scaled near 240 (libjpeg scale_denom = PIL draft mode) */
struct jerr_jmp { struct jpeg_error_mgr mgr; jmp_buf jb; };
static void jerr_exit(j_common_ptr ci) { longjmp(((jerr_jmp *)ci->err)->jb, 1); }
static bool jpeg_decode_240(const char *path, Img &out) {
    FILE *f = fopen(path, "rb");
    if (!f) return false;
    struct jpeg_decompress_struct ci; jerr_jmp jerr;
    ci.err = jpeg_std_error(&jerr.mgr);
    jerr.mgr.error_exit = jerr_exit;                   /* default handler exit()s the app! */
    if (setjmp(jerr.jb)) { jpeg_destroy_decompress(&ci); fclose(f); return false; }
    jpeg_create_decompress(&ci);
    jpeg_stdio_src(&ci, f);
    if (jpeg_read_header(&ci, TRUE) != JPEG_HEADER_OK) { jpeg_destroy_decompress(&ci); fclose(f); return false; }
    ci.scale_num = 1; ci.scale_denom = 8;               /* 2592/8 = 324 ≥ 240 */
    while (ci.scale_denom > 1 && (ci.image_width / ci.scale_denom) < 240) ci.scale_denom /= 2;
    ci.out_color_space = JCS_RGB;
    ci.dct_method = JDCT_IFAST;
    ci.do_fancy_upsampling = FALSE;
    jpeg_start_decompress(&ci);
    Img dec(ci.output_width, ci.output_height);
    std::vector<uint8_t> row(ci.output_width * 3);
    for (unsigned y = 0; y < ci.output_height; y++) {
        JSAMPROW rp = row.data();
        jpeg_read_scanlines(&ci, &rp, 1);
        uint8_t *o = dec.at(0, y);
        for (unsigned x = 0; x < ci.output_width; x++) {
            o[x*4] = row[x*3]; o[x*4+1] = row[x*3+1]; o[x*4+2] = row[x*3+2]; o[x*4+3] = 255;
        }
    }
    jpeg_finish_decompress(&ci); jpeg_destroy_decompress(&ci); fclose(f);
    out = resize_bilinear(dec, 240, 240);
    return true;
}
static std::string gallery_thumb_path(const char *path, int size) {
    const char *base = strrchr(path, '/');
    base = base ? base + 1 : path;
    return std::string("/data/photos/.thumbs/") + base + "_" + std::to_string(size) + ".jpg";
}
static void jpeg_save_thumb_240(const char *src_path, const Img &img) {
    mkdir("/data/photos/.thumbs", 0755);
    std::string fin = gallery_thumb_path(src_path, 240), tmp = fin + ".tmp";
    FILE *f = fopen(tmp.c_str(), "wb");
    if (!f) return;
    struct jpeg_compress_struct ci; struct jpeg_error_mgr jerr;
    ci.err = jpeg_std_error(&jerr);
    jpeg_create_compress(&ci);
    jpeg_stdio_dest(&ci, f);
    ci.image_width = 240; ci.image_height = 240;
    ci.input_components = 3; ci.in_color_space = JCS_RGB;
    jpeg_set_defaults(&ci);
    jpeg_set_quality(&ci, 82, TRUE);
    jpeg_start_compress(&ci, TRUE);
    std::vector<uint8_t> row(240 * 3);
    while (ci.next_scanline < ci.image_height) {
        const uint8_t *p = img.at(0, ci.next_scanline);
        for (int x = 0; x < 240; x++) {
            row[x*3] = p[x*4]; row[x*3+1] = p[x*4+1]; row[x*3+2] = p[x*4+2];
        }
        JSAMPROW rp = row.data();
        jpeg_write_scanlines(&ci, &rp, 1);
    }
    jpeg_finish_compress(&ci);
    jpeg_destroy_compress(&ci);
    fflush(f); fsync(fileno(f)); fclose(f);
    rename(tmp.c_str(), fin.c_str());
}
static bool jpeg_load_240(const char *path, Img &out) {
    std::string tp = gallery_thumb_path(path, 240);
    if (jpeg_decode_240(tp.c_str(), out)) return true;
    if (!jpeg_decode_240(path, out)) return false;
    jpeg_save_thumb_240(path, out);
    return true;
}
static bool gif_thumb_load_240(const char *path, Img &out) {
    std::string tp = gallery_thumb_path(path, 240);
    if (jpeg_decode_240(tp.c_str(), out)) return true;
    tp = gallery_thumb_path(path, 400);
    return jpeg_decode_240(tp.c_str(), out);
}
/* gif gallery playback: frames pre-composited at 240 with the exact overlays
 * ("GIF" top-left + position counter bottom-left blurred; frame counter
 * top-right with the cheap +1px grey offset shadow, font 22) */
static std::vector<Img> g_gifanim;
static std::mutex g_gif_mtx;
static std::atomic<int> g_gif_token(0);
static std::string g_gifanim_file;
static int g_gifanim_idx = 0; static double g_gifanim_last = 0;
static double g_gifanim_iv = 0.5;   /* frame delay from the file's own GCE */
static void gif_frames_build(const std::string &path, int index, int total, int token, int upto);
static void gif_gallery_load(const std::string &path, int index, int total) {
    { std::lock_guard<std::mutex> gk(g_gif_mtx); g_gifanim.clear(); }
    g_gifanim_file = path; g_gifanim_idx = 0; g_gifanim_last = now_s();
    int dly = gif_first_delay_ms(path.c_str());
    g_gifanim_iv = dly >= 20 ? dly / 1000.0 : 0.5;
    int token = g_gif_token.fetch_add(1) + 1;
    static std::atomic<bool> busy(false);
    std::thread([path, index, total, token] {            /* rest in background */
        usleep(180000);                                  /* user may be scrubbing past */
        if (g_gif_token.load() != token) return;
        while (busy.exchange(true)) {                    /* single decode at a time */
            usleep(50000);
            if (g_gif_token.load() != token) return;
        }
        if (g_gif_token.load() == token) {
            gif_frames_build(path, index, total, token, 1);
            if (g_gif_token.load() == token) g_gifanim_last = now_s();
            gif_frames_build(path, index, total, token, 64);
        }
        busy.store(false);
    }).detach();
}
static void gif_frames_build(const std::string &path, int index, int total, int token, int upto) {
    int w, h; std::vector<std::vector<uint8_t>> raw;
    if (!gif_decode(path.c_str(), w, h, raw, upto)) return;
    if (g_gif_token.load() != token) return;             /* user moved on */
    std::lock_guard<std::mutex> rk(g_render_mtx);
    Img ovl(240,240);                                  /* static overlay */
    Font *f25 = g_te.font(25);
    {
        Img sh(240,240), wh(240,240);
        int b0,b1,b2,b3; g_te.textbbox(f25, "GIF", b0,b1,b2,b3);
        g_te.draw(sh, f25, 15, 15-b1, "GIF", 0,0,0,250);
        g_te.draw(wh, f25, 15, 15-b1, "GIF", 255,255,255,255);
        char pos[16]; snprintf(pos, sizeof pos, "%d/%d", index, total);
        g_te.textbbox(f25, pos, b0,b1,b2,b3);
        g_te.draw(sh, f25, 15, 240-15-(b3-b1), pos, 0,0,0,250);
        g_te.draw(wh, f25, 15, 240-15-(b3-b1), pos, 255,255,255,255);
        gaussian_blur_roi(sh, 4);
        alpha_composite(sh, wh);
        ovl = std::move(sh);
    }
    Font *f22 = g_te.font(22);
    int n = gif_frame_count(path.c_str());             /* true total, like PIL n_frames */
    if (n < (int)raw.size()) n = (int)raw.size();
    std::vector<Img> built;
    int nb = (int)raw.size();
    for (int i = 0; i < nb; i++) {
        Img big(w, h);
        for (int j = 0; j < w*h; j++) {
            uint8_t *o = &big.px[(size_t)j*4];
            o[0]=raw[i][j*3]; o[1]=raw[i][j*3+1]; o[2]=raw[i][j*3+2]; o[3]=255;
        }
        Img fr = resize_bilinear(big, 240, 240);
        alpha_composite(fr, ovl);
        char fc[16]; snprintf(fc, sizeof fc, "%d/%d", i+1, n);
        int b0,b1,b2,b3; g_te.textbbox(f22, fc, b0,b1,b2,b3);
        int x = 240-15-b2, y = 15-b1;
        g_te.draw(fr, f22, x+1, y+1, fc, 60,60,60);
        g_te.draw(fr, f22, x, y, fc, 255,255,255);
        /* publish incrementally: playback starts as soon as frame 2 exists */
        {
            std::lock_guard<std::mutex> gk(g_gif_mtx);
            if (g_gif_token.load() != token) return;
            if ((int)g_gifanim.size() == i) g_gifanim.push_back(std::move(fr));
        }
        if (g_gif_token.load() != token) return;
    }
    (void)built;
}
/* decoded-photo LRU (RAM only) + neighbor prefetch = instant gallery nav */
static std::mutex g_gcache_mtx;
static std::vector<std::pair<std::string, Img>> g_gcache;   /* newest at back */
static bool gcache_get(const std::string &k, Img &out) {
    std::lock_guard<std::mutex> lk(g_gcache_mtx);
    for (auto it = g_gcache.begin(); it != g_gcache.end(); ++it)
        if (it->first == k) {
            out = it->second;
            auto e = *it;                       /* real LRU: touched entries stay hot */
            g_gcache.erase(it);
            g_gcache.push_back(std::move(e));
            return true;
        }
    return false;
}
static void gcache_put(const std::string &k, const Img &img) {
    std::lock_guard<std::mutex> lk(g_gcache_mtx);
    for (auto &e : g_gcache) if (e.first == k) return;
    g_gcache.push_back({k, img});
    if (g_gcache.size() > 20) g_gcache.erase(g_gcache.begin());
}
static void gcache_drop(const std::string &k) {
    std::lock_guard<std::mutex> lk(g_gcache_mtx);
    for (auto it = g_gcache.begin(); it != g_gcache.end(); ++it)
        if (it->first == k) { g_gcache.erase(it); return; }
}
static bool photo_load_cached(const std::string &path, Img &out) {
    if (gcache_get(path, out)) return true;
    if (!jpeg_load_240(path.c_str(), out)) return false;
    gcache_put(path, out);
    return true;
}
static void warm_initial_gallery_cache(void) {
    data_ready();
    seed_photo_counter_if_needed();
    DIR *d = opendir("/data/photos");
    if (!d) return;
    std::vector<std::pair<int,std::string>> v;
    struct dirent *e;
    while ((e = readdir(d))) {
        int n = cap_number_of(e->d_name);
        if (n >= 0) v.push_back({n, std::string("/data/photos/") + e->d_name});
    }
    closedir(d);
    if (v.empty()) return;
    std::sort(v.begin(), v.end());
    Img tmp;
    const std::string &latest = v.back().second;
    if (latest.size() > 4 && !strcasecmp(latest.c_str() + latest.size() - 4, ".gif"))
        gif_thumb_load_240(latest.c_str(), tmp);
    else
        photo_load_cached(latest, tmp);
}
static void render_gallery_frame(Img img, int index, int total, bool confirm) {
    char cnt[16]; snprintf(cnt, sizeof cnt, "%d/%d", index, total);
    Font *f25 = g_te.font(25);
    int b0,b1,b2,b3; g_te.textbbox(f25, cnt, b0,b1,b2,b3);
    int x = 15, y = 240 - 15 - (b3 - b1);
    std::lock_guard<std::mutex> rk(g_render_mtx);
    alpha_composite(img, cached_shadow("gcnt", cnt, 25, x, y));
    g_te.draw(img, f25, x, y, cnt, 255,255,255);
    if (confirm) {                                       /* exact dialog layout */
        for (int i = 0; i < 240*240; i++) {              /* black 160-alpha overlay */
            uint8_t *px = &img.px[i*4];
            for (int c = 0; c < 3; c++) px[c] = px[c] * (255-160) / 255;
        }
        int c0,c1,c2,c3;
        g_te.textbbox(f25, "Delete?", c0,c1,c2,c3);
        g_te.draw(img, f25, (240-(c2-c0))/2, 76, "Delete?", 255,255,255);
        /* Colon gap = 80% of a space advance, shared by both rows so the
         * "YES:"->arrow gap matches the "NO:"->"Any Button" gap. */
        int s0,s1,s2,s3, e0,e1,e2,e3;
        g_te.textbbox(f25, "N N", s0,s1,s2,s3);
        g_te.textbbox(f25, "NN",  e0,e1,e2,e3);
        int gap = (int)std::lround(0.8 * ((s2-s0) - (e2-e0)));
        const int AW = 19, AH = 11;                       /* arrow width / height */
        g_te.textbbox(f25, "YES:", c0,c1,c2,c3);
        int tw = c2-c0, th = c3-c1;
        int total = tw + gap + AW, bx = (240-total)/2, by = 114;
        g_te.draw(img, f25, bx, by, "YES:", 255,255,255);
        int mid = by + (c1+c3)/2 - 1;                     /* text centre; arrow nudged up 1px */
        /* Up-chevron (matches the hotspot web UI's stroked arrows): anti-aliased
         * round-capped strokes, centred on the text. */
        float lx = (float)(bx + tw + gap);
        float apx = lx + AW/2.0f, topy = (float)mid - AH/2.0f, boty = (float)mid + AH/2.0f;
        draw_line_aa(img, lx,      boty, apx, topy, 2.4f, 255,255,255,255);
        draw_line_aa(img, lx + AW, boty, apx, topy, 2.4f, 255,255,255,255);
        /* "NO:" + "Any Button" drawn as two pieces so the colon gap matches. */
        int n0,n1,n2,n3, y0,y1,y2,y3;
        g_te.textbbox(f25, "NO:", n0,n1,n2,n3);
        g_te.textbbox(f25, "Any Button", y0,y1,y2,y3);
        int wNO = n2-n0, wAny = y2-y0, ntot = wNO + gap + wAny;
        int nx = (240-ntot)/2, ny = by+th+10;
        g_te.draw(img, f25, nx, ny, "NO:", 180,180,180);
        g_te.draw(img, f25, nx + wNO + gap, ny, "Any Button", 180,180,180);
    }
    img_to_display(img);
}
static void render_gif_thumb_frame(Img img, int index, int total, int nframes) {
    Font *f25 = g_te.font(25);
    Font *f22 = g_te.font(22);
    std::lock_guard<std::mutex> rk(g_render_mtx);

    int b0,b1,b2,b3;
    g_te.textbbox(f25, "GIF", b0,b1,b2,b3);
    int gx = 15, gy = 15 - b1;
    alpha_composite(img, cached_shadow("giflbl", "GIF", 25, gx, gy));
    g_te.draw(img, f25, gx, gy, "GIF", 255,255,255);

    char pos[16]; snprintf(pos, sizeof pos, "%d/%d", index, total);
    g_te.textbbox(f25, pos, b0,b1,b2,b3);
    int px = 15, py = 240 - 15 - (b3 - b1);
    alpha_composite(img, cached_shadow("gifpos", pos, 25, px, py));
    g_te.draw(img, f25, px, py, pos, 255,255,255);

    char fc[16]; snprintf(fc, sizeof fc, "1/%d", nframes > 0 ? nframes : 10);
    g_te.textbbox(f22, fc, b0,b1,b2,b3);
    int fx = 240 - 15 - b2, fy = 15 - b1;
    g_te.draw(img, f22, fx+1, fy+1, fc, 60,60,60);
    g_te.draw(img, f22, fx, fy, fc, 255,255,255);

    img_to_display(img);
}
static void prefetch_neighbors(const std::vector<std::string> &files, int idx) {
    static std::mutex mtx;
    static std::condition_variable cv;
    static std::vector<std::string> pending;
    static uint64_t seq = 0;
    static std::atomic<bool> started(false);

    if (!started.exchange(true)) {
        std::thread([] {
            uint64_t seen = 0;
            for (;;) {
                std::vector<std::string> local;
                uint64_t myseq;
                {
                    std::unique_lock<std::mutex> lk(mtx);
                    cv.wait(lk, [&]{ return seq != seen; });
                    seen = seq;
                    myseq = seq;
                    local = pending;
                }
                for (auto &f : local) {
                    {
                        std::lock_guard<std::mutex> lk(mtx);
                        if (seq != myseq) break;        /* user moved: pivot to latest */
                    }
                    Img tmp;
                    photo_load_cached(f, tmp);
                }
            }
        }).detach();
    }

    std::vector<std::string> next;
    int n = (int)files.size();
    for (int d : {1, -1, 2, -2, 3, -3}) {
        int j = ((idx + d) % n + n) % n;
        if (j == idx) continue;
        const std::string &f = files[j];
        if (f.size() > 4 && strcasecmp(f.c_str() + f.size() - 4, ".gif") &&
            std::find(next.begin(), next.end(), f) == next.end())
            next.push_back(f);
    }
    {
        std::lock_guard<std::mutex> lk(mtx);
        pending = std::move(next);
        seq++;
    }
    cv.notify_one();
}
static void display_gallery_image(const std::string &path, int index, int total, bool confirm) {
    Img img;
    bool is_gif = path.size() > 4 && !strcasecmp(path.c_str() + path.size() - 4, ".gif");
    if (is_gif && !confirm) {
        gif_gallery_load(path, index, total);
        if (gif_thumb_load_240(path.c_str(), img)) {
            render_gif_thumb_frame(img, index, total, gif_frame_count(path.c_str()));
            return;
        }
        std::lock_guard<std::mutex> gk(g_gif_mtx);
        if (!g_gifanim.empty()) { img_to_display(g_gifanim[0]); return; }
    }
    bool have_gif_bg = false;
    if (is_gif && confirm) {
        have_gif_bg = gif_thumb_load_240(path.c_str(), img);
        if (!have_gif_bg) {
            std::lock_guard<std::mutex> gk(g_gif_mtx);
            if (g_gifanim_file == path && !g_gifanim.empty()) {
                int i = std::max(0, std::min(g_gifanim_idx, (int)g_gifanim.size() - 1));
                img = g_gifanim[i];
                have_gif_bg = true;
            }
        }
    }
    g_gif_token.fetch_add(1);
    { std::lock_guard<std::mutex> gk(g_gif_mtx); g_gifanim.clear(); }
    g_gifanim_file.clear();
    if (is_gif) {
        if (!have_gif_bg) img = Img(240,240);
    } else if (!photo_load_cached(path, img)) img = Img(240,240);
    render_gallery_frame(img, index, total, confirm);
}
static int transfer_station_count() {
    FILE *pp = popen("iw dev wlan0 station dump 2>/dev/null | grep -c \"^Station \"", "r");
    if (!pp) return 0;
    int n = 0; if (fscanf(pp, "%d", &n) != 1) n = 0;
    pclose(pp);
    return n;
}

/* ---- live view (transfer mode only): the hotspot Filter Maker's LIVE tab.
 * The web heartbeats /data/.live_active (like .calib_active); while it's
 * fresh the camera runs the 640x640 GIF stream — the one square mode above
 * preview size, and g_frame_copy is already sized for it — and unfiltered
 * WB-applied frames are JPEG-encoded into tmpfs for the server to poll.
 * The browser applies the filter itself (its preview math mirrors the
 * firmware), so slider moves cost nothing over the wire. */
#define LIVE_FLAG "/data/.live_active"
#define LIVE_JPG  "/run/optocam-live.jpg"
static bool live_flag_fresh(void) {
    struct stat st;
    if (stat(LIVE_FLAG, &st) != 0) return false;
    long age = (long)(time(NULL) - st.st_mtime);
    if (age >= 0 && age < 5) return true; /* negative = clock skew -> stale */
    unlink(LIVE_FLAG);                    /* phone gone without /live-close */
    return false;
}
/* Same 90° rotation + channel order as save_jpeg, but tmpfs + atomic rename
 * and no fsync (nothing to persist). q85: ~60-90KB per 640px frame. */
static void write_live_jpeg(const uint8_t *rgb, unsigned W, unsigned H, unsigned stride) {
    const char *tmp = LIVE_JPG ".tmp";
    FILE *f = fopen(tmp, "wb");
    if (!f) return;
    struct jpeg_compress_struct ci; struct jpeg_error_mgr jerr;
    ci.err = jpeg_std_error(&jerr);
    jpeg_create_compress(&ci);
    jpeg_stdio_dest(&ci, f);
    ci.image_width = H; ci.image_height = W;      /* rotated: WxH swap */
    ci.input_components = 3; ci.in_color_space = JCS_RGB;
    jpeg_set_defaults(&ci);
    jpeg_set_quality(&ci, 85, TRUE);
    jpeg_start_compress(&ci, TRUE);
    std::vector<uint8_t> row(H * 3);
    while (ci.next_scanline < ci.image_height) {
        unsigned y = ci.next_scanline;
        unsigned scol = W - 1 - y;                /* out(x,y) = src(row=x, col=W-1-y) */
        for (unsigned x = 0; x < H; x++) {
            const uint8_t *px = rgb + (size_t)x * stride + (size_t)scol * 3;
            /* preview/GIF stream is BGR (unlike the still stream save_jpeg
             * gets) — swap into the JPEG's RGB */
            row[x*3] = px[2]; row[x*3+1] = px[1]; row[x*3+2] = px[0];
        }
        JSAMPROW rp = row.data();
    jpeg_write_scanlines(&ci, &rp, 1);
    }
    jpeg_finish_compress(&ci);
    jpeg_destroy_compress(&ci);
    fclose(f);
    rename(tmp, LIVE_JPG);
}
static void draw_aa_dot(Img &img, int cx, int cy, float radius,
                        uint8_t r, uint8_t g, uint8_t b, uint8_t a) {
    int lx = (int)std::floor(cx - radius - 1), hx = (int)std::ceil(cx + radius + 1);
    int ly = (int)std::floor(cy - radius - 1), hy = (int)std::ceil(cy + radius + 1);
    for (int y = ly; y <= hy; y++)
        for (int x = lx; x <= hx; x++) {
            float dx = (x + 0.5f) - cx, dy = (y + 0.5f) - cy;
            float cov = std::clamp(radius + 0.5f - std::sqrt(dx * dx + dy * dy), 0.0f, 1.0f);
            if (cov > 0.0f) set_px(img, x, y, r, g, b, (uint8_t)(a * cov));
        }
}
/* Hotspot-screen palette, mirrors the web theme in /data/.theme so the on-device
 * transfer screen matches the phone UI. Re-read each 0.5s refresh, so changing
 * the theme on the phone recolours the LCD within half a second. */
struct TScol { uint8_t bg[3], title[3], label[3], value[3], accent[3], divider[3], hint[3]; };
static TScol transfer_theme() {
    TScol c = {{0,0,0},{160,160,160},{100,100,100},{255,255,255},
               {60,200,80},{40,40,40},{60,60,60}};              /* dark (default) */
    FILE *f = fopen("/data/.theme", "r");
    if (f) {
        char b[16] = {0};
        if (fscanf(f, "%15s", b) == 1) {
            if (!strcmp(b, "light"))
                c = {{244,242,234},{90,88,82},{132,127,116},{22,20,17},
                     {58,148,72},{206,201,190},{160,155,144}};
            else if (!strcmp(b, "yellow"))
                c = {{255,217,59},{70,60,0},{112,96,0},{20,18,4},
                     {0,153,102},{203,169,28},{124,106,0}};
        }
        fclose(f);
    }
    return c;
}
static void show_transfer_screen(int stations) {
    TScol c = transfer_theme();
    Img img(240, 240);
    for (size_t i = 0; i + 3 < img.px.size(); i += 4) {
        img.px[i] = c.bg[0]; img.px[i+1] = c.bg[1]; img.px[i+2] = c.bg[2]; img.px[i+3] = 255;
    }
    std::lock_guard<std::mutex> rk(g_render_mtx);
    Font *f17 = g_te.font(17), *f16 = g_te.font(16), *f20 = g_te.font(20), *f18 = g_te.font(18);
    double now = now_s();
    bool connected = stations > 0;
    bool dot_visible = connected || ((long)(now * 2) % 2 == 0);

    int b0,b1,b2,b3;
    g_te.textbbox(f17, "Transfer Mode", b0,b1,b2,b3);
    g_te.draw(img, f17, 20, 13, "Transfer Mode", c.title[0],c.title[1],c.title[2]);
    int dot_cy = 13 + (b1 + b3) / 2, dot_cx = 214, dot_r = 6;
    if (dot_visible) {
        const uint8_t *d = connected ? c.accent : c.label;
        draw_aa_dot(img, dot_cx, dot_cy, dot_r, d[0], d[1], d[2], 255);
    }
    if (connected) {                                    /* count left of the dot */
        char cnt[8]; snprintf(cnt, sizeof cnt, "%d", stations);
        int c0,c1,c2,c3; g_te.textbbox(f17, cnt, c0,c1,c2,c3);
        int cx = dot_cx - dot_r - (c2-c0) - 8;
        int cy = dot_cy - (c3-c1)/2 - c1;
        g_te.draw(img, f17, cx, cy, cnt, c.accent[0],c.accent[1],c.accent[2]);
    }
    draw_line(img, 0, 43, 239, 43, 1, c.divider[0],c.divider[1],c.divider[2],255);
    g_te.draw(img, f16, 20, 51,  "WiFi",         c.label[0],c.label[1],c.label[2]);
    g_te.draw(img, f20, 20, 69,  "Optocam Zero", c.value[0],c.value[1],c.value[2]);
    g_te.draw(img, f16, 20, 99,  "Password",     c.label[0],c.label[1],c.label[2]);
    g_te.draw(img, f20, 20, 117, "0026opto",     c.value[0],c.value[1],c.value[2]);
    g_te.draw(img, f16, 20, 147, "Browser",      c.label[0],c.label[1],c.label[2]);
    g_te.draw(img, f20, 20, 165, "192.168.4.1",  c.value[0],c.value[1],c.value[2]);
    draw_line(img, 0, 197, 239, 197, 1, c.divider[0],c.divider[1],c.divider[2],255);
    g_te.textbbox(f18, "Hold center to exit", b0,b1,b2,b3);
    int hint_y = 197 + (43 - (b3-b1)) / 2 - b1;
    g_te.draw(img, f18, (240-(b2-b0))/2, hint_y, "Hold center to exit", c.hint[0],c.hint[1],c.hint[2]);
    img_to_display(img);
}

static void show_splash_screen() {
    FILE *sf = fopen("/usr/share/optocam/splash.raw", "rb");
    if (sf) { size_t rd = fread(fb565, 1, sizeof fb565, sf); (void)rd; fclose(sf); }
    else memset(fb565, 0, sizeof fb565);
    fb_flush();
}
static bool disk_has_space() {                          /* < 20MB free → no-space toast */
    data_ready();                                       /* mount /data first — before it's
                                                         * mounted, statvfs sees the ro rootfs
                                                         * and reports a false "no space" */
    struct statvfs st;
    if (statvfs("/data/photos", &st) != 0) return true; /* can't judge → allow */
    return (uint64_t)st.f_bavail * st.f_frsize >= 20ull * 1024 * 1024;
}
static std::atomic<int> g_bl_mode(1);               /* 0 off, 1 full, 2 dim(8/255) */
/* persistent backlight event trace for the wake-then-black hunt: appends to
 * /data (survives power cycles). Events are rare (dim/wake/off), cost ~nothing. */
static void bl_trace(const char *ev) {
    FILE *f = fopen("/data/bl-trace", "a");
    if (!f) return;
    float up = 0; FILE *u = fopen("/proc/uptime", "r");
    if (u) { int n = fscanf(u, "%f", &up); (void)n; fclose(u); }
    fprintf(f, "%.2f %s\n", up, ev);
    fclose(f);
}
static void set_backlight(bool on) {
    bl_trace(on ? "set_backlight ON" : "set_backlight OFF");
    g_bl_mode.store(on ? 1 : 0); bl_set(on ? 1 : 0);
}
static void backlight_dim() { bl_trace("dim"); g_bl_mode.store(2); }
/* Effective backlight duty for the current mode: off 0, dim 8 (the shipped
 * idle-dim level), on = the calibrated screen brightness (255 = solid DC). */
static inline int bl_duty(void) {
    int m = g_bl_mode.load();
    return m == 0 ? 0 : m == 2 ? 8 : g_bl_level.load();
}
static void backlight_pwm_thread() {          /* soft-PWM whenever duty < 255 */
    /* Brightness = pulse WIDTH / period, and at the dim duty the pulse is only
     * 63µs — so scheduler wakeup latency (WiFi IRQs, the hotspot's python
     * server) used to stretch or clip it into visible brightness wander.
     * Strategy: absolute-deadline sleeps set each pulse's START (lateness only
     * shifts the pulse inside its period — invisible), and a short busy-spin
     * times the pulse's END, making the width exact on every cycle. Spin cost
     * is ≤150µs per 2ms cycle, and only while duty < 255 (dim / calibrated
     * backlight); the factory backlight is solid DC and costs nothing. */
    struct sched_param sp_; sp_.sched_priority = 80;
    pthread_setschedparam(pthread_self(), SCHED_FIFO, &sp_);
    const long PERIOD_NS = 2000000;           /* 500Hz, same as the original dim */
    /* Spin budget = worst wakeup lateness the pulse width can absorb. 150µs
     * was enough for an idle system but live-view JPEG encoding delays even a
     * FIFO thread by more — at the calib panel's mid duties that leaked into
     * pulse width as ~10Hz brightness pulsing (the encode rhythm). 400µs
     * covers it; cost is a sub-ms spin per 2ms cycle, and only while duty <
     * 255 (factory backlight is solid DC and never enters this path). */
    const long SPIN_NS   = 400000;
    auto ns_between = [](const struct timespec &a, const struct timespec &b) {
        return (b.tv_sec - a.tv_sec) * 1000000000L + (b.tv_nsec - a.tv_nsec); };
    auto add_ns = [](struct timespec &ts, long ns) {
        ts.tv_nsec += ns;
        while (ts.tv_nsec >= 1000000000) { ts.tv_nsec -= 1000000000; ts.tv_sec++; } };
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    for (;;) {
        int duty = bl_duty();
        if (duty > 0 && duty < 255) {
            /* stalled past a whole period: drop the missed cycles and resync —
             * racing to catch up would burst back-to-back pulses (a flash) */
            struct timespec nw; clock_gettime(CLOCK_MONOTONIC, &nw);
            if (ns_between(t, nw) > PERIOD_NS) t = nw;
            long on_ns = PERIOD_NS / 255 * duty;
            struct timespec s0; clock_gettime(CLOCK_MONOTONIC, &s0);
            bl_set(1);
            if (on_ns > SPIN_NS) {            /* long pulse: sleep the bulk... */
                struct timespec ps = s0; add_ns(ps, on_ns - SPIN_NS);
                clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &ps, nullptr);
            }
            for (;;) {                        /* ...and spin the tail: exact width */
                struct timespec s1; clock_gettime(CLOCK_MONOTONIC, &s1);
                if (ns_between(s0, s1) >= on_ns) break;
            }
            int d2 = bl_duty();               /* mode may have flipped mid-cycle */
            if (d2 > 0 && d2 < 255) bl_set(0);
            else bl_set(d2 ? 1 : 0);
            add_ns(t, PERIOD_NS);             /* sleep out the period */
            clock_nanosleep(CLOCK_MONOTONIC, TIMER_ABSTIME, &t, nullptr);
        } else {
            /* re-assert the level every tick: a wake press racing our
             * mode-check/gpio_set pair could leave the pin low with mode=1
             * (screen black, app alive — read as a "crash"); this heals any
             * lost update within 20ms */
            bl_set(duty ? 1 : 0);
            usleep(20000); clock_gettime(CLOCK_MONOTONIC, &t);
        }
    }
}

int main() {
    install_crash_handlers();
    logstep("main enter");
    g_madctl_legacy = access("/tmp/madctl-legacy", F_OK) == 0;
    build_565_luts();
    build_wb_trim();
    filters_init();
    if (!g_te.init("/usr/share/optocam/cmunvt.ttf")) printf("optocam: FONT INIT FAILED\n");
    logstep("luts+font ready");
    /* early-splash takeover: if S01 claimed the panel, wait for it (≤3s) and
     * take over WITHOUT reinitialising — splash stays perfectly still until
     * the first preview frame replaces it */
    bool soft = false;
    if (access("/tmp/splash-claimed", F_OK) == 0) {
        for (int t = 0; t < 150 && access("/tmp/display-up", F_OK) != 0; t++) usleep(20000);
        soft = access("/tmp/display-up", F_OK) == 0;
    }
    display_init(soft);
    if (!soft) {
        /* splash.raw (RGB565 blob) if present, else black */
        static uint8_t vis[240*240*2];
        FILE *sf = fopen("/usr/share/optocam/splash.raw", "rb");
        if (sf) { size_t rd = fread(vis, 1, sizeof vis, sf); (void)rd; fclose(sf); }
        else memset(vis, 0, sizeof vis);
        fb_store_565(vis);
        fb_flush();
        gpio_set(bl_fd, 1);      /* splash drawn -> light it: no white flash */
    }
    buttons_init();
    logstep("display up, starting camera");

    /* (page-cache warm moved to S01: it must start BEFORE our exec to overlap
     * the library loading, not after) */
    printf("optocam: waiting for imx708 sensor link...\n"); fflush(stdout);
    for (int t = 0; t < 600; t++) {                /* 20ms polls, up to 12s */
        /* S02's backgrounded modprobes can race/fail; retry them ourselves
         * rather than waiting on a sensor that will never appear */
        if (t == 200 || t == 400)
            system("modprobe i2c-bcm2835; modprobe bcm2835-unicam; "
                   "modprobe bcm2835-isp; modprobe imx708; modprobe dw9807-vcm");
        int found = 0;
        for (int m = 0; m < 8 && !found; m++) {
            char path[32]; snprintf(path, sizeof path, "/dev/media%d", m);
            int fd = open(path, O_RDONLY);
            if (fd < 0) continue;
            struct media_entity_desc e; memset(&e, 0, sizeof e);
            while (1) {
                e.id |= MEDIA_ENT_ID_FLAG_NEXT;
                if (ioctl(fd, MEDIA_IOC_ENUM_ENTITIES, &e) < 0) break;
                if (strstr(e.name, "imx708")) { found = 1; break; }
            }
            close(fd);
        }
        if (found) { printf("optocam: imx708 linked at ~%d ms\n", t * 20); fflush(stdout); break; }
        usleep(20000);
    }

    setenv("LIBCAMERA_LOG_LEVELS", "*:ERROR", 1);
    signal(SIGALRM, wd_timeout);
    wd_arm(25);            /* covers cm.start/acquire; setup_stream re-arms */
    logstep("cm.start begin");
    CameraManager cm; cm.start();
    logstep("cm.start done");
    for (int i = 0; i < 50 && cm.cameras().empty(); i++) usleep(100000);
    if (cm.cameras().empty()) { printf("optocam: NO CAMERAS\n"); return 1; }
    camera = cm.cameras()[0];
    camera->acquire();

    std::unique_ptr<CameraConfiguration> previewCfg =
        camera->generateConfiguration({ StreamRole::Viewfinder });
    previewCfg->at(0).size = Size(240, 240);
    previewCfg->at(0).pixelFormat = formats::RGB888;
    previewCfg->at(0).bufferCount = 4;

    std::unique_ptr<CameraConfiguration> stillCfg =
        camera->generateConfiguration({ StreamRole::StillCapture });
    stillCfg->at(0).size = Size(2592, 2592);
    stillCfg->at(0).pixelFormat = formats::RGB888;
    stillCfg->at(0).bufferCount = 1;

    std::unique_ptr<CameraConfiguration> gifCfg =
        camera->generateConfiguration({ StreamRole::Viewfinder });
    gifCfg->at(0).size = Size(640, 640);
    gifCfg->at(0).pixelFormat = formats::RGB888;
    gifCfg->at(0).bufferCount = 4;

    std::thread([] {
        /* warm the filter sprites with a PRIVATE TextEngine: each build is
         * ~200ms of supersampled blur+lanczos, and doing that under
         * g_render_mtx visibly stuttered the fresh preview. Off-mutex the
         * builds run on another core; only the finished-sprite insert takes
         * the lock (microseconds). 2s delay keeps even the memory-bandwidth
         * cost out of the boot attention window. */
        usleep(2000000);
        saving_spinner_sprite(0);
        TextEngine te;
        if (!te.init("/usr/share/optocam/cmunvt.ttf")) return;
        /* presets only: /data may not be mounted yet, so customs (>= CUSTOM_BASE)
         * are built lazily by filter_indicator() when first shown */
        for (int i = 0; i < CUSTOM_BASE; i++) {
            Img s = build_filter_indicator(FILTERS[i], te);
            std::lock_guard<std::mutex> rk(g_render_mtx);
            if (!g_filt_ind_cache.count(i)) g_filt_ind_cache.emplace(i, std::move(s));
        }
        for (int i = 0; i < 2; i++) {
            Img s = build_gif_mode_indicator(i, te);
            std::lock_guard<std::mutex> rk(g_render_mtx);
            if (!g_gif_ind_built[i]) {
                g_gif_ind_cache[i] = std::move(s);
                g_gif_ind_built[i] = true;
            }
        }
        {
            std::lock_guard<std::mutex> rk(g_render_mtx);
            centre_msg_sprite("GIF Mode");
            centre_msg_sprite("Photo Mode");
        }
        printf("optocam: sprites warmed\n"); fflush(stdout);
    }).detach();

    camera->requestCompleted.connect(requestComplete);
    if (!setup_stream(previewCfg.get(), true)) return 1;
    printf("optocam: preview running\n"); fflush(stdout);

    /* pre-warm the first-capture path: mounting /data (journal replay after a
     * power-cut can take >1s) and the cold photo-dir scan otherwise happen
     * synchronously inside the first shutter press — the "first capture
     * freezes longer" bug */
    std::thread([] {
        for (int i = 0; access("/tmp/optocam-data-ready", F_OK) != 0 && i < 30; i++)
            usleep(100000);
        data_ready();
        build_565_luts();          /* /data/.dispcal wasn't readable at start */
        build_wb_trim();           /* same for /data/.wbtrim */
        build_custom_filters();    /* /data/.effect0-4 — the custom filters */
        load_startup_defaults();   /* /data/.defaults — startup filter + WB */
        load_ae_limits();          /* /data/.aelimits — min shutter / max ISO */
        warm_initial_gallery_cache();
    }).detach();

    /* ---------- main loop: exact port of button_handler + mode loop ---------- */
    enum { M_PREVIEW, M_GALLERY, M_SPLASH, M_OFF, M_TRANSFER };
    int mode = M_PREVIEW;
    bool camera_running = true;
    int gal_idx = 0; bool gal_confirm = false, gal_dirty = false;
    bool gif_mode = false;
    double gif_label_time = 0, empty_msg_time = 0, nospace_msg_time = 0;
    bl_fast_init();
    std::thread(backlight_pwm_thread).detach();
    double idle_last = now_s(); bool idle_dimmed = false;
    uint8_t prev = 0;
    double last_capture = 0, last_preview_btn = 0, last_joy_ud = 0, last_lr = 0;
    bool cap_was_down = false, cap_long_fired = false; double cap_down_time = 0;
    bool joy_was_down = false, joy_long_fired = false; double joy_down_time = 0;
    double transfer_last_refresh = 0, transfer_last_activity = 0;
    std::vector<double> joy_press_times;
    double left_held = 0, right_held = 0, last_scroll = 0;
    const double DEB = 0.3, CAP_LONG = 0.6, HOLD_TH = 0.35, FAST_IV = 0.10;

    bool live_running = false;      /* transfer-mode live view holds the camera on the GIF stream */
    auto stop_cam = [&] { if (camera_running) { g_streaming.store(false); camera->stop(); camera_running = false; } };
    auto start_cam = [&] { if (!camera_running) { g_ctrl_dirty.store(true); setup_stream(previewCfg.get(), true); camera_running = true; } };
    auto stop_live = [&] {
        if (!live_running) return;
        stop_cam(); live_running = false;
        g_live_neutral.store(false);
        g_ctrl_dirty.store(true);       /* restore the active filter's ISP next stream */
        unlink(LIVE_JPG);           /* never serve a frozen last frame */
    };
    auto enter_transfer = [&](int &m) {
        m = M_TRANSFER;
        /* session flags from a previous boot are never valid — with no RTC the
         * restored clock can sit BEHIND their mtime, which made the 8s stale
         * check read them as "fresh" and show a ghost calibration pattern */
        unlink("/data/.calib_active"); unlink("/data/.calib");
        unlink("/data/.calib_img");    unlink(LIVE_FLAG);
        transfer_last_refresh = 0; transfer_last_activity = now_s();
        set_backlight(true);
        show_transfer_screen(0);                         /* draw before camera->stop(), which can take seconds */
        stop_cam();
        system("/usr/share/optocam/transfer-start.sh >/dev/null 2>&1 &");
    };
    auto exit_transfer = [&](int &m) {
        system("/usr/share/optocam/transfer-stop.sh >/dev/null 2>&1 &");
        /* live view holds the camera on the 640px GIF config — release it so
         * start_cam below reconfigures for the 240px preview. */
        stop_live();
        unlink(LIVE_FLAG);
        /* drop any in-progress screen calibration and restore the saved profile
         * so the returning live preview renders with the persisted white point. */
        unlink("/data/.calib_active");
        unlink("/data/.calib");
        unlink("/data/.calib_img");
        build_565_luts();
        build_custom_filters();    /* pick up filters created/renamed/deleted in the hotspot UI */
        {   /* names/count may have changed: stale custom pills + an index past
             * the new end are both possible. Camera is stopped, but the render
             * mutex still guards the sprite cache against the warm thread. */
            std::lock_guard<std::mutex> rk(g_render_mtx);
            drop_custom_filter_sprites();
        }
        if (g_filter_idx.load() >= filter_count()) g_filter_idx.store(0);
        load_ae_limits();          /* min shutter / max ISO may have changed in the UI */
        g_ctrl_dirty.store(true);  /* re-apply the (possibly changed) custom ISP */
        m = M_PREVIEW; set_backlight(true); start_cam();
    };


    for (;;) {
        double bnow = now_s();
        uint8_t cur = buttons_read();
        uint8_t pressed = cur & ~prev;
        prev = cur;

        /* idle dim: 90s without input -> brightness 8/255. The waking press
         * ONLY brightens — it is swallowed and never acts (like the original:
         * wake, sleep 0.3, continue). */
        if (cur) {
            idle_last = bnow;
            if (idle_dimmed) {
                bl_trace("wake press (global)");
                idle_dimmed = false; set_backlight(true);
                /* transfer mode keeps its own activity clock — without this
                 * its 30s check sees the stale pre-dim time and re-dims on
                 * the very next iteration (the "wake doesn't work" bug) */
                transfer_last_activity = bnow;
                usleep(300000);
                prev = buttons_read();          /* consume the waking press */
                continue;
            }
        } else if (!idle_dimmed && mode != M_OFF && bnow - idle_last > 90.0) {
            idle_dimmed = true; backlight_dim();
        }

        /* ---- splash mode: any button closes ---- */
        if (mode == M_SPLASH) {
            if (cur) { mode = M_PREVIEW; joy_press_times.clear(); set_backlight(true); start_cam(); prev = 0xFF; }
            usleep(50000); continue;
        }

        /* ---- transfer mode: 0.5s refresh, 30s dim, buttons only wake/exit ---- */
        if (mode == M_TRANSFER) {
            /* long-press exit must be handled HERE — the loop continues below
             * and never reaches the main joystick handler in this mode */
            bool tj = cur & (1 << JOY_PRESS);
            if (tj && !joy_was_down) { joy_was_down = true; joy_down_time = bnow; joy_long_fired = false; }
            else if (tj && joy_was_down && !joy_long_fired && bnow - joy_down_time >= 1.5) {
                joy_long_fired = true;
                bl_trace("exit_transfer (joy long-press)");
                exit_transfer(mode);
                prev = buttons_read();
                usleep(50000); continue;
            } else if (!tj) joy_was_down = false;

            /* ---- live view: run the camera while the phone keeps the flag
             * fresh, export ~10fps unfiltered JPEGs for the Filter Maker's
             * LIVE tab. Placed before the calibration branch (it continues). */
            {
                static double next_live_try = 0;
                static double next_live_enc = 0;
                static uint64_t live_last_seq = 0;
                bool lv = live_flag_fresh();
                if (lv && !live_running && bnow >= next_live_try) {
                    g_live_neutral.store(true);
                    g_ctrl_dirty.store(true);            /* neutral ISP + AE limits on the live stream */
                    if (setup_stream(gifCfg.get(), true)) {
                        camera_running = true; live_running = true;
                        live_last_seq = g_frame_seq.load();
                    } else {
                        g_live_neutral.store(false);
                        next_live_try = bnow + 2.0;      /* don't thrash a failing start */
                    }
                } else if (!lv && live_running) {
                    stop_live();
                }
                if (live_running) {
                    uint64_t seq = g_frame_seq.load();
                    if (seq != live_last_seq && bnow >= next_live_enc) {
                        live_last_seq = seq;
                        next_live_enc = bnow + 0.1;
                        static std::vector<uint8_t> lvbuf(sizeof g_frame_copy);
                        {
                            std::lock_guard<std::mutex> lk(g_frame_mtx);
                            memcpy(lvbuf.data(), g_frame_copy, sizeof g_frame_copy);
                        }
                        g_frame_wanted.store(true);
                        /* WB like the still/preview paths, so the browser's
                         * filtered live view matches what a capture would get */
                        wb_apply(lvbuf.data(), (size_t)FH * (FSTRIDE / 3), true);
                        write_live_jpeg(lvbuf.data(), FW, FH, FSTRIDE);
                    }
                }
            }

            /* ---- live screen calibration: the hotspot Screen-Calibration panel
             * touches /data/.calib_active and streams "GR GG GB SAT GAMMA CONTRAST"
             * into /data/.calib as the sliders move (plus /data/.calib_img for
             * the reference-pattern choice). Show the chosen pattern and rebuild
             * the LUTs in place so changes appear on the panel in real time.
             * Everything here is transfer-mode only. */
            static bool was_calib = false;
            static float lr = -9, lg = -9, lb = -9, ls = -9, lga = -9, lct = -9, lbl = -9;
            static char cur_img[16] = {0};
            static uint8_t calimg[240*240*3]; static bool img_loaded = false;
            static bool drawn = false;
            /* Calibration is active only while the phone keeps the flag fresh
             * (the web heartbeats it ~every second). A stale flag — phone closed
             * the tab or dropped WiFi without a clean /calib-close — expires
             * after 8s so the LCD returns to the hotspot screen on its own. */
            bool calib = false;
            { struct stat cst;
              if (stat("/data/.calib_active", &cst) == 0) {
                  long age = (long)(time(NULL) - cst.st_mtime);      /* wall clock, matches mtime */
                  if (age >= 0 && age < 8) calib = true;             /* negative = clock skew -> stale */
                  else { unlink("/data/.calib_active"); unlink("/data/.calib"); unlink("/data/.calib_img"); }
              } }
            if (was_calib && !calib) {                 /* panel closed calibration */
                build_565_luts();                      /* restore saved/baked profile */
                transfer_last_refresh = 0;             /* force a transfer-screen redraw */
            }
            bool just_entered = calib && !was_calib;
            was_calib = calib;
            if (calib) {
                if (just_entered) { drawn = false; lr = lg = lb = ls = lga = lct = lbl = -9; cur_img[0] = 0; }
                transfer_last_activity = bnow;         /* phone drives it — don't idle-dim */
                if (idle_dimmed) { idle_dimmed = false; set_backlight(true); }
                /* joystick left/right cycles the reference pattern (mirrors the
                 * web selector); the write is picked up by read_calib_img_name below */
                const char *base_img = cur_img[0] ? cur_img : "color";
                if (pressed & (1 << JOY_RIGHT)) cycle_calib_img(base_img, +1);
                else if (pressed & (1 << JOY_LEFT)) cycle_calib_img(base_img, -1);
                char want_img[16]; read_calib_img_name(want_img, sizeof want_img);
                if (strcmp(want_img, cur_img) != 0) {  /* pattern selection changed */
                    img_loaded = load_calib_image(calimg, want_img);
                    snprintf(cur_img, sizeof cur_img, "%s", want_img);
                    drawn = false;
                }
                float r = 1, g = 1, b = 1, s = 1, ga = 1, ct = BASE_CONTRAST, bl = 1;
                if (read_calib_live(r, g, b, s, ga, ct, bl)) {
                    /* brightness rides the backlight alone — no LUT rebuild and
                     * no redraw for a bl-only change: the full-frame SPI blit
                     * stalls the PWM thread into a visible flicker pulse */
                    if (bl != lbl) { g_bl_level.store((int)lrintf(bl * 255.0f)); lbl = bl; }
                    if (r != lr || g != lg || b != lb || s != ls || ga != lga || ct != lct) {
                        build_565_luts_from(r, g, b, s, ga, ct);
                        lr = r; lg = g; lb = b; ls = s; lga = ga; lct = ct; drawn = false;
                    }
                }
                if (!drawn && img_loaded) { display_blit_240(calimg); drawn = true; }
                usleep(50000); continue;
            }

            if (cur) {
                transfer_last_activity = bnow;
                if (idle_dimmed) { idle_dimmed = false; set_backlight(true); transfer_last_refresh = 0; }
            } else if (!idle_dimmed && bnow - transfer_last_activity > 30.0) {
                idle_dimmed = true; backlight_dim();
            }
            /* no redraws while dimmed: the content is static and each SPI blit
             * stalls the backlight PWM into a visible flicker pulse; waking
             * zeroes transfer_last_refresh so the screen repaints immediately */
            if (!idle_dimmed && bnow - transfer_last_refresh >= 0.5) {
                transfer_last_refresh = bnow;
                show_transfer_screen(transfer_station_count());
            }
            usleep(50000); continue;
        }

        /* ---- render preview frames (camera thread hands them off) ---- */
        static uint64_t last_seq = 0;
        static std::vector<uint8_t> local(sizeof g_frame_copy);
        uint64_t seq = g_frame_seq.load();
        if (g_frame_resync.exchange(false)) last_seq = seq;   /* stale-stride guard */
        static double next_render = 0;
        if (mode == M_PREVIEW && seq != last_seq && g_mode.load() == 0 && camera_running
            && now_s() >= next_render) {
            next_render = 0;                      /* uncapped: tearing is fixed by
                                                   * scan-order writes, the old fps
                                                   * cap was a workaround for it */
            last_seq = seq;
            float gain; int32_t exp_us;
            {
                std::lock_guard<std::mutex> lk(g_frame_mtx);
                memcpy(local.data(), g_frame_copy, sizeof g_frame_copy);
                gain = g_meta_gain; exp_us = g_meta_exp;
            }
            g_frame_wanted.store(true);
            wb_apply(local.data(), (size_t)FH * (FSTRIDE / 3), true);
            {
                std::lock_guard<std::mutex> rk(g_render_mtx);
                render_preview(local.data(), gain, exp_us);
            }
            static int fps_frames = 0; static double fps_t0 = now_s();
            fps_frames++;
            if (now_s() - fps_t0 >= 5.0) {
                printf("fps %.1f  awb=%s ct=%dK gains=%.2f/%.2f  exp=%dus ag=%.2f dg=%.2f cap=%lldus\n",
                       fps_frames / (now_s() - fps_t0),
                       AWB_MODES[g_awb_idx.load()].abbr, g_meta_ct.load(),
                       g_meta_cgr_x100.load()/100.0, g_meta_cgb_x100.load()/100.0,
                       exp_us, gain, g_meta_dg_x100.load()/100.0,
                       (long long)g_max_exp_us.load());
                fflush(stdout);
                fps_frames = 0; fps_t0 = now_s();
            }
        }

        /* ---- joystick press: short=gallery, triple=splash (long=transfer TODO) ---- */
        bool joy_dn = cur & (1 << JOY_PRESS);
        if (joy_dn && !joy_was_down) { joy_was_down = true; joy_down_time = bnow; joy_long_fired = false; }
        else if (joy_dn && joy_was_down) {
            if (!joy_long_fired && bnow - joy_down_time >= 1.5) {   /* transfer toggle */
                joy_long_fired = true;
                joy_press_times.clear();
                if (mode == M_TRANSFER) exit_transfer(mode);
                else { gal_confirm = false; enter_transfer(mode); }
            }
        }
        else if (!joy_dn && joy_was_down) {
            joy_was_down = false;
            if (!joy_long_fired && bnow - joy_down_time > 0.02) {
                joy_press_times.push_back(bnow);
                joy_press_times.erase(std::remove_if(joy_press_times.begin(), joy_press_times.end(),
                    [&](double t){ return bnow - t >= 0.8; }), joy_press_times.end());
                if (joy_press_times.size() >= 3) {
                    joy_press_times.clear();
                    mode = M_SPLASH; gal_confirm = false;
                    stop_cam(); set_backlight(true); show_splash_screen();
                } else if (mode == M_GALLERY) {
                    if (gal_confirm) { gal_confirm = false; gal_dirty = true; }
                    else { mode = M_PREVIEW; start_cam(); }
                } else if (mode == M_PREVIEW) {
                    gallery_scan();
                    if (!gallery_files.empty()) {
                        gal_idx = (int)gallery_files.size() - 1;
                        mode = M_GALLERY; gal_confirm = false; gal_dirty = true;
                        stop_cam();
                    } else empty_msg_time = bnow;
                }
            }
        }

        /* ---- capture button: tap=photo (gif record TODO), long=gif-mode toggle ---- */
        bool cap_dn = cur & (1 << BTN_CAPTURE);
        if (cap_dn && !cap_was_down) { cap_was_down = true; cap_down_time = bnow; cap_long_fired = false; }
        else if (cap_dn && cap_was_down) {
            if (!cap_long_fired && bnow - cap_down_time >= CAP_LONG && mode == M_PREVIEW) {
                cap_long_fired = true;
                gif_mode = !gif_mode;
                g_gif_mode.store(gif_mode);
                gif_label_time = bnow;
            }
        } else if (!cap_dn && cap_was_down) {
            cap_was_down = false;
            if (cap_long_fired) last_capture = bnow;
            else if (bnow - last_capture > DEB) {
                last_capture = bnow;
                if (mode == M_GALLERY) {
                    if (gal_confirm) { gal_confirm = false; gal_dirty = true; }
                    else { mode = M_PREVIEW; start_cam(); }
                } else if (mode == M_PREVIEW) {
                    if (!disk_has_space()) nospace_msg_time = bnow;
                    else if (!gif_mode) {
                        g_capture_dot_time.store(bnow);
                        do_capture(previewCfg, stillCfg);
                        camera_running = true;
                        prev = buttons_read();
                    } else {
                        record_gif(previewCfg, gifCfg);
                        g_frame_resync.store(true);
                        camera_running = true;
                        prev = buttons_read();
                    }
                }
            }
        }

        /* ---- preview toggle button ---- */
        if ((pressed & (1 << BTN_PREVIEW)) && bnow - last_preview_btn > DEB) {
            if (mode == M_GALLERY && gal_confirm) { gal_confirm = false; gal_dirty = true; last_preview_btn = bnow; }
            else if (mode != M_GALLERY) {
                last_preview_btn = bnow;
                if (mode == M_PREVIEW) { mode = M_OFF; stop_cam(); set_backlight(false); memset(fb565,0,sizeof fb565); fb_flush(); }
                else if (mode == M_OFF) { mode = M_PREVIEW; set_backlight(true); start_cam(); }
            }
        }

        /* ---- joystick up/down ---- */
        if ((cur & (1 << JOY_UP)) && bnow - last_joy_ud > DEB) {
            last_joy_ud = bnow;
            if (mode == M_GALLERY && !gallery_files.empty()) {
                if (gal_confirm) {
                    gcache_drop(gallery_files[gal_idx]);
                    unlink(gallery_files[gal_idx].c_str());
                    gallery_files.erase(gallery_files.begin() + gal_idx);
                    gal_confirm = false;
                    if (gallery_files.empty()) { mode = M_PREVIEW; start_cam(); }
                    else { gal_idx = std::min(gal_idx, (int)gallery_files.size() - 1); gal_dirty = true; }
                } else { gal_confirm = true; gal_dirty = true; }
            } else if (mode == M_PREVIEW) {
                int nf = filter_count();
                g_filter_idx.store((g_filter_idx.load() + nf - 1) % nf);
                g_filter_label_time.store(bnow); g_ctrl_dirty.store(true); g_user_touched.store(true);
            }
        }
        if ((cur & (1 << JOY_DOWN)) && bnow - last_joy_ud > DEB) {
            last_joy_ud = bnow;
            if (mode == M_GALLERY && gal_confirm) { gal_confirm = false; gal_dirty = true; }
            else if (mode == M_PREVIEW) {
                g_filter_idx.store((g_filter_idx.load() + 1) % filter_count());
                g_filter_label_time.store(bnow); g_ctrl_dirty.store(true); g_user_touched.store(true);
            }
        }

        /* ---- joystick left/right: gallery nav (hold-repeat) or AWB ---- */
        if (mode == M_GALLERY && !gallery_files.empty()) {
            bool L = cur & (1 << JOY_LEFT), R = cur & (1 << JOY_RIGHT);
            if (L) {
                if (gal_confirm) { gal_confirm = false; gal_dirty = true; left_held = bnow; }
                else if (left_held == 0) { left_held = bnow; gal_idx = (gal_idx + gallery_files.size() - 1) % gallery_files.size(); gal_dirty = true; last_scroll = bnow; }
                else if (bnow - left_held > HOLD_TH && bnow - last_scroll > FAST_IV) { gal_idx = (gal_idx + gallery_files.size() - 1) % gallery_files.size(); gal_dirty = true; last_scroll = bnow; }
            } else left_held = 0;
            if (R) {
                if (gal_confirm) { gal_confirm = false; gal_dirty = true; right_held = bnow; }
                else if (right_held == 0) { right_held = bnow; gal_idx = (gal_idx + 1) % gallery_files.size(); gal_dirty = true; last_scroll = bnow; }
                else if (bnow - right_held > HOLD_TH && bnow - last_scroll > FAST_IV) { gal_idx = (gal_idx + 1) % gallery_files.size(); gal_dirty = true; last_scroll = bnow; }
            } else right_held = 0;
        } else if (mode == M_PREVIEW) {
            if ((cur & (1 << JOY_LEFT)) && bnow - last_lr > DEB) {
                last_lr = bnow;
                g_awb_idx.store((g_awb_idx.load() + 4) % 5);
                g_awb_changed_time.store(bnow); g_ctrl_dirty.store(true); g_user_touched.store(true);
            }
            if ((cur & (1 << JOY_RIGHT)) && bnow - last_lr > DEB) {
                last_lr = bnow;
                g_awb_idx.store((g_awb_idx.load() + 1) % 5);
                g_awb_changed_time.store(bnow); g_ctrl_dirty.store(true); g_user_touched.store(true);
            }
        }

        /* ---- gallery redraw + gif animation (file's own frame delay) ---- */
        if (mode == M_GALLERY && gal_dirty && !gallery_files.empty()) {
            gal_dirty = false;
            display_gallery_image(gallery_files[gal_idx], gal_idx + 1, (int)gallery_files.size(), gal_confirm);
            prefetch_neighbors(gallery_files, gal_idx);
        }
        if (mode == M_GALLERY && !gal_confirm && !gallery_files.empty() &&
            gallery_files[gal_idx] == g_gifanim_file && bnow - g_gifanim_last >= g_gifanim_iv) {
            std::lock_guard<std::mutex> gk(g_gif_mtx);
            if ((int)g_gifanim.size() > 1) {
                g_gifanim_last = bnow;
                g_gifanim_idx = (g_gifanim_idx + 1) % (int)g_gifanim.size();
                img_to_display(g_gifanim[g_gifanim_idx]);
            }
        }

        /* transient toasts are rendered by render_preview via these fields */
        if (empty_msg_time > 0)   { g_toast_time.store(empty_msg_time);   g_toast_id.store(1); if (bnow - empty_msg_time > 1.0) empty_msg_time = 0; }
        else if (nospace_msg_time > 0) { g_toast_time.store(nospace_msg_time); g_toast_id.store(2); if (bnow - nospace_msg_time > 1.0) nospace_msg_time = 0; }
        else if (gif_label_time > 0)   { g_toast_time.store(gif_label_time);   g_toast_id.store(gif_mode ? 3 : 4); if (bnow - gif_label_time > 1.5) gif_label_time = 0; }
        else { g_toast_id.store(0); }

        usleep(5000);
    }
    return 0;
}
