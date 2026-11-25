# Mixer Tools (Qu-16 and DM7)

`MixerTools.exe` is one Windows app with two tabs:

- **Feedback suppressor**: pick the audio input and the desk, then press Start. Details below.
- **Qu-16 / DM7 tools**: the probe tools in `desktools/`. Discover, sync and meters work on the
  Qu-16. Info, dump, watch, get, set and raw work on the DM7. Each run is logged to
  `%USERPROFILE%\MixerTools\logs`.

Settings and saved notches are kept in `%USERPROFILE%\MixerTools`. The exe also runs any tool from
a command prompt, for example `MixerTools.exe --tool qu16 discover` (output goes to the
console only when started from a console build, so in the window build use the tools tab).
The exe is built by `.github/workflows/mixer-tools-windows.yml`. That workflow runs the unit tests,
packages the app with PyInstaller and self-tests the result.

# Feedback suppressor

This tool listens to a feed from the desk, finds frequencies that are starting to howl, and puts
narrow cuts on that channel's or mix's **own parametric EQ** over the network. No audio goes back
through the PC, so it adds no latency, and it needs no third-party licence. It's our own design.
None of it comes from Alpha Labs De-Feedback (see `../defeedback-notes.md`).

Requirements: Python 3.9+ and `numpy`. For live input you also need `pip install sounddevice`.

## How it decides something is feedback

Every ~21 ms it takes an 8192-point FFT and checks each strong peak against all of these:

| Test | Why |
|---|---|
| Peak-to-average power ratio ≥ 16 dB | A howl stands far above the rest of the spectrum |
| Peak-to-neighbour ratio ≥ 12 dB | A howl is one sharp line, not a broad formant |
| Peak-to-harmonic ratio ≥ 15 dB (2nd, 3rd and sub-harmonic) | A howl is close to a pure sine. Voices and instruments have harmonics |
| Holds for ≥ 0.3 s and drifts ≤ ±3 Hz | Speech moves, and vibrato wobbles |
| Level not falling faster than 6 dB/s | A struck note decays, while feedback holds or grows |

When a peak passes every test, the notch manager acts on it:

1. A new frequency gets a free EQ band, cut by −6 dB at 1/9 octave.
2. If the same frequency (within 1/6 octave) rings again, that band goes 3 dB deeper and is
   re-centred. A band changes at most once every 0.5 s.
3. The deepest cut is −12 dB on the Qu (the most its PEQ allows) and −18 dB on the DM7.
4. When all the allowed bands are used, it reports `full` and changes nothing.

## Commands

```
python feedback_suppressor.py simulate            # closed-loop demo; writes sim-no-control.wav / sim-with-suppressor.wav
python feedback_suppressor.py analyse show.wav --channel 3   # list howls in a multitrack recording
python feedback_suppressor.py devices
python feedback_suppressor.py live --device "Qu-16" --channel 1 --desk qu --host 192.168.1.50 --target Ip1
python feedback_suppressor.py live ... --apply    # actually write the EQ
python feedback_suppressor.py notch --desk qu --host ... --target Ip1 --band 2 --freq 2500 --gain -6 --apply
python feedback_suppressor.py clear --desk qu --host ... --target Ip1 --bands 1,2 --apply
```

**Nothing is sent to a desk without `--apply`.** Without it, every command prints the exact bytes
(Qu) or RCP lines (DM7) it would send. On Ctrl+C, `live` saves its notches to `notches.json`, and
`--load notches.json` puts them back next time. You can use that to ring out a system before the show.

Targets and bands:

- **Qu:** `Ip1`–`Ip32`, `ST1`–`ST3`, `Mix1`–`Mix4`, `LR` (or a raw code such as `0x64`). Bands are
  0 = LF, 1 = LM, 2 = HM, 3 = HF. The default is 1 and 2. If you use LF or HF, the tool switches
  that band to bell first.
- **DM7:** `InCh:0`, `Mix:3`, `Mtrx:1` (0-based). Bands are 0–3. The default is 1 and 2.

Wedge feedback is usually the problem, so point it at the wedge's mix (Qu `Mix1`, DM7 `Mix:0`) and
feed it that mix's output.

## Audio routing (important)

The tool has to hear the signal **after** the EQ it's changing. If it doesn't, the notch never
shows up in what it hears, and it keeps deepening the same band to the maximum.

- **Qu-16:** set the USB send for the channel or mix you're watching to a point after the PEQ
  (for example a direct-out pick-off at pre-fader, or the mix output). A post-preamp pick-off
  won't work. Then pick that USB channel with `--device` / `--channel`.
- **DM7:** patch a direct out (after EQ) or the mix output to Dante or USB, and select it the same way.

## Things to check on your own desk before `--apply`

The public docs give only part of the scaling. These parts are assumptions:

| Desk | Assumed | How to check |
|---|---|---|
| Qu | Freq 00–7F is log from 20 Hz to 20 kHz, and width 00–7F is log from 1.5 to 1/9 octave. The doc gives only these end points | Run `qu16_probe.py sync`, set HM freq to a few values on the desk (100 Hz, 1 kHz, 5 kHz) and note the `va` it prints. Put the points in a JSON file such as `{"freq": [[va, hz], ...], "width": [[va, oct], ...]}` and pass it with `--calibration` |
| Qu | NRPN data-entry LSB (`BN 26`) is `07` for PEQ | Taken from the V1.9 doc. `sync` should show `vx=07` |
| DM7 | Freq is Hz×10, gain is dB×100, Q is ×1000, and band index Y runs 0–3 | `dm7_rcp_probe.py get <ip> MIXER:Current/InCh/PEQ/Band/Gain 0 1` while the desk shows a known gain |

The first time you use `--apply`, send one manual `notch` to a channel that isn't live and check it
on the desk screen.

## Known limits

- A long, steady, pure tone (a flute note, a test oscillator, some synth pads) passes all the
  tests and can be notched by mistake. You can lower sensitivity with `--papr` / `--persist`.
- It removes howl. It doesn't de-reverb or de-noise. That's what the VST3 hosting phase is for.
- Notches don't release by themselves yet. Use `clear`, or recall the scene.

## Tests

`python -m unittest discover -s tests` runs 13 tests. They cover no false alarms on synthetic speech,
harmonic music with vibrato, or a decaying tone. They check that howls at 180 Hz to 6.3 kHz are
found under speech and music while still quiet. They also run a **closed loop** (mic → EQ → speaker
→ room with loop gain 1–3 dB over unity). Without control the loop howls 30+ dB over the source.
With the suppressor it settles to within 3 dB of the source.

## Phase 2: hosting the Alpha Labs De-Feedback VST3

This part is planned and not built yet. The app becomes a small VST3 host, using Steinberg's
public VST3 SDK or JUCE's `AudioPluginFormatManager`. It would:

1. load `Defeedback.vst3` from `C:\Program Files\Common Files\VST3`, using your own activated licence,
2. take one or two channels from the desk over USB or Dante, process them, and send them back on a
   spare return,
3. expose the plugin's `Strength` parameter and bypass in the mixer app, and use the Qu and DM7
   control code to patch and unpatch the return.

It uses only the plugin's public VST3 interface. That means no decompiling and no work around its
licensing.
