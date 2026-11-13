# Photo editor experiment

A browser photo editor that learns your editing style and applies it to new photos. It is separate from REA's runtime: nothing in `src/` depends on it, and it has no dependencies of its own.

Everything runs locally in the browser. Photos are never uploaded.

## Run

```bash
npm start
```

Then open the printed address. Run the engine tests with:

```bash
npm test
```

## What it does

- **Edit**: exposure, contrast, highlights, shadows, whites, blacks, temperature, tint, vibrance and saturation, with a live histogram, before/after view (hold `\`), copy/paste of settings and full-resolution JPEG export.
- **Teach your style**: after editing a photo, _Teach from this edit_ stores a description of the unedited photo together with your slider settings. _Learn from before/after_ does the same from pairs of original and finished files by recovering the settings that turn one into the other.
- **AI edit**: predicts slider settings for photos it has not seen. The result is ordinary slider values, so every AI edit can be refined by hand.
- **Style files**: the learned examples are kept in the browser and can be saved to or loaded from a JSON file.

## How the style model works

`src/style.js` describes each unedited photo with eleven numbers (brightness, tonal spread, percentiles, clipped fractions, colour balance, saturation). With four or more examples it fits a ridge regression from those numbers to each slider, so dark photos get the exposure you would give dark photos, warm ones the white balance you would give warm ones. With fewer examples it applies your average edit.

Before/after pairs whose difference the sliders cannot reproduce (crops, local adjustments, a different photo) are rejected rather than learned from.

## Known limits

- JPEG, PNG and WebP only. Raw camera files are not decoded.
- Edits are global. There are no local adjustments, cropping, straightening, or culling.
- Sliders are named after the familiar controls but are this project's own maths; values do not transfer one-to-one to other editors.
- The style model is small and statistical. It has only been checked against synthetic images and a simulated photographer, not against real wedding or portrait sets.
- Export writes 8-bit sRGB JPEG and does not carry over metadata.
