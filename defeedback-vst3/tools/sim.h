// Closed-loop feedback simulator used for training data and tests.
// mic = source + room(speaker); speaker = clip(G * plugin(mic)), with delay.
#pragma once
#include <cmath>
#include <cstdint>
#include <random>
#include <vector>

#include "../core/engine.h"
#include "../core/fft.h"

namespace sim {

struct Rng {
    std::mt19937 g;
    explicit Rng(uint32_t s) : g(s) {}
    double u(double a = 0, double b = 1) { return std::uniform_real_distribution<double>(a, b)(g); }
    double n() { return std::normal_distribution<double>(0, 1)(g); }
    int i(int a, int b) { return std::uniform_int_distribution<int>(a, b)(g); }
};

// ---------------------------------------------------------------- sources
enum class Src { Sung, Held, Speech, Chord, Noise, Drums, Silence, COUNT };
inline const char* srcName(Src s) {
    static const char* n[] = {"sung", "held", "speech", "chord", "noise", "drums", "silence"};
    return n[(int)s];
}

struct Voice { // harmonic voice with vibrato, formants, phrasing
    double f0, vibHz, vibCents, jitter, bright; int harm; double onP, offP;
};

inline void addVoice(std::vector<float>& y, double fs, Rng& r, const Voice& v, double gain, bool phrased) {
    const int N = (int)y.size();
    std::vector<double> amps(v.harm + 1);
    // random formant-ish envelope
    double F1 = r.u(300, 900), F2 = r.u(900, 2600);
    for (int h = 1; h <= v.harm; ++h) {
        double f = v.f0 * h;
        double env = std::pow(h, -v.bright) * (1.0 + 2.5 * std::exp(-std::pow((f - F1) / 200, 2)) + 1.8 * std::exp(-std::pow((f - F2) / 300, 2)));
        amps[h] = env;
    }
    double ph[64] = {0}; double drift = 0; double env = phrased ? 0 : 1; bool on = true;
    double target = 1; double f0 = v.f0; double nextChange = r.u(0.4, 1.2);
    double vibPh = r.u(0, 6.28);
    for (int n = 0; n < N; ++n) {
        double t = n / fs;
        if (phrased && t > nextChange) { on = !on; nextChange = t + (on ? r.u(0.5, 2.0) : r.u(0.15, 0.6)); if (on && r.u() < 0.6) f0 = v.f0 * std::pow(2.0, r.i(-5, 7) / 12.0); }
        target = on ? 1 : 0;
        env += (target - env) * (on ? 0.002 : 0.0008);
        drift += (r.n() * v.jitter - drift) * 0.001;
        vibPh += 2 * M_PI * v.vibHz / fs;
        double cents = v.vibCents * std::sin(vibPh) + drift * 100;
        double f = f0 * std::pow(2.0, cents / 1200.0);
        double s = 0;
        for (int h = 1; h <= v.harm && h < 64; ++h) {
            if (f * h > fs * 0.45) break;
            ph[h] += 2 * M_PI * f * h / fs;
            if (ph[h] > 6.283185307) ph[h] -= 6.283185307;
            s += amps[h] * std::sin(ph[h]);
        }
        y[n] += (float)(gain * env * s);
    }
}

inline std::vector<float> makeSource(Src kind, double fs, double secs, Rng& r) {
    std::vector<float> y((size_t)(fs * secs), 0.f);
    const int N = (int)y.size();
    switch (kind) {
    case Src::Sung: {
        Voice v{r.u(110, 520), r.u(4.5, 6.5), r.u(15, 70), r.u(0.05, 0.3), r.u(0.8, 1.6), 30, 0, 0};
        addVoice(y, fs, r, v, 1.0, true); break; }
    case Src::Held: { // the hard case: steady notes, few harmonics, little/no vibrato
        int notes = r.i(1, 2);
        for (int k = 0; k < notes; ++k) {
            Voice v{r.u(150, 1800), r.u(4, 6), r.u(0, 8), r.u(0.0, 0.05), r.u(1.5, 4.0), r.i(1, 8), 0, 0};
            addVoice(y, fs, r, v, 1.0, r.u() < 0.5);
        }
        break; }
    case Src::Speech: {
        Voice v{r.u(90, 250), 0.0, 0.0, r.u(1.5, 4.0), 1.2, 40, 0, 0};
        addVoice(y, fs, r, v, 1.0, true);
        double p = 0;
        for (int n = 0; n < N; ++n) { // fricative bursts
            p = 0.995 * p + (r.u() < 0.0001 ? 1 : 0);
            y[n] += (float)(0.3 * p * r.n());
        }
        break; }
    case Src::Chord: {
        double root = r.u(100, 300);
        int iv[3] = {0, r.u() < 0.5 ? 4 : 3, 7};
        for (int k = 0; k < 3; ++k) {
            Voice v{root * std::pow(2.0, iv[k] / 12.0), r.u(4, 6), r.u(0, 20), r.u(0, 0.1), r.u(0.9, 2.0), 20, 0, 0};
            addVoice(y, fs, r, v, 0.6, false);
        }
        break; }
    case Src::Noise: {
        double b0 = 0, b1 = 0, b2 = 0;
        for (int n = 0; n < N; ++n) { double w = r.n(); b0 = 0.99765 * b0 + w * 0.099; b1 = 0.963 * b1 + w * 0.2965; b2 = 0.57 * b2 + w * 1.0527; y[n] = (float)((b0 + b1 + b2 + w * 0.1848) * 0.2); }
        break; }
    case Src::Drums: {
        double env = 0, ph = 0, f = 60;
        for (int n = 0; n < N; ++n) {
            if (r.u() < 3.0 / fs) { env = 1; f = r.u(50, 200); }
            env *= 0.9996; ph += 2 * M_PI * f / fs; f *= 0.99995;
            y[n] = (float)(env * (0.7 * std::sin(ph) + 0.3 * r.n() * env));
        }
        break; }
    default: break;
    }
    // normalise to a random level (RMS around -30..-14 dBFS)
    double sq = 0; for (float v : y) sq += v * v;
    double rms = std::sqrt(sq / std::max(N, 1));
    if (rms > 1e-9) { double g = std::pow(10.0, r.u(-30, -14) / 20.0) / rms; for (auto& v : y) v = (float)(v * g); }
    for (int n = 0; n < N; ++n) y[n] += (float)(r.n() * 1e-4); // mic self-noise
    return y;
}

// ---------------------------------------------------------------- room / PA path
// Loudspeaker-to-mic impulse response: delay, early reflections, a decaying
// tail, and a coloured PA/monitor response with a few resonances (this is what
// sets which frequencies ring first). Normalised so max |H(f)| == 1.
inline std::vector<double> makeIR(double fs, Rng& r, int len) {
    std::vector<double> h(len, 0.0);
    const int d0 = (int)(fs * r.u(0.002, 0.012));
    h[d0] = 1.0;
    for (int k = 0; k < 6; ++k) { int d = d0 + (int)(fs * r.u(0.001, 0.025)); if (d < len) h[d] += r.u(-0.6, 0.6); }
    const double rt60 = r.u(0.25, 1.0);
    const double decay = std::exp(-6.9 / (rt60 * fs));
    double e = r.u(0.05, 0.3);
    for (int n = d0 + (int)(fs * 0.005); n < len; ++n) { h[n] += e * r.n() * 0.3; e *= decay; }
    // colour with a few peaking resonances (biquad, RBJ)
    int npk = r.i(2, 5);
    for (int k = 0; k < npk; ++k) {
        double f = std::exp(r.u(std::log(150.0), std::log(6000.0))), g = r.u(3, 10), Q = r.u(2, 8);
        double A = std::pow(10, g / 40), w = 2 * M_PI * f / fs, al = std::sin(w) / (2 * Q);
        double b0 = 1 + al * A, b1 = -2 * std::cos(w), b2 = 1 - al * A, a0 = 1 + al / A, a1 = b1, a2 = 1 - al / A;
        double x1 = 0, x2 = 0, y1 = 0, y2 = 0;
        for (int n = 0; n < len; ++n) { double x = h[n]; double yy = (b0 * x + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2) / a0; x2 = x1; x1 = x; y2 = y1; y1 = yy; h[n] = yy; }
    }
    // band-limit like a PA (HPF ~80 Hz, LPF ~12 kHz, one-pole each)
    double lp = 0, hp = 0, prev = 0, ah = std::exp(-2 * M_PI * 80 / fs), al = 1 - std::exp(-2 * M_PI * 12000 / fs);
    for (int n = 0; n < len; ++n) { lp += (h[n] - lp) * al; hp = ah * (hp + lp - prev); prev = lp; h[n] = hp; }
    // normalise peak magnitude
    int nf = 1; while (nf < len * 2) nf <<= 1;
    dfb::FFT fft(nf); std::vector<double> buf(nf, 0.0), p(nf / 2 + 1);
    for (int n = 0; n < len; ++n) buf[n] = h[n];
    fft.power(buf.data(), p.data());
    double mx = 0; for (int k = (int)(80.0 * nf / fs); k < (int)(12000.0 * nf / fs); ++k) mx = std::max(mx, p[k]);
    const double s = 1.0 / std::sqrt(mx);
    for (auto& v : h) v *= s;
    return h;
}

inline std::vector<double> irMagnitude(const std::vector<double>& h, int nf) {
    dfb::FFT fft(nf); std::vector<double> buf(nf, 0.0), p(nf / 2 + 1);
    for (size_t n = 0; n < h.size() && (int)n < nf; ++n) buf[n] = h[n];
    fft.power(buf.data(), p.data());
    for (auto& v : p) v = std::sqrt(v);
    return p;
}

// ---------------------------------------------------------------- closed loop
struct LoopResult {
    std::vector<float> mic, out;
    double howlSecs = 0;    // seconds where output was dominated by a ring
};

// gainDb(t): forward gain relative to the edge of stability (0 dB = loop gain 1 at the worst frequency).
template <class GainFn, class Plugin>
LoopResult runLoop(const std::vector<float>& src, const std::vector<double>& h, double fs, GainFn gainDb, Plugin&& plugin, int block = 64) {
    const int N = (int)src.size(), L = (int)h.size();
    LoopResult res; res.mic.assign(N, 0.f); res.out.assign(N, 0.f);
    std::vector<double> spk(N + L, 0.0); // speaker history (index n+L)
    // h[0..d0) are zero so mic at n only depends on speaker at n-d0; block <= d0 keeps it causal
    std::vector<float> buf(block);
    for (int b = 0; b < N; b += block) {
        const int m = std::min(block, N - b);
        for (int i = 0; i < m; ++i) {
            const int n = b + i;
            double acc = 0;
            const double* sp = &spk[n + L];
            for (int k = 1; k < L; ++k) acc += h[k] * sp[-k];
            buf[i] = (float)(src[n] + acc);
            res.mic[n] = buf[i];
        }
        float* ch[1] = {buf.data()};
        plugin(ch, m);
        for (int i = 0; i < m; ++i) {
            const int n = b + i;
            res.out[n] = buf[i];
            const double g = std::pow(10.0, gainDb(n / fs) / 20.0);
            spk[n + L] = std::tanh(g * buf[i]); // amp/speaker clip keeps a howl bounded
        }
    }
    return res;
}

} // namespace sim
