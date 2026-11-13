// Personal style model: learns how a photographer sets the sliders from the
// look of the unedited photo, then predicts settings for photos it has not seen.
//
// A sample is { features: number[], params: {slider: value} }. The model is a
// ridge regression per slider over standardized image features. With too few
// samples for a regression it falls back to the photographer's average edit.

import { SLIDERS, SLIDER_KEYS, applyEdit, clampParams, neutralParams } from "./pipeline.js";

export const FEATURE_NAMES = [
  "meanLuma",
  "stdLuma",
  "p05",
  "p50",
  "p95",
  "shadowFraction",
  "highlightFraction",
  "logRedGreen",
  "logBlueGreen",
  "meanSaturation",
  "stdSaturation",
];

export const MIN_SAMPLES_FOR_REGRESSION = 4;

/** Describes an unedited photo. Works best on a small thumbnail (about 64px). */
export function extractFeatures(pixels) {
  const count = pixels.length / 4;
  if (count === 0) throw new Error("Cannot describe an empty image");
  const bins = new Uint32Array(256);
  let sumLuma = 0;
  let sumLumaSq = 0;
  let sumSat = 0;
  let sumSatSq = 0;
  let sumR = 0;
  let sumG = 0;
  let sumB = 0;
  let shadows = 0;
  let highlights = 0;
  for (let i = 0; i < pixels.length; i += 4) {
    const r = pixels[i] / 255;
    const g = pixels[i + 1] / 255;
    const b = pixels[i + 2] / 255;
    const luma = 0.2126 * r + 0.7152 * g + 0.0722 * b;
    const sat = Math.max(r, g, b) - Math.min(r, g, b);
    bins[Math.min(255, (luma * 255 + 0.5) | 0)] += 1;
    sumLuma += luma;
    sumLumaSq += luma * luma;
    sumSat += sat;
    sumSatSq += sat * sat;
    sumR += r;
    sumG += g;
    sumB += b;
    if (luma < 0.1) shadows += 1;
    if (luma > 0.9) highlights += 1;
  }
  const percentile = (fraction) => {
    const target = fraction * count;
    let seen = 0;
    for (let i = 0; i < 256; i += 1) {
      seen += bins[i];
      if (seen >= target) return i / 255;
    }
    return 1;
  };
  const meanLuma = sumLuma / count;
  const meanSat = sumSat / count;
  const epsilon = 1e-3;
  return [
    meanLuma,
    Math.sqrt(Math.max(0, sumLumaSq / count - meanLuma * meanLuma)),
    percentile(0.05),
    percentile(0.5),
    percentile(0.95),
    shadows / count,
    highlights / count,
    Math.log((sumR / count + epsilon) / (sumG / count + epsilon)),
    Math.log((sumB / count + epsilon) / (sumG / count + epsilon)),
    meanSat,
    Math.sqrt(Math.max(0, sumSatSq / count - meanSat * meanSat)),
  ];
}

function solveLinearSystem(matrix, rhs) {
  const n = rhs.length;
  const a = matrix.map((row, i) => [...row, rhs[i]]);
  for (let col = 0; col < n; col += 1) {
    let pivot = col;
    for (let row = col + 1; row < n; row += 1) {
      if (Math.abs(a[row][col]) > Math.abs(a[pivot][col])) pivot = row;
    }
    [a[col], a[pivot]] = [a[pivot], a[col]];
    const diagonal = a[col][col];
    if (Math.abs(diagonal) < 1e-12) continue;
    for (let row = col + 1; row < n; row += 1) {
      const factor = a[row][col] / diagonal;
      if (factor === 0) continue;
      for (let k = col; k <= n; k += 1) a[row][k] -= factor * a[col][k];
    }
  }
  const x = new Array(n).fill(0);
  for (let row = n - 1; row >= 0; row -= 1) {
    let sum = a[row][n];
    for (let k = row + 1; k < n; k += 1) sum -= a[row][k] * x[k];
    x[row] = Math.abs(a[row][row]) < 1e-12 ? 0 : sum / a[row][row];
  }
  return x;
}

/**
 * Trains a style model. Returns null when there are no samples.
 * `ridge` trades fidelity to the samples for stability on unseen photos.
 */
export function trainStyle(samples, { ridge = 1 } = {}) {
  if (samples.length === 0) return null;
  const n = samples.length;
  const d = samples[0].features.length;
  const meanParams = neutralParams();
  for (const key of SLIDER_KEYS) {
    meanParams[key] = samples.reduce((sum, s) => sum + (s.params[key] ?? 0), 0) / n;
  }
  if (n < MIN_SAMPLES_FOR_REGRESSION) {
    return { kind: "average", sampleCount: n, meanParams };
  }

  const featureMean = new Array(d).fill(0);
  const featureScale = new Array(d).fill(0);
  for (const s of samples) for (let j = 0; j < d; j += 1) featureMean[j] += s.features[j] / n;
  for (const s of samples) {
    for (let j = 0; j < d; j += 1) featureScale[j] += (s.features[j] - featureMean[j]) ** 2 / n;
  }
  for (let j = 0; j < d; j += 1) featureScale[j] = Math.sqrt(featureScale[j]) || 1;
  const rows = samples.map((s) => s.features.map((v, j) => (v - featureMean[j]) / featureScale[j]));

  const gram = Array.from({ length: d }, () => new Array(d).fill(0));
  for (const row of rows) {
    for (let j = 0; j < d; j += 1) {
      for (let k = j; k < d; k += 1) gram[j][k] += row[j] * row[k];
    }
  }
  for (let j = 0; j < d; j += 1) {
    for (let k = 0; k < j; k += 1) gram[j][k] = gram[k][j];
    gram[j][j] += ridge;
  }

  const weights = {};
  for (const key of SLIDER_KEYS) {
    const rhs = new Array(d).fill(0);
    rows.forEach((row, i) => {
      const residual = (samples[i].params[key] ?? 0) - meanParams[key];
      for (let j = 0; j < d; j += 1) rhs[j] += row[j] * residual;
    });
    weights[key] = solveLinearSystem(gram, rhs);
  }
  return { kind: "regression", sampleCount: n, meanParams, featureMean, featureScale, weights };
}

/** Predicts slider settings for an unedited photo described by `features`. */
export function predictParams(model, features) {
  if (!model) return neutralParams();
  if (model.kind === "average") return roundParams(model.meanParams);
  const out = {};
  for (const key of SLIDER_KEYS) {
    let value = model.meanParams[key];
    const w = model.weights[key];
    for (let j = 0; j < w.length; j += 1) {
      // Clamp how far outside the training range a photo can pull the edit.
      const z = (features[j] - model.featureMean[j]) / model.featureScale[j];
      value += w[j] * Math.max(-3, Math.min(3, z));
    }
    out[key] = value;
  }
  return roundParams(out);
}

function roundParams(params) {
  const clamped = clampParams(params);
  for (const { key, step } of SLIDERS) clamped[key] = Math.round(clamped[key] / step) * step;
  for (const key of SLIDER_KEYS) clamped[key] = Number(clamped[key].toFixed(2));
  return clamped;
}

function meanSquaredError(a, b) {
  let sum = 0;
  for (let i = 0; i < a.length; i += 4) {
    const dr = a[i] - b[i];
    const dg = a[i + 1] - b[i + 1];
    const db = a[i + 2] - b[i + 2];
    sum += dr * dr + dg * dg + db * db;
  }
  return sum / ((a.length / 4) * 3);
}

/**
 * Recovers the slider settings that best turn `before` into `after`.
 * Both are same-sized RGBA thumbnails of one photo. Returns the settings and
 * the remaining error (RMSE in 0-255 levels) so callers can judge the fit;
 * local edits, crops and presets this engine cannot express leave a high error.
 */
export function fitParamsFromPair(before, after) {
  if (before.length !== after.length || before.length === 0) {
    throw new Error("Before and after thumbnails must be the same non-zero size");
  }
  const scratch = new Uint8ClampedArray(before.length);
  const error = (params) => meanSquaredError(applyEdit(before, scratch, params), after);
  // Several sliders can produce a similar result. A small cost on slider
  // travel makes the search prefer the simplest edit that explains the pair.
  const score = (params) => {
    let travel = 0;
    for (const { key, min, max } of SLIDERS) travel += Math.abs(params[key]) / (max - min);
    return error(params) + 0.5 * travel;
  };
  let best = neutralParams();
  let bestScore = score(best);
  const startError = error(best);
  // Coarse sweep of each slider's whole range before refining, so a large
  // global change is attributed to one slider instead of many small steps.
  for (let sweep = 0; sweep < 2; sweep += 1) {
    for (const { key, min, max } of SLIDERS) {
      for (let step = 0; step <= 32; step += 1) {
        const candidate = { ...best, [key]: min + ((max - min) * step) / 32 };
        const candidateScore = score(candidate);
        if (candidateScore < bestScore) {
          best = candidate;
          bestScore = candidateScore;
        }
      }
    }
  }
  for (let level = 2; level < 9; level += 1) {
    const fraction = 0.25 / 2 ** level;
    for (let pass = 0; pass < 3; pass += 1) {
      let improved = false;
      for (const { key, min, max } of SLIDERS) {
        const delta = (max - min) * fraction;
        for (const direction of [1, -1]) {
          const value = Math.min(max, Math.max(min, best[key] + direction * delta));
          if (value === best[key]) continue;
          const candidate = { ...best, [key]: value };
          const candidateScore = score(candidate);
          if (candidateScore < bestScore - 1e-9) {
            best = candidate;
            bestScore = candidateScore;
            improved = true;
            break;
          }
        }
      }
      if (!improved) break;
    }
  }
  return {
    params: roundParams(best),
    rmse: Math.sqrt(error(best)),
    startRmse: Math.sqrt(startError),
  };
}
