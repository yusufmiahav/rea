// Smoothed dynamic notch (bell cut) bank.
// Each slot is a TPT state-variable-filter bell (Simper/Zavalishin topology),
// which stays stable and click-free while its gain and width move. Gain and
// width glide with one-pole smoothing, so cuts fade in and out instead of
// snapping. Original code for this project.
#pragma once
#include <algorithm>
#include <array>
#include <cmath>

namespace dfb {

constexpr int kMaxNotches = 12;
constexpr int kMaxChannels = 8;

struct NotchSlot {
    bool active = false;     // holds a frequency (may be fading out)
    bool locked = false;     // never releases (recurring ring, or Lock all)
    double freq = 1000.0;
    double targetDb = 0.0;   // negative = cut
    double targetOct = 0.125;// bandwidth in octaves
    double curDb = 0.0, curOct = 0.125;
    double holdLeft = 0.0;   // seconds before release starts
    double sinceHit = 1e9;   // seconds since last trigger
    int hits = 0;
    float severity = 0.f;
};

struct NotchSettings {
    double maxDepthDb = 18.0;   // deepest cut allowed (positive number)
    double firstCutDb = 4.0;    // first cut depth
    double stepDb = 3.0;        // added each time the ring comes back
    double widthOct = 1.0 / 8.0;
    double holdS = 8.0;         // keep a cut this long after the last hit
    double releaseDbPerS = 1.5; // then fade it out at this rate
    int lockAfterHits = 3;      // a ring that keeps returning becomes a fixed cut
    bool lockAll = false;
    double attackMs = 25.0;     // fade-in time constant
    double minRetriggerS = 0.12;// let the cut act before deepening again
};

class NotchBank {
public:
    void prepare(double sampleRate, int numChannels) {
        fs = sampleRate;
        chans = std::min(numChannels, kMaxChannels);
        reset();
    }

    void reset() {
        for (auto& s : slots) s = NotchSlot{};
        for (auto& c : ic1) c.fill(0.0);
        for (auto& c : ic2) c.fill(0.0);
        for (auto& c : coef) c = Coef{};
    }

    NotchSettings settings;
    std::array<NotchSlot, kMaxNotches> slots{};

    // A howl was confirmed at freq. severity 0..1 makes the first cut deeper.
    // Returns the slot used, or -1.
    int trigger(double freq, float severity) {
        const double nearOct = std::max(settings.widthOct * 0.75, 1.0 / 24.0);
        int best = -1; double bestDist = 1e9;
        for (int i = 0; i < kMaxNotches; ++i) {
            auto& s = slots[i];
            if (!s.active) continue;
            double d = std::fabs(std::log2(freq / s.freq));
            if (d < nearOct && d < bestDist) { best = i; bestDist = d; }
        }
        if (best >= 0) {
            auto& s = slots[best];
            if (s.sinceHit < settings.minRetriggerS) return best; // still acting
            s.hits++;
            s.sinceHit = 0.0;
            s.holdLeft = settings.holdS;
            s.severity = std::max(s.severity, severity);
            s.targetDb = std::max(s.targetDb - settings.stepDb, -settings.maxDepthDb);
            // nearby but not identical ring: widen this bell rather than add another
            double off = std::fabs(std::log2(freq / s.freq));
            s.targetOct = std::min(std::max(s.targetOct, settings.widthOct) + off, 1.0 / 3.0);
            if (s.hits >= settings.lockAfterHits) s.locked = true;
            return best;
        }
        // free slot (fully faded), else steal the shallowest unlocked one
        int slot = -1;
        for (int i = 0; i < kMaxNotches; ++i) if (!slots[i].active) { slot = i; break; }
        if (slot < 0) {
            double shallow = -1e9;
            for (int i = 0; i < kMaxNotches; ++i)
                if (!slots[i].locked && slots[i].curDb > shallow) { shallow = slots[i].curDb; slot = i; }
        }
        if (slot < 0) return -1;
        auto& s = slots[slot];
        const bool reuse = s.active; // stolen: must glide gain to 0 first; just snap state
        s = NotchSlot{};
        s.active = true;
        s.freq = freq;
        s.hits = 1;
        s.sinceHit = 0.0;
        s.holdLeft = settings.holdS;
        s.severity = severity;
        s.targetDb = -std::min(settings.firstCutDb + 6.0 * severity, settings.maxDepthDb);
        s.targetOct = s.curOct = settings.widthOct;
        s.curDb = 0.0;
        s.locked = settings.lockAll;
        (void)reuse;
        resetState(slot);
        return slot;
    }

    void clear(bool keepLocked = false) {
        for (auto& s : slots)
            if (s.active && !(keepLocked && s.locked)) { s.locked = false; s.holdLeft = 0; s.targetDb = 0; }
    }

    // Restore a fixed cut (from saved state).
    void addFixed(double freq, double db, double oct) {
        for (int i = 0; i < kMaxNotches; ++i) if (!slots[i].active) {
            auto& s = slots[i];
            s = NotchSlot{};
            s.active = s.locked = true;
            s.freq = freq; s.targetDb = db; s.curDb = db; s.targetOct = s.curOct = oct;
            s.hits = settings.lockAfterHits; s.holdLeft = settings.holdS;
            resetState(i); computeCoef(i);
            return;
        }
    }

    // In-place processing. data[ch][n].
    void process(float* const* data, int numChannels, int numSamples) {
        numChannels = std::min(numChannels, chans);
        const double dt = 1.0 / fs;
        const double aAtk = 1.0 - std::exp(-1.0 / (settings.attackMs * 1e-3 * fs));
        int pos = 0;
        while (pos < numSamples) {
            const int n = std::min(kSub, numSamples - pos);
            const double blockDt = n * dt;
            // control-rate update every kSub samples
            const double aBlock = 1.0 - std::pow(1.0 - aAtk, n);
            for (int i = 0; i < kMaxNotches; ++i) {
                auto& s = slots[i];
                if (!s.active) continue;
                s.sinceHit += blockDt;
                if (!s.locked && !settings.lockAll) {
                    if (s.holdLeft > 0) s.holdLeft -= blockDt;
                    else s.targetDb = std::min(0.0, s.targetDb + settings.releaseDbPerS * blockDt);
                }
                // deeper cuts glide in fast; releases are already slow via targetDb
                s.curDb += (s.targetDb - s.curDb) * aBlock;
                s.curOct += (s.targetOct - s.curOct) * aBlock;
                if (!s.locked && s.targetDb >= -0.01 && s.curDb > -0.05) { s.active = false; continue; }
                computeCoef(i);
            }
            for (int i = 0; i < kMaxNotches; ++i) {
                if (!slots[i].active) continue;
                const Coef& c = coef[i];
                for (int ch = 0; ch < numChannels; ++ch) {
                    float* x = data[ch] + pos;
                    double s1 = ic1[i][ch], s2 = ic2[i][ch];
                    for (int k = 0; k < n; ++k) {
                        const double v0 = x[k];
                        const double v3 = v0 - s2;
                        const double v1 = c.a1 * s1 + c.a2 * v3;
                        const double v2 = s2 + c.a2 * s1 + c.a3 * v3;
                        s1 = 2.0 * v1 - s1;
                        s2 = 2.0 * v2 - s2;
                        x[k] = (float)(v0 + c.m1 * v1);
                    }
                    ic1[i][ch] = s1; ic2[i][ch] = s2;
                }
            }
            pos += n;
        }
    }

    // Combined magnitude response in dB at f (for the display).
    double responseDb(double f) const {
        double db = 0.0;
        for (const auto& s : slots) {
            if (!s.active) continue;
            // analog bell magnitude: matches the TPT bell well below Nyquist
            const double A = std::pow(10.0, s.curDb / 40.0);
            const double Q = octToQ(s.curOct);
            const double w = f / s.freq;
            const double re = 1.0 - w * w;
            const double imN = w * A / Q, imD = w / (A * Q);
            const double num = re * re + imN * imN, den = re * re + imD * imD;
            db += 10.0 * std::log10(num / den);
        }
        return db;
    }

    static double octToQ(double oct) {
        const double p = std::pow(2.0, oct);
        return std::sqrt(p) / (p - 1.0);
    }

private:
    static constexpr int kSub = 16;
    struct Coef { double a1 = 1, a2 = 0, a3 = 0, m1 = 0; };

    void resetState(int i) { ic1[i].fill(0.0); ic2[i].fill(0.0); }

    void computeCoef(int i) {
        const auto& s = slots[i];
        // Bell with constant-Q-on-cut form: k = 1/(Q*A) for the cut.
        const double A = std::pow(10.0, s.curDb / 40.0);
        const double Q = octToQ(s.curOct);
        const double f = std::min(s.freq, fs * 0.45);
        const double g = std::tan(M_PI * f / fs);
        const double k = 1.0 / (Q * A);
        Coef& c = coef[i];
        c.a1 = 1.0 / (1.0 + g * (g + k));
        c.a2 = g * c.a1;
        c.a3 = g * c.a2;
        c.m1 = k * (A * A - 1.0);
    }

    double fs = 48000.0;
    int chans = 2;
    std::array<Coef, kMaxNotches> coef{};
    std::array<std::array<double, kMaxChannels>, kMaxNotches> ic1{}, ic2{};
};

} // namespace dfb
