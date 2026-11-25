// CSV log written in Learn mode (plugin) and by `dfb_sim learn` (recordings).
// Columns: kind,t,freq,label,mode,p,f0..f9
//   kind  track = one detector decision (cues f0..f9), cut = a notch was triggered,
//         mark  = you pressed "Was feedback" (label 1) or "Not feedback" (label 0);
//         training/train.py applies a mark to the cuts and track rows near that
//         frequency in the seconds before it.
//   label -1 = not labelled yet.  mode 0 = Live, 1 = Ring-out.
#pragma once
#include <cstdio>
#include <string>

#include "engine.h"

namespace dfb {

class LearnLog {
public:
    ~LearnLog() { close(); }
    bool open(const std::string& path) {
        close();
        f = std::fopen(path.c_str(), "w");
        if (!f) return false;
        std::fprintf(f, "kind,t,freq,label,mode,p");
        for (int i = 0; i < kNumFeatures; ++i) std::fprintf(f, ",f%d", i);
        std::fprintf(f, "\n");
        rows = 0;
        return true;
    }
    bool isOpen() const { return f != nullptr; }
    void close() { if (f) { std::fclose(f); f = nullptr; } }
    void write(const Engine::LearnRow& r, int label = -1) {
        if (!f) return;
        std::fprintf(f, "%s,%.3f,%.2f,%d,%d,%.4f", r.kind ? "cut" : "track", r.t, r.freq, label, (int)r.mode, r.p);
        for (int i = 0; i < kNumFeatures; ++i) std::fprintf(f, ",%.4f", r.kind ? 0.f : r.feat[i]);
        std::fprintf(f, "\n");
        ++rows;
    }
    void mark(double t, int label, double freq) {
        if (!f) return;
        std::fprintf(f, "mark,%.3f,%.2f,%d,0,0", t, freq, label);
        for (int i = 0; i < kNumFeatures; ++i) std::fprintf(f, ",0");
        std::fprintf(f, "\n");
        std::fflush(f);
    }
    void flush() { if (f) std::fflush(f); }
    long rows = 0;
private:
    FILE* f = nullptr;
};

} // namespace dfb
