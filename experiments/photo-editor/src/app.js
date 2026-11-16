import {
  SLIDERS,
  SLIDER_KEYS,
  applyEdit,
  clampParams,
  histogram,
  isNeutral,
  neutralParams,
} from "./pipeline.js";
import {
  FEATURE_NAMES,
  MIN_SAMPLES_FOR_REGRESSION,
  extractFeatures,
  fitParamsFromPair,
  predictParams,
  trainStyle,
} from "./style.js";
import { paramsToXmp, xmpToParams } from "./xmp.js";

const PREVIEW_EDGE = 1600;
const THUMB_EDGE = 160;
const FIT_EDGE = 64;
// Above this residual (0-255 levels) a before/after pair shows changes the
// sliders cannot express, such as crops, local edits or a different photo.
const MAX_PAIR_RMSE = 12;
const STORAGE_KEY = "photo-editor.style.v1";

const $ = (id) => document.getElementById(id);
const el = {
  fileInput: $("fileInput"),
  status: $("status"),
  filmstrip: $("filmstrip"),
  stage: $("stage"),
  dropHint: $("dropHint"),
  view: $("view"),
  beforeButton: $("beforeButton"),
  histogram: $("histogram"),
  styleSummary: $("styleSummary"),
  sliders: $("sliders"),
  styleFile: $("styleFile"),
  pairsDialog: $("pairsDialog"),
  pairsBefore: $("pairsBefore"),
  pairsAfter: $("pairsAfter"),
  pairsResult: $("pairsResult"),
};

const state = {
  photos: [],
  currentId: null,
  preview: null, // { id, source: ImageData, output: ImageData }
  samples: [],
  model: null,
  clipboard: null,
  showBefore: false,
  renderQueued: false,
};

const sliderInputs = new Map();

// ---------- style storage ----------

function parseSamples(value) {
  if (!Array.isArray(value)) throw new Error("Style file has no sample list");
  return value.map((sample, index) => {
    const features = sample?.features;
    if (
      !Array.isArray(features) ||
      features.length !== FEATURE_NAMES.length ||
      !features.every((v) => typeof v === "number" && Number.isFinite(v))
    ) {
      throw new Error(
        `Sample ${index + 1} does not match this version's photo description`,
      );
    }
    return {
      id: String(sample.id ?? `imported-${index}`),
      name: String(sample.name ?? "photo"),
      source: sample.source === "pair" ? "pair" : "edit",
      features,
      params: clampParams(sample.params),
    };
  });
}

function loadSamples() {
  let stored;
  try {
    stored = localStorage.getItem(STORAGE_KEY);
  } catch {
    return {
      samples: [],
      problem:
        "This browser is blocking storage, so your style will not be remembered.",
    };
  }
  if (stored === null) return { samples: [] };
  try {
    return { samples: parseSamples(JSON.parse(stored).samples) };
  } catch (error) {
    // Leave the unreadable data in place; it is only replaced when the user teaches a new sample.
    return {
      samples: [],
      problem: `Saved style could not be read (${error.message}).`,
    };
  }
}

function saveSamples() {
  state.model = trainStyle(state.samples);
  try {
    localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({ version: 1, samples: state.samples }),
    );
  } catch {
    setStatus(
      "Style updated for this session, but the browser would not store it.",
    );
  }
  renderStyleSummary();
}

function upsertSample(sample) {
  const index = state.samples.findIndex((s) => s.id === sample.id);
  if (index >= 0) state.samples[index] = sample;
  else state.samples.push(sample);
}

// ---------- image helpers ----------

async function decode(file, maxEdge, exactSize) {
  const bitmap = await createImageBitmap(file, {
    imageOrientation: "from-image",
  });
  try {
    const scale = Math.min(1, maxEdge / Math.max(bitmap.width, bitmap.height));
    const width =
      exactSize?.width ?? Math.max(1, Math.round(bitmap.width * scale));
    const height =
      exactSize?.height ?? Math.max(1, Math.round(bitmap.height * scale));
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    const context = canvas.getContext("2d", { willReadFrequently: true });
    context.drawImage(bitmap, 0, 0, width, height);
    return context.getImageData(0, 0, width, height);
  } finally {
    bitmap.close();
  }
}

const photoId = (file) => `${file.name}:${file.size}:${file.lastModified}`;
const baseName = (name) => name.replace(/\.[^.]+$/, "").toLowerCase();
const currentPhoto = () =>
  state.photos.find((p) => p.id === state.currentId) ?? null;

function setStatus(message) {
  el.status.textContent = message;
}

// ---------- import ----------

async function importFiles(fileList) {
  const all = [...fileList];
  const isSidecar = (file) => file.name.toLowerCase().endsWith(".xmp");
  const sidecars = all.filter(isSidecar);
  const files = all.filter((file) => !isSidecar(file));
  if (all.length === 0) return;
  const failed = [];
  let added = 0;
  for (const [index, file] of files.entries()) {
    setStatus(`Importing ${index + 1} of ${files.length}…`);
    const id = photoId(file);
    if (state.photos.some((p) => p.id === id)) continue;
    try {
      const thumb = await decode(file, THUMB_EDGE);
      const photo = {
        id,
        file,
        name: file.name,
        thumb,
        features: extractFeatures(thumb.data),
        params: neutralParams(),
        aiEdited: false,
        element: null,
      };
      state.photos.push(photo);
      addThumb(photo);
      added += 1;
    } catch {
      failed.push(file.name);
    }
  }
  const parts = [`${added} photo${added === 1 ? "" : "s"} imported`];
  const sidecarNote = await applySidecars(sidecars);
  if (sidecarNote) parts.push(sidecarNote);
  if (failed.length > 0) {
    parts.push(
      `${failed.length} could not be opened (${failed.slice(0, 3).join(", ")}${failed.length > 3 ? "…" : ""}); raw camera files are not supported yet`,
    );
  }
  setStatus(parts.join(". ") + ".");
  if (!state.currentId && state.photos.length > 0)
    await selectPhoto(state.photos[0].id);
  updateControls();
}

// A Lightroom sidecar next to a photo carries the edit the photographer made
// there: show that edit and learn from it.
async function applySidecars(sidecars) {
  if (sidecars.length === 0) return "";
  let learned = 0;
  let unusable = 0;
  let unmatched = 0;
  for (const sidecar of sidecars) {
    const photo = state.photos.find(
      (p) => baseName(p.name) === baseName(sidecar.name),
    );
    if (!photo) {
      unmatched += 1;
      continue;
    }
    const parsed = xmpToParams(await sidecar.text());
    if (!parsed) {
      unusable += 1;
      continue;
    }
    setParams(photo, parsed.params, { aiEdited: false });
    upsertSample({
      id: photo.id,
      name: photo.name,
      source: "edit",
      features: photo.features,
      params: { ...photo.params },
    });
    learned += 1;
  }
  if (learned > 0) {
    saveSamples();
    for (const photo of state.photos) renderThumb(photo);
  }
  const notes = [
    `Learned ${learned} Lightroom edit${learned === 1 ? "" : "s"} from sidecars`,
  ];
  if (unmatched > 0)
    notes.push(
      `${unmatched} sidecar${unmatched === 1 ? "" : "s"} had no photo with the same name`,
    );
  if (unusable > 0) notes.push(`${unusable} carried no develop settings`);
  return notes.join("; ");
}

function addThumb(photo) {
  const button = document.createElement("button");
  button.className = "thumb";
  button.title = photo.name;
  button.setAttribute("aria-label", photo.name);
  const canvas = document.createElement("canvas");
  canvas.width = photo.thumb.width;
  canvas.height = photo.thumb.height;
  const badge = document.createElement("span");
  badge.className = "badge";
  badge.hidden = true;
  button.append(canvas, badge);
  button.addEventListener("click", () => selectPhoto(photo.id));
  photo.element = button;
  el.filmstrip.append(button);
  renderThumb(photo);
}

function renderThumb(photo) {
  const canvas = photo.element.querySelector("canvas");
  const output = new ImageData(photo.thumb.width, photo.thumb.height);
  applyEdit(photo.thumb.data, output.data, photo.params);
  canvas.getContext("2d").putImageData(output, 0, 0);
  const badge = photo.element.querySelector(".badge");
  const taught = state.samples.some((s) => s.id === photo.id);
  badge.textContent = taught ? "Style" : photo.aiEdited ? "AI" : "";
  badge.hidden = badge.textContent === "";
}

// ---------- viewing and editing ----------

async function selectPhoto(id) {
  const photo = state.photos.find((p) => p.id === id);
  if (!photo) return;
  state.currentId = id;
  for (const p of state.photos)
    p.element.setAttribute("aria-current", String(p.id === id));
  photo.element.scrollIntoView({ block: "nearest", inline: "nearest" });
  syncSliders();
  updateControls();
  let source;
  try {
    source = await decode(photo.file, PREVIEW_EDGE);
  } catch {
    setStatus(`${photo.name} could not be opened.`);
    return;
  }
  if (state.currentId !== id) return; // user moved on while decoding
  state.preview = {
    id,
    source,
    output: new ImageData(source.width, source.height),
  };
  el.view.width = source.width;
  el.view.height = source.height;
  el.view.hidden = false;
  el.dropHint.hidden = true;
  el.beforeButton.hidden = false;
  queueRender();
}

function queueRender() {
  if (state.renderQueued) return;
  state.renderQueued = true;
  requestAnimationFrame(() => {
    state.renderQueued = false;
    render();
  });
}

function render() {
  const photo = currentPhoto();
  const preview = state.preview;
  if (!photo || !preview || preview.id !== photo.id) return;
  const params = state.showBefore ? neutralParams() : photo.params;
  applyEdit(preview.source.data, preview.output.data, params);
  el.view.getContext("2d").putImageData(preview.output, 0, 0);
  drawHistogram(preview.output.data);
}

function drawHistogram(pixels) {
  const context = el.histogram.getContext("2d");
  const { width, height } = el.histogram;
  context.clearRect(0, 0, width, height);
  const channels = histogram(pixels);
  let peak = 1;
  for (const bins of Object.values(channels)) {
    for (let i = 2; i < 254; i += 1) if (bins[i] > peak) peak = bins[i];
  }
  context.globalCompositeOperation = "lighter";
  const colors = { r: "#c8453d", g: "#3f9a4c", b: "#3f6fd0" };
  for (const [channel, bins] of Object.entries(channels)) {
    context.fillStyle = colors[channel];
    context.beginPath();
    context.moveTo(0, height);
    for (let i = 0; i < 256; i += 1) {
      context.lineTo(
        (i / 255) * width,
        height - Math.min(1, bins[i] / peak) * height,
      );
    }
    context.lineTo(width, height);
    context.fill();
  }
  context.globalCompositeOperation = "source-over";
}

function buildSliders() {
  let group = null;
  for (const slider of SLIDERS) {
    if (slider.group !== group) {
      group = slider.group;
      const heading = document.createElement("h3");
      heading.textContent = group;
      el.sliders.append(heading);
    }
    const row = document.createElement("div");
    row.className = "slider";
    const label = document.createElement("label");
    label.textContent = slider.label;
    label.htmlFor = `slider-${slider.key}`;
    label.title = "Double-click to reset";
    const input = document.createElement("input");
    input.type = "range";
    input.id = `slider-${slider.key}`;
    input.min = slider.min;
    input.max = slider.max;
    input.step = slider.step;
    input.value = 0;
    input.disabled = true;
    const output = document.createElement("output");
    output.htmlFor = input.id;
    row.append(label, input, output);
    el.sliders.append(row);
    sliderInputs.set(slider.key, { input, output, slider });
    input.addEventListener("input", () =>
      setParam(slider.key, Number(input.value)),
    );
    label.addEventListener("dblclick", () => setParam(slider.key, 0));
  }
  syncSliders();
}

function formatValue(slider, value) {
  const text = slider.step < 1 ? value.toFixed(2) : String(Math.round(value));
  return value > 0 ? `+${text}` : text;
}

function syncSliders() {
  const photo = currentPhoto();
  for (const [key, { input, output, slider }] of sliderInputs) {
    const value = photo ? photo.params[key] : 0;
    input.value = value;
    input.disabled = !photo;
    output.textContent = formatValue(slider, value);
  }
}

function setParam(key, value) {
  const photo = currentPhoto();
  if (!photo) return;
  setParams(photo, { ...photo.params, [key]: value }, { aiEdited: false });
}

function setParams(photo, params, { aiEdited }) {
  photo.params = clampParams(params);
  photo.aiEdited = aiEdited;
  renderThumb(photo);
  if (photo.id === state.currentId) {
    syncSliders();
    queueRender();
    updateControls();
  }
}

function updateControls() {
  const photo = currentPhoto();
  const hasPhotos = state.photos.length > 0;
  const hasStyle = state.model !== null;
  $("exportCurrent").disabled = !photo;
  $("exportAll").disabled = !hasPhotos;
  $("exportXmp").disabled = !hasPhotos;
  $("aiEdit").disabled = !photo || !hasStyle;
  $("aiEditAll").disabled = !hasPhotos || !hasStyle;
  $("teach").disabled = !photo || isNeutral(photo.params);
  $("copySettings").disabled = !photo;
  $("pasteSettings").disabled = !photo || !state.clipboard;
  $("resetSettings").disabled = !photo || isNeutral(photo.params);
}

// ---------- style ----------

function renderStyleSummary() {
  const count = state.samples.length;
  let text;
  if (count === 0) {
    text =
      "No style yet. Edit a photo and choose Teach from this edit, or learn from before/after pairs.";
  } else if (count < MIN_SAMPLES_FOR_REGRESSION) {
    text = `${count} example${count === 1 ? "" : "s"}. AI edit applies your average edit until it has ${MIN_SAMPLES_FOR_REGRESSION}; after that it adapts to each photo.`;
  } else {
    text = `${count} examples. AI edit adapts your settings to each photo. More varied examples make it more reliable.`;
  }
  el.styleSummary.textContent = text;
  updateControls();
}

function teachFromCurrent() {
  const photo = currentPhoto();
  if (!photo) return;
  upsertSample({
    id: photo.id,
    name: photo.name,
    source: "edit",
    features: photo.features,
    params: { ...photo.params },
  });
  saveSamples();
  renderThumb(photo);
  setStatus(`Learned from ${photo.name}.`);
}

function aiEdit(photos) {
  if (!state.model) return;
  for (const photo of photos) {
    setParams(photo, predictParams(state.model, photo.features), {
      aiEdited: true,
    });
  }
  setStatus(
    `AI edit applied to ${photos.length} photo${photos.length === 1 ? "" : "s"}. Adjust any slider to refine.`,
  );
}

async function learnFromPairs() {
  const beforeFiles = [...el.pairsBefore.files];
  const afterByName = new Map(
    [...el.pairsAfter.files].map((file) => [baseName(file.name), file]),
  );
  if (beforeFiles.length === 0 || afterByName.size === 0) {
    el.pairsResult.textContent =
      "Choose both the originals and the edited versions.";
    return;
  }
  let learned = 0;
  let unmatched = 0;
  const rejected = [];
  for (const [index, before] of beforeFiles.entries()) {
    el.pairsResult.textContent = `Studying pair ${index + 1} of ${beforeFiles.length}…`;
    const after = afterByName.get(baseName(before.name));
    if (!after) {
      unmatched += 1;
      continue;
    }
    try {
      const beforeThumb = await decode(before, FIT_EDGE);
      const afterThumb = await decode(after, FIT_EDGE, beforeThumb);
      const fit = fitParamsFromPair(beforeThumb.data, afterThumb.data);
      if (fit.rmse > MAX_PAIR_RMSE) {
        rejected.push(before.name);
        continue;
      }
      upsertSample({
        id: photoId(before),
        name: before.name,
        source: "pair",
        features: extractFeatures(beforeThumb.data),
        params: fit.params,
      });
      learned += 1;
    } catch {
      rejected.push(before.name);
    }
    // Let the dialog repaint between pairs.
    await new Promise((resolve) => setTimeout(resolve));
  }
  if (learned > 0) saveSamples();
  const parts = [`Learned from ${learned} pair${learned === 1 ? "" : "s"}.`];
  if (unmatched > 0)
    parts.push(
      `${unmatched} original${unmatched === 1 ? " has" : "s have"} no edited file with the same name.`,
    );
  if (rejected.length > 0) {
    parts.push(
      `${rejected.length} skipped because the edit goes beyond these sliders or the files could not be read (${rejected.slice(0, 3).join(", ")}${rejected.length > 3 ? "…" : ""}).`,
    );
  }
  el.pairsResult.textContent = parts.join(" ");
  for (const photo of state.photos) renderThumb(photo);
}

function download(blob, name) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), 10_000);
}

// ---------- export ----------

async function renderFullSize(photo) {
  const bitmap = await createImageBitmap(photo.file, {
    imageOrientation: "from-image",
  });
  const canvas = document.createElement("canvas");
  canvas.width = bitmap.width;
  canvas.height = bitmap.height;
  const context = canvas.getContext("2d", { willReadFrequently: true });
  context.drawImage(bitmap, 0, 0);
  bitmap.close();
  const image = context.getImageData(0, 0, canvas.width, canvas.height);
  applyEdit(image.data, image.data, photo.params);
  context.putImageData(image, 0, 0);
  const blob = await new Promise((resolve) =>
    canvas.toBlob(resolve, "image/jpeg", 0.92),
  );
  if (!blob) throw new Error("The browser could not encode the image");
  return blob;
}

const exportName = (photo, kind) =>
  kind === "xmp"
    ? `${photo.name.replace(/\.[^.]+$/, "")}.xmp`
    : `${photo.name.replace(/\.[^.]+$/, "")}-edited.jpg`;

async function exportPhotos(photos, kind = "jpeg") {
  let directory = null;
  if (photos.length > 1 && "showDirectoryPicker" in window) {
    try {
      directory = await window.showDirectoryPicker({ mode: "readwrite" });
    } catch (error) {
      if (error.name === "AbortError") return;
      directory = null;
    }
  }
  const failed = [];
  for (const [index, photo] of photos.entries()) {
    setStatus(`Exporting ${index + 1} of ${photos.length}…`);
    try {
      const blob =
        kind === "xmp"
          ? new Blob([paramsToXmp(photo.params)], {
              type: "application/rdf+xml",
            })
          : await renderFullSize(photo);
      if (directory) {
        const handle = await directory.getFileHandle(exportName(photo, kind), {
          create: true,
        });
        const writable = await handle.createWritable();
        await writable.write(blob);
        await writable.close();
      } else {
        download(blob, exportName(photo, kind));
        // Browsers drop downloads fired in the same instant.
        if (photos.length > 1)
          await new Promise((resolve) => setTimeout(resolve, 250));
      }
    } catch {
      failed.push(photo.name);
    }
  }
  const done = photos.length - failed.length;
  setStatus(
    (kind === "xmp"
      ? `Wrote ${done} Lightroom sidecar${done === 1 ? "" : "s"}. Put them beside the photos and use Read Metadata from File in Lightroom.`
      : `Exported ${done} photo${done === 1 ? "" : "s"} as JPEG.`) +
      (failed.length > 0
        ? ` ${failed.length} failed: ${failed.slice(0, 3).join(", ")}.`
        : ""),
  );
}

// ---------- wiring ----------

function setShowBefore(value) {
  if (state.showBefore === value) return;
  state.showBefore = value;
  queueRender();
}

function wire() {
  $("importButton").addEventListener("click", () => el.fileInput.click());
  el.fileInput.addEventListener("change", async () => {
    await importFiles(el.fileInput.files);
    el.fileInput.value = "";
  });

  el.stage.addEventListener("dragover", (event) => {
    event.preventDefault();
    el.stage.classList.add("dragging");
  });
  el.stage.addEventListener("dragleave", () =>
    el.stage.classList.remove("dragging"),
  );
  el.stage.addEventListener("drop", (event) => {
    event.preventDefault();
    el.stage.classList.remove("dragging");
    importFiles(event.dataTransfer.files);
  });

  el.beforeButton.addEventListener("pointerdown", () => setShowBefore(true));
  for (const type of ["pointerup", "pointerleave", "pointercancel"]) {
    el.beforeButton.addEventListener(type, () => setShowBefore(false));
  }

  document.addEventListener("keydown", (event) => {
    if (el.pairsDialog.open) return;
    const typing =
      event.target instanceof HTMLInputElement && event.target.type !== "range";
    if (typing) return;
    if (event.key === "\\") setShowBefore(true);
    const onSlider = event.target instanceof HTMLInputElement;
    if (
      !onSlider &&
      (event.key === "ArrowLeft" || event.key === "ArrowRight") &&
      state.currentId
    ) {
      const index = state.photos.findIndex((p) => p.id === state.currentId);
      const next = state.photos[index + (event.key === "ArrowRight" ? 1 : -1)];
      if (next) selectPhoto(next.id);
    }
  });
  document.addEventListener("keyup", (event) => {
    if (event.key === "\\") setShowBefore(false);
  });

  $("aiEdit").addEventListener("click", () => aiEdit([currentPhoto()]));
  $("aiEditAll").addEventListener("click", () => aiEdit(state.photos));
  $("teach").addEventListener("click", teachFromCurrent);
  $("teachPairs").addEventListener("click", () => {
    el.pairsResult.textContent = "";
    el.pairsDialog.showModal();
  });
  $("pairsLearn").addEventListener("click", async (event) => {
    event.target.disabled = true;
    try {
      await learnFromPairs();
    } finally {
      event.target.disabled = false;
    }
  });

  $("styleExport").addEventListener("click", () => {
    const json = JSON.stringify(
      { version: 1, samples: state.samples },
      null,
      2,
    );
    download(new Blob([json], { type: "application/json" }), "my-style.json");
  });
  $("styleImport").addEventListener("click", () => el.styleFile.click());
  el.styleFile.addEventListener("change", async () => {
    const file = el.styleFile.files[0];
    el.styleFile.value = "";
    if (!file) return;
    try {
      const samples = parseSamples(JSON.parse(await file.text()).samples);
      for (const sample of samples) upsertSample(sample);
      saveSamples();
      for (const photo of state.photos) renderThumb(photo);
      setStatus(
        `Loaded ${samples.length} example${samples.length === 1 ? "" : "s"} from ${file.name}.`,
      );
    } catch (error) {
      setStatus(`${file.name} is not a usable style file: ${error.message}.`);
    }
  });
  $("styleReset").addEventListener("click", () => {
    const count = state.samples.length;
    if (count === 0) return;
    if (
      !confirm(
        `Forget all ${count} learned example${count === 1 ? "" : "s"}? Save a style file first if you want a backup.`,
      )
    )
      return;
    state.samples = [];
    saveSamples();
    for (const photo of state.photos) renderThumb(photo);
    setStatus("Style forgotten.");
  });

  $("copySettings").addEventListener("click", () => {
    state.clipboard = { ...currentPhoto().params };
    updateControls();
    setStatus("Settings copied.");
  });
  $("pasteSettings").addEventListener("click", () => {
    setParams(currentPhoto(), state.clipboard, { aiEdited: false });
  });
  $("resetSettings").addEventListener("click", () => {
    setParams(currentPhoto(), neutralParams(), { aiEdited: false });
  });

  $("exportCurrent").addEventListener("click", () =>
    exportPhotos([currentPhoto()]),
  );
  $("exportAll").addEventListener("click", () => exportPhotos(state.photos));
  $("exportXmp").addEventListener("click", () =>
    exportPhotos(state.photos, "xmp"),
  );
}

buildSliders();
wire();
const loaded = loadSamples();
state.samples = loaded.samples;
state.model = trainStyle(state.samples);
renderStyleSummary();
if (loaded.problem) setStatus(loaded.problem);

// Exposed for automated checks of the running app.
window.photoEditor = { state, SLIDER_KEYS };
