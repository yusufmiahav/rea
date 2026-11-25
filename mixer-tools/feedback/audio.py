"""Audio sources: WAV files (standard library) and live input (optional `sounddevice`)."""
from __future__ import annotations

import wave

import numpy as np


def read_wav(path, channel=0):
    """Returns (mono float64 in -1..1, sample_rate). channel is 0-based."""
    with wave.open(str(path), "rb") as w:
        sr, nch, width, n = w.getframerate(), w.getnchannels(), w.getsampwidth(), w.getnframes()
        raw = w.readframes(n)
    if width == 2:
        data = np.frombuffer(raw, dtype="<i2").astype(np.float64) / 32768.0
    elif width == 3:
        b = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 3)
        ints = (b[:, 0].astype(np.int32) | (b[:, 1].astype(np.int32) << 8) | (b[:, 2].astype(np.int32) << 16))
        ints = np.where(ints & 0x800000, ints - 0x1000000, ints)
        data = ints.astype(np.float64) / 8388608.0
    elif width == 4:
        data = np.frombuffer(raw, dtype="<i4").astype(np.float64) / 2147483648.0
    else:
        raise ValueError(f"unsupported sample width {width}")
    data = data.reshape(-1, nch)
    if not 0 <= channel < nch:
        raise ValueError(f"file has {nch} channels; asked for channel {channel + 1}")
    return data[:, channel], sr


def write_wav(path, mono, sr):
    x = np.clip(np.asarray(mono), -1, 1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes((x * 32767).astype("<i2").tobytes())


def list_devices():
    import sounddevice as sd  # pip install sounddevice
    return sd.query_devices()


def live_stream(device, channel, sr, block, callback):
    """Calls callback(mono_block) from the audio thread. channel is 0-based.
    Returns the open stream; call .close() to stop."""
    import sounddevice as sd  # pip install sounddevice

    def cb(indata, frames, t, status):
        if status:
            print(f"[audio] {status}")
        callback(indata[:, channel].astype(np.float64))

    stream = sd.InputStream(device=device, channels=channel + 1, samplerate=sr, blocksize=block,
                            dtype="float32", callback=cb)
    stream.start()
    return stream
