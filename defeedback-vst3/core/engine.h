// De-feedback engine: smoothed notch bank on the audio path (zero latency),
// howl detector listening to the processed output on a side chain.
#pragma once
#include <atomic>
#include <cmath>
#include <vector>

#include "detector.h"
#include "notches.h"

namespace dfb {

struct Meter {
    std::atomic<float> peak{0.f}, rms{0.f};
};

class Engine {
public:
    static constexpr int kDisplayBins = 256;

    Detector detector;
    NotchBank bank;
    bool bypass = false;
    bool frozen = false; // stop adding new cuts (keep the ones already there)

    // GUI-side snapshots, written on the audio thread, read anywhere.
    Meter inMeter[kMaxChannels], outMeter[kMaxChannels];
    std::atomic<float> display[kDisplayBins];
    std::atomic<int> numChannels{2};
    struct SlotView { std::atomic<float> freq{0}, db{0}, oct{0}; std::atomic<int> state{0}; }; // 0 off,1 live,2 fixed
    SlotView slotView[kMaxNotches];
    std::atomic<int> eventCount{0};
    std::atomic<float> lastEventFreq{0};

    void prepare(double sampleRate, int channels, int maxBlock) {
        fs = sampleRate;
        chans = std::min(channels, kMaxChannels);
        numChannels = chans;
        detector.prepare(fs);
        bank.prepare(fs, chans);
        mono.assign(std::max(maxBlock, 1), 0.f);
        events.reserve(64);
        for (auto& d : display) d = -120.f;
        meterDecay = std::exp(-1.0 / (0.3 * fs)); // 300 ms peak fall
    }

    double sampleRate() const { return fs; }
    static double displayFreq(int i) { return 20.0 * std::pow(1000.0, (i + 0.5) / kDisplayBins); } // 20 Hz..20 kHz

    void process(float* const* data, int channels, int num) {
        channels = std::min(channels, chans);
        if ((int)mono.size() < num) mono.resize(num); // only if the host lied about block size
        measure(data, channels, num, inMeter);
        if (!bypass) bank.process(data, channels, num);
        measure(data, channels, num, outMeter);

        for (int i = 0; i < num; ++i) {
            float s = 0; for (int c = 0; c < channels; ++c) s += data[c][i];
            mono[i] = channels ? s / channels : 0.f;
        }
        events.clear();
        detector.process(mono.data(), num, events);
        if (!bypass && !frozen)
            for (const auto& e : events) {
                bank.trigger(e.freq, e.severity);
                eventCount.fetch_add(1);
                lastEventFreq = (float)e.freq;
            }
        if (detector.newFrame) publishSpectrum();
        publishSlots();
    }

    // detector + bank settings in one place
    void setSensitivity(float s) { detector.settings.sensitivity = s; }
    void setRingOut(bool r) { detector.settings.ringOut = r; bank.settings.lockAll = r || lockAll; ringOut = r; }
    void setLockAll(bool l) { lockAll = l; bank.settings.lockAll = l || ringOut; if (l) for (auto& s : bank.slots) if (s.active) s.locked = true; }

private:
    void measure(float* const* d, int ch, int num, Meter* m) {
        for (int c = 0; c < ch; ++c) {
            float pk = m[c].peak.load(std::memory_order_relaxed) * (float)std::pow(meterDecay, num);
            double sq = 0;
            for (int i = 0; i < num; ++i) { const float v = std::fabs(d[c][i]); pk = std::max(pk, v); sq += (double)v * v; }
            m[c].peak.store(pk, std::memory_order_relaxed);
            const float r = (float)std::sqrt(sq / std::max(num, 1));
            const float old = m[c].rms.load(std::memory_order_relaxed);
            const float a = (float)(1.0 - std::exp(-num / (0.3 * fs)));
            m[c].rms.store(old + (r - old) * a, std::memory_order_relaxed);
        }
    }

    void publishSpectrum() {
        const auto& db = detector.spectrumDb();
        const double bw = detector.binWidth();
        const int last = (int)db.size() - 1;
        for (int i = 0; i < kDisplayBins; ++i) {
            const double f0 = 20.0 * std::pow(1000.0, (double)i / kDisplayBins);
            const double f1 = 20.0 * std::pow(1000.0, (double)(i + 1) / kDisplayBins);
            int k0 = std::clamp((int)std::floor(f0 / bw), 1, last), k1 = std::clamp((int)std::ceil(f1 / bw), 1, last);
            double v = -200;
            for (int k = k0; k <= k1; ++k) v = std::max(v, db[k]);
            const float old = display[i].load(std::memory_order_relaxed);
            const float nv = (float)std::max(v, -120.0);
            display[i].store(nv > old ? nv : old + (nv - old) * 0.35f, std::memory_order_relaxed); // fast up, smooth down
        }
    }

    void publishSlots() {
        for (int i = 0; i < kMaxNotches; ++i) {
            const auto& s = bank.slots[i];
            slotView[i].freq.store((float)s.freq, std::memory_order_relaxed);
            slotView[i].db.store((float)s.curDb, std::memory_order_relaxed);
            slotView[i].oct.store((float)s.curOct, std::memory_order_relaxed);
            slotView[i].state.store(s.active ? (s.locked ? 2 : 1) : 0, std::memory_order_relaxed);
        }
    }

    double fs = 48000, meterDecay = 0.9999;
    int chans = 2;
    bool lockAll = false, ringOut = false;
    std::vector<float> mono;
    std::vector<HowlEvent> events;
};

} // namespace dfb
