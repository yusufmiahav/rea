// Raw camera files: locates the JPEG preview the camera embedded in the file.
//
// This editor does not develop sensor data. A raw file is shown, analysed and
// exported through its embedded preview, and the real development is left to
// Lightroom via .xmp sidecars. Works on bytes only, so it also runs in Node.

export const RAW_EXTENSIONS = [
  "3fr",
  "arw",
  "cr2",
  "cr3",
  "crw",
  "dng",
  "erf",
  "iiq",
  "kdc",
  "mef",
  "mos",
  "mrw",
  "nef",
  "nrw",
  "orf",
  "pef",
  "raf",
  "raw",
  "rw2",
  "rwl",
  "sr2",
  "srf",
  "srw",
  "x3f",
];

export function isRawName(name) {
  const extension = name.slice(name.lastIndexOf(".") + 1).toLowerCase();
  return RAW_EXTENSIONS.includes(extension);
}

const MIN_PREVIEW_EDGE = 160;

// Walks one JPEG stream from its start marker. Returns its extent and frame
// size, or null when the bytes are not a displayable (8-bit, DCT) JPEG;
// lossless-JPEG sensor data inside raw files is rejected here.
function readJpeg(bytes, start) {
  let offset = start + 2;
  let frame = null;
  let orientation = null;
  while (offset + 2 <= bytes.length) {
    if (bytes[offset] !== 0xff) return null;
    const marker = bytes[offset + 1];
    if (marker === 0xff) {
      offset += 1;
      continue;
    }
    if (marker === 0xd9)
      return frame ? { start, end: offset + 2, ...frame, orientation } : null;
    if (
      marker === 0xd8 ||
      (marker >= 0xd0 && marker <= 0xd7) ||
      marker === 0x01
    ) {
      offset += 2;
      continue;
    }
    if (offset + 4 > bytes.length) return null;
    const length = (bytes[offset + 2] << 8) | bytes[offset + 3];
    if (length < 2 || offset + 2 + length > bytes.length) return null;
    const isFrame =
      marker >= 0xc0 &&
      marker <= 0xcf &&
      marker !== 0xc4 &&
      marker !== 0xc8 &&
      marker !== 0xcc;
    if (isFrame) {
      if (marker > 0xc2) return null; // lossless, hierarchical or arithmetic
      if (bytes[offset + 4] !== 8) return null;
      const components = bytes[offset + 9];
      if (components !== 1 && components !== 3) return null;
      frame = {
        height: (bytes[offset + 5] << 8) | bytes[offset + 6],
        width: (bytes[offset + 7] << 8) | bytes[offset + 8],
      };
    }
    if (marker === 0xe1 && orientation === null) {
      const isExif =
        bytes[offset + 4] === 0x45 &&
        bytes[offset + 5] === 0x78 &&
        bytes[offset + 6] === 0x69 &&
        bytes[offset + 7] === 0x66 &&
        bytes[offset + 8] === 0 &&
        bytes[offset + 9] === 0;
      if (isExif)
        orientation = readTiffOrientation(
          bytes,
          offset + 10,
          offset + 2 + length,
        );
    }
    offset += 2 + length;
    if (marker === 0xda) {
      if (!frame) return null;
      // Entropy-coded data: a marker is 0xFF followed by anything except a
      // stuffed zero or a restart marker.
      for (;;) {
        offset = bytes.indexOf(0xff, offset);
        if (offset < 0 || offset + 1 >= bytes.length) return null;
        const next = bytes[offset + 1];
        if (next === 0 || (next >= 0xd0 && next <= 0xd7) || next === 0xff) {
          offset += 1;
          continue;
        }
        break;
      }
    }
  }
  return null;
}

/** Every displayable JPEG stream embedded in `bytes`, in file order. */
export function findEmbeddedJpegs(bytes) {
  const found = [];
  let offset = 0;
  for (;;) {
    offset = bytes.indexOf(0xff, offset);
    if (offset < 0 || offset + 3 >= bytes.length) break;
    if (bytes[offset + 1] === 0xd8 && bytes[offset + 2] === 0xff) {
      const jpeg = readJpeg(bytes, offset);
      if (jpeg) {
        found.push(jpeg);
        offset = jpeg.end; // skips thumbnails nested inside this stream
        continue;
      }
    }
    offset += 1;
  }
  return found;
}

/**
 * Reads the EXIF Orientation tag (1-8) from the first directory of a TIFF
 * structure starting at `start`. Returns null when there is none.
 */
export function readTiffOrientation(bytes, start = 0, end = bytes.length) {
  if (start + 8 > end) return null;
  const little = bytes[start] === 0x49 && bytes[start + 1] === 0x49;
  const big = bytes[start] === 0x4d && bytes[start + 1] === 0x4d;
  if (!little && !big) return null;
  const u16 = (o) =>
    little ? bytes[o] | (bytes[o + 1] << 8) : (bytes[o] << 8) | bytes[o + 1];
  const u32 = (o) =>
    little
      ? (bytes[o] |
          (bytes[o + 1] << 8) |
          (bytes[o + 2] << 16) |
          (bytes[o + 3] << 24)) >>>
        0
      : ((bytes[o] << 24) |
          (bytes[o + 1] << 16) |
          (bytes[o + 2] << 8) |
          bytes[o + 3]) >>>
        0;
  const directory = start + u32(start + 4);
  if (directory + 2 > end) return null;
  const count = u16(directory);
  for (let i = 0; i < count; i += 1) {
    const entry = directory + 2 + i * 12;
    if (entry + 12 > end) return null;
    if (u16(entry) === 0x0112) {
      const value = u16(entry + 8);
      return value >= 1 && value <= 8 ? value : null;
    }
  }
  return null;
}

// Canon CR3 keeps its TIFF metadata inside a "CMT1" box near the file start.
function readContainerOrientation(bytes) {
  const direct = readTiffOrientation(bytes);
  if (direct !== null) return direct;
  const limit = Math.min(bytes.length - 12, 1 << 20);
  for (let i = 4; i < limit; i += 1) {
    if (
      bytes[i] === 0x43 &&
      bytes[i + 1] === 0x4d &&
      bytes[i + 2] === 0x54 &&
      bytes[i + 3] === 0x31
    ) {
      return readTiffOrientation(bytes, i + 4);
    }
  }
  return null;
}

/**
 * Chooses the preview to show for a raw file: the largest embedded JPEG.
 * `orientation` is the rotation still to be applied when drawing it: 1 when
 * the preview carries its own EXIF orientation (the browser applies that),
 * otherwise the raw file's orientation, or 1 when the file declares none.
 * Returns null when the file embeds no usable preview.
 */
export function findRawPreview(bytes) {
  let best = null;
  for (const jpeg of findEmbeddedJpegs(bytes)) {
    if (Math.max(jpeg.width, jpeg.height) < MIN_PREVIEW_EDGE) continue;
    if (!best || jpeg.width * jpeg.height > best.width * best.height)
      best = jpeg;
  }
  if (!best) return null;
  const orientation =
    best.orientation === null ? (readContainerOrientation(bytes) ?? 1) : 1;
  return {
    start: best.start,
    end: best.end,
    width: best.width,
    height: best.height,
    orientation,
  };
}
