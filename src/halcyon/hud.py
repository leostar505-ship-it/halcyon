import math
import threading
import time

import psutil
from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QFontMetricsF, QGuiApplication, QLinearGradient, QPainter, QPainterPath,
                           QPen, QPolygonF, QRadialGradient)
from PySide6.QtWidgets import QWidget

W = 1920
H = 1080
NAME = "HALCYON"
LEVELS = {"info": (90, 220, 255), "warn": (255, 176, 64), "alert": (255, 72, 72)}
STATE_LABEL = {"idle": "STANDBY", "listening": "LISTENING", "thinking": "PROCESSING", "speaking": "RESPONDING"}
DISPLAY = "Bahnschrift"
MONO = "Consolas"


def ease(p):
    p = min(1.0, max(0.0, p))
    return 1 - (1 - p) ** 3


class HudBridge(QObject):
    call = Signal(str, object)

    def __init__(self):
        super().__init__()
        self._id = 0
        self._lock = threading.Lock()

    def send(self, name, *args):
        self.call.emit(name, args)

    def job_add(self, text, state="run"):
        with self._lock:
            self._id += 1
            jid = self._id
        self.send("job_add", jid, text, state)
        return jid

    def job_set(self, jid, text=None, state=None):
        self.send("job_set", jid, text, state)

    def __getattr__(self, name):
        if name.startswith("_") or name in ("call",):
            raise AttributeError(name)

        def fn(*args):
            self.send(name, *args)
        return fn


class Hud(QWidget):
    def __init__(self, monitor=0):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.WindowTransparentForInput |
                            Qt.NoDropShadowWindowHint | Qt.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        screens = QGuiApplication.screens()
        self.monitor = min(max(0, int(monitor)), max(0, len(screens) - 1))
        self.bridge = HudBridge()
        self.bridge.call.connect(self.dispatch)
        self.p = 0.0
        self.target = 0.0
        self.mini = 0.0
        self.mini_t = 0.0
        self.col = list(LEVELS["info"])
        self.col_t = list(LEVELS["info"])
        self.sev = 0.0
        self.sev_t = 0.0
        self.state = "idle"
        self.transcript = ""
        self.said = ""
        self.shown = ""
        self.type_t = 0.0
        self.amp = 0.0
        self.panel = None
        self.panel_p = 0.0
        self.jobs = []
        self.jobs_active = False
        self.job_a = 0.0
        self.open_t = 0.0
        self.last = time.monotonic()
        self.sys_cache = None
        self.sys_at = 0.0
        self.close_at = 0.0
        self.timer = QTimer(self)
        self.timer.setInterval(16)
        self.timer.timeout.connect(self.tick)
        self._place()

    def _place(self):
        screens = QGuiApplication.screens()
        if screens:
            self.setGeometry(screens[self.monitor].geometry())

    def dispatch(self, name, args):
        fn = getattr(self, "do_" + name, None)
        if fn:
            fn(*args)

    def do_begin(self, mini):
        self._place()
        self.mini_t = 1.0 if mini else 0.0
        if self.p == 0:
            self.mini = self.mini_t
            self.open_t = time.monotonic()
        self.target = 1.0
        self.close_at = 0.0
        self.transcript = ""
        self.said = ""
        self.shown = ""
        self.panel = None
        self.do_level("info")
        if not self.isVisible():
            self.show()
        if not self.timer.isActive():
            self.timer.start()

    def do_mini(self, on):
        self.mini_t = 1.0 if on else 0.0

    def do_state(self, s):
        self.state = s

    def do_transcript(self, t):
        self.transcript = t

    def do_said(self, t):
        self.said = t
        self.type_t = time.monotonic()

    def do_level(self, level):
        self.col_t = list(LEVELS.get(level, LEVELS["info"]))
        self.sev_t = 1.0 if level == "alert" else 0.0

    def do_panel(self, name):
        self.panel = name

    def do_amp(self, v):
        self.amp = max(self.amp, float(v))

    def do_job_start(self):
        self.jobs = []
        self.jobs_active = True

    def do_job_add(self, jid, text, state):
        self.jobs.append({"id": jid, "txt": text, "state": state})

    def do_job_set(self, jid, text, state):
        for it in self.jobs:
            if it["id"] == jid:
                if text is not None:
                    it["txt"] = text
                if state is not None:
                    it["state"] = state

    def do_job_end(self):
        self.jobs_active = False

    def do_close_in(self, seconds):
        self.close_at = time.monotonic() + float(seconds)

    def do_close(self):
        self.target = 0.0
        self.jobs_active = False
        self.panel = None
        self.state = "idle"
        self.do_level("info")

    def tick(self):
        now = time.monotonic()
        dt = min(.05, now - self.last)
        self.last = now
        if self.close_at and now >= self.close_at:
            self.close_at = 0.0
            self.do_close()
        if self.target > self.p:
            self.p = min(self.target, self.p + dt / 1.1)
        elif self.target < self.p:
            self.p = max(self.target, self.p - dt / .6)
        ck = min(1.0, dt * 4)
        for i in range(3):
            self.col[i] += (self.col_t[i] - self.col[i]) * ck
        self.sev += (self.sev_t - self.sev) * ck
        self.mini += (self.mini_t - self.mini) * min(1.0, dt * 6)
        self.amp *= .9
        self.panel_p += ((1 if self.panel else 0) - self.panel_p) * min(1.0, dt * 6)
        if not self.panel and self.panel_p < .01:
            self.panel_p = 0.0
        self.job_a += ((1 if self.jobs_active else 0) - self.job_a) * min(1.0, dt * 5)
        if self.state == "speaking":
            n = int((now - self.type_t) * 24)
            self.shown = self.said[:max(0, min(len(self.said), n))]
        if self.p <= 0 and self.target <= 0 and self.job_a < .01:
            self.timer.stop()
            self.hide()
            return
        self.update()

    def rgba(self, a=1.0):
        pulse = 1 - self.sev * .3 * (.5 + .5 * math.sin(time.monotonic() * 6))
        return QColor(int(self.col[0]), int(self.col[1]), int(self.col[2]), int(255 * max(0, min(1, a * pulse))))

    def font(self, family, px, weight=QFont.Medium, spacing=0.0):
        f = QFont(family)
        f.setPixelSize(int(px))
        f.setWeight(weight)
        f.setLetterSpacing(QFont.AbsoluteSpacing, spacing)
        return f

    def glass(self, p, x, y, w, h, cut=26, tint=.1, sw=3):
        path = QPainterPath()
        path.moveTo(x, y)
        path.lineTo(x + w - cut, y)
        path.lineTo(x + w, y + cut)
        path.lineTo(x + w, y + h)
        path.lineTo(x, y + h)
        path.closeSubpath()
        p.fillPath(path, self.rgba(tint))
        p.setPen(QPen(self.rgba(1), sw))
        p.drawPath(path)

    def header(self, p, x, y, w, h, text, cut=26):
        path = QPainterPath()
        path.moveTo(x, y)
        path.lineTo(x + w - cut, y)
        path.lineTo(x + w, y + cut)
        path.lineTo(x + w, y + h)
        path.lineTo(x, y + h)
        path.closeSubpath()
        g = QLinearGradient(x, 0, x + w, 0)
        g.setColorAt(0, self.rgba(.5))
        g.setColorAt(1, self.rgba(.12))
        p.fillPath(path, g)
        p.setPen(QPen(self.rgba(1), 3))
        p.drawPath(path)
        p.setPen(QColor(235, 252, 255))
        p.setFont(self.font(DISPLAY, 19, QFont.DemiBold, 3))
        p.drawText(QPointF(x + 22, y + h / 2 + 7), text)

    def sysinfo(self):
        now = time.monotonic()
        if self.sys_cache is None or now - self.sys_at > 1.0:
            procs = []
            for pr in psutil.process_iter(["name", "memory_info"]):
                try:
                    procs.append((pr.info["name"] or "", pr.info["memory_info"].rss))
                except Exception:
                    pass
            merged = {}
            for n, m in procs:
                n = n[:-4] if n.lower().endswith(".exe") else n
                merged[n] = merged.get(n, 0) + m
            top = sorted(merged.items(), key=lambda kv: -kv[1])[:5]
            try:
                disk = psutil.disk_usage("C:\\" if psutil.WINDOWS else "/").percent
            except Exception:
                disk = 0
            self.sys_cache = {"cpu": psutil.cpu_percent(), "ram": psutil.virtual_memory().percent, "disk": disk,
                              "top": top}
            self.sys_at = now
        return self.sys_cache

    def paintEvent(self, event):
        pv = ease(self.p)
        if pv <= 0 and self.job_a < .01:
            return
        t = time.monotonic()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        p.scale(self.width() / W, self.height() / H)
        full = pv * (1 - self.mini)
        since = t - self.open_t
        if full > .01:
            self.paint_full(p, t, full, since)
        self.paint_jobs(p, t, pv)
        if pv > .5:
            self.paint_voice(p, t, min(1.0, (pv - .5) * 2))
        p.end()

    def paint_full(self, p, t, a, since):
        vg = QRadialGradient(W / 2, H / 2, 1180)
        vg.setColorAt(.4, QColor(0, 0, 0, 0))
        c = self.rgba(.2 * a)
        vg.setColorAt(1, c)
        p.fillRect(QRectF(0, 0, W, H), vg)
        m = 14
        cut = 54
        frame = QPainterPath()
        frame.moveTo(m + cut, m)
        frame.lineTo(W - m - cut, m)
        frame.lineTo(W - m, m + cut)
        frame.lineTo(W - m, H - m - cut)
        frame.lineTo(W - m - cut, H - m)
        frame.lineTo(m + cut, H - m)
        frame.lineTo(m, H - m - cut)
        frame.lineTo(m, m + cut)
        frame.closeSubpath()
        p.setOpacity(a)
        p.setPen(QPen(self.rgba(1), 4))
        p.drawPath(frame)
        p.setPen(QPen(self.rgba(.35), 2))
        inner = QPainterPath()
        inner.addRoundedRect(QRectF(m + 14, m + 14, W - 2 * m - 28, H - 2 * m - 28), 10, 10)
        p.drawPath(inner)
        p.setPen(QPen(self.rgba(.5), 2))
        for i in range(0, W - 2 * m - 2 * cut, 24):
            x = m + cut + i
            ln = 14 if (i // 24) % 5 == 0 else 7
            p.drawLine(QPointF(x, m + 22), QPointF(x, m + 22 + ln))
            p.drawLine(QPointF(x, H - m - 22), QPointF(x, H - m - 22 - ln))
        for i in range(0, H - 2 * m - 2 * cut, 24):
            y = m + cut + i
            ln = 14 if (i // 24) % 5 == 0 else 7
            p.drawLine(QPointF(m + 22, y), QPointF(m + 22 + ln, y))
            p.drawLine(QPointF(W - m - 22, y), QPointF(W - m - 22 - ln, y))
        p.setPen(QPen(self.rgba(1), 7))
        L = 120
        for sx, sy in ((m, m), (W - m, m), (m, H - m), (W - m, H - m)):
            dx = 1 if sx < W / 2 else -1
            dy = 1 if sy < H / 2 else -1
            p.drawLine(QPointF(sx + dx * cut * .35, sy + dy * (cut + 6)), QPointF(sx + dx * cut * .35, sy + dy * (cut + L)))
            p.drawLine(QPointF(sx + dx * (cut + 6), sy + dy * cut * .35), QPointF(sx + dx * (cut + L), sy + dy * cut * .35))
        plate = QPolygonF([QPointF(m + 520, m), QPointF(W - m - 520, m), QPointF(W - m - 590, m + 78),
                           QPointF(m + 590, m + 78)])
        pg = QPainterPath()
        pg.addPolygon(plate)
        pg.closeSubpath()
        p.fillPath(pg, self.rgba(.2))
        p.setPen(QPen(self.rgba(1), 3))
        p.drawPath(pg)
        p.setPen(QColor(242, 252, 255))
        p.setFont(self.font(DISPLAY, 30, QFont.DemiBold, 10))
        p.drawText(QRectF(0, m + 8, W, 40), Qt.AlignHCenter | Qt.AlignVCenter, NAME)
        p.setPen(self.rgba(1))
        p.setFont(self.font(DISPLAY, 13, QFont.DemiBold, 6))
        p.drawText(QRectF(0, m + 48, W, 20), Qt.AlignHCenter | Qt.AlignVCenter, STATE_LABEL.get(self.state, "STANDBY"))
        if since < .9:
            y = m + (H - 2 * m) * (since / .9)
            sg = QLinearGradient(0, y - 90, 0, y)
            sg.setColorAt(0, self.rgba(0))
            sg.setColorAt(1, self.rgba(.35))
            p.fillRect(QRectF(m, y - 90, W - 2 * m, 90), sg)
            p.setPen(QPen(self.rgba(.9), 3))
            p.drawLine(QPointF(m, y), QPointF(W - m, y))
        p.setOpacity(1)
        if self.panel_p > .01 and self.panel == "system":
            self.paint_system(p, t, ease(self.panel_p) * a)
        p.setOpacity(1)

    def paint_system(self, p, t, pa):
        info = self.sysinfo()
        x = 120 - (1 - pa) * 90
        y = 250
        w = 420
        rows = info["top"]
        h = 46 + 4 * 46 + 70 + len(rows) * 38
        p.setOpacity(pa)
        self.glass(p, x, y, w, h)
        self.header(p, x, y, w, 46, "TASK MANAGER")
        meters = [("CPU", info["cpu"]), ("RAM", info["ram"]), ("DISK", info["disk"])]
        for k, (label, val) in enumerate(meters):
            ry = y + 46 + 36 + k * 46
            p.setPen(self.rgba(.9))
            p.setFont(self.font(MONO, 16))
            p.drawText(QPointF(x + 28, ry), label)
            p.drawText(QRectF(x + w - 100, ry - 16, 72, 22), Qt.AlignRight, "%d%%" % val)
            p.fillRect(QRectF(x + 28, ry + 10, w - 56, 8), self.rgba(.18))
            p.fillRect(QRectF(x + 28, ry + 10, (w - 56) * min(1, val / 100), 8), self.rgba(1))
        ry0 = y + 46 + 36 + 3 * 46 + 18
        p.setPen(self.rgba(.6))
        p.setFont(self.font(MONO, 14, QFont.Medium, 4))
        p.drawText(QPointF(x + 28, ry0), "RUNNING")
        top_max = max([m for _, m in rows] + [1])
        for k, (name, mem) in enumerate(rows):
            ry = ry0 + 30 + k * 38
            p.setPen(self.rgba(.95))
            p.setFont(self.font(MONO, 15))
            p.drawText(QRectF(x + 28, ry - 16, 170, 22), Qt.AlignLeft, name[:16])
            p.fillRect(QRectF(x + 205, ry - 8, 110, 6), self.rgba(.18))
            p.fillRect(QRectF(x + 205, ry - 8, 110 * mem / top_max, 6), self.rgba(.9))
            p.drawText(QRectF(x + w - 100, ry - 16, 72, 22), Qt.AlignRight, "%d MB" % (mem // (1024 * 1024)))

    def paint_jobs(self, p, t, pv):
        if self.job_a < .01 or not self.jobs:
            return
        ja = ease(min(1.0, self.job_a))
        items = self.jobs[-7:]

        def row_h(it):
            return 70 if it["state"] == "ask" else 42
        jh = 44 + 24 + sum(row_h(i) for i in items)
        jx = W - 400 + (1 - ja) * 90
        jy = 120
        p.setOpacity(ja * max(pv, .0))
        self.glass(p, jx, jy, 360, jh)
        self.header(p, jx, jy, 360, 44, "ACTIONS")
        ry = jy + 44 + 18
        for it in items:
            rh = row_h(it)
            gx = jx + 38
            gy = ry + 16
            ask = it["state"] == "ask"
            fail = it["state"] == "fail"
            pulse = .65 + .35 * math.sin(t * 6)
            if ask:
                colr = QColor(255, 176, 64, int(255 * pulse))
            elif fail:
                colr = QColor(255, 72, 72, 242)
            else:
                colr = self.rgba(1)
            pen = QPen(colr, 3)
            pen.setCapStyle(Qt.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            if it["state"] == "done":
                path = QPainterPath()
                path.moveTo(gx - 8, gy)
                path.lineTo(gx - 2, gy + 6)
                path.lineTo(gx + 8, gy - 6)
                p.drawPath(path)
            elif it["state"] == "run":
                start = int(-(t * 6) * 180 / math.pi * 16)
                p.drawArc(QRectF(gx - 9, gy - 9, 18, 18), start, int(1.3 * 180 * 16))
            elif fail:
                p.drawLine(QPointF(gx - 7, gy - 7), QPointF(gx + 7, gy + 7))
                p.drawLine(QPointF(gx + 7, gy - 7), QPointF(gx - 7, gy + 7))
            else:
                p.drawEllipse(QPointF(gx, gy), 11, 11)
                p.setFont(self.font(DISPLAY, 15, QFont.Bold))
                p.drawText(QRectF(gx - 11, gy - 11, 22, 22), Qt.AlignCenter, "?")
            font = self.font(DISPLAY, 17)
            p.setFont(font)
            fm = QFontMetricsF(font)
            txt = it["txt"]
            while len(txt) > 4 and fm.horizontalAdvance(txt) > 262:
                txt = txt[:-2]
            if txt != it["txt"]:
                txt += "..."
            p.setPen(QColor(255, 214, 150) if ask else QColor(255, 150, 150) if fail else self.rgba(1))
            p.drawText(QPointF(jx + 66, gy + 6), txt)
            if ask:
                p.setPen(QColor(255, 176, 64, 217))
                p.setFont(self.font(DISPLAY, 12, QFont.DemiBold, 2))
                p.drawText(QPointF(jx + 66, gy + 30), "SAY YES OR NO")
            ry += rh
        p.setOpacity(1)

    def paint_voice(self, p, t, gp):
        p.setOpacity(gp)
        self.glass(p, 520, 956, 880, 76, 20, .08, 2.5)
        bars = 70
        bw = 8
        gap = 4
        bx0 = 960 - (bars * (bw + gap)) / 2 + gap / 2
        by0 = 994
        step = int(t * 14)
        p.setPen(Qt.NoPen)
        p.setBrush(self.rgba(1))
        for i in range(bars):
            n = abs(math.sin(i * 12.9898 + step * 78.233))
            env = .35 + .65 * math.cos(((i - bars / 2) / (bars / 2)) * math.pi / 2)
            hh = 4.0
            if self.state == "listening":
                hh = 5 + 44 * max(n * env, self.amp * env) * (.65 + .35 * math.sin(t * 2.4))
            elif self.state == "speaking":
                hh = 5 + (10 + 40 * self.amp) * (.35 + .65 * n) * env
            elif self.state == "thinking":
                pos = (math.sin(t * 3) * .5 + .5) * bars
                hh = 4 + 36 * math.exp(-((i - pos) ** 2) / 14)
            p.drawRect(QRectF(bx0 + i * (bw + gap), by0 - hh / 2, bw, hh))
        p.setBrush(Qt.NoBrush)
        if self.shown:
            font = self.font(DISPLAY, 32, QFont.Medium)
            p.setFont(font)
            fm = QFontMetricsF(font)
            text = self.shown
            tw = fm.horizontalAdvance(text) + 56
            box = QRectF(960 - max(tw, 240) / 2, 868, max(tw, 240), 64)
            path = QPainterPath()
            path.addRoundedRect(box, 6, 6)
            p.fillPath(path, QColor(0, 20, 30, 150))
            p.setPen(QPen(self.rgba(1), 2))
            p.drawPath(path)
            p.setPen(QColor(235, 250, 255))
            p.drawText(box, Qt.AlignCenter, text)
        if self.transcript:
            p.setPen(self.rgba(.9))
            p.setFont(self.font(MONO, 16, QFont.Medium, 2))
            p.drawText(QRectF(0, 840, W, 24), Qt.AlignHCenter | Qt.AlignVCenter, self.transcript)
        p.setOpacity(1)
