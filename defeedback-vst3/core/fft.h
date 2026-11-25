// Small iterative radix-2 real-input FFT (power spectrum only).
// Original code for this project. No allocation after construction.
#pragma once
#include <cmath>
#include <complex>
#include <vector>

#ifndef M_PI
#define M_PI 3.14159265358979323846
#endif

namespace dfb {

class FFT {
public:
    explicit FFT(int size = 4096) { setSize(size); }

    void setSize(int size) {
        n = size;
        bits = 0;
        while ((1 << bits) < n) ++bits;
        rev.assign(n, 0);
        for (int i = 0; i < n; ++i) {
            int r = 0;
            for (int b = 0; b < bits; ++b) if (i & (1 << b)) r |= 1 << (bits - 1 - b);
            rev[i] = r;
        }
        tw.resize(n / 2);
        for (int k = 0; k < n / 2; ++k)
            tw[k] = std::polar(1.0, -2.0 * M_PI * k / n);
        buf.assign(n, {0.0, 0.0});
    }

    int size() const { return n; }

    // in: n real samples. out: n/2+1 power values |X|^2.
    void power(const double* in, double* out) {
        for (int i = 0; i < n; ++i) buf[rev[i]] = {in[i], 0.0};
        for (int len = 2; len <= n; len <<= 1) {
            const int half = len >> 1, step = n / len;
            for (int i = 0; i < n; i += len)
                for (int j = 0; j < half; ++j) {
                    auto t = buf[i + j + half] * tw[j * step];
                    auto u = buf[i + j];
                    buf[i + j] = u + t;
                    buf[i + j + half] = u - t;
                }
        }
        for (int k = 0; k <= n / 2; ++k) out[k] = std::norm(buf[k]);
    }

private:
    int n = 0, bits = 0;
    std::vector<int> rev;
    std::vector<std::complex<double>> tw, buf;
};

} // namespace dfb
