#pragma once
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

namespace wav {
// Minimal WAV I/O: reads PCM16/24/32 and float32 (mixes to channels), writes float32.
inline bool read(const std::string& path, std::vector<std::vector<float>>& ch, double& fs) {
    FILE* f = std::fopen(path.c_str(), "rb"); if (!f) return false;
    std::vector<uint8_t> d; uint8_t tmp[65536]; size_t r;
    while ((r = std::fread(tmp, 1, sizeof tmp, f)) > 0) d.insert(d.end(), tmp, tmp + r);
    std::fclose(f);
    if (d.size() < 12 || std::memcmp(d.data(), "RIFF", 4) || std::memcmp(d.data() + 8, "WAVE", 4)) return false;
    int fmt = 0, nch = 0, bits = 0; size_t pos = 12; const uint8_t* data = nullptr; size_t dlen = 0;
    auto u16 = [&](size_t p) { return (int)(d[p] | d[p + 1] << 8); };
    auto u32 = [&](size_t p) { return (uint32_t)(d[p] | d[p + 1] << 8 | d[p + 2] << 16 | (uint32_t)d[p + 3] << 24); };
    while (pos + 8 <= d.size()) {
        uint32_t len = u32(pos + 4);
        if (!std::memcmp(&d[pos], "fmt ", 4)) { fmt = u16(pos + 8); nch = u16(pos + 10); fs = u32(pos + 12); bits = u16(pos + 22); if (fmt == 0xFFFE) fmt = u16(pos + 32); }
        else if (!std::memcmp(&d[pos], "data", 4)) { data = &d[pos + 8]; dlen = std::min<size_t>(len, d.size() - pos - 8); }
        pos += 8 + len + (len & 1);
    }
    if (!data || nch <= 0) return false;
    const int bps = bits / 8; const size_t frames = dlen / (bps * nch);
    ch.assign(nch, std::vector<float>(frames));
    for (size_t i = 0; i < frames; ++i) for (int c = 0; c < nch; ++c) {
        const uint8_t* p = data + (i * nch + c) * bps; float v = 0;
        if (fmt == 3 && bits == 32) std::memcpy(&v, p, 4);
        else if (bits == 16) v = (int16_t)(p[0] | p[1] << 8) / 32768.f;
        else if (bits == 24) v = ((int32_t)((uint32_t)p[0] << 8 | (uint32_t)p[1] << 16 | (uint32_t)p[2] << 24) >> 8) / 8388608.f;
        else if (bits == 32) v = (int32_t)(p[0] | p[1] << 8 | p[2] << 16 | (uint32_t)p[3] << 24) / 2147483648.f;
        ch[c][i] = v;
    }
    return true;
}

inline bool write(const std::string& path, const std::vector<std::vector<float>>& ch, double fs) {
    FILE* f = std::fopen(path.c_str(), "wb"); if (!f) return false;
    const uint32_t nch = (uint32_t)ch.size(), frames = nch ? (uint32_t)ch[0].size() : 0, bytes = frames * nch * 4;
    auto w32 = [&](uint32_t v) { std::fwrite(&v, 4, 1, f); };
    auto w16 = [&](uint16_t v) { std::fwrite(&v, 2, 1, f); };
    std::fwrite("RIFF", 1, 4, f); w32(36 + bytes); std::fwrite("WAVEfmt ", 1, 8, f);
    w32(16); w16(3); w16((uint16_t)nch); w32((uint32_t)fs); w32((uint32_t)fs * nch * 4); w16((uint16_t)(nch * 4)); w16(32);
    std::fwrite("data", 1, 4, f); w32(bytes);
    for (uint32_t i = 0; i < frames; ++i) for (uint32_t c = 0; c < nch; ++c) std::fwrite(&ch[c][i], 4, 1, f);
    std::fclose(f); return true;
}
} // namespace wav
