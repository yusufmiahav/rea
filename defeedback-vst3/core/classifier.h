// Tiny feed-forward network: P(feedback) from a track's cues.
// The weights in model_weights.h are trained by training/train.py on
// simulated closed-loop feedback versus sung, played and spoken material.
#pragma once
#include <algorithm>
#include <cmath>

#include "model_weights.h"

namespace dfb {

constexpr int kNumFeatures = 10;
static_assert(model::kIn == kNumFeatures, "model input size must match the feature vector");

inline float classify(const float* feat) {
    float x[model::kIn], h1[model::kH1], h2[model::kH2];
    for (int i = 0; i < model::kIn; ++i) x[i] = (feat[i] - model::mean[i]) / model::scale[i];
    for (int j = 0; j < model::kH1; ++j) {
        float a = model::b1[j];
        for (int i = 0; i < model::kIn; ++i) a += model::w1[j][i] * x[i];
        h1[j] = std::tanh(a);
    }
    for (int j = 0; j < model::kH2; ++j) {
        float a = model::b2[j];
        for (int i = 0; i < model::kH1; ++i) a += model::w2[j][i] * h1[i];
        h2[j] = std::tanh(a);
    }
    float o = model::b3;
    for (int i = 0; i < model::kH2; ++i) o += model::w3[i] * h2[i];
    return 1.0f / (1.0f + std::exp(-std::clamp(o, -30.f, 30.f)));
}

} // namespace dfb
