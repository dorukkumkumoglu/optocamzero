/* optocam_gif.h — GIF encoding for the exact OptoCam port.
 * Mirrors the Python save path: ONE adaptive palette built from frame 0
 * (median cut, 256 colours), every frame quantised against it with NO
 * dithering, caller-set frame delay (the recording interval), infinite
 * loop, disposal 1. GIF89a + LZW.
 */
#pragma once
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <vector>
#include <algorithm>
#include <unistd.h>

struct GifPalette { uint8_t rgb[256][3]; int count = 0; };

/* median-cut over a sample of frame-0 pixels (PIL Image.ADAPTIVE equivalent) */
inline GifPalette gif_build_palette(const uint8_t *rgb, int npx) {
    struct Box { std::vector<uint32_t> px; };
    std::vector<uint32_t> all;
    all.reserve(npx / 4);
    for (int i = 0; i < npx; i += 4)                    /* sample every 4th px */
        all.push_back((uint32_t)rgb[i*3] << 16 | rgb[i*3+1] << 8 | rgb[i*3+2]);
    std::vector<std::vector<uint32_t>> boxes{all};
    while ((int)boxes.size() < 256) {
        /* split the box with the largest channel range */
        int best = -1, bestRange = -1, bestCh = 0;
        for (int b = 0; b < (int)boxes.size(); b++) {
            if (boxes[b].size() < 2) continue;
            uint8_t mn[3] = {255,255,255}, mx[3] = {0,0,0};
            for (uint32_t p : boxes[b])
                for (int c = 0; c < 3; c++) {
                    uint8_t v = (p >> (16 - c*8)) & 0xFF;
                    mn[c] = std::min(mn[c], v); mx[c] = std::max(mx[c], v);
                }
            for (int c = 0; c < 3; c++)
                if (mx[c] - mn[c] > bestRange) { bestRange = mx[c] - mn[c]; best = b; bestCh = c; }
        }
        if (best < 0 || bestRange <= 0) break;
        int sh = 16 - bestCh * 8;
        auto &bx = boxes[best];
        std::sort(bx.begin(), bx.end(), [sh](uint32_t a, uint32_t b) {
            return ((a >> sh) & 0xFF) < ((b >> sh) & 0xFF); });
        std::vector<uint32_t> hi(bx.begin() + bx.size()/2, bx.end());
        bx.resize(bx.size()/2);
        boxes.push_back(std::move(hi));
    }
    GifPalette pal;
    for (auto &bx : boxes) {
        if (bx.empty() || pal.count >= 256) continue;
        uint64_t r = 0, g = 0, b = 0;
        for (uint32_t p : bx) { r += (p>>16)&0xFF; g += (p>>8)&0xFF; b += p&0xFF; }
        pal.rgb[pal.count][0] = (uint8_t)(r / bx.size());
        pal.rgb[pal.count][1] = (uint8_t)(g / bx.size());
        pal.rgb[pal.count][2] = (uint8_t)(b / bx.size());
        pal.count++;
    }
    while (pal.count < 256) { memset(pal.rgb[pal.count], 0, 3); pal.count++; }
    return pal;
}

/* nearest-palette map with a 15-bit lookup cache (no dithering, like the app) */
struct GifQuant {
    const GifPalette &pal;
    std::vector<int16_t> cache;
    GifQuant(const GifPalette &p) : pal(p), cache(1 << 15, -1) {}
    uint8_t map(uint8_t r, uint8_t g, uint8_t b) {
        int key = (r >> 3 << 10) | (g >> 3 << 5) | (b >> 3);
        int16_t &e = cache[key];
        if (e >= 0) return (uint8_t)e;
        int best = 0, bd = 1 << 30;
        for (int i = 0; i < 256; i++) {
            int dr = r - pal.rgb[i][0], dg = g - pal.rgb[i][1], db = b - pal.rgb[i][2];
            int d = dr*dr + dg*dg + db*db;
            if (d < bd) { bd = d; best = i; }
        }
        e = (int16_t)best;
        return (uint8_t)best;
    }
};

/* LZW for GIF, 8-bit min code size */
struct GifLZW {
    FILE *f; uint8_t buf[255]; int nbuf = 0;
    uint32_t acc = 0; int nbits = 0;
    void byte(uint8_t b) { buf[nbuf++] = b; if (nbuf == 255) { fputc(255, f); fwrite(buf, 1, 255, f); nbuf = 0; } }
    void code(int c, int size) {
        acc |= (uint32_t)c << nbits; nbits += size;
        while (nbits >= 8) { byte(acc & 0xFF); acc >>= 8; nbits -= 8; }
    }
    void flushbits() { if (nbits > 0) byte(acc & 0xFF); acc = 0; nbits = 0; }
    void flushblk() { if (nbuf) { fputc(nbuf, f); fwrite(buf, 1, nbuf, f); nbuf = 0; } fputc(0, f); }
};
inline void gif_write_image_lzw(FILE *f, const uint8_t *idx, int n) {
    fputc(8, f);                                       /* min code size */
    GifLZW z; z.f = f;
    const int CLEAR = 256, END = 257;
    std::vector<int32_t> next(4096 * 256, -1);         /* [code][byte] -> code */
    int ncodes = 258, csize = 9;
    z.code(CLEAR, csize);
    int cur = idx[0];
    for (int i = 1; i < n; i++) {
        uint8_t k = idx[i];
        int32_t &slot = next[(size_t)cur * 256 + k];
        if (slot >= 0) { cur = slot; continue; }
        z.code(cur, csize);
        if (ncodes < 4096) {
            slot = ncodes++;
            if (ncodes - 1 == (1 << csize) && csize < 12) csize++;
        } else {
            z.code(CLEAR, csize);
            std::fill(next.begin(), next.end(), -1);
            ncodes = 258; csize = 9;
        }
        cur = k;
    }
    z.code(cur, csize);
    z.code(END, csize);
    z.flushbits();
    z.flushblk();
}

/* write a complete looping GIF: frames = quantised index buffers w*h */
inline bool gif_write(const char *path, int w, int h, const GifPalette &pal,
                      const std::vector<std::vector<uint8_t>> &frames, int frame_ms) {
    FILE *f = fopen(path, "wb");
    if (!f) return false;
    fwrite("GIF89a", 1, 6, f);
    uint8_t lsd[7] = {(uint8_t)(w&0xFF),(uint8_t)(w>>8),(uint8_t)(h&0xFF),(uint8_t)(h>>8),
                      0xF7 /* GCT, 256 colours, 8-bit */, 0, 0};
    fwrite(lsd, 1, 7, f);
    fwrite(pal.rgb, 1, 256*3, f);
    /* NETSCAPE loop extension (loop=0 forever) */
    const uint8_t loopext[] = {0x21,0xFF,0x0B,'N','E','T','S','C','A','P','E','2','.','0',3,1,0,0,0};
    fwrite(loopext, 1, sizeof loopext, f);
    for (auto &fr : frames) {
        int cs = frame_ms / 10;
        uint8_t gce[8] = {0x21,0xF9,4, 1<<2 /* disposal 1 */, (uint8_t)(cs&0xFF),(uint8_t)(cs>>8), 0, 0};
        fwrite(gce, 1, 8, f);
        uint8_t id[10] = {0x2C,0,0,0,0,(uint8_t)(w&0xFF),(uint8_t)(w>>8),(uint8_t)(h&0xFF),(uint8_t)(h>>8),0};
        fwrite(id, 1, 10, f);
        gif_write_image_lzw(f, fr.data(), w*h);
    }
    fputc(0x3B, f);                                    /* trailer */
    fflush(f); fsync(fileno(f)); fclose(f);
    return true;
}

/* ---------- GIF decode (LZW) — targets this camera's own files: GCT-256,
 * full-frame images, no transparency/interlace (PIL decoded the same files) */
inline bool gif_decode(const char *path, int &w, int &h,
                       std::vector<std::vector<uint8_t>> &rgb_frames, int max_frames = 64) {
    FILE *f = fopen(path, "rb");
    if (!f) return false;
    uint8_t hdr[13];
    if (fread(hdr, 1, 13, f) != 13 || memcmp(hdr, "GIF8", 4)) { fclose(f); return false; }
    w = hdr[6] | hdr[7] << 8; h = hdr[8] | hdr[9] << 8;
    uint8_t gct[256][3] = {};
    if (hdr[10] & 0x80) {
        int n = 2 << (hdr[10] & 7);
        if ((int)fread(gct, 3, n, f) != n) { fclose(f); return false; }
    }
    auto skip_blocks = [&] { int n; while ((n = fgetc(f)) > 0) fseek(f, n, SEEK_CUR); };
    while (rgb_frames.size() < (size_t)max_frames) {
        int b = fgetc(f);
        if (b == 0x3B || b < 0) break;                 /* trailer / EOF */
        if (b == 0x21) { fgetc(f); skip_blocks(); continue; }
        if (b != 0x2C) break;
        uint8_t id[9]; if (fread(id, 1, 9, f) != 9) break;
        int iw = id[4] | id[5] << 8, ih = id[6] | id[7] << 8;
        if (id[8] & 0x80) fseek(f, 3 * (2 << (id[8] & 7)), SEEK_CUR);  /* skip LCT */
        int mincode = fgetc(f);
        /* LZW decode */
        std::vector<uint8_t> idx; idx.reserve((size_t)iw * ih);
        std::vector<int16_t> prefix(4096); std::vector<uint8_t> suffix(4096), stack(4096);
        int CLEAR = 1 << mincode, END = CLEAR + 1, ncodes = END + 1, csize = mincode + 1;
        int prev = -1; uint32_t acc = 0; int nbits = 0, blk = 0;
        auto getcode = [&]() -> int {
            while (nbits < csize) {
                if (blk == 0) { blk = fgetc(f); if (blk <= 0) return END; }
                acc |= (uint32_t)fgetc(f) << nbits; nbits += 8; blk--;
            }
            int c = acc & ((1 << csize) - 1); acc >>= csize; nbits -= csize;
            return c;
        };
        bool ended = false;
        while ((int)idx.size() < iw * ih && !ended) {
            int c = getcode();
            if (c == END) { ended = true; break; }
            if (c == CLEAR) { ncodes = END + 1; csize = mincode + 1; prev = -1; continue; }
            if (prev < 0) { idx.push_back((uint8_t)c); prev = c; continue; }
            int sp = 0, code = (c >= ncodes) ? prev : c;
            while (code >= END + 1) { stack[sp++] = suffix[code]; code = prefix[code]; }
            uint8_t first = (uint8_t)code;
            idx.push_back(first);                       /* root char, then the tail */
            while (sp > 0) idx.push_back(stack[--sp]);
            if (c >= ncodes) idx.push_back(first);      /* KwK case */
            if (ncodes < 4096) {
                prefix[ncodes] = (int16_t)prev; suffix[ncodes] = first; ncodes++;
                if (ncodes == (1 << csize) && csize < 12) csize++;
            }
            prev = c;
        }
        while (blk > 0) { fgetc(f); blk--; }           /* drain current sub-block */
        skip_blocks();                                  /* then remaining sub-blocks */
        rgb_frames.emplace_back((size_t)w * h * 3);
        auto &out = rgb_frames.back();
        for (size_t i = 0; i < idx.size() && i < (size_t)w * h; i++) {
            out[i*3] = gct[idx[i]][0]; out[i*3+1] = gct[idx[i]][1]; out[i*3+2] = gct[idx[i]][2];
        }
    }
    fclose(f);
    return !rgb_frames.empty();
}

/* first frame's GCE delay in ms (0 if none/none yet) — playback pacing */
inline int gif_first_delay_ms(const char *path) {
    FILE *f = fopen(path, "rb");
    if (!f) return 0;
    uint8_t hdr[13];
    if (fread(hdr, 1, 13, f) != 13) { fclose(f); return 0; }
    if (hdr[10] & 0x80) fseek(f, 3 * (2 << (hdr[10] & 7)), SEEK_CUR);
    auto skip_blocks = [&] { int n; while ((n = fgetc(f)) > 0) fseek(f, n, SEEK_CUR); };
    int ms = 0, b;
    while ((b = fgetc(f)) >= 0 && b != 0x3B && b != 0x2C) {
        if (b != 0x21) break;
        int label = fgetc(f), n = fgetc(f);
        if (label == 0xF9 && n >= 4) {
            uint8_t d[4]; if (fread(d, 1, 4, f) != 4) break;
            ms = (d[1] | d[2] << 8) * 10;
            fseek(f, n - 4, SEEK_CUR);
            skip_blocks();
            break;
        }
        fseek(f, n, SEEK_CUR);
        skip_blocks();
    }
    fclose(f);
    return ms;
}

/* count frames without decoding (image descriptors only) — PIL's n_frames */
inline int gif_frame_count(const char *path) {
    FILE *f = fopen(path, "rb");
    if (!f) return 0;
    uint8_t hdr[13];
    if (fread(hdr, 1, 13, f) != 13) { fclose(f); return 0; }
    if (hdr[10] & 0x80) fseek(f, 3 * (2 << (hdr[10] & 7)), SEEK_CUR);
    auto skip_blocks = [&] { int n; while ((n = fgetc(f)) > 0) fseek(f, n, SEEK_CUR); };
    int count = 0, b;
    while ((b = fgetc(f)) >= 0 && b != 0x3B) {
        if (b == 0x21) { fgetc(f); skip_blocks(); }
        else if (b == 0x2C) {
            uint8_t id[9]; if (fread(id, 1, 9, f) != 9) break;
            if (id[8] & 0x80) fseek(f, 3 * (2 << (id[8] & 7)), SEEK_CUR);
            fgetc(f);                                  /* min code size */
            skip_blocks();
            count++;
        } else break;
    }
    fclose(f);
    return count;
}
