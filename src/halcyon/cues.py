import io
import math
import struct
import sys
import threading
import wave

RATE = 22050
CUTOFF = 2400.0

RECIPES = {
    "wake": [(0.00, 880, 880, 0.08, 0.9, 0.04), (0.08, 1175, 1175, 0.16, 1.0, 0.10)],
    "done": [(0.00, 784, 784, 0.09, 0.9, 0.05), (0.09, 1175, 1175, 0.22, 1.0, 0.16)],
    "ask": [(0.00, 660, 880, 0.20, 0.9, 0.08)],
    "nevermind": [(0.00, 700, 600, 0.20, 0.55, 0.14)],
    "info": [(0.00, 1047, 1047, 0.30, 0.7, 0.24)],
    "alert": [(0.00, 523, 523, 0.20, 1.0, 0.10), (0.34, 523, 523, 0.20, 1.0, 0.10), (0.00, 261, 261, 0.54, 0.5, 0.30)],
    "error": [(0.00, 392, 392, 0.15, 1.0, 0.06), (0.15, 311, 311, 0.28, 1.0, 0.20)],
}


def triangle(phase):
    return 2.0 / math.pi * math.asin(math.sin(phase))


def render_samples(name, volume):
    parts = RECIPES[name]
    total = max(s + d for s, _, _, d, _, _ in parts) + 0.05
    out = [0.0] * int(RATE * total)
    alpha = 1.0 - math.exp(-2.0 * math.pi * CUTOFF / RATE)
    for start, f0, f1, dur, level, rel in parts:
        n = int(RATE * dur)
        off = int(RATE * start)
        phase = 0.0
        for i in range(n):
            frac = i / max(1, n - 1)
            freq = f0 * (f1 / f0) ** frac
            phase += 2.0 * math.pi * freq / RATE
            env = min(1.0, i / (RATE * 0.014)) * min(1.0, (n - i) / (RATE * rel))
            out[off + i] += triangle(phase) * env * level
    y = 0.0
    for i, v in enumerate(out):
        y += alpha * (v - y)
        out[i] = y
    peak = max(1e-6, max(abs(v) for v in out))
    return [int(32767 * volume * v / peak) for v in out]


def render(name, volume=0.25):
    samples = render_samples(name, volume)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(struct.pack("<%dh" % len(samples), *samples))
    return buf.getvalue()


class Cues:
    def __init__(self, config):
        self.config = config
        self.cache = {}
        self.lock = threading.Lock()

    def play(self, name):
        if not self.config.get("cues_enabled") or name not in RECIPES or sys.platform != "win32":
            return
        try:
            import winsound
            vol = max(0.0, min(1.0, float(self.config.get("cues_volume")))) * 0.5
            key = (name, round(vol, 2))
            with self.lock:
                data = self.cache.get(key)
                if data is None:
                    data = render(name, vol)
                    self.cache[key] = data
            winsound.PlaySound(data, winsound.SND_MEMORY | winsound.SND_ASYNC)
        except Exception:
            pass


class NullCues:
    def play(self, name):
        pass
