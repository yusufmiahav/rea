// Edit engine: turns slider settings into pixels. Pure functions over RGBA
// byte buffers so the same code runs in the browser, in a worker and in Node.

export const SLIDERS = [
  { key: "exposure", label: "Exposure", min: -4, max: 4, step: 0.05, group: "Light" },
  { key: "contrast", label: "Contrast", min: -100, max: 100, step: 1, group: "Light" },
  { key: "highlights", label: "Highlights", min: -100, max: 100, step: 1, group: "Light" },
  { key: "shadows", label: "Shadows", min: -100, max: 100, step: 1, group: "Light" },
  { key: "whites", label: "Whites", min: -100, max: 100, step: 1, group: "Light" },
  { key: "blacks", label: "Blacks", min: -100, max: 100, step: 1, group: "Light" },
  { key: "temperature", label: "Temperature", min: -100, max: 100, step: 1, group: "Color" },
  { key: "tint", label: "Tint", min: -100, max: 100, step: 1, group: "Color" },
  { key: "vibrance", label: "Vibrance", min: -100, max: 100, step: 1, group: "Color" },
  { key: "saturation", label: "Saturation", min: -100, max: 100, step: 1, group: "Color" },
];

export const SLIDER_KEYS = SLIDERS.map((slider) => slider.key);

export function neutralParams() {
  return Object.fromEntries(SLIDER_KEYS.map((key) => [key, 0]));
}

export function clampParams(params) {
  const out = {};
  for (const { key, min, max } of SLIDERS) {
    const value = Number(params?.[key]);
    out[key] = Number.isFinite(value) ? Math.min(max, Math.max(min, value)) : 0;
  }
  return out;
}

export function isNeutral(params) {
  return SLIDER_KEYS.every((key) => !params[key]);
}

const LINEAR_SIZE = 4096;
const TONE_SIZE = 1024;

const SRGB_TO_LINEAR = new Float32Array(256);
for (let i = 0; i < 256; i += 1) {
  const v = i / 255;
  SRGB_TO_LINEAR[i] = v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
}

const LINEAR_TO_SRGB = new Float32Array(LINEAR_SIZE + 1);
for (let i = 0; i <= LINEAR_SIZE; i += 1) {
  const v = i / LINEAR_SIZE;
  LINEAR_TO_SRGB[i] = v <= 0.0031308 ? v * 12.92 : 1.055 * v ** (1 / 2.4) - 0.055;
}

function smoothstep(edge0, edge1, x) {
  const t = Math.min(1, Math.max(0, (x - edge0) / (edge1 - edge0)));
  return t * t * (3 - 2 * t);
}

// Contrast, highlights, shadows, whites and blacks all depend only on the
// channel value, so they collapse into one lookup table per edit.
function buildToneCurve(params) {
  const contrast = params.contrast / 100;
  const highlights = params.highlights / 100;
  const shadows = params.shadows / 100;
  const whites = params.whites / 100;
  const blacks = params.blacks / 100;
  const curve = new Float32Array(TONE_SIZE + 1);
  const whitePoint = 1 - whites * 0.25;
  const blackPoint = -blacks * 0.15;
  for (let i = 0; i <= TONE_SIZE; i += 1) {
    let v = i / TONE_SIZE;
    v = (v - blackPoint) / (whitePoint - blackPoint);
    v = Math.min(1, Math.max(0, v));
    v += shadows * 0.35 * (1 - smoothstep(0, 0.6, v)) * v * 2.2 * (1 - v);
    v += highlights * 0.35 * smoothstep(0.4, 1, v) * v * 2.2 * (1 - v);
    if (contrast > 0) {
      // S-curve around mid grey that keeps black and white pinned.
      const x = v - 0.5;
      v = 0.5 + Math.sign(x) * 0.5 * (1 - (1 - Math.abs(x) * 2) ** (1 + contrast * 1.5));
    } else if (contrast < 0) {
      v = 0.5 + (v - 0.5) * (1 + contrast * 0.6);
    }
    curve[i] = Math.min(1, Math.max(0, v));
  }
  return curve;
}

function whiteBalanceGains(params) {
  const temp = params.temperature / 100;
  const tint = params.tint / 100;
  let r = Math.exp(0.35 * temp + 0.12 * tint);
  let g = Math.exp(-0.24 * tint);
  let b = Math.exp(-0.35 * temp + 0.12 * tint);
  // Keep perceived brightness steady so white balance does not act as exposure.
  const luma = 0.2126 * r + 0.7152 * g + 0.0722 * b;
  r /= luma;
  g /= luma;
  b /= luma;
  return [r, g, b];
}

/**
 * Applies `params` to `src` and writes the result into `dst`.
 * Both are RGBA byte arrays of equal length; they may be the same array.
 */
export function applyEdit(src, dst, params) {
  const p = clampParams(params);
  if (isNeutral(p)) {
    if (dst !== src) dst.set(src);
    return dst;
  }
  const gain = 2 ** p.exposure;
  const [wr, wg, wb] = whiteBalanceGains(p);
  const gr = gain * wr * LINEAR_SIZE;
  const gg = gain * wg * LINEAR_SIZE;
  const gb = gain * wb * LINEAR_SIZE;
  const tone = buildToneCurve(p);
  const saturation = p.saturation / 100;
  const vibrance = p.vibrance / 100;
  const adjustColor = saturation !== 0 || vibrance !== 0;

  for (let i = 0; i < src.length; i += 4) {
    let r = SRGB_TO_LINEAR[src[i]] * gr;
    let g = SRGB_TO_LINEAR[src[i + 1]] * gg;
    let b = SRGB_TO_LINEAR[src[i + 2]] * gb;
    r = tone[(LINEAR_TO_SRGB[r > LINEAR_SIZE ? LINEAR_SIZE : r | 0] * TONE_SIZE) | 0];
    g = tone[(LINEAR_TO_SRGB[g > LINEAR_SIZE ? LINEAR_SIZE : g | 0] * TONE_SIZE) | 0];
    b = tone[(LINEAR_TO_SRGB[b > LINEAR_SIZE ? LINEAR_SIZE : b | 0] * TONE_SIZE) | 0];
    if (adjustColor) {
      const max = r > g ? (r > b ? r : b) : g > b ? g : b;
      const min = r < g ? (r < b ? r : b) : g < b ? g : b;
      const luma = 0.2126 * r + 0.7152 * g + 0.0722 * b;
      // Vibrance pushes muted pixels harder than already-saturated ones.
      let factor = 1 + saturation + vibrance * (1 - (max - min));
      if (factor < 0) factor = 0;
      r = luma + (r - luma) * factor;
      g = luma + (g - luma) * factor;
      b = luma + (b - luma) * factor;
    }
    dst[i] = r <= 0 ? 0 : r >= 1 ? 255 : (r * 255 + 0.5) | 0;
    dst[i + 1] = g <= 0 ? 0 : g >= 1 ? 255 : (g * 255 + 0.5) | 0;
    dst[i + 2] = b <= 0 ? 0 : b >= 1 ? 255 : (b * 255 + 0.5) | 0;
    dst[i + 3] = src[i + 3];
  }
  return dst;
}

/** 256-bin luminance and per-channel histograms for display. */
export function histogram(pixels) {
  const r = new Uint32Array(256);
  const g = new Uint32Array(256);
  const b = new Uint32Array(256);
  for (let i = 0; i < pixels.length; i += 4) {
    r[pixels[i]] += 1;
    g[pixels[i + 1]] += 1;
    b[pixels[i + 2]] += 1;
  }
  return { r, g, b };
}
