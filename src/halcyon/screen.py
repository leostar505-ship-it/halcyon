import math
import random
import threading
import time

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QColor, QFont, QFontMetricsF, QImage, QLinearGradient, QPainter, QPainterPath)

from .vendor import protocol as P
from .vendor import regions as R
from .vendor.link import SerialLink, list_ports

W = 160
H = 120
SS = 4

EXPR = {
    "neutral": dict(hs=1, ws=1, tc=0, slant=0, bc=0, bcv=0, hl=1, hr=1, tl=0, tr=0),
    "happy": dict(hs=1, ws=1, tc=0, slant=0, bc=.1, bcv=.24, hl=1, hr=1),
    "curious": dict(hs=1.04, ws=1, tc=0, slant=0, bc=0, bcv=0, hl=1.12, hr=.94, tl=0, tr=.22),
    "suspicious": dict(hs=.82, ws=1.05, tc=.3, slant=.85, bc=.08, bcv=0, hl=1, hr=1),
    "sad": dict(hs=1.06, ws=.94, tc=.1, slant=-1.15, bc=0, bcv=0, hl=1, hr=1),
    "surprised": dict(hs=1.22, ws=.88, tc=0, slant=0, bc=0, bcv=0, hl=1, hr=1),
    "sleepy": dict(hs=.5, ws=1, tc=.35, slant=0, bc=.05, bcv=0, hl=1, hr=1),
}
ALIASES = {"worried": "sad", "concerned": "sad", "nosy": "curious", "alert": "surprised", "shocked": "surprised"}

LEVELS = {
    "info": dict(top=(155, 224, 255), mid=(63, 168, 255), bot=(31, 107, 224), label=(95, 159, 214), bar=(42, 111, 176)),
    "warn": dict(top=(255, 215, 154), mid=(255, 168, 61), bot=(217, 115, 15), label=(209, 154, 74), bar=(168, 102, 28)),
    "alert": dict(top=(255, 176, 176), mid=(255, 74, 74), bot=(192, 24, 24), label=(216, 106, 106), bar=(160, 40, 40)),
}

IDLE_SECONDS = 60


def clamp(v, a=0.0, b=1.0):
    return min(b, max(a, v))


def ease(p):
    return 1 - (1 - p) ** 3


def qcol(c, a=1.0):
    return QColor(int(c[0]), int(c[1]), int(c[2]), int(255 * a))


class Spring:
    def __init__(self, v):
        self.x = v
        self.v = 0.0
        self.t = v

    def step(self, dt, k, d):
        n = 4
        h = dt / n
        for _ in range(n):
            a = k * (self.t - self.x) - d * self.v
            self.v += a * h
            self.x += self.v * h


class Eyes:
    def __init__(self):
        self.lock = threading.RLock()
        self.cur = dict(EXPR["neutral"])
        self.col = {k: list(v) for k, v in LEVELS["info"].items()}
        self.level = "info"
        self.expr = "neutral"
        self.activity = "none"
        self.cx = Spring(80.0)
        self.cy = Spring(54.0)
        self.sc = Spring(1.0)
        self.lx = Spring(0.0)
        self.ly = Spring(0.0)
        self.card = None
        self.side = False
        self.reveal_t = 0.0
        self.panel_a = 0.0
        self.footer = ""
        self.footer_level = "info"
        self.blink = 0.0
        self.blink_start = -9.0
        self.next_blink = 2.0
        self.next_glance = 1.5
        self.last_active = time.monotonic()
        self.slow_until = 0.0
        self.peek_start = -99.0
        self.next_peek = 6.0
        self.frantic = 0.0
        self.next_dart = 0.0
        self.drowse_start = 0.0
        self.timers = []
        self.hide_at = 0.0
        self.last_t = time.monotonic()
        self.layer = QImage(W * SS, H * SS, QImage.Format_ARGB32_Premultiplied)
        self.out = QImage(W * SS, H * SS, QImage.Format_RGB32)
        self.font = QFont("Consolas")
        self.font.setStyleHint(QFont.Monospace)

    def touch(self):
        self.last_active = time.monotonic()

    def set_expression(self, name):
        name = ALIASES.get(name, name)
        if name not in EXPR:
            return
        with self.lock:
            self.drowse_start = 0.0
            self.expr = name
            self.touch()
            if name == "curious":
                self.next_glance = 0.0

    def set_level(self, level):
        if level in LEVELS:
            with self.lock:
                self.level = level

    def set_activity(self, a):
        with self.lock:
            self.drowse_start = 0.0
            self.activity = a
            self.touch()

    def set_footer(self, text, level="info"):
        with self.lock:
            self.footer = text or ""
            self.footer_level = level

    def after(self, delay, fn):
        with self.lock:
            self.timers.append((time.monotonic() + delay, fn))

    def start_frantic(self):
        t0 = time.monotonic()
        self.frantic = t0 + .4
        self.next_dart = 0.0
        self.next_blink = 0.0
        self.expr = "surprised"
        self.touch()

    def wake(self):
        with self.lock:
            self.timers.clear()
            self.close_card()
            self.activity = "none"
            self.drowse_start = 0.0
            self.expr = "surprised"
            self.touch()
        self.after(.5, lambda: self.set_expression("neutral"))

    def sleep_now(self):
        with self.lock:
            self.timers.clear()
            self.close_card()
            self.expr = "neutral"
            self.activity = "sleep"
            self.drowse_start = 0.0
            self.slow_until = time.monotonic() + 3
            self.next_peek = self.slow_until + 1
            self.touch()

    def drowse(self):
        with self.lock:
            self.timers.clear()
            self.close_card()
            self.activity = "none"
            self.expr = "neutral"
            self.drowse_start = time.monotonic()
            self.touch()

    def open_card(self, lines, level, expr, keep=0):
        with self.lock:
            was_asleep = self.activity == "sleep"
            self.timers.clear()
            self.activity = "none"
            self.drowse_start = 0.0
            self.touch()
            if was_asleep:
                self.start_frantic()
            wait = .45 if was_asleep else (.5 if self.side else 0)
            if self.side:
                self.close_card()
            self.hide_at = time.monotonic() + wait + .45 + (keep or 9)

        def stage1():
            self.set_expression("surprised")
            self.set_level(level)
            self.after(.45, stage2)

        def stage2():
            self.set_expression(expr)
            with self.lock:
                self.card = {"lines": lines}
                self.side = True
                self.reveal_t = 0.0
                self.touch()

        self.after(wait, stage1)

    def close_card(self):
        with self.lock:
            self.side = False
            self.card = None
            self.level = "info"
            if self.activity in ("none", "speak"):
                self.expr = "neutral"

    def step(self, t, dt):
        with self.lock:
            self._step(t, dt)

    def _step(self, t, dt):
        due = [x for x in self.timers if x[0] <= t]
        self.timers = [x for x in self.timers if x[0] > t]
        for _, fn in due:
            fn()
        if self.side and self.hide_at and t > self.hide_at:
            self.hide_at = 0.0
            self.close_card()
        if self.activity == "none" and not self.side and not self.drowse_start and t >= self.frantic and \
                t - self.last_active > IDLE_SECONDS:
            self.drowse_start = t
            self.expr = "neutral"
        dip = 0.0
        d_base = 1.0
        if self.drowse_start:
            p = t - self.drowse_start
            d_base = max(.4, 1 - p * .2)
            d1 = (p - 1.7) / .8
            d2 = (p - 3.1) / .8
            dip = max(math.sin(d1 * math.pi) if 0 <= d1 < 1 else 0, math.sin(d2 * math.pi) if 0 <= d2 < 1 else 0)
            if p >= 4.6:
                self.drowse_start = 0.0
                self.activity = "sleep"
                self.expr = "neutral"
                self.slow_until = t + 2.2
                self.next_peek = t + 5
                dip = 0.0
        tgt = dict(EXPR["neutral"])
        tgt.update(EXPR[self.expr])
        if self.activity == "sleep":
            if t > self.next_peek:
                self.peek_start = t
                self.next_peek = t + random.uniform(7, 13)
            pp = (t - self.peek_start) / 1.4
            peek = math.sin(pp * math.pi) if 0 <= pp < 1 else 0
            tgt.update(EXPR["neutral"])
            tgt["hs"] = max(.03, .05 + .012 * math.sin(t * 1.7) + .3 * peek)
        if self.drowse_start:
            tgt.update(EXPR["neutral"])
            tgt.update(hs=max(.1, d_base - .3 * dip), tc=.34 * (1 - d_base) + .1 * dip, bc=.03)
        if self.activity == "think":
            tgt.update(EXPR["neutral"])
            tgt.update(hs=.92, tc=.08, slant=0)
        if t < self.frantic:
            tgt.update(EXPR["neutral"])
            tgt.update(hs=1.18, ws=.92)
        rate = 20 if t < self.frantic else (2.2 if t < self.slow_until else (4 if self.drowse_start else 12))
        k = 1 - math.exp(-dt * rate)
        for key in self.cur:
            self.cur[key] += (tgt.get(key, 0) - self.cur[key]) * k
        lt = LEVELS[self.level]
        for key in self.col:
            for c in range(3):
                self.col[key][c] += (lt[key][c] - self.col[key][c]) * k
        self.cx.t = 134 if self.side else 80
        self.sc.t = .46 if self.side else 1
        self.cy.t = (86 + math.sin(t * 1.7) * .6) if self.activity == "sleep" else \
            (54 + ((1 - d_base) * 4 + dip * 3 if self.drowse_start else 0))
        if t < self.frantic:
            if t > self.next_dart:
                self.lx.t = random.uniform(-1.6, 1.6)
                self.ly.t = random.uniform(-1, .8)
                self.next_dart = t + .13
        elif self.activity == "sleep":
            self.lx.t = 0
            self.ly.t = 0
        elif self.drowse_start:
            self.lx.t = 0
            self.ly.t = .7
        elif self.activity == "think":
            self.lx.t = math.sin(t * 1.5) * .9 - .2
            self.ly.t = -.9
        elif t > self.next_glance:
            worried = self.expr == "sad"
            nosy = self.expr == "curious"
            if self.side:
                self.lx.t = -1.4 if random.random() < .6 else .4
                self.ly.t = random.uniform(-.3, .3)
            elif nosy:
                self.lx.t = random.choice((-1, 1)) * random.uniform(.6, 1.3)
                self.ly.t = random.uniform(-.9, -.4)
            else:
                self.lx.t = random.uniform(-1, 1) * (1.4 if worried else 1)
                self.ly.t = random.uniform(-.7, .7) * (.6 if worried else 1)
            self.next_glance = t + (random.uniform(.5, 1.2) if worried else random.uniform(1.2, 2.4) if nosy
                                    else random.uniform(1.6, 3.6))
        if self.activity != "sleep" and not self.drowse_start and t > self.next_blink:
            self.blink_start = t
            self.next_blink = t + random.uniform(2.5, 6)
        bp = (t - self.blink_start) / .16
        self.blink = 0.0 if self.activity == "sleep" else (math.sin(bp * math.pi) if 0 <= bp < 1 else 0.0)
        self.cx.step(dt, 120, 14)
        self.cy.step(dt, 120, 16)
        self.sc.step(dt, 120, 14)
        self.lx.step(dt, 90, 16)
        self.ly.step(dt, 90, 16)
        self.reveal_t = self.reveal_t + dt if self.card else 0.0
        self.panel_a += ((1 if self.side else 0) - self.panel_a) * min(1, dt * 8)

    def render(self, t):
        with self.lock:
            return self._render(t)

    def _draw_eyes(self, p, t):
        s = self.sc.x
        cur = self.cur
        w = 34 * s * cur["ws"]
        base = 44 * s * cur["hs"]
        gap = 13 * s
        pulse = 1 + .07 * math.sin(t * 11) if self.activity == "speak" else 1
        for i in range(2):
            inner = 1 if i == 0 else -1
            eh = max(1.5, base * (cur["hl"] if i == 0 else cur["hr"]) * pulse * (1 - self.blink * .94))
            ex = self.cx.x + (-1 if i == 0 else 1) * (w / 2 + gap / 2) + self.lx.x * 4.5 * s
            if self.expr == "sad":
                ex += math.sin(t * 38) * .4 * s
            if t < self.frantic:
                ex += math.sin(t * 55) * .35 * s
            ey = self.cy.x + self.ly.x * 4 * s
            p.save()
            p.translate(ex, ey)
            r = max(.5, min(11 * s, eh / 2, w / 2))
            path = QPainterPath()
            path.addRoundedRect(QRectF(-w / 2, -eh / 2, w, eh), r, r)
            g = QLinearGradient(0, -eh / 2, 0, eh / 2)
            g.setColorAt(0, qcol(self.col["top"]))
            g.setColorAt(.5, qcol(self.col["mid"]))
            g.setColorAt(1, qcol(self.col["bot"]))
            p.fillPath(path, g)
            if eh > 6:
                hl = QPainterPath()
                hl.addRoundedRect(QRectF(-w / 2 + w * .14, -eh / 2 + eh * .1, w * .26, eh * .16),
                                  max(.5, min(3 * s, eh * .08)), max(.5, min(3 * s, eh * .08)))
                p.setOpacity(.45)
                p.fillPath(hl, QColor(232, 248, 255))
                p.setOpacity(1)
            p.setCompositionMode(QPainter.CompositionMode_DestinationOut)
            x0 = -w / 2 - 2
            x1 = w / 2 + 2
            tcov = (cur["tc"] + (cur["tl"] if i == 0 else cur["tr"])) * eh
            sl = cur["slant"] * eh * .4 * inner

            def yt(x):
                return -eh / 2 + tcov + sl * (x / (w / 2))

            top = QPainterPath()
            top.moveTo(x0, -eh / 2 - 3)
            top.lineTo(x1, -eh / 2 - 3)
            top.lineTo(x1, yt(x1))
            top.lineTo(x0, yt(x0))
            top.closeSubpath()
            p.fillPath(top, QColor(0, 0, 0))
            if cur["bc"] > .001 or cur["bcv"] > .001:
                bot = QPainterPath()
                bot.moveTo(x0, eh / 2 + 3)
                for k in range(17):
                    x = x0 + (x1 - x0) * k / 16
                    nx = x / (w / 2)
                    y = eh / 2 - (cur["bc"] + cur["bcv"] * max(0, 1 - nx * nx)) * eh
                    bot.lineTo(x, y)
                bot.lineTo(x1, eh / 2 + 3)
                bot.closeSubpath()
                p.fillPath(bot, QColor(0, 0, 0))
            p.restore()

    def _draw_card(self, p):
        if not self.card or self.panel_a < .01:
            return
        lines = self.card["lines"]
        n = len(lines)
        gap_y = 18
        y0 = self.cy.x - (n - 1) * gap_y / 2
        bar = QColor(*[int(c) for c in self.col["bar"]])
        p.setOpacity(self.panel_a * .7)
        p.fillRect(QRectF(3.5, y0 - 7, 1, (n - 1) * gap_y + 14), bar)
        for i, (label, value) in enumerate(lines):
            pr = ease(clamp((self.reveal_t - .25 - i * .13) / .4))
            a = pr * self.panel_a
            if a <= 0:
                continue
            x = 8 + (1 - pr) * -13
            y = y0 + i * gap_y
            fs = 9
            text = label + ": "
            while True:
                self.font.setPixelSize(fs)
                fm = QFontMetricsF(self.font)
                if fm.horizontalAdvance(text + value) <= 100 or fs <= 6:
                    break
                fs -= 1
            p.setFont(self.font)
            p.setOpacity(a)
            p.setPen(qcol(self.col["label"]))
            p.drawText(QPointF(x, y + fs * .35), text)
            lw = fm.horizontalAdvance(text)
            p.setPen(QColor(230, 244, 255))
            p.drawText(QPointF(x + lw, y + fs * .35), value)
        p.setOpacity(1)

    def _draw_footer(self, p):
        if not self.footer:
            return
        self.font.setPixelSize(8)
        p.setFont(self.font)
        c = LEVELS.get(self.footer_level, LEVELS["info"])["mid"]
        p.setPen(qcol(c))
        fm = QFontMetricsF(self.font)
        text = self.footer
        while fm.horizontalAdvance(text) > W - 12 and len(text) > 4:
            text = text[:-2]
        p.drawText(QPointF(6, H - 6), text)

    def _draw_z(self, p, t):
        if self.activity != "sleep":
            return
        self.font.setPixelSize(8)
        p.setFont(self.font)
        p.setPen(qcol(self.col["mid"]))
        for k in range(2):
            pp = (t * .35 + k * .5) % 1
            p.setOpacity(math.sin(pp * math.pi) * .7)
            p.drawText(QPointF(self.cx.x + 17 + pp * 11 + k * 4, self.cy.x - 13 - pp * 19), "z")
        p.setOpacity(1)

    def _render(self, t):
        self.layer.fill(Qt.transparent)
        lp = QPainter(self.layer)
        lp.setRenderHint(QPainter.Antialiasing)
        lp.scale(SS, SS)
        self._draw_eyes(lp, t)
        lp.end()
        glow = self.layer.scaled(W // 2, H // 2, Qt.IgnoreAspectRatio, Qt.SmoothTransformation).scaled(
            W * SS, H * SS, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        self.out.fill(QColor(0, 0, 0))
        op = QPainter(self.out)
        op.setRenderHint(QPainter.Antialiasing)
        op.setRenderHint(QPainter.TextAntialiasing)
        op.setOpacity(.55)
        op.drawImage(0, 0, glow)
        op.setOpacity(1)
        op.drawImage(0, 0, self.layer)
        op.scale(SS, SS)
        self._draw_card(op)
        self._draw_z(op, t)
        self._draw_footer(op)
        op.end()
        return self.out.scaled(W, H, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)


def image_to_565(img):
    img = img.convertToFormat(QImage.Format_RGB888)
    w, h = img.width(), img.height()
    ptr = img.constBits()
    arr = np.frombuffer(ptr, dtype=np.uint8, count=img.bytesPerLine() * h).reshape(h, img.bytesPerLine())[:, :w * 3]
    arr = arr.reshape(h, w, 3).astype(np.uint16)
    return (((arr[..., 0] & 0xF8) << 8) | ((arr[..., 1] & 0xFC) << 3) | (arr[..., 2] >> 3)).astype("<u2")


class Framer:
    SWEEP = 3
    REFRESH = 3

    def __init__(self):
        self.prev = None
        self.stale = []
        self.calls = 0

    def reset(self):
        self.prev = None
        self.stale = []
        self.calls = 0

    def encode(self, px):
        n = R.strip_count(px.shape[1])
        if self.prev is None or self.prev.shape != px.shape:
            self.prev = np.zeros_like(px)
            self.stale = list(range(n))
        self.calls += 1
        if self.calls % self.REFRESH == 0:
            rr = (self.calls // self.REFRESH) % n
            if rr not in self.stale:
                self.stale.append(rr)
        now = self.stale[:self.SWEEP]
        later = set(self.stale[self.SWEEP:])
        self.stale = self.stale[self.SWEEP:]
        rects, images = R.encode(self.prev, px, force=set(now), skip=later)
        prev = self.prev.copy()
        for si in range(n):
            if si not in later:
                prev[:, si * R.STRIP:(si + 1) * R.STRIP] = px[:, si * R.STRIP:(si + 1) * R.STRIP]
        self.prev = prev
        if not rects and not images:
            return b""
        out = bytearray()
        if rects:
            out += P.rects(rects)
        for x, y, w, h, data in images:
            out += P.image(x, y, w, h, data)
        out += P.present()
        return bytes(out)


class Screen:
    def __init__(self, config, on_status=None):
        self.config = config
        self.eyes = Eyes()
        self.on_status = on_status or (lambda s: None)
        self.link = None
        self.framer = Framer()
        self.thread = None
        self.stop_flag = False
        self.connected = False
        self.preview = None

    def start(self):
        if self.thread is not None:
            return
        self.stop_flag = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_flag = True
        if self.thread:
            self.thread.join(2)
        self.thread = None
        self._close()

    def _close(self):
        if self.link is not None:
            try:
                self.link.close()
            except Exception:
                pass
        self.link = None
        self.connected = False

    def find_port(self):
        wanted = self.config.get("serial_port")
        if wanted and wanted != "auto":
            return wanted
        ports = list_ports()
        for dev, desc in ports:
            if "ESP32-S3" in desc:
                return dev
        for dev, desc in ports:
            if "USB" in desc.upper() or "SERIAL" in desc.upper():
                return dev
        return None

    def _open(self):
        port = self.find_port()
        if not port:
            return False
        link = SerialLink(port)
        if not link.open():
            return False
        self.link = link
        self.framer.reset()
        self.connected = True
        self.on_status("Small screen connected on %s" % port)
        return True

    def _run(self):
        last = time.monotonic()
        next_try = 0.0
        while not self.stop_flag:
            now = time.monotonic()
            if self.link is not None and not self.link.is_open:
                self._close()
                self.on_status("Small screen disconnected")
            if self.link is None and self.config.get("screen_enabled") and now >= next_try:
                if not self._open():
                    next_try = now + 3
            dt = min(.05, now - last)
            last = now
            self.eyes.step(now, dt)
            if self.link is not None:
                img = self.eyes.render(now)
                data = self.framer.encode(image_to_565(img))
                if data:
                    self.link.send_latest(data)
                time.sleep(max(0.0, 1 / 20 - (time.monotonic() - now)))
            else:
                time.sleep(.2)

    def set_expression(self, name):
        self.eyes.set_expression(name)

    def set_level(self, level):
        self.eyes.set_level(level)

    def set_activity(self, a):
        self.eyes.set_activity(a)

    def footer(self, text, level="info"):
        self.eyes.set_footer(text, level)

    def card(self, lines, level, expr):
        self.eyes.open_card(lines, level, expr)

    def close_card(self):
        self.eyes.close_card()
