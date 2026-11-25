// Howl (acoustic feedback) detector.
//
// A side-chain FFT finds spectral peaks every hop and follows each one as a
// track. Per track it measures the classic feedback cues from the literature
// (van Waterschoot & Moonen, "Fifty years of acoustic feedback control", 2011):
//   PAPR  peak over the mean spectrum         a howl towers over everything
//   PNPR  peak over its neighbouring bins     a howl is one sharp line
//   PHPR  peak over its 2nd/3rd harmonics     a howl is close to a pure sine
//   SUB   peak over f/2 and f/3               a sung note's overtone has a fundamental below it
//   growth, pitch wobble and level wobble     a howl holds steady or grows; voices move
// A small trained network (classifier.h) turns those cues into P(feedback).
// Original code for this project; nothing is taken from any commercial product.
#pragma once
#include <algorithm>
#include <array>
#include <cmath>
#include <vector>

#include "classifier.h"
#include "fft.h"

namespace dfb {

struct DetectorSettings {
    float sensitivity = 0.5f;  // 0 = cautious, 1 = catches rings fastest
    bool ringOut = false;      // soundcheck mode: faster and more trusting
    double minFreq = 60.0, maxFreq = 16000.0;

    // derived from sensitivity
    double minAgeS() const   { return lerp(0.22, 0.035, s()); }
    float  probThr() const   { return (float)lerp(0.85, 0.42, s()); }
    double floorDb() const   { return lerp(-48.0, -68.0, s()); }
    double minPaprDb() const { return lerp(15.0, 8.0, s()); }
    double s() const { return std::clamp((double)sensitivity + (ringOut ? 0.25 : 0.0), 0.0, 1.0); }
    static double lerp(double a, double b, double t) { return a + (b - a) * t; }
};

struct HowlEvent {
    double freq;
    float prob;
    float severity;
    float levelDb;
};

class Detector {
public:
    static constexpr int kHist = 24;
    static constexpr int kMaxTracks = 32;
    static constexpr int kMaxCand = 16;

    DetectorSettings settings;

    void prepare(double sampleRate) {
        fs = sampleRate;
        n = 4096;
        while (n < fs * 0.08) n <<= 1;        // ~85 ms window, ~11.7 Hz bins at 48 k
        hop = n / 8;
        fft.setSize(n);
        win.resize(n);
        double wsum = 0;
        for (int i = 0; i < n; ++i) { win[i] = 0.5 - 0.5 * std::cos(2 * M_PI * i / (n - 1)); wsum += win[i]; }
        norm = (wsum / 2) * (wsum / 2);
        ring.assign(n, 0.0); frame.assign(n, 0.0);
        pow_.assign(n / 2 + 1, 0.0); db.assign(n / 2 + 1, -200.0);
        binHz = fs / n;
        writePos = 0; sinceHop = 0; frameIdx = 0;
        tracks.clear();
        frameDt = hop / fs;
    }

    int fftSize() const { return n; }
    double nowSeconds() const { return frameIdx * frameDt; }
    double binWidth() const { return binHz; }
    double hopSeconds() const { return frameDt; }
    const std::vector<double>& spectrumDb() const { return db; }
    bool newFrame = false;

    // Feed mono samples. Confirmed howls are appended to `events`.
    void process(const float* x, int num, std::vector<HowlEvent>& events) {
        newFrame = false;
        for (int i = 0; i < num; ++i) {
            ring[writePos] = x[i];
            writePos = (writePos + 1) % n;
            if (++sinceHop >= hop) {
                sinceHop = 0;
                analyse(events);
                newFrame = true;
            }
        }
    }

    // Feature vector for one track (exposed for training/tests).
    struct Track {
        int id = 0;
        double bin = 0;
        int frames = 0, missed = 0;
        int len = 0, head = 0;
        std::array<float, kHist> freq{}, level{}, papr{}, pnpr{}, phpr{}, sub{};
        float pSmooth = 0.f;
        double lastFire = -1e9;
        void push(float f, float l, float a, float b, float c, float d) {
            freq[head] = f; level[head] = l; papr[head] = a; pnpr[head] = b; phpr[head] = c; sub[head] = d;
            head = (head + 1) % kHist; len = std::min(len + 1, kHist);
        }
        float at(const std::array<float, kHist>& a, int back) const { // back=0 newest
            return a[(head - 1 - back + 2 * kHist) % kHist];
        }
    };

    static void features(const Track& t, double frameDt, float* out) {
        const int K = std::min(t.len, 12);
        double sp = 0, sn = 0, sh = 0, ss = 0, sf = 0;
        for (int b = 0; b < K; ++b) {
            sp += t.at(t.papr, b); sn += t.at(t.pnpr, b); sh += t.at(t.phpr, b); ss += t.at(t.sub, b);
            sf += t.at(t.freq, b);
        }
        sp /= K; sn /= K; sh /= K; ss /= K; sf /= K;
        // level slope (dB/s) by least squares, and residual wobble
        double mx = (K - 1) / 2.0, sxy = 0, sxx = 0, ml = 0;
        for (int b = 0; b < K; ++b) ml += t.at(t.level, b);
        ml /= K;
        for (int b = 0; b < K; ++b) { double xx = (K - 1 - b) - mx; sxy += xx * (t.at(t.level, b) - ml); sxx += xx * xx; }
        double slope = sxx > 0 ? sxy / sxx : 0.0;          // dB per frame
        double res = 0;
        for (int b = 0; b < K; ++b) { double xx = (K - 1 - b) - mx; double e = t.at(t.level, b) - (ml + slope * xx); res += e * e; }
        res = std::sqrt(res / K);
        double fv = 0;
        for (int b = 0; b < K; ++b) { double c = 1200.0 * std::log2(t.at(t.freq, b) / sf); fv += c * c; }
        fv = std::sqrt(fv / K);
        out[0] = (float)std::clamp(sp, -10.0, 60.0);
        out[1] = (float)std::clamp(sn, -10.0, 50.0);
        out[2] = (float)std::clamp(sh, -10.0, 50.0);
        out[3] = (float)std::clamp(ss, -20.0, 50.0);
        out[4] = (float)std::clamp(slope / frameDt, -80.0, 150.0);
        out[5] = (float)std::clamp(fv, 0.0, 100.0);
        out[6] = (float)std::clamp(res, 0.0, 12.0);
        out[7] = (float)std::clamp(t.frames * frameDt, 0.0, 2.0);
        out[8] = (float)std::log2(sf / 1000.0);
        out[9] = (float)std::clamp((double)t.at(t.level, 0), -90.0, 6.0);
    }

    // Optional hook for training: called for every decision-ready track.
    void (*featureTap)(void* ctx, double timeS, double freq, const float* f, float p) = nullptr;
    void* tapCtx = nullptr;

private:
    double peakInterp(int k, double& valDb) const {
        const double a = db[k - 1], b = db[k], c = db[k + 1];
        const double d = a - 2 * b + c;
        double p = d < 0 ? 0.5 * (a - c) / d : 0.0;
        p = std::clamp(p, -0.5, 0.5);
        valDb = b - 0.25 * (a - c) * p;
        return k + p;
    }

    double dbNear(double binF) const {
        const int k = (int)std::lround(binF);
        if (k < 2 || k >= (int)db.size() - 2) return -200.0;
        return std::max({db[k - 1], db[k], db[k + 1]});
    }

    void analyse(std::vector<HowlEvent>& events) {
        ++frameIdx;
        for (int i = 0; i < n; ++i) frame[i] = ring[(writePos + i) % n] * win[i];
        fft.power(frame.data(), pow_.data());
        const int kmin = std::max(9, (int)(settings.minFreq / binHz));
        const int kmax = std::min(n / 2 - 9, (int)(settings.maxFreq / binHz));
        double mean = 0;
        for (int k = 0; k <= n / 2; ++k) db[k] = 10.0 * std::log10(pow_[k] / norm + 1e-20);
        for (int k = kmin; k <= kmax; ++k) mean += pow_[k] / norm;
        mean /= (kmax - kmin + 1);
        const double meanDb = 10.0 * std::log10(mean + 1e-20);
        const double floorDb = settings.floorDb();

        struct Cand { double bin, lev, papr, pnpr, phpr, sub; };
        std::array<Cand, kMaxCand> cand; int nc = 0;
        for (int k = kmin; k <= kmax; ++k) {
            const double v = db[k];
            if (v < floorDb || v - meanDb < 6.0) continue;
            if (!(v > db[k - 1] && v >= db[k + 1] && v >= db[k - 2] && v >= db[k + 2])) continue;
            double lev; const double bf = peakInterp(k, lev);
            double nb = 0; int cnt = 0;
            for (int d = 4; d <= 8; ++d) { nb += pow_[k - d] + pow_[k + d]; cnt += 2; }
            const double nbDb = 10.0 * std::log10(nb / cnt / norm + 1e-20);
            const double h = std::max(dbNear(2 * bf), dbNear(3 * bf));
            const double s = std::max(dbNear(bf / 2), dbNear(bf / 3));
            Cand c{bf, lev, lev - meanDb, lev - nbDb, lev - h, lev - s};
            if (nc < kMaxCand) cand[nc++] = c;
            else {
                int w = 0; for (int i = 1; i < kMaxCand; ++i) if (cand[i].lev < cand[w].lev) w = i;
                if (c.lev > cand[w].lev) cand[w] = c;
            }
        }
        std::sort(cand.begin(), cand.begin() + nc, [](const Cand& a, const Cand& b) { return a.lev > b.lev; });

        // track association
        std::array<bool, kMaxTracks> used{}; 
        for (auto& t : tracks) t.missed++;
        for (int ci = 0; ci < nc; ++ci) {
            const auto& c = cand[ci];
            int best = -1; double bd = 2.0;
            for (int ti = 0; ti < (int)tracks.size() && ti < kMaxTracks; ++ti) {
                if (used[ti]) continue;
                const double d = std::fabs(tracks[ti].bin - c.bin);
                if (d < bd) { bd = d; best = ti; }
            }
            if (best < 0) {
                if ((int)tracks.size() >= kMaxTracks) continue;
                tracks.push_back(Track{});
                best = (int)tracks.size() - 1;
                tracks[best].id = nextId++;
            }
            used[best] = true;
            auto& t = tracks[best];
            t.bin = c.bin; t.missed = 0; t.frames++;
            t.push((float)(c.bin * binHz), (float)c.lev, (float)c.papr, (float)c.pnpr,
                   (float)std::min(c.phpr, 50.0), (float)std::min(c.sub, 50.0));
        }
        tracks.erase(std::remove_if(tracks.begin(), tracks.end(), [](const Track& t) { return t.missed > 2; }), tracks.end());

        // decisions
        const double now = frameIdx * frameDt;
        const int minFrames = std::max(2, (int)std::lround(settings.minAgeS() / frameDt));
        float f[kNumFeatures];
        for (auto& t : tracks) {
            if (t.missed > 0 || t.frames < minFrames) continue;
            features(t, frameDt, f);
            const float p = classify(f);
            t.pSmooth = t.frames == minFrames ? p : 0.5f * t.pSmooth + 0.5f * p;
            if (featureTap) featureTap(tapCtx, now, t.bin * binHz, f, p);
            if (f[0] < settings.minPaprDb()) continue;
            if (t.pSmooth < settings.probThr()) continue;
            if (now - t.lastFire < 0.12) continue;
            t.lastFire = now;
            const float sev = (float)std::clamp((f[0] - 15.0) / 25.0 + std::max(0.0, (double)f[4]) / 80.0, 0.0, 1.0);
            events.push_back({t.bin * binHz, t.pSmooth, sev, f[9]});
        }
    }

    double fs = 48000, binHz = 11.7, norm = 1, frameDt = 0.0107;
    int n = 4096, hop = 512, writePos = 0, sinceHop = 0, nextId = 1;
    long long frameIdx = 0;
    FFT fft;
    std::vector<double> win, ring, frame, pow_, db;
    std::vector<Track> tracks;
};

} // namespace dfb
