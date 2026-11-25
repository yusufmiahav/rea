// dfb_sim: training-data generator, closed-loop evaluation and offline WAV
// processing for the de-feedback engine.
//   dfb_sim data <seed> <clips> <out.csv>
//   dfb_sim eval <seed> <clips> [sensitivity]
//   dfb_sim demo <outdir> [seed]
//   dfb_sim wav <in.wav> <out.wav> [sensitivity]
#include <cstdio>
#include <cstdlib>
#include <map>
#include <memory>
#include <set>
#include <string>

#include "sim.h"
#include "wav.h"

using namespace sim;
static const double FS = 48000.0;

struct TapRow { double t, f; float feat[dfb::kNumFeatures]; float p; };
static void tapCollect(void* ctx, double t, double f, const float* feat, float p) {
    auto* v = (std::vector<TapRow>*)ctx; TapRow r{t, f, {}, p};
    for (int i = 0; i < dfb::kNumFeatures; ++i) r.feat[i] = feat[i];
    v->push_back(r);
}

static std::unique_ptr<dfb::Engine> makeEngine(float sens, bool bypass) {
    auto e = std::make_unique<dfb::Engine>();
    e->prepare(FS, 1, 64);
    e->setSensitivity(sens);
    e->bypass = bypass;
    return e;
}

static double interpMag(const std::vector<double>& mag, int nf, double f) {
    double k = f * nf / FS; int k0 = (int)k; if (k0 < 0 || k0 + 1 >= (int)mag.size()) return 0;
    // max over +-1 bins: howl frequency sits on a local loop-gain maximum
    return std::max({mag[k0 - (k0 > 0)], mag[k0], mag[k0 + 1]});
}

static int cmdData(uint32_t seed, int clips, const char* out) {
    FILE* f = std::fopen(out, "w"); if (!f) return 1;
    std::fprintf(f, "clip,src,t,freq,label");
    for (int i = 0; i < dfb::kNumFeatures; ++i) std::fprintf(f, ",f%d", i);
    std::fprintf(f, "\n");
    Rng r(seed);
    long pos = 0, neg = 0;
    for (int c = 0; c < clips; ++c) {
        Src kind = (Src)(c % (int)Src::COUNT);
        const double secs = 7.0;
        auto src = makeSource(kind, FS, secs, r);
        auto h = makeIR(FS, r, (int)(FS * r.u(0.08, 0.16)));
        int nf = 1; while (nf < (int)h.size() * 4) nf <<= 1;
        auto mag = irMagnitude(h, nf);
        // 1) source alone: every track is a negative
        std::vector<TapRow> a, b;
        {
            auto e = makeEngine(1.0f, true);
            e->detector.featureTap = tapCollect; e->detector.tapCtx = &a;
            std::vector<float> buf = src; float* ch[1] = {buf.data()};
            for (size_t i = 0; i < buf.size(); i += 64) { ch[0] = buf.data() + i; e->process(ch, 1, (int)std::min<size_t>(64, buf.size() - i)); }
        }
        std::set<std::pair<int, int>> srcPeaks; // (frame, bin) where the source has its own tracks
        const double hopS = 512 / FS, bw = FS / 4096;
        for (auto& row : a) {
            std::fprintf(f, "%d,%s,%.4f,%.2f,0", c, srcName(kind), row.t, row.f);
            for (float v : row.feat) std::fprintf(f, ",%.4f", v);
            std::fprintf(f, "\n"); ++neg;
            srcPeaks.insert({(int)std::lround(row.t / hopS), (int)std::lround(row.f / bw)});
        }
        // 2) closed loop, no suppression, gain ramps through the edge of stability
        const double g0 = r.u(-9, -4), g1 = r.u(1, 5);
        auto gain = [&](double t) { return g0 + (g1 - g0) * std::min(1.0, t / (secs * 0.8)); };
        auto e = makeEngine(1.0f, true);
        e->detector.featureTap = tapCollect; e->detector.tapCtx = &b;
        runLoop(src, h, FS, gain, [&](float* const* ch, int m) { e->process(ch, 1, m); });
        for (auto& row : b) {
            const int fr = (int)std::lround(row.t / hopS), bn = (int)std::lround(row.f / bw);
            bool sp = false;
            for (int df = -3; df <= 3 && !sp; ++df) for (int db = -3; db <= 3 && !sp; ++db) sp = srcPeaks.count({fr + df, bn + db}) > 0;
            const double lg = interpMag(mag, nf, row.f) * std::pow(10.0, gain(row.t) / 20.0);
            int label;
            if (sp) { if (lg < 0.6) label = 0; else continue; }
            else if (lg >= 0.75) label = 1;
            else if (lg < 0.5) label = 0;
            else continue;
            std::fprintf(f, "%d,%s,%.4f,%.2f,%d", c, srcName(kind), row.t, row.f, label);
            for (float v : row.feat) std::fprintf(f, ",%.4f", v);
            std::fprintf(f, "\n"); (label ? pos : neg)++;
        }
        std::fprintf(stderr, "clip %d/%d %s rows pos=%ld neg=%ld\n", c + 1, clips, srcName(kind), pos, neg);
    }
    std::fclose(f);
    return 0;
}

// Gain (dB re edge of stability) at which the output first howls for 300 ms.
static double howlOnsetGain(const std::vector<float>& src, const std::vector<float>& out, double secs, double g0, double g1, double* tOut = nullptr) {
    const int W = (int)(FS * 0.05); int run = 0;
    for (size_t b = 0; b + W <= out.size(); b += W) {
        double so = 0, ss = 0;
        for (int i = 0; i < W; ++i) { so += out[b + i] * out[b + i]; ss += src[b + i] * src[b + i]; }
        const bool howl = so > 1e-4 && so > ss * 31.6; // output 15 dB over the dry source
        run = howl ? run + 1 : 0;
        if (run >= 6) {
            double t = (b - 5 * W) / FS; if (tOut) *tOut = t;
            return g0 + (g1 - g0) * std::min(1.0, t / secs);
        }
    }
    if (tOut) *tOut = -1;
    return g1;
}

static int cmdEval(uint32_t seed, int clips, float sens) {
    Rng r(seed);
    std::map<std::string, std::vector<double>> added, falseCuts, falseDepth;
    double sumAdded = 0; int n = 0;
    for (int c = 0; c < clips; ++c) {
        Src kind = (Src)(c % ((int)Src::COUNT - 1)); // skip silence
        const double secs = 16.0;
        auto src = makeSource(kind, FS, secs, r);
        auto h = makeIR(FS, r, (int)(FS * r.u(0.08, 0.16)));
        const double g0 = -8, g1 = 14;
        auto gain = [&](double t) { return g0 + (g1 - g0) * std::min(1.0, t / secs); };
        auto off = runLoop(src, h, FS, gain, [](float* const*, int) {});
        auto e = makeEngine(sens, false);
        auto on = runLoop(src, h, FS, gain, [&](float* const* ch, int m) { e->process(ch, 1, m); });
        const double gOff = howlOnsetGain(src, off.out, secs, g0, g1), gOn = howlOnsetGain(src, on.out, secs, g0, g1);
        added[srcName(kind)].push_back(gOn - gOff);
        sumAdded += gOn - gOff; ++n;
        // false cuts: same source, no loop at all
        auto e2 = makeEngine(sens, false);
        std::vector<float> buf = src;
        for (size_t i = 0; i < buf.size(); i += 64) { float* ch[1] = {buf.data() + i}; e2->process(ch, 1, (int)std::min<size_t>(64, buf.size() - i)); }
        int cuts = e2->eventCount.load(); double deepest = 0;
        for (auto& s : e2->bank.slots) if (s.active) deepest = std::min(deepest, s.curDb);
        falseCuts[srcName(kind)].push_back(cuts); falseDepth[srcName(kind)].push_back(deepest);
        std::fprintf(stderr, "clip %2d %-7s howl at %+5.1f dB off, %+5.1f dB on (+%4.1f), false triggers %d (deepest %.1f dB)\n",
                     c, srcName(kind), gOff, gOn, gOn - gOff, cuts, deepest);
    }
    std::printf("%-8s %10s %14s %12s\n", "source", "added_dB", "false_trig/16s", "false_depth");
    for (auto& [k, v] : added) {
        double a = 0, fc = 0, fd = 0; for (double x : v) a += x; for (double x : falseCuts[k]) fc += x; for (double x : falseDepth[k]) fd += x;
        std::printf("%-8s %10.1f %14.1f %12.1f\n", k.c_str(), a / v.size(), fc / v.size(), fd / v.size());
    }
    std::printf("mean added stable gain: %.1f dB  (model: %s, sensitivity %.2f)\n", sumAdded / n, dfb::model::kInfo, sens);
    return 0;
}

static int cmdDemo(const char* dir, uint32_t seed) {
    Rng r(seed);
    const double secs = 14.0;
    auto src = makeSource(Src::Sung, FS, secs, r);
    auto h = makeIR(FS, r, (int)(FS * 0.12));
    auto gain = [&](double t) { return -6.0 + 12.0 * std::min(1.0, t / (secs * 0.7)); };
    auto off = runLoop(src, h, FS, gain, [](float* const*, int) {});
    auto e = makeEngine(0.5f, false);
    auto on = runLoop(src, h, FS, gain, [&](float* const* ch, int m) { e->process(ch, 1, m); });
    std::string d(dir);
    wav::write(d + "/demo-no-control.wav", {off.out}, FS);
    wav::write(d + "/demo-with-defeedback.wav", {on.out}, FS);
    std::printf("wrote demos; active cuts:\n");
    for (auto& s : e->bank.slots) if (s.active) std::printf("  %7.1f Hz  %5.1f dB  %s\n", s.freq, s.curDb, s.locked ? "fixed" : "live");
    return 0;
}

static int cmdWav(const char* in, const char* out, float sens) {
    std::vector<std::vector<float>> ch; double fs;
    if (!wav::read(in, ch, fs)) { std::fprintf(stderr, "cannot read %s\n", in); return 1; }
    dfb::Engine e; e.prepare(fs, (int)ch.size(), 512); e.setSensitivity(sens);
    const size_t N = ch[0].size();
    std::vector<float*> p(ch.size());
    for (size_t i = 0; i < N; i += 512) {
        for (size_t c = 0; c < ch.size(); ++c) p[c] = ch[c].data() + i;
        e.process(p.data(), (int)ch.size(), (int)std::min<size_t>(512, N - i));
    }
    wav::write(out, ch, fs);
    std::printf("triggers: %d\n", e.eventCount.load());
    for (auto& s : e.bank.slots) if (s.active) std::printf("  %7.1f Hz  %5.1f dB  %s\n", s.freq, s.curDb, s.locked ? "fixed" : "live");
    return 0;
}

int main(int argc, char** argv) {
    if (argc < 2) { std::fprintf(stderr, "usage: see source header\n"); return 2; }
    std::string cmd = argv[1];
    if (cmd == "data" && argc >= 5) return cmdData(std::atoi(argv[2]), std::atoi(argv[3]), argv[4]);
    if (cmd == "eval" && argc >= 4) return cmdEval(std::atoi(argv[2]), std::atoi(argv[3]), argc > 4 ? std::atof(argv[4]) : 0.5f);
    if (cmd == "demo" && argc >= 3) return cmdDemo(argv[2], argc > 3 ? std::atoi(argv[3]) : 7);
    if (cmd == "wav" && argc >= 4) return cmdWav(argv[2], argv[3], argc > 4 ? std::atof(argv[4]) : 0.5f);
    std::fprintf(stderr, "bad arguments\n"); return 2;
}
