/* optocam_render.h — PIL-equivalent rendering core for the exact OptoCam port.
 * RGBA straight-alpha images, FreeType text (PIL's own engine, same cmunvt.ttf),
 * Gaussian blur (3-pass box approximation), alpha_composite, BILINEAR/LANCZOS
 * resize, and the PIL ImageDraw shapes the HUD uses. Header-only.
 */
#pragma once
#include <cstdint>
#include <cstring>
#include <cstdio>
#include <cmath>
#include <vector>
#include <string>
#include <map>
#include <algorithm>
#include <ft2build.h>
#include FT_FREETYPE_H

struct Img {                       /* RGBA8, straight alpha */
    int w = 0, h = 0;
    std::vector<uint8_t> px;       /* w*h*4 */
    Img() {}
    Img(int W, int H) : w(W), h(H), px((size_t)W * H * 4, 0) {}
    uint8_t *at(int x, int y) { return &px[((size_t)y * w + x) * 4]; }
    const uint8_t *at(int x, int y) const { return &px[((size_t)y * w + x) * 4]; }
};

/* ---------- alpha compositing (PIL Image.alpha_composite) ---------- */
inline void alpha_composite(Img &dst, const Img &src) {
    size_t n = (size_t)dst.w * dst.h;
    for (size_t i = 0; i < n; i++) {
        uint8_t *d = &dst.px[i * 4];
        const uint8_t *s = &src.px[i * 4];
        unsigned sa = s[3];
        if (sa == 0) continue;
        if (sa == 255) { d[0]=s[0]; d[1]=s[1]; d[2]=s[2]; d[3]=255; continue; }
        unsigned da = d[3];
        unsigned oa = sa + da * (255 - sa) / 255;
        if (oa == 0) { d[0]=d[1]=d[2]=d[3]=0; continue; }
        for (int c = 0; c < 3; c++)
            d[c] = (uint8_t)((s[c] * sa + d[c] * da * (255 - sa) / 255) / oa);
        d[3] = (uint8_t)oa;
    }
}
inline void scale_alpha(Img &img, float a) {           /* PIL putalpha(point(p*a)) */
    size_t n = (size_t)img.w * img.h;
    for (size_t i = 0; i < n; i++)
        img.px[i * 4 + 3] = (uint8_t)std::min(255, (int)(img.px[i * 4 + 3] * a));
}

/* ---------- Gaussian blur (PIL ImageFilter.GaussianBlur equivalent) ----------
 * Three box blurs approximate a Gaussian; radii per Ivan Kutskir's method. */
inline void _box_blur_ch(std::vector<float> &src, std::vector<float> &dst, int w, int h, int r) {
    /* horizontal then vertical accumulation, radius r box */
    float iarr = 1.0f / (r + r + 1);
    for (int y = 0; y < h; y++) {                       /* horizontal */
        float *s = &src[(size_t)y * w], *d = &dst[(size_t)y * w];
        float acc = s[0] * (r + 1);
        for (int x = 0; x < r && x < w; x++) acc += s[std::min(x, w - 1)];
        for (int x = 0; x < w; x++) {
            acc += s[std::min(x + r, w - 1)] - s[std::max(x - r - 1, 0)];
            d[x] = acc * iarr;
        }
    }
    for (int x = 0; x < w; x++) {                       /* vertical, dst in place via temp col */
        float acc = dst[x] * (r + 1);
        for (int y = 0; y < r && y < h; y++) acc += dst[(size_t)std::min(y, h - 1) * w + x];
        std::vector<float> col(h);
        for (int y = 0; y < h; y++) {
            acc += dst[(size_t)std::min(y + r, h - 1) * w + x] - dst[(size_t)std::max(y - r - 1, 0) * w + x];
            col[y] = acc * iarr;
        }
        for (int y = 0; y < h; y++) dst[(size_t)y * w + x] = col[y];
    }
    src = dst;
}
inline void gaussian_blur(Img &img, float radius) {
    if (radius <= 0) return;
    /* box sizes for 3 passes */
    float sigma = radius;                               /* PIL: radius == sigma */
    int n = 3;
    float wIdeal = std::sqrt(12.0f * sigma * sigma / n + 1.0f);
    int wl = (int)wIdeal; if (wl % 2 == 0) wl--;
    int wu = wl + 2;
    float mIdeal = (12.0f * sigma * sigma - n * wl * wl - 4.0f * n * wl - 3.0f * n) / (-4.0f * wl - 4.0f);
    int m = (int)std::round(mIdeal);
    int w = img.w, h = img.h;
    /* blur premultiplied so transparent black pixels don't darken edges */
    std::vector<float> ch[4], tmp((size_t)w * h);
    for (int c = 0; c < 4; c++) ch[c].resize((size_t)w * h);
    size_t np = (size_t)w * h;
    for (size_t i = 0; i < np; i++) {
        float a = img.px[i * 4 + 3] / 255.0f;
        ch[0][i] = img.px[i * 4 + 0] * a;
        ch[1][i] = img.px[i * 4 + 1] * a;
        ch[2][i] = img.px[i * 4 + 2] * a;
        ch[3][i] = img.px[i * 4 + 3];
    }
    for (int c = 0; c < 4; c++)
        for (int p = 0; p < 3; p++) {
            int r = (p < m ? wl : wu) / 2;
            if (r > 0) _box_blur_ch(ch[c], tmp, w, h, r);
        }
    for (size_t i = 0; i < np; i++) {
        float a = ch[3][i];
        img.px[i * 4 + 3] = (uint8_t)std::clamp(a, 0.0f, 255.0f);
        float ia = a > 0.5f ? 255.0f / a : 0.0f;
        img.px[i * 4 + 0] = (uint8_t)std::clamp(ch[0][i] * ia, 0.0f, 255.0f);
        img.px[i * 4 + 1] = (uint8_t)std::clamp(ch[1][i] * ia, 0.0f, 255.0f);
        img.px[i * 4 + 2] = (uint8_t)std::clamp(ch[2][i] * ia, 0.0f, 255.0f);
    }
}

/* ---------- resize: BILINEAR + LANCZOS(3), premultiplied like PIL ---------- */
inline Img resize_bilinear(const Img &src, int W, int H) {
    Img out(W, H);
    for (int y = 0; y < H; y++) {
        float fy = (y + 0.5f) * src.h / H - 0.5f;
        int y0 = (int)std::floor(fy); float ty = fy - y0;
        int y1 = std::min(y0 + 1, src.h - 1); y0 = std::max(y0, 0);
        for (int x = 0; x < W; x++) {
            float fx = (x + 0.5f) * src.w / W - 0.5f;
            int x0 = (int)std::floor(fx); float tx = fx - x0;
            int x1 = std::min(x0 + 1, src.w - 1); x0 = std::max(x0, 0);
            const uint8_t *p00 = src.at(x0, y0), *p10 = src.at(x1, y0),
                          *p01 = src.at(x0, y1), *p11 = src.at(x1, y1);
            float a00=p00[3], a10=p10[3], a01=p01[3], a11=p11[3];
            float oa = (a00*(1-tx)+a10*tx)*(1-ty) + (a01*(1-tx)+a11*tx)*ty;
            uint8_t *o = out.at(x, y);
            for (int c = 0; c < 3; c++) {
                float v = (p00[c]*a00*(1-tx)+p10[c]*a10*tx)*(1-ty)
                        + (p01[c]*a01*(1-tx)+p11[c]*a11*tx)*ty;
                o[c] = oa > 0.5f ? (uint8_t)std::clamp(v / oa, 0.0f, 255.0f) : 0;
            }
            o[3] = (uint8_t)std::clamp(oa, 0.0f, 255.0f);
        }
    }
    return out;
}
inline float _lanczos(float x) {
    if (x == 0) return 1.0f;
    if (x <= -3.0f || x >= 3.0f) return 0.0f;
    float px = (float)M_PI * x;
    return 3.0f * std::sin(px) * std::sin(px / 3.0f) / (px * px);
}
inline Img resize_lanczos(const Img &src, int W, int H) {
    /* separable lanczos3, premultiplied */
    auto pass = [](const Img &in, int outW, bool horiz) {
        int inL = horiz ? in.w : in.h;
        int outH = horiz ? in.h : in.w;
        Img out(horiz ? outW : in.w, horiz ? in.h : outW);
        float scale = (float)inL / outW;
        float support = 3.0f * std::max(scale, 1.0f);
        for (int o = 0; o < outW; o++) {
            float centre = (o + 0.5f) * scale - 0.5f;
            int lo = std::max(0, (int)std::floor(centre - support));
            int hi = std::min(inL - 1, (int)std::ceil(centre + support));
            std::vector<float> wgt(hi - lo + 1);
            float sum = 0;
            for (int i = lo; i <= hi; i++) { wgt[i - lo] = _lanczos((i - centre) / std::max(scale, 1.0f)); sum += wgt[i - lo]; }
            for (auto &wv : wgt) wv /= sum;
            for (int t = 0; t < outH; t++) {
                float acc[4] = {0, 0, 0, 0};
                for (int i = lo; i <= hi; i++) {
                    const uint8_t *p = horiz ? in.at(i, t) : in.at(t, i);
                    float a = p[3] * wgt[i - lo];
                    acc[0] += p[0] * a; acc[1] += p[1] * a; acc[2] += p[2] * a; acc[3] += a;
                }
                uint8_t *op = horiz ? out.at(o, t) : out.at(t, o);
                float oa = std::clamp(acc[3], 0.0f, 255.0f);
                op[3] = (uint8_t)oa;
                for (int c = 0; c < 3; c++)
                    op[c] = acc[3] > 0.5f ? (uint8_t)std::clamp(acc[c] / acc[3], 0.0f, 255.0f) : 0;
            }
        }
        return out;
    };
    return pass(pass(src, W, true), H, false);
}

/* ---------- FreeType text (PIL ImageFont.truetype equivalent) ---------- */
struct Font {
    FT_Face face = nullptr;
    int size = 0;
};
class TextEngine {
    FT_Library lib_ = nullptr;
    std::map<int, Font> cache_;
    std::string path_;
public:
    bool init(const char *ttf_path) {
        path_ = ttf_path;
        return FT_Init_FreeType(&lib_) == 0;
    }
    Font *font(int size) {                              /* load_font(size), cached */
        auto it = cache_.find(size);
        if (it != cache_.end()) return &it->second;
        Font f; f.size = size;
        if (FT_New_Face(lib_, path_.c_str(), 0, &f.face)) return nullptr;
        FT_Set_Pixel_Sizes(f.face, 0, size);
        return &cache_.emplace(size, f).first->second;
    }
    /* PIL draw.textbbox((0,0), text): returns x0,y0,x1,y1 of inked area with
     * pen starting at origin, y measured downward from ascender-anchored top.
     * PIL anchors text top at y: glyph top = y + (ascender - bitmap_top). */
    void textbbox(Font *f, const std::string &s, int &x0, int &y0, int &x1, int &y1) {
        int asc = f->face->size->metrics.ascender >> 6;
        int penx = 0;
        x0 = 1 << 30; y0 = 1 << 30; x1 = -(1 << 30); y1 = -(1 << 30);
        for (unsigned char c : s) {
            if (FT_Load_Char(f->face, c, FT_LOAD_RENDER)) continue;
            FT_GlyphSlot g = f->face->glyph;
            int gx0 = penx + g->bitmap_left;
            int gy0 = asc - g->bitmap_top;
            x0 = std::min(x0, gx0); y0 = std::min(y0, gy0);
            x1 = std::max(x1, gx0 + (int)g->bitmap.width);
            y1 = std::max(y1, gy0 + (int)g->bitmap.rows);
            penx += g->advance.x >> 6;
        }
        if (x1 < x0) { x0 = y0 = 0; x1 = y1 = 0; }
    }
    /* draw.text((x,y), s, fill=(r,g,b,a)) — y is the ascender-anchored top */
    void draw(Img &img, Font *f, int x, int y, const std::string &s,
              uint8_t r, uint8_t g, uint8_t b, uint8_t a = 255) {
        int asc = f->face->size->metrics.ascender >> 6;
        int penx = x;
        for (unsigned char c : s) {
            if (FT_Load_Char(f->face, c, FT_LOAD_RENDER)) continue;
            FT_GlyphSlot gl = f->face->glyph;
            for (unsigned row = 0; row < gl->bitmap.rows; row++) {
                int py = y + asc - gl->bitmap_top + (int)row;
                if (py < 0 || py >= img.h) continue;
                for (unsigned col = 0; col < gl->bitmap.width; col++) {
                    int px = penx + gl->bitmap_left + (int)col;
                    if (px < 0 || px >= img.w) continue;
                    unsigned cov = gl->bitmap.buffer[row * gl->bitmap.pitch + col];
                    if (!cov) continue;
                    unsigned sa = cov * a / 255;
                    uint8_t *d = img.at(px, py);
                    unsigned da = d[3];
                    unsigned oa = sa + da * (255 - sa) / 255;
                    if (oa) {
                        d[0] = (uint8_t)((r * sa + d[0] * da * (255 - sa) / 255) / oa);
                        d[1] = (uint8_t)((g * sa + d[1] * da * (255 - sa) / 255) / oa);
                        d[2] = (uint8_t)((b * sa + d[2] * da * (255 - sa) / 255) / oa);
                        d[3] = (uint8_t)oa;
                    }
                }
            }
            penx += gl->advance.x >> 6;
        }
    }
};

/* ---------- shapes (PIL ImageDraw equivalents used by the HUD) ---------- */
inline void set_px(Img &img, int x, int y, uint8_t r, uint8_t g, uint8_t b, uint8_t a) {
    if (x < 0 || y < 0 || x >= img.w || y >= img.h) return;
    uint8_t *d = img.at(x, y);
    unsigned da = d[3], oa = a + da * (255 - a) / 255;
    if (!oa) return;
    d[0] = (uint8_t)((r * a + d[0] * da * (255 - a) / 255) / oa);
    d[1] = (uint8_t)((g * a + d[1] * da * (255 - a) / 255) / oa);
    d[2] = (uint8_t)((b * a + d[2] * da * (255 - a) / 255) / oa);
    d[3] = (uint8_t)oa;
}
inline void draw_ellipse_outline(Img &img, float x0, float y0, float x1, float y1,
                                 int width, uint8_t r, uint8_t g, uint8_t b, uint8_t a) {
    float cx = (x0 + x1) / 2, cy = (y0 + y1) / 2, rx = (x1 - x0) / 2, ry = (y1 - y0) / 2;
    for (int y = (int)y0 - 1; y <= (int)y1 + 1; y++)
        for (int x = (int)x0 - 1; x <= (int)x1 + 1; x++) {
            float dx = (x - cx), dy = (y - cy);
            float dist = std::sqrt(dx * dx / (rx * rx) + dy * dy / (ry * ry));
            float ring = std::fabs(dist - 1.0f) * std::min(rx, ry);
            if (ring <= width / 2.0f) set_px(img, x, y, r, g, b, a);
        }
}
inline void draw_rounded_rect_outline(Img &img, float x0, float y0, float x1, float y1,
                                      float rad, int width, uint8_t r, uint8_t g, uint8_t b, uint8_t a) {
    auto sdf = [&](float px, float py) {                /* rounded-rect signed distance */
        float qx = std::fabs(px - (x0 + x1) / 2) - ((x1 - x0) / 2 - rad);
        float qy = std::fabs(py - (y0 + y1) / 2) - ((y1 - y0) / 2 - rad);
        float ox = std::max(qx, 0.0f), oy = std::max(qy, 0.0f);
        return std::sqrt(ox * ox + oy * oy) + std::min(std::max(qx, qy), 0.0f) - rad;
    };
    for (int y = (int)y0 - 1; y <= (int)y1 + 1; y++)
        for (int x = (int)x0 - 1; x <= (int)x1 + 1; x++)
            if (std::fabs(sdf((float)x, (float)y)) <= width / 2.0f)
                set_px(img, x, y, r, g, b, a);
}
inline void draw_line(Img &img, float x0, float y0, float x1, float y1,
                      int width, uint8_t r, uint8_t g, uint8_t b, uint8_t a) {
    float dx = x1 - x0, dy = y1 - y0, len = std::sqrt(dx * dx + dy * dy);
    if (len < 0.001f) { set_px(img, (int)x0, (int)y0, r, g, b, a); return; }
    int lo_x = (int)std::min(x0, x1) - width, hi_x = (int)std::max(x0, x1) + width;
    int lo_y = (int)std::min(y0, y1) - width, hi_y = (int)std::max(y0, y1) + width;
    for (int y = lo_y; y <= hi_y; y++)
        for (int x = lo_x; x <= hi_x; x++) {
            float t = std::clamp(((x - x0) * dx + (y - y0) * dy) / (len * len), 0.0f, 1.0f);
            float px = x0 + t * dx, py = y0 + t * dy;
            float d = std::sqrt((x - px) * (x - px) + (y - py) * (y - py));
            if (d <= width / 2.0f) set_px(img, x, y, r, g, b, a);
        }
}
/* Anti-aliased round-capped line: same distance-to-segment as draw_line, but the
 * edge pixel's alpha is scaled by coverage (via set_px's alpha blend) so the
 * stroke doesn't stair-step. width is a float so strokes can be sub-pixel. */
inline void draw_line_aa(Img &img, float x0, float y0, float x1, float y1,
                         float width, uint8_t r, uint8_t g, uint8_t b, uint8_t a) {
    float dx = x1 - x0, dy = y1 - y0, len2 = dx * dx + dy * dy, hw = width / 2.0f;
    int lo_x = (int)std::floor(std::min(x0, x1) - hw - 1);
    int hi_x = (int)std::ceil (std::max(x0, x1) + hw + 1);
    int lo_y = (int)std::floor(std::min(y0, y1) - hw - 1);
    int hi_y = (int)std::ceil (std::max(y0, y1) + hw + 1);
    for (int y = lo_y; y <= hi_y; y++)
        for (int x = lo_x; x <= hi_x; x++) {
            float t = len2 > 0.0f ? std::clamp(((x - x0) * dx + (y - y0) * dy) / len2, 0.0f, 1.0f) : 0.0f;
            float px = x0 + t * dx, py = y0 + t * dy;
            float d = std::sqrt((x - px) * (x - px) + (y - py) * (y - py));
            float cov = std::clamp(hw + 0.5f - d, 0.0f, 1.0f);   /* 1px soft edge */
            if (cov > 0.0f) set_px(img, x, y, r, g, b, (uint8_t)std::lround((float)a * cov));
        }
}
inline void draw_polygon_fill(Img &img, const std::vector<std::pair<float,float>> &pts,
                              uint8_t r, uint8_t g, uint8_t b, uint8_t a) {
    float ymin = 1e9, ymax = -1e9;
    for (auto &p : pts) { ymin = std::min(ymin, p.second); ymax = std::max(ymax, p.second); }
    for (int y = (int)ymin; y <= (int)ymax; y++) {
        std::vector<float> xs;
        size_t n = pts.size();
        for (size_t i = 0; i < n; i++) {
            auto &p = pts[i], &q = pts[(i + 1) % n];
            if ((p.second <= y && q.second > y) || (q.second <= y && p.second > y))
                xs.push_back(p.first + (y - p.second) * (q.first - p.first) / (q.second - p.second));
        }
        std::sort(xs.begin(), xs.end());
        for (size_t i = 0; i + 1 < xs.size(); i += 2)
            for (int x = (int)std::ceil(xs[i]); x <= (int)xs[i + 1]; x++)
                set_px(img, x, y, r, g, b, a);
    }
}
inline void draw_arc(Img &img, float x0, float y0, float x1, float y1,
                     float start_deg, float end_deg, int width,
                     uint8_t r, uint8_t g, uint8_t b, uint8_t a) {
    float cx = (x0 + x1) / 2, cy = (y0 + y1) / 2, rad = (x1 - x0) / 2;
    for (float ang = start_deg; ang <= end_deg; ang += 0.8f) {
        float t = ang * (float)M_PI / 180.0f;
        set_px(img, (int)std::round(cx + rad * std::cos(t)), (int)std::round(cy + rad * std::sin(t)), r, g, b, a);
        if (width > 1) {
            for (int wboost = 1; wboost < width; wboost++) {
                float rr = rad - wboost * 0.7f;
                set_px(img, (int)std::round(cx + rr * std::cos(t)), (int)std::round(cy + rr * std::sin(t)), r, g, b, a);
            }
        }
    }
}
