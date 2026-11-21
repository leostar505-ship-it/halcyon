import io
import math
import struct
import sys
import threading
import wave

RATE = 22050
CUTOFF = 2400.0

RECIPES = {
    "wake": [(0.00, 560, 900, 0.30, 1.0, 0.18, 0.10)],
    "ask": [(0.00, 660, 660, 0.11, 0.9, 0.06), (0.11, 990, 990, 0.22, 1.0, 0.16)],
    "curious": [(0.00, 660, 880, 0.20, 0.9, 0.08)],
    "done": [(0.00, 988, 988, 0.07, 0.9, 0.04), (0.07, 1319, 1319, 0.24, 1.0, 0.18)],
    "nevermind": [(0.00, 700, 520, 0.28, 0.6, 0.20)],
    "info": [(0.00, 1047, 1047, 0.30, 0.7, 0.24)],
    "alert": [(0.00, 1047, 1047, 0.30, 0.7, 0.24), (0.13, 1047, 1047, 0.30, 0.7, 0.24)],
    "error": [(0.00, 659, 659, 0.09, 0.9, 0.05), (0.09, 494, 494, 0.22, 1.0, 0.16)],
}


def triangle(phase):
    return 2.0 / math.pi * math.asin(math.sin(phase))


def render_samples(name, volume):
    parts = RECIPES[name]
    total = max(p[0] + p[3] for p in parts) + 0.05
    out = [0.0] * int(RATE * total)
    alpha = 1.0 - math.exp(-2.0 * math.pi * CUTOFF / RATE)
    for part in parts:
        start, f0, f1, dur, level, rel = part[:6]
        attack = part[6] if len(part) > 6 else 0.014
        n = int(RATE * dur)
        off = int(RATE * start)
        phase = 0.0
        for i in range(n):
            frac = i / max(1, n - 1)
            freq = f0 * (f1 / f0) ** frac
            phase += 2.0 * math.pi * freq / RATE
            env = min(1.0, i / (RATE * attack)) * min(1.0, (n - i) / (RATE * rel))
            out[off + i] += triangle(phase) * env * level
    y = 0.0
    for i, v in enumerate(out):
        y += alpha * (v - y)
        out[i] = y
    return [int(32767 * max(-1.0, min(1.0, volume * v))) for v in out]


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
