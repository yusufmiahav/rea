# De-Feedback AI (VST3 + standalone)

A live-sound feedback suppressor. It listens for ringing frequencies and drops smooth,
narrow cuts on them before they take off. Original code; nothing is taken from any
commercial plugin.

## How it works
- **Zero latency.** The audio only passes through up to 12 notch (bell-cut) filters. Detection runs on a side chain.
- **Detection.** An 85 ms FFT every 10.7 ms finds spectral peaks and follows each one. For every peak it measures the classic feedback cues: how far the peak stands above the spectrum, how sharp it is, whether it has harmonics or a fundamental below it (sung and played notes do; feedback doesn't), whether it is growing, and how much its pitch and level wobble.
- **The AI part.** A small trained neural network (10 inputs, 16 and 8 hidden units) turns those cues into "probability this is feedback". It was trained on about 1.5 million frames from simulated closed loops (PA + room + mic with the gain ramped past the point of feedback), against sung phrases, held notes, chords, speech, drums and noise. Training code: `training/train.py`; data generator: `tools/dfb_sim.cpp`.
- **Smooth cuts.** A cut fades in over about 25 ms, starts at -4 dB (deeper if the ring is loud or fast-growing), and deepens 3 dB each time the ring comes back. Nearby rings widen the same bell instead of adding another. After **Hold** seconds without the ring, the cut fades out at 1.5 dB/s. A ring that returns 3 times becomes a **fixed** cut (amber). Fixed cuts are saved with your session.

## Controls
| Control | What it does |
|---|---|
| Sensitivity | 0-100. Higher catches rings sooner and quieter: about 220 ms at 0, 130 ms at 50, 35-45 ms at 100. Higher also means more risk of cutting a held note. |
| Mode | **Live** protects music and lets cuts release. **Ring-out** is for soundcheck: extra sensitive, and every cut stays fixed. |
| Max cut | The deepest any notch may go (3-24 dB). |
| Width | Notch width, 1/20 to 1/3 octave (default 1/8). |
| Hold | How long a cut stays after the ring stops. |
| Lock all / Freeze / Bypass | Keep every cut / stop adding new cuts / bypass the cuts (detection keeps showing rings). |
| Clear all / Clear live | Remove every cut, or only the live (non-fixed) ones. |
| Name box | Label the desk channel, e.g. "Qu-16 Ch 3 Vox". It is saved with the session. |

The IN and OUT meters show the channel coming from the desk (peak, RMS and peak hold) and what leaves the plugin.

## Using it with the Qu-16 or DM7
- **In a DAW or plugin host** (Reaper, Gig Performer, Cantabile and so on): pick the desk's USB audio device. Put the vocal channel's USB/direct-out send on a track, insert De-Feedback AI, and route the track back to the desk on a USB return channel.
- **Without a DAW:** run `De-Feedback AI (Standalone).exe`. Open *Options > Audio/MIDI Settings*, choose the desk's USB audio device, and pick the input and output channels.

## Install
Copy the `De-Feedback AI.vst3` folder to `C:\Program Files\Common Files\VST3\`, then rescan plugins in your host. The build is unsigned, so Windows SmartScreen may warn on the standalone exe. Choose "More info", then "Run anyway".

## Results in simulation (`dfb_sim eval 99 12 0.5`)
At sensitivity 50, the mean added gain before feedback is about 7 dB across sung, held-note, speech, chord, drum and noise sources. That is in line with the 5-8 dB the literature reports for notch-based suppression. False triggers without any feedback were 0-2.5 per 16 s, mostly shallow and released afterwards. This is a simulation; real rooms will differ, so start at sensitivity 40-60 and use Ring-out at soundcheck.

## Build
```
cmake -B build -DCMAKE_BUILD_TYPE=Release   # fetches JUCE 8.0.9
cmake --build build --config Release
build/dfb_tests
```
Windows builds come from `.github/workflows/defeedback-vst3-windows.yml`.
JUCE is used under its AGPLv3 or free Starter licence.

## Retraining the classifier
```
dfb_sim data 11 84 d11.csv   # repeat with other seeds
python training/train.py d11.csv d12.csv d13.csv --val d14.csv --out core/model_weights.h
```
