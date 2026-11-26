// De-feedback engine: smoothed notch bank on the audio path (zero latency),
// howl detector listening to the processed output on a side chain.
#pragma once
#include <atomic>
#include <cmath>
#include <cstdint>
#include <chrono>
#include <thread>
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
    // ---- Learn mode: every decision-ready track's cues go into a lock-free FIFO
    // that the message thread drains to a CSV for retraining on real sessions.
    struct LearnRow { double t, freq; float feat[kNumFeatures]; float p; uint8_t kind, mode; }; // kind 0 = track cues, 1 = a cut was made
    static constexpr int kLearnCap = 8192;
    std::atomic<bool> learn{false};
    std::atomic<int> learnDropped{0};
    int popLearn(LearnRow* out, int max) {
        const int w = lw.load(std::memory_order_acquire), r = lr.load(std::memory_order_relaxed);
        int n = 0;
        for (int i = r; i != w && n < max; i = (i + 1) % kLearnCap) out[n++] = learnBuf[i];
        lr.store((r + n) % kLearnCap, std::memory_order_release);
        return n;
    }
    double nowSeconds() const { return nowS.load(std::memory_order_relaxed); }

    struct SlotView { std::atomic<float> freq{0}, db{0}, oct{0}; std::atomic<int> state{0}; }; // 0 off,1 live,2 fixed
    SlotView slotView[kMaxNotches];
    std::atomic<int> eventCount{0};
    std::atomic<float> lastEventFreq{0};

    void prepare(double sampleRate, int channels, int maxBlock) {
        stopWorker();
        fs = sampleRate;
        chans = std::min(channels, kMaxChannels);
        numChannels = chans;
        detector.prepare(fs);
        detector.featureTap = &Engine::tap;
        detector.tapCtx = this;
        learnBuf.resize(kLearnCap);
        bank.prepare(fs, chans);
        mono.assign(std::max(maxBlock, 1), 0.f);
        events.reserve(64);
        for (auto& d : display) d = -120.f;
        meterDecay = std::exp(-1.0 / (0.3 * fs)); // 300 ms peak fall
        mono.assign(std::max(maxBlock, 8192), 0.f);
        ring.assign(kRing, 0.f); ringW = 0; ringR = 0;
        evW = 0; evR = 0;
        work.assign(kRing, 0.f);
        if (threaded) startWorker();
    }

    void stopWorker() {
        running = false;
        if (worker.joinable()) worker.join();
    }

    double sampleRate() const { return fs; }
    static double displayFreq(int i) { return 20.0 * std::pow(1000.0, (i + 0.5) / kDisplayBins); } // 20 Hz..20 kHz

    // Threaded mode (the plugin): the FFT analysis runs on a background thread
    // so the audio callback only does the notch filters plus a copy. Offline
    // tools leave it off and get the same, fully deterministic behaviour.
    void setThreaded(bool t) { threaded = t; }
    ~Engine() { stopWorker(); }

    void process(float* const* data, int channels, int num) {
        channels = std::min(channels, chans);
        if ((int)mono.size() < num) mono.resize(num); // only if the host lied about block size
        measure(data, channels, num, inMeter);

        // cuts found by the analysis thread
        HowlEvent ev;
        while (popEvent(ev)) applyEvent(ev);

        if (!bypass) bank.process(data, channels, num);
        measure(data, channels, num, outMeter);

        for (int i = 0; i < num; ++i) {
            float s = 0; for (int c = 0; c < channels; ++c) s += data[c][i];
            mono[i] = channels ? s / channels : 0.f;
        }
        bypassFlag.store(bypass, std::memory_order_relaxed);
        frozenFlag.store(frozen, std::memory_order_relaxed);
        if (threaded && worker.joinable()) {
            pushSamples(mono.data(), num);
        } else {
            analyse(mono.data(), num);
            while (popEvent(ev)) applyEvent(ev);
        }
        publishSlots();
    }

    // detector + bank settings in one place
    void setSensitivity(float s) { sensAtomic.store(s, std::memory_order_relaxed); if (!worker.joinable()) detector.settings.sensitivity = s; }
    void setRingOut(bool r) { ringOutAtomic.store(r); if (!worker.joinable()) detector.settings.ringOut = r; bank.settings.lockAll = r || lockAll; ringOut = r; }
    void setLockAll(bool l) { lockAll = l; bank.settings.lockAll = l || ringOut; if (l) for (auto& s : bank.slots) if (s.active) s.locked = true; }

private:
    static constexpr int kRing = 1 << 17, kEv = 256;

    void startWorker() {
        running = true;
        worker = std::thread([this] {
            while (running.load(std::memory_order_relaxed)) {
                const size_t w = ringW.load(std::memory_order_acquire), r = ringR.load(std::memory_order_relaxed);
                size_t n = w - r;
                if (n == 0) { std::this_thread::sleep_for(std::chrono::milliseconds(2)); continue; }
                if (n > (size_t)kRing) { ringR.store(w - kRing); continue; } // fell far behind: skip ahead
                n = std::min<size_t>(n, 4096);
                for (size_t i = 0; i < n; ++i) work[i] = ring[(r + i) & (kRing - 1)];
                ringR.store(r + n, std::memory_order_release);
                detector.settings.sensitivity = sensAtomic.load(std::memory_order_relaxed);
                detector.settings.ringOut = ringOutAtomic.load(std::memory_order_relaxed);
                analyse(work.data(), (int)n);
            }
        });
    }

    void pushSamples(const float* x, int n) {
        const size_t w = ringW.load(std::memory_order_relaxed), r = ringR.load(std::memory_order_acquire);
        if (w + n - r > (size_t)kRing) return; // analysis thread stalled: drop, never block audio
        for (int i = 0; i < n; ++i) ring[(w + i) & (kRing - 1)] = x[i];
        ringW.store(w + n, std::memory_order_release);
    }

    // Runs on the analysis thread (or inline when not threaded).
    void analyse(const float* x, int n) {
        events.clear();
        detector.process(x, n, events);
        nowS.store(detector.nowSeconds(), std::memory_order_relaxed);
        const bool act = !bypassFlag.load(std::memory_order_relaxed) && !frozenFlag.load(std::memory_order_relaxed);
        for (const auto& e : events) {
            if (!act) continue;
            pushCut(e);
            const int w = evW.load(std::memory_order_relaxed), nw = (w + 1) % kEv;
            if (nw == evR.load(std::memory_order_acquire)) break;
            evBuf[w] = e;
            evW.store(nw, std::memory_order_release);
        }
        if (detector.newFrame) publishSpectrum();
    }

    bool popEvent(HowlEvent& e) {
        const int r = evR.load(std::memory_order_relaxed);
        if (r == evW.load(std::memory_order_acquire)) return false;
        e = evBuf[r];
        evR.store((r + 1) % kEv, std::memory_order_release);
        return true;
    }

    void applyEvent(const HowlEvent& e) {
        if (bypass || frozen) return;
        bank.trigger(e.freq, e.severity);
        eventCount.fetch_add(1);
        lastEventFreq = (float)e.freq;
    }

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

    static void tap(void* ctx, double t, double f, const float* feat, float p) {
        auto* e = static_cast<Engine*>(ctx);
        if (!e->learn.load(std::memory_order_relaxed)) return;
        if (feat[0] < 10.f) return; // only peaks that stand out; keeps files small
        const int w = e->lw.load(std::memory_order_relaxed), nw = (w + 1) % kLearnCap;
        if (nw == e->lr.load(std::memory_order_acquire)) { e->learnDropped.fetch_add(1); return; }
        auto& row = e->learnBuf[w];
        row.t = t; row.freq = f; row.p = p;
        for (int i = 0; i < kNumFeatures; ++i) row.feat[i] = feat[i];
        row.kind = 0;
        row.mode = (uint8_t)(e->ringOut ? 1 : 0);
        e->lw.store(nw, std::memory_order_release);
    }

    void pushCut(const HowlEvent& ev) {
        if (!learn.load(std::memory_order_relaxed)) return;
        const int w = lw.load(std::memory_order_relaxed), nw = (w + 1) % kLearnCap;
        if (nw == lr.load(std::memory_order_acquire)) { learnDropped.fetch_add(1); return; }
        auto& row = learnBuf[w];
        row = LearnRow{};
        row.t = detector.nowSeconds(); row.freq = ev.freq; row.p = ev.prob; row.kind = 1; row.mode = (uint8_t)(ringOut ? 1 : 0);
        lw.store(nw, std::memory_order_release);
    }

    std::vector<LearnRow> learnBuf;
    std::atomic<int> lw{0}, lr{0};
    double fs = 48000, meterDecay = 0.9999;
    int chans = 2;
    bool lockAll = false, ringOut = false;
    std::vector<float> mono, ring, work;
    std::vector<HowlEvent> events;
    HowlEvent evBuf[kEv]{};
    std::atomic<size_t> ringW{0}, ringR{0};
    std::atomic<int> evW{0}, evR{0};
    std::atomic<bool> running{false}, bypassFlag{false}, frozenFlag{false}, ringOutAtomic{false};
    std::atomic<float> sensAtomic{0.5f};
    std::atomic<double> nowS{0.0};
    std::thread worker;
    bool threaded = false;
};

} // namespace dfb
