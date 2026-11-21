import io
import math
import struct
import sys
import threading
import wave

RATE = 22050

RECIPES = {
    "wake": [(660, 0.07), (990, 0.11)],
    "done": [(880, 0.07), (660, 0.12)],
    "nevermind": [(520, 0.06), (390, 0.10)],
    "alert": [(440, 0.09), (0, 0.05), (440, 0.09)],
    "ask": [(740, 0.06), (0, 0.03), (740, 0.06)],
    "error": [(300, 0.14), (225, 0.16)],
    "info": [(1175, 0.06)],
}


def tone(freq, seconds, volume):
    n = int(RATE * seconds)
    out = []
    for i in range(n):
        if freq <= 0:
            out.append(0)
            continue
        t = i / RATE
        env = min(1.0, i / (RATE * 0.008)) * min(1.0, (n - i) / (RATE * 0.04))
        v = math.sin(2 * math.pi * freq * t) * 0.8 + math.sin(2 * math.pi * freq * 2 * t) * 0.12
        out.append(int(32767 * volume * env * v))
    return out


def render(name, volume=0.25):
    samples = []
    for freq, seconds in RECIPES[name]:
        samples += tone(freq, seconds, volume)
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
