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

## Several channels at once

The feedback tab takes a list of channels to watch. Each row pairs one input channel on the audio
device (for example Qu USB channel 3) with its own desk target and PEQ bands. Every row has its own
detector and its own cuts, and all rows share one network connection to the desk (the Qu only allows
one). The cuts are saved per target when you press Stop.

## Ring-out mode

Pick a watched row, set the limits, press Start, then **Start ring-out**. The app:

1. sets that target's fader to **Start dB**, then raises it by **Step dB** every **Every s**,
2. pulls the fader back by **Back off dB** whenever something rings. The suppressor cuts that
   frequency, and the app waits a few seconds before it climbs again,
3. finishes at **Max dB**, or when the EQ has no bands left. It then reports the highest level held
   without howling and puts the fader back to the start level,
4. drops the fader 10 dB and stops straight away if the input goes over **Stop above dBFS**.

**STOP ring-out** (or Stop, or closing the window) puts the fader back to the start level at any
time. Max is capped at +10 dB and the step at 3 dB. Start with the system turned down. The fader
values on the Qu use the V1.9 doc's fader table, so check one move on the desk first.

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
| Holds for ≥ 0.5 s and drifts ≤ ±3 Hz | Speech moves, and vibrato wobbles |
| Level holding or growing (not falling faster than 2 dB/s) | A struck note decays, while feedback holds or grows |

When a peak passes every test, the notch manager acts on it:

1. A new frequency gets a free EQ band as a gentle bell cut 1/4 octave wide. It starts at −4 dB, or up
   to 3 dB deeper for a worse howl (one that stands further out of the spectrum or is already loud).
2. Once a band has a frequency it stays there. If that frequency rings again (and at least 1.5 s has
   passed since the last change), the bell goes 2 dB deeper and a little wider, up to 2/3 octave.
3. **Recovery:** once a cut's frequency has been quiet for 8 s, the cut eases back 1 dB every 2 s until
   it's flat and the band is free again. If it rings again on the way up, it drops 1 dB below where it
   rang and never comes back up past that point.
4. **Total cap:** one channel can carry at most 18 dB of cut across all its bands (24 dB on a GEQ). Past
   that, the app reports `budget` and cuts nothing more.
5. If a howl rings within 1/3 octave of an existing cut, that same bell is re-centred between the two
   and widened to cover both. It doesn't take another band.
6. When every band is in use, a howl within 2/3 octave of a cut widens that cut. Anything further
   away is reported as `full`, and no band is moved.
7. Rows on the same desk channel share one set of bands, so they can't fight over one.
8. On the Qu GEQ, all 28 faders are available and each can go to −12 dB. Each cut is spread over the faders either side of it as a bell curve (for example −4/−8/−12/−8/−4). Nearby cuts are joined into one shape, no two neighbouring faders differ by more than 4 dB, and
   overlapping cuts take the deeper value at each fader.
9. With the HPF switched on (the app's "HPF up to" switch, or `--hpf-max 160`), a low howl raises the
   input's high-pass filter to half an octave above the howl instead of using a PEQ band. If it rings
   again, the HPF goes up another 1/3 octave, never past the limit. Once the HPF is at the limit, a PEQ
   band takes over. Mixes and LR have no HPF, so this only works on inputs. The Qu HPF uses NRPN `13`
   (frequency, assumed log from 20 Hz to 2 kHz) and `14` (in). On the DM7 it uses `InCh/HPF/Freq` and `On`.
   **Clear cuts** doesn't move the HPF back, because the app can't read where it was before.
10. The deepest cut is −12 dB on the Qu (the most its PEQ allows) and −18 dB on the DM7. A band
   changes at most once every 0.5 s.

The app's **Analyser (RTA)** tab shows the live spectrum of a watched channel. Peaks that are starting to
ring show in orange, feedback that was found shows in red, and the cuts on the desk show as a green curve.

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

Targets and bands (band numbers are 1-4, the same as on the desk):

- **Qu PEQ** (`--desk qu`): targets `Ip1`–`Ip32`, `ST1`–`ST3`, `Mix1`–`Mix4`, `LR` (or a raw code
  such as `0x64`). Bands are 1 = LF, 2 = LM, 3 = HM, 4 = HF, and the default is 2,3. If you use LF
  or HF, the tool switches that band to bell first.
- **Qu GEQ** (`--desk qu-geq`): only on mixes and LR (`Mix1`–`Mix4`, `LR`). Each howl pulls down
  the nearest of the 28 graphic faders (31.5 Hz–16 kHz), using at most 6 faders.
- **DM7** (`--desk dm7`): targets `InCh:0`, `Mix:3`, `Mtrx:1` (0-based). Bands are 1-4, and the
  default is 2,3.

When you write to the desk, it first asks the desk for its state. On a Qu that also tells it the
desk's MIDI channel, which it then uses. Every message sent is shown in the log as `[qu sent]` or
`[dm7 sent]`. Peaks quieter than −50 dBFS (adjustable) are ignored, because those are usually hum
or room tone, not feedback.

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

## Qu routing page

The **Qu routing** tab sends the input channels you pick to a mix, **post-fade**, and takes them off L+R.
The mix output is patched to a socket and cabled (or looped over USB) into a spare **return input**, which
goes on L+R. You can then EQ the mix (and point the feedback suppressor at it) while the L+R EQ stays clean.
**Read from desk** asks the desk for its system state and shows each channel's L+R and mix assignment.
**Put back on L+R** undoes it.

The messages used are NRPN `55` (mix assign), `50` (pre/post), `20` (send level), `18` (L+R assign) and
`17` (fader), with VX set to the mix's channel code (Mix1 = `60`). Pre/post `00` = post-fade and the L+R
assign VX `07` are **assumed**. If **Read from desk** sees a different VX for L+R, the app uses that one.
Check the routing screen on the desk the first time you use it.
