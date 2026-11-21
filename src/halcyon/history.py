import threading
import time


class History:
    def __init__(self, turns=12, minutes=30, clock=time.monotonic):
        self.turns = int(turns or 0)
        self.minutes = float(minutes or 0)
        self.clock = clock
        self.items = []
        self.lock = threading.Lock()

    def add(self, who, text):
        if self.turns <= 0:
            return
        text = " ".join(str(text or "").split())
        if not text:
            return
        if len(text) > 300:
            text = text[:297] + "..."
        now = self.clock()
        cutoff = now - self.minutes * 60
        with self.lock:
            self.items = [item for item in self.items if item[0] >= cutoff]
            self.items.append((now, who, text))
            self.items = self.items[-self.turns * 2:]

    def recent(self):
        cutoff = self.clock() - self.minutes * 60
        with self.lock:
            return [(who, text) for stamp, who, text in self.items if stamp >= cutoff]

    def text(self):
        return "\n".join("%s: %s" % (who, text) for who, text in self.recent())

    def clear(self):
        with self.lock:
            count = len(self.items)
            self.items = []
        return count
