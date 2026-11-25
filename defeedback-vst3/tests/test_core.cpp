// Core self-tests: run by CI before the plugin is packaged.
#include <cstdio>
#include <cstdlib>

#include "../tools/sim.h"

static int failures = 0;
#define CHECK(cond, ...) do { if (!(cond)) { ++failures; std::printf("FAIL: "); std::printf(__VA_ARGS__); std::printf("\n"); } else { std::printf("ok:   "); std::printf(__VA_ARGS__); std::printf("\n"); } } while (0)

static const double FS = 48000;

static double sineGainDb(dfb::NotchBank& b, double f) {
    std::vector<float> x(48000);
    for (size_t i = 0; i < x.size(); ++i) x[i] = (float)std::sin(2 * M_PI * f * i / FS);
    float* ch[1] = {x.data()};
    b.process(ch, 1, (int)x.size());
    double si = 0, so = 0;
    for (size_t i = 24000; i < x.size(); ++i) { double r = std::sin(2 * M_PI * f * i / FS); si += r * r; so += x[i] * x[i]; }
    return 10 * std::log10(so / si);
}

int main() {
    { // notch accuracy
        dfb::NotchBank b; b.prepare(FS, 1); b.addFixed(1000, -12, 1.0 / 8);
        const double at = sineGainDb(b, 1000);
        b.reset(); b.addFixed(1000, -12, 1.0 / 8);
        const double off = sineGainDb(b, 2000);
        CHECK(std::fabs(at + 12) < 0.5, "fixed -12 dB notch at 1 kHz measures %.2f dB", at);
        CHECK(std::fabs(off) < 0.5, "an octave away it measures %.2f dB", off);
    }
    { // empty engine is transparent (zero latency)
        dfb::Engine e; e.prepare(FS, 2, 64);
        std::vector<float> l(256, 0.f), r(256, 0.f); l[10] = 1.f; r[20] = 0.5f;
        float* ch[2] = {l.data(), r.data()};
        e.process(ch, 2, 256);
        CHECK(l[10] == 1.f && r[20] == 0.5f && l[11] == 0.f, "no cuts = bit-exact passthrough, no latency");
    }
    { // cuts fade in smoothly
        dfb::NotchBank b; b.prepare(FS, 1);
        std::vector<float> x(9600);
        for (size_t i = 0; i < x.size(); ++i) x[i] = (float)(0.5 * std::sin(2 * M_PI * 1000 * i / FS));
        std::vector<float> y = x; float* ch[1] = {y.data()};
        b.process(ch, 1, 2400);
        b.trigger(1000, 1.f);
        ch[0] = y.data() + 2400; b.process(ch, 1, 7200);
        // level per 1 ms cycle must glide, never jump
        double maxStep = 0, prev = 0;
        for (size_t c = 48; c + 48 <= 7200; c += 48) {
            double e = 0; for (size_t i = c; i < c + 48; ++i) e += (double)y[i] * y[i];
            const double db = 10 * std::log10(e / 48 + 1e-12);
            if (c > 48) maxStep = std::max(maxStep, std::fabs(db - prev));
            prev = db;
        }
        CHECK(maxStep < 1.0, "first cut fades in smoothly (largest level change %.2f dB per ms)", maxStep);
        CHECK(b.slots[0].curDb < -6.0, "cut reached %.1f dB within 150 ms", b.slots[0].curDb);
    }
    { // reaction time vs sensitivity on a building ring
        double t[2];
        for (int k = 0; k < 2; ++k) {
            dfb::Engine e; e.prepare(FS, 1, 64); e.setSensitivity(k == 0 ? 0.5f : 1.0f); e.bypass = false;
            std::vector<float> x((size_t)FS * 2);
            sim::Rng r(3);
            for (size_t i = 0; i < x.size(); ++i) {
                const double ts = i / FS;
                const double amp = ts < 0.5 ? 0 : 0.01 * std::pow(10.0, (ts - 0.5) * 20 / 20); // grows 20 dB/s from -40 dBFS
                x[i] = (float)(amp * std::sin(2 * M_PI * 2500 * ts) + 0.003 * r.n());
            }
            t[k] = -1;
            for (size_t i = 0; i < x.size(); i += 64) {
                float* ch[1] = {x.data() + i}; e.process(ch, 1, 64);
                if (t[k] < 0 && e.eventCount.load() > 0) t[k] = i / FS - 0.5;
            }
        }
        CHECK(t[0] > 0 && t[0] < 0.3, "growing ring caught after %.0f ms at sensitivity 50", t[0] * 1000);
        CHECK(t[1] > 0 && t[1] < t[0], "and sooner at sensitivity 100 (%.0f ms)", t[1] * 1000);
    }
    { // no cuts on a sung phrase without feedback
        int total = 0;
        for (int s = 0; s < 3; ++s) {
            sim::Rng r(100 + s);
            auto src = sim::makeSource(sim::Src::Sung, FS, 6, r);
            dfb::Engine e; e.prepare(FS, 1, 64); e.setSensitivity(0.5f);
            for (size_t i = 0; i < src.size(); i += 64) { float* ch[1] = {src.data() + i}; e.process(ch, 1, 64); }
            total += e.eventCount.load();
        }
        CHECK(total <= 3, "sung phrases without feedback: %d triggers in 18 s", total);
    }
    { // closed loop: more gain before feedback
        double sum = 0; int n = 0;
        for (int c = 0; c < 4; ++c) {
            sim::Rng r(500 + c);
            auto src = sim::makeSource(c % 2 ? sim::Src::Speech : sim::Src::Sung, FS, 10, r);
            auto h = sim::makeIR(FS, r, (int)(FS * 0.1));
            auto gain = [](double t) { return -8.0 + 2.0 * t; };
            auto howlAt = [&](const std::vector<float>& out) {
                const int W = 2400; int run = 0;
                for (size_t b = 0; b + W <= out.size(); b += W) {
                    double so = 0, ss = 0;
                    for (int i = 0; i < W; ++i) { so += out[b + i] * out[b + i]; ss += src[b + i] * src[b + i]; }
                    run = (so > 1e-4 && so > ss * 31.6) ? run + 1 : 0;
                    if (run >= 6) return gain((b - 5.0 * W) / FS);
                }
                return gain(10.0);
            };
            auto off = sim::runLoop(src, h, FS, gain, [](float* const*, int) {});
            dfb::Engine e; e.prepare(FS, 1, 64); e.setSensitivity(0.5f);
            auto on = sim::runLoop(src, h, FS, gain, [&](float* const* ch, int m) { e.process(ch, 1, m); });
            sum += howlAt(on.out) - howlAt(off.out); ++n;
        }
        CHECK(sum / n > 3.0, "closed loop: %.1f dB more gain before feedback on average", sum / n);
    }
    std::printf(failures ? "\n%d FAILED\n" : "\nall passed\n", failures);
    return failures ? 1 : 0;
}
