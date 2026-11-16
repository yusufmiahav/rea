import assert from "node:assert/strict";
import { test } from "node:test";

import {
  SLIDERS,
  applyEdit,
  clampParams,
  neutralParams,
} from "../src/pipeline.js";
import {
  extractFeatures,
  fitParamsFromPair,
  predictParams,
  trainStyle,
} from "../src/style.js";

// Deterministic synthetic "photo": smooth gradients plus pseudo-random texture.
function syntheticPhoto(seed, { size = 32, brightness = 1, warmth = 0 } = {}) {
  let state = seed;
  const random = () => {
    state = (state * 1664525 + 1013904223) >>> 0;
    return state / 2 ** 32;
  };
  const pixels = new Uint8ClampedArray(size * size * 4);
  for (let y = 0; y < size; y += 1) {
    for (let x = 0; x < size; x += 1) {
      const i = (y * size + x) * 4;
      const base = (x / size) * 0.6 + (y / size) * 0.25 + random() * 0.15;
      pixels[i] =
        255 * Math.min(1, base * brightness * (1 + warmth) + random() * 0.1);
      pixels[i + 1] =
        255 * Math.min(1, base * brightness * 0.9 + random() * 0.1);
      pixels[i + 2] =
        255 * Math.min(1, base * brightness * (0.8 - warmth) + random() * 0.1);
      pixels[i + 3] = 255;
    }
  }
  return pixels;
}

function meanLuma(pixels) {
  let sum = 0;
  for (let i = 0; i < pixels.length; i += 4) {
    sum += 0.2126 * pixels[i] + 0.7152 * pixels[i + 1] + 0.0722 * pixels[i + 2];
  }
  return sum / (pixels.length / 4);
}

function edit(pixels, params) {
  return applyEdit(pixels, new Uint8ClampedArray(pixels.length), {
    ...neutralParams(),
    ...params,
  });
}

test("neutral settings leave the photo untouched", () => {
  const photo = syntheticPhoto(1);
  assert.deepEqual(edit(photo, {}), photo);
});

test("each light slider moves brightness in its named direction", () => {
  const photo = syntheticPhoto(2);
  const base = meanLuma(photo);
  for (const key of ["exposure", "highlights", "shadows", "whites", "blacks"]) {
    const amount = key === "exposure" ? 1 : 60;
    assert.ok(
      meanLuma(edit(photo, { [key]: amount })) > base,
      `${key} up brightens`,
    );
    assert.ok(
      meanLuma(edit(photo, { [key]: -amount })) < base,
      `${key} down darkens`,
    );
  }
});

test("contrast widens the tonal spread and negative contrast narrows it", () => {
  const photo = syntheticPhoto(3);
  const spread = (pixels) => {
    const mean = meanLuma(pixels);
    let sum = 0;
    for (let i = 0; i < pixels.length; i += 4) {
      sum +=
        (0.2126 * pixels[i] +
          0.7152 * pixels[i + 1] +
          0.0722 * pixels[i + 2] -
          mean) **
        2;
    }
    return sum;
  };
  assert.ok(spread(edit(photo, { contrast: 60 })) > spread(photo));
  assert.ok(spread(edit(photo, { contrast: -60 })) < spread(photo));
});

test("temperature warms, saturation -100 removes colour", () => {
  const photo = syntheticPhoto(4);
  const channelMean = (pixels, offset) => {
    let sum = 0;
    for (let i = offset; i < pixels.length; i += 4) sum += pixels[i];
    return sum;
  };
  const warm = edit(photo, { temperature: 60 });
  assert.ok(channelMean(warm, 0) > channelMean(photo, 0));
  assert.ok(channelMean(warm, 2) < channelMean(photo, 2));
  const grey = edit(photo, { saturation: -100 });
  for (let i = 0; i < grey.length; i += 4) {
    assert.ok(
      Math.abs(grey[i] - grey[i + 1]) <= 1 &&
        Math.abs(grey[i + 1] - grey[i + 2]) <= 1,
    );
  }
});

test("clampParams bounds values and replaces non-numbers with zero", () => {
  const clamped = clampParams({ exposure: 99, contrast: "abc", tint: -500 });
  assert.equal(clamped.exposure, 4);
  assert.equal(clamped.contrast, 0);
  assert.equal(clamped.tint, -100);
  assert.equal(Object.keys(clamped).length, SLIDERS.length);
});

test("fitParamsFromPair recovers an edit from before and after images", () => {
  const before = syntheticPhoto(5, { size: 48 });
  const truth = {
    exposure: 0.6,
    contrast: 25,
    temperature: 20,
    saturation: 15,
  };
  const after = edit(before, truth);
  const fit = fitParamsFromPair(before, after);
  assert.ok(fit.rmse < 2, `residual ${fit.rmse} should be small`);
  assert.ok(fit.rmse < fit.startRmse / 5);
  assert.ok(
    Math.abs(fit.params.exposure - truth.exposure) < 0.25,
    `exposure ${fit.params.exposure}`,
  );
  assert.ok(Math.abs(fit.params.temperature - truth.temperature) < 10);
});

test("a style with few samples predicts the photographer's average edit", () => {
  const samples = [
    {
      features: extractFeatures(syntheticPhoto(6)),
      params: { ...neutralParams(), contrast: 10 },
    },
    {
      features: extractFeatures(syntheticPhoto(7)),
      params: { ...neutralParams(), contrast: 30 },
    },
  ];
  const model = trainStyle(samples);
  assert.equal(model.kind, "average");
  assert.equal(
    predictParams(model, extractFeatures(syntheticPhoto(8))).contrast,
    20,
  );
  assert.equal(trainStyle([]), null);
  assert.deepEqual(predictParams(null, []), neutralParams());
});

test("a trained style adapts the edit to each photo", () => {
  // The simulated photographer brightens dark photos, cools warm ones and
  // always adds the same contrast.
  const photographer = (features) => ({
    ...neutralParams(),
    exposure: (0.45 - features[0]) * 3,
    temperature: -features[7] * 80,
    contrast: 18,
  });
  const samples = [];
  for (let seed = 10; seed < 50; seed += 1) {
    const photo = syntheticPhoto(seed, {
      brightness: 0.4 + ((seed * 7) % 10) / 10,
      warmth: (((seed * 3) % 7) - 3) / 15,
    });
    const features = extractFeatures(photo);
    samples.push({ features, params: photographer(features) });
  }
  const model = trainStyle(samples);
  assert.equal(model.kind, "regression");

  const dark = extractFeatures(
    syntheticPhoto(101, { brightness: 0.45, warmth: 0.15 }),
  );
  const bright = extractFeatures(
    syntheticPhoto(102, { brightness: 1.25, warmth: -0.15 }),
  );
  const darkEdit = predictParams(model, dark);
  const brightEdit = predictParams(model, bright);
  assert.ok(
    darkEdit.exposure > brightEdit.exposure + 0.3,
    "dark photo gets more exposure",
  );
  assert.ok(Math.abs(darkEdit.exposure - photographer(dark).exposure) < 0.3);
  assert.ok(
    Math.abs(brightEdit.exposure - photographer(bright).exposure) < 0.3,
  );
  assert.ok(
    darkEdit.temperature < brightEdit.temperature,
    "warm photo is cooled more",
  );
  assert.ok(Math.abs(darkEdit.contrast - 18) < 4);
});

test("Lightroom sidecars round-trip slider settings", async () => {
  const { paramsToXmp, xmpToParams } = await import("../src/xmp.js");
  const params = {
    ...neutralParams(),
    exposure: 0.65,
    contrast: 12,
    shadows: -30,
    temperature: 8,
  };
  const xmp = paramsToXmp(params);
  assert.match(xmp, /crs:Exposure2012="\+0\.65"/);
  assert.match(xmp, /crs:Shadows2012="-30"/);
  const parsed = xmpToParams(xmp);
  assert.deepEqual(parsed.params, params);
  assert.equal(parsed.found.length, SLIDERS.length);
});

test("sidecar reading keeps only settings the file carries", async () => {
  const { xmpToParams } = await import("../src/xmp.js");
  const elementForm =
    "<rdf:Description><crs:Contrast2012>+25</crs:Contrast2012><crs:Vibrance>abc</crs:Vibrance></rdf:Description>";
  assert.deepEqual(xmpToParams(elementForm).found, ["contrast"]);
  assert.equal(xmpToParams(elementForm).params.contrast, 25);
  assert.equal(xmpToParams('<rdf:Description xmp:Rating="3"/>'), null);
  assert.equal(xmpToParams('crs:Exposure2012="+9.00"').params.exposure, 4);
});

// Builds a structurally valid JPEG stream (no real image data) for scanner tests.
function fakeJpeg({
  width,
  height,
  sof = 0xc0,
  orientation = null,
  nested = null,
}) {
  const parts = [[0xff, 0xd8]];
  const segment = (marker, body) =>
    parts.push([
      0xff,
      marker,
      (body.length + 2) >> 8,
      (body.length + 2) & 255,
      ...body,
    ]);
  if (orientation !== null || nested) {
    const tiff = [
      0x4d,
      0x4d,
      0,
      42,
      0,
      0,
      0,
      8,
      0,
      1,
      0x01,
      0x12,
      0,
      3,
      0,
      0,
      0,
      1,
      0,
      orientation ?? 1,
      0,
      0,
      0,
      0,
      0,
      0,
    ];
    segment(0xe1, [0x45, 0x78, 0x69, 0x66, 0, 0, ...tiff, ...(nested ?? [])]);
  }
  segment(sof, [
    8,
    height >> 8,
    height & 255,
    width >> 8,
    width & 255,
    3,
    1,
    0x11,
    0,
    2,
    0x11,
    1,
    3,
    0x11,
    1,
  ]);
  segment(0xda, [3, 1, 0, 2, 0x11, 3, 0x11, 0, 63, 0]);
  parts.push([0x12, 0xff, 0x00, 0x34, 0xff, 0xd3, 0x56], [0xff, 0xd9]);
  return parts.flat();
}

test("raw preview: picks the largest displayable embedded JPEG", async () => {
  const { findEmbeddedJpegs, findRawPreview, isRawName } =
    await import("../src/raw.js");
  const thumb = fakeJpeg({ width: 160, height: 120 });
  const sensor = fakeJpeg({ width: 6000, height: 4000, sof: 0xc3 }); // lossless raw data
  const preview = fakeJpeg({
    width: 3000,
    height: 2000,
    nested: fakeJpeg({ width: 320, height: 200 }),
  });
  // Big-endian TIFF header whose first directory says "rotate 90 CW" (6).
  const header = [
    0x4d, 0x4d, 0, 42, 0, 0, 0, 8, 0, 1, 0x01, 0x12, 0, 3, 0, 0, 0, 1, 0, 6, 0,
    0, 0, 0, 0, 0,
  ];
  const noise = [0xff, 0xd8, 0xff, 0x00, 0x13, 0xff, 0xd8, 0x01];
  const bytes = Uint8Array.from([
    ...header,
    ...thumb,
    ...noise,
    ...sensor,
    ...preview,
    1,
    2,
    3,
  ]);

  const sizes = findEmbeddedJpegs(bytes).map((j) => `${j.width}x${j.height}`);
  assert.deepEqual(
    sizes,
    ["160x120", "3000x2000"],
    "nested thumbnail and sensor data are not candidates",
  );
  const found = findRawPreview(bytes);
  assert.equal(found.width, 3000);
  assert.equal(found.end - found.start, preview.length);
  assert.equal(
    found.orientation,
    1,
    "preview with its own EXIF orientation needs no extra rotation",
  );

  const bare = Uint8Array.from([
    ...header,
    ...fakeJpeg({ width: 1600, height: 1200 }),
  ]);
  assert.equal(
    findRawPreview(bare).orientation,
    6,
    "falls back to the raw file's orientation",
  );
  assert.equal(findRawPreview(Uint8Array.from([...header, ...sensor])), null);
  assert.equal(
    findRawPreview(
      Uint8Array.from(fakeJpeg({ width: 3000, height: 2000 }).slice(0, -2)),
    ),
    null,
    "truncated stream",
  );
  assert.ok(
    isRawName("IMG_0001.CR3") && isRawName("a.nef") && !isRawName("a.jpg"),
  );
});

test("raw sidecars leave white balance as shot and skip incremental values", async () => {
  const { paramsToXmp, xmpToParams } = await import("../src/xmp.js");
  const xmp = paramsToXmp(
    { ...neutralParams(), exposure: 0.5, temperature: 20 },
    { raw: true },
  );
  assert.match(xmp, /crs:WhiteBalance="As Shot"/);
  assert.doesNotMatch(xmp, /Incremental/);
  const parsed = xmpToParams(
    '<x crs:Exposure2012="+0.50" crs:Temperature="5200" crs:Tint="+8"/>',
  );
  assert.deepEqual(parsed.found, ["exposure"]);
  assert.equal(parsed.params.temperature, 0);
});
