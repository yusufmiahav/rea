// Lightroom interchange: reads and writes the Camera Raw (crs) develop
// settings that Lightroom stores in .xmp sidecar files.
//
// The slider values map one-to-one by name. Temperature and tint use the
// incremental (-100..100) form Lightroom applies to JPEG, PNG and TIFF; raw
// files use Kelvin values instead, which this editor does not model.

import { SLIDER_KEYS, clampParams, neutralParams } from "./pipeline.js";

const CRS_NAMES = {
  exposure: "Exposure2012",
  contrast: "Contrast2012",
  highlights: "Highlights2012",
  shadows: "Shadows2012",
  whites: "Whites2012",
  blacks: "Blacks2012",
  temperature: "IncrementalTemperature",
  tint: "IncrementalTint",
  vibrance: "Vibrance",
  saturation: "Saturation",
};

const signed = (value, digits) => {
  const text = value.toFixed(digits);
  return value > 0 ? `+${text}` : text;
};

/** Builds a sidecar that Lightroom reads through "Read Metadata from File". */
export function paramsToXmp(params) {
  const p = clampParams(params);
  const attributes = SLIDER_KEYS.map(
    (key) =>
      `   crs:${CRS_NAMES[key]}="${signed(p[key], key === "exposure" ? 2 : 0)}"`,
  ).join("\n");
  return `<?xml version="1.0" encoding="UTF-8"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/"
   crs:ProcessVersion="11.0"
${attributes}
   crs:HasSettings="True"/>
 </rdf:RDF>
</x:xmpmeta>
`;
}

/**
 * Reads develop settings from sidecar text. Returns the settings plus which
 * sliders the file actually carried, or null when it holds none of them
 * (for example a sidecar with only ratings or keywords).
 */
export function xmpToParams(text) {
  const params = neutralParams();
  const found = [];
  for (const key of SLIDER_KEYS) {
    const name = CRS_NAMES[key];
    const match =
      new RegExp(`crs:${name}\\s*=\\s*["']([^"']*)["']`).exec(text) ??
      new RegExp(`<crs:${name}>\\s*([^<]*?)\\s*</crs:${name}>`).exec(text);
    if (!match) continue;
    const value = Number(match[1]);
    if (!Number.isFinite(value) || match[1].trim() === "") continue;
    params[key] = value;
    found.push(key);
  }
  if (found.length === 0) return null;
  return { params: clampParams(params), found };
}
