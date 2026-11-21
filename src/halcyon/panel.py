import math
import os
import json
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request

from PySide6.QtCore import QObject, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QLinearGradient, QPainter, QPen, QRadialGradient
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QSpinBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QPlainTextEdit, QPushButton, QStackedWidget, QVBoxLayout, QWidget)

from . import EXPANSION, NAME, __version__
from .brain import BrainError, ask_model, find_claude
from .vendor import whispercpp as W
from .voice_in import match_wake

STYLE = """
QWidget { background: #070d18; color: #cfe8ff; font-family: 'Segoe UI'; font-size: 13px; }
QLabel { background: transparent; }
QFrame#side { background: #0a1424; border-right: 1px solid #14304f; }
QFrame#card { background: #0c1830; border: 1px solid #17406b; border-radius: 10px; }
QPushButton { background: #11213b; border: 1px solid #1f6fb5; padding: 8px 14px; border-radius: 6px; }
QPushButton:hover { background: #18305a; }
QPushButton:disabled { color: #5f7690; border-color: #1a3350; }
QPushButton#nav { text-align: left; border: none; border-radius: 0; padding: 14px 20px; background: transparent; font-size: 14px; }
QPushButton#nav:hover { background: #0f2040; }
QPushButton#nav:checked { background: #11284a; border-left: 3px solid #4cc3ff; color: #ffffff; }
QLabel#title { font-size: 22px; font-weight: 600; color: #ffffff; }
QLabel#sub { color: #6f93b8; }
QLabel#h { font-size: 15px; color: #4cc3ff; font-weight: 600; }
QLabel#ok { color: #4ade80; }
QLabel#bad { color: #ff6b6b; }
QLabel#warn { color: #ffb84d; }
QLineEdit, QComboBox, QListWidget, QPlainTextEdit { background: #091324; border: 1px solid #17406b; border-radius: 6px; padding: 6px; }
QPlainTextEdit { font-family: Consolas; }
QListWidget::item:selected { background: #18305a; }
"""

STATES = {
    "idle": ((63, 168, 255), "Idle", .35),
    "listening": ((0, 255, 235), "Listening", 1.0),
    "thinking": ((255, 184, 77), "Thinking", 1.6),
    "speaking": ((96, 190, 255), "Speaking", .9),
    "alert": ((255, 74, 74), "Alert", 1.2),
}


class Bus(QObject):
    log = Signal(str)
    claude = Signal(str, str)
    voice = Signal(str, str)
    chat = Signal(str, str)
    state = Signal(str)
    amp = Signal(float)
    lights = Signal(list)
    cbdone = Signal(bool)
    gpudone = Signal(bool)


class Core(QWidget):
    def __init__(self):
        super().__init__()
        self.setMinimumSize(380, 380)
        self.state = "idle"
        self.col = [63.0, 168.0, 255.0]
        self.speed = .35
        self.amp = 0.0
        self.target_amp = 0.0
        self.t0 = time.monotonic()
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(16)

    def set_state(self, name):
        self.state = name

    def set_amp(self, v):
        self.target_amp = max(0.0, min(1.0, v))

    def tick(self):
        rgb, _, spd = STATES[self.state]
        for i in range(3):
            self.col[i] += (rgb[i] - self.col[i]) * .12
        self.speed += (spd - self.speed) * .08
        base = {"listening": .25, "speaking": .35, "thinking": .12}.get(self.state, .04)
        goal = max(self.target_amp, base)
        self.amp += (goal - self.amp) * .2
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        c = QPointF(w / 2, (h - 40) / 2)
        r = min(w, h - 40) / 2 - 14
        t = time.monotonic() - self.t0
        col = QColor(int(self.col[0]), int(self.col[1]), int(self.col[2]))
        bg = QRadialGradient(c, r * 1.25)
        glow = QColor(col)
        glow.setAlpha(int(40 + 60 * self.amp))
        bg.setColorAt(0, glow)
        bg.setColorAt(1, QColor(7, 13, 24, 0))
        p.fillRect(self.rect(), QColor(7, 13, 24))
        p.setBrush(QBrush(bg))
        p.setPen(Qt.NoPen)
        p.drawEllipse(c, r * 1.25, r * 1.25)
        for i in range(72):
            a = math.radians(i * 5 - 90)
            major = i % 6 == 0
            r0 = r * (.96 if major else .975)
            r1 = r * 1.0
            pen = QPen(QColor(col.red(), col.green(), col.blue(), 200 if major else 90), 2 if major else 1)
            p.setPen(pen)
            p.drawLine(QPointF(c.x() + r0 * math.cos(a), c.y() + r0 * math.sin(a)),
                       QPointF(c.x() + r1 * math.cos(a), c.y() + r1 * math.sin(a)))
        for k, (rad, span, width, sp) in enumerate(((.88, 110, 5, 1.0), (.78, 70, 3, -1.6), (.68, 200, 2, 2.2),
                                                    (.58, 40, 6, -.7))):
            pen = QPen(QColor(col.red(), col.green(), col.blue(), 220 - k * 30), width, Qt.SolidLine, Qt.RoundCap)
            p.setPen(pen)
            p.setBrush(Qt.NoBrush)
            rect = QRectF(c.x() - r * rad, c.y() - r * rad, r * rad * 2, r * rad * 2)
            for j in range(3 if k < 2 else 2):
                start = (t * 60 * sp * self.speed * 2 + j * 120) % 360
                p.drawArc(rect, int(-start * 16), int(-span * 16))
        pts = 120
        wave = QPen(QColor(col.red(), col.green(), col.blue(), 235), 2)
        p.setPen(wave)
        p.setBrush(Qt.NoBrush)
        pr = r * .46
        last = None
        for i in range(pts + 1):
            a = i / pts * math.tau
            mod = math.sin(a * 7 + t * 5 * (1 + self.speed)) * math.sin(a * 3 - t * 2.3)
            rr = pr + mod * (4 + 26 * self.amp)
            pt = QPointF(c.x() + rr * math.cos(a), c.y() + rr * math.sin(a))
            if last is not None:
                p.drawLine(last, pt)
            last = pt
        core = QRadialGradient(c, pr * .7)
        inner = QColor(col)
        inner.setAlpha(int(120 + 120 * self.amp))
        core.setColorAt(0, QColor(255, 255, 255, int(200 + 55 * self.amp)))
        core.setColorAt(.35, inner)
        core.setColorAt(1, QColor(col.red(), col.green(), col.blue(), 0))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(core))
        p.drawEllipse(c, pr * .7, pr * .7)
        p.setPen(QColor(235, 246, 255))
        f = QFont("Segoe UI", 13)
        f.setBold(True)
        f.setLetterSpacing(QFont.AbsoluteSpacing, 3)
        p.setFont(f)
        p.drawText(QRectF(0, h - 38, w, 30), Qt.AlignHCenter, STATES[self.state][1].upper())
        p.end()


def card():
    f = QFrame()
    f.setObjectName("card")
    return f


def row_label(text, name=None):
    lab = QLabel(text)
    if name:
        lab.setObjectName(name)
    lab.setWordWrap(True)
    return lab


class Meter(QWidget):
    def __init__(self):
        super().__init__()
        self.setFixedHeight(22)
        self.v = 0.0

    def set_value(self, v):
        self.v = max(0.0, min(1.0, v))
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor(23, 64, 107)))
        p.setBrush(QColor(9, 19, 36))
        p.drawRoundedRect(QRectF(.5, .5, self.width() - 1, self.height() - 1), 5, 5)
        g = QLinearGradient(0, 0, self.width(), 0)
        g.setColorAt(0, QColor(0, 220, 255))
        g.setColorAt(.7, QColor(76, 195, 255))
        g.setColorAt(1, QColor(255, 120, 80))
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(g))
        p.drawRoundedRect(QRectF(2, 2, (self.width() - 4) * self.v, self.height() - 4), 4, 4)
        p.end()


class Panel(QWidget):
    def __init__(self, config, voice, hud, controller, events, lights=None, chatterbox=None):
        super().__init__()
        self.chatterbox = chatterbox
        self.lights = lights
        self.hud_widget = hud
        self.controller = controller
        self.events = events
        self.setWindowTitle("Halcyon")
        self.resize(1080, 700)
        self.setStyleSheet(STYLE)
        self.config = config
        self.voice = voice
        self.bus = Bus()
        self.bus.log.connect(self.add_log)
        self.bus.claude.connect(self.set_claude)
        self.bus.voice.connect(self.set_voice)
        self.bus.chat.connect(self.add_chat)
        self.bus.state.connect(self.set_state)
        self.bus.amp.connect(lambda v: self.core.set_amp(v))
        self.bus.lights.connect(self.got_lights)
        self.bus.cbdone.connect(self.cb_finished)
        self.bus.gpudone.connect(self.gpu_finished)
        self.mic_stream = None
        self.mic_level = 0.0
        root = QHBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        side = QFrame()
        side.setObjectName("side")
        side.setFixedWidth(220)
        sl = QVBoxLayout(side)
        sl.setContentsMargins(0, 0, 0, 0)
        sl.setSpacing(0)
        brand = QLabel("  %s" % NAME.upper())
        brand.setStyleSheet("font-size:20px;font-weight:700;letter-spacing:4px;color:#4cc3ff;padding:22px 0 4px 8px;")
        sl.addWidget(brand)
        ver = QLabel("   v%s" % __version__)
        ver.setObjectName("sub")
        ver.setStyleSheet("padding:0 0 18px 8px;")
        sl.addWidget(ver)
        self.stack = QStackedWidget()
        self.navs = []
        for i, name in enumerate(("Overview", "Claude", "Voice engine", "Wake words", "Lights", "Webhook")):
            b = QPushButton(name)
            b.setObjectName("nav")
            b.setCheckable(True)
            b.clicked.connect(lambda _=False, n=i: self.go(n))
            sl.addWidget(b)
            self.navs.append(b)
        sl.addStretch(1)
        tag = QLabel(EXPANSION)
        tag.setObjectName("sub")
        tag.setWordWrap(True)
        tag.setStyleSheet("padding:14px;font-size:11px;")
        sl.addWidget(tag)
        root.addWidget(side)
        root.addWidget(self.stack, 1)
        self.stack.addWidget(self.page_overview())
        self.stack.addWidget(self.page_claude())
        self.stack.addWidget(self.page_voice())
        self.stack.addWidget(self.page_wake())
        self.stack.addWidget(self.page_lights())
        self.stack.addWidget(self.page_webhook())
        self.go(0)
        QTimer(self, interval=60, timeout=self.poll_mic).start()
        self.refresh_claude()
        self.refresh_voice()
        QTimer(self, interval=120, timeout=self.follow).start()

    def follow(self):
        st = getattr(self.hud_widget, "state", "idle")
        if self.mic_stream is None and st in STATES and st != self.core.state:
            self.core.set_state(st)

    def note(self, text):
        self.add_log(text)
        self.add_chat("Halcyon", text)

    def go(self, i):
        self.stack.setCurrentIndex(i)
        for n, b in enumerate(self.navs):
            b.setChecked(n == i)

    def page(self, title, sub):
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(28, 24, 28, 24)
        lay.setSpacing(14)
        t = QLabel(title)
        t.setObjectName("title")
        s = QLabel(sub)
        s.setObjectName("sub")
        lay.addWidget(t)
        lay.addWidget(s)
        return w, lay

    def page_overview(self):
        w, lay = self.page("Overview", "Live view of what Halcyon is doing.")
        body = QHBoxLayout()
        self.core = Core()
        body.addWidget(self.core, 3)
        right = QVBoxLayout()
        self.chat = QPlainTextEdit()
        self.chat.setReadOnly(True)
        right.addWidget(self.chat, 1)
        grid = QHBoxLayout()
        for label, fn in (("Talk now", self.talk), ("Yes", lambda: self.controller.answer(True)),
                          ("No", lambda: self.controller.answer(False)), ("Cancel", self.controller.cancel)):
            b = QPushButton(label)
            b.clicked.connect(fn)
            grid.addWidget(b)
        right.addLayout(grid)
        ev = QHBoxLayout()
        for key in self.events:
            b = QPushButton(key.capitalize())
            b.clicked.connect(lambda _=False, k=key: self.fire(k))
            ev.addWidget(b)
        right.addWidget(row_label("Try an event", "h"))
        right.addLayout(ev)
        body.addLayout(right, 2)
        lay.addLayout(body, 1)
        self.add_chat("Halcyon", "Ready.")
        return w

    def talk(self):
        self.controller.on_wake()
        lst = getattr(self.controller, "listener", None)
        if lst is not None:
            lst.request_dictation()

    def fire(self, key):
        threading.Thread(target=self.controller.on_webhook, args=(dict(self.events[key], at=time.time()),),
                         daemon=True).start()

    def page_claude(self):
        w, lay = self.page("Claude", "Halcyon uses Claude Code as its brain.")
        c = card()
        cl = QVBoxLayout(c)
        cl.addWidget(row_label("Claude Code", "h"))
        self.cl_path = row_label("Looking for it...")
        self.cl_ver = row_label("")
        self.cl_test = row_label("Not tested yet.")
        cl.addWidget(self.cl_path)
        cl.addWidget(self.cl_ver)
        cl.addWidget(self.cl_test)
        row = QHBoxLayout()
        b1 = QPushButton("Check again")
        b1.clicked.connect(self.refresh_claude)
        b2 = QPushButton("Test Claude (asks the router model)")
        b2.clicked.connect(self.test_claude)
        b3 = QPushButton("Sign in (opens a terminal)")
        b3.clicked.connect(self.sign_in)
        for b in (b1, b2, b3):
            row.addWidget(b)
        cl.addLayout(row)
        lay.addWidget(c)
        c2 = card()
        l2 = QVBoxLayout(c2)
        l2.addWidget(row_label("Models", "h"))
        l2.addWidget(row_label("Router: %s     Tasks: %s     Task timeout: %ss" % (
            self.config.get("router_model"), self.config.get("task_model"), self.config.get("task_timeout"))))
        l2.addWidget(row_label("Sign in once by running `claude` in a terminal. Halcyon uses your Claude subscription, no API key.", "sub"))
        lay.addWidget(c2)
        lay.addStretch(1)
        return w

    def page_voice(self):
        w, lay = self.page("Voice engine", "Microphone, speech recognition and the voice.")
        c = card()
        cl = QVBoxLayout(c)
        cl.addWidget(row_label("Microphone", "h"))
        self.dev = QComboBox()
        self.devs = []
        try:
            import sounddevice as sd
            apis = [a["name"] for a in sd.query_hostapis()]
            pick = next((n for n in apis if "WASAPI" in n), None)
            seen = set()
            self.dev.addItem("System default", None)
            for i, d in enumerate(sd.query_devices()):
                if d.get("max_input_channels", 0) <= 0:
                    continue
                if pick and apis[d["hostapi"]] != pick:
                    continue
                name = d["name"]
                if name in seen or "Sound Mapper" in name or "Primary Sound" in name:
                    continue
                seen.add(name)
                self.devs.append(i)
                self.dev.addItem(name, i)
        except Exception as exc:
            self.dev.addItem("sounddevice unavailable: %s" % exc, None)
        cl.addWidget(self.dev)
        self.meter = Meter()
        cl.addWidget(self.meter)
        row = QHBoxLayout()
        self.mic_btn = QPushButton("Start mic test")
        self.mic_btn.clicked.connect(self.toggle_mic)
        row.addWidget(self.mic_btn)
        row.addStretch(1)
        cl.addLayout(row)
        lay.addWidget(c)
        c2 = card()
        l2 = QVBoxLayout(c2)
        l2.addWidget(row_label("Engines", "h"))
        self.v_whisper = row_label("")
        self.v_model = row_label("")
        self.v_tts = row_label("")
        for x in (self.v_whisper, self.v_model, self.v_tts):
            l2.addWidget(x)
        row = QHBoxLayout()
        b1 = QPushButton("Check engines")
        b1.clicked.connect(self.refresh_voice)
        b2 = QPushButton("Speak a test line")
        b2.clicked.connect(self.speak_test)
        row.addWidget(b1)
        row.addWidget(b2)
        row.addStretch(1)
        l2.addLayout(row)
        l2.addWidget(row_label("Chatterbox (natural voice, runs on your CPU, a few seconds per sentence)", "sub"))
        row2 = QHBoxLayout()
        self.cb_install = QPushButton("Install Chatterbox")
        self.cb_install.clicked.connect(self.install_chatterbox)
        self.cb_start = QPushButton("Start")
        self.cb_start.clicked.connect(self.start_chatterbox)
        self.cb_stop = QPushButton("Stop")
        self.cb_stop.clicked.connect(self.stop_chatterbox)
        self.cb_gpu = QPushButton("Use my AMD GPU")
        self.cb_gpu.clicked.connect(self.gpu_chatterbox)
        for b in (self.cb_install, self.cb_gpu, self.cb_start, self.cb_stop):
            row2.addWidget(b)
        row2.addStretch(1)
        l2.addLayout(row2)
        lay.addWidget(c2)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumHeight(130)
        lay.addWidget(self.log)
        lay.addStretch(1)
        return w

    def page_wake(self):
        w, lay = self.page("Wake words", "Type what the microphone might hear and see if it would wake Halcyon.")
        row = QHBoxLayout()
        left = QVBoxLayout()
        left.addWidget(row_label("Wake words (%d)" % len(self.config.get("wake_words")), "h"))
        self.filter = QLineEdit()
        self.filter.setPlaceholderText("Filter")
        self.words = QListWidget()
        for word in self.config.get("wake_words"):
            self.words.addItem(word)
        self.filter.textChanged.connect(self.filter_words)
        left.addWidget(self.filter)
        left.addWidget(self.words, 1)
        row.addLayout(left, 1)
        right = QVBoxLayout()
        right.addWidget(row_label("Try a phrase", "h"))
        self.phrase = QLineEdit()
        self.phrase.setPlaceholderText("hey hal see on what's the weather")
        self.phrase.textChanged.connect(self.try_phrase)
        right.addWidget(self.phrase)
        self.result = row_label("Type something.")
        right.addWidget(self.result)
        for sample in ("hal see on what time is it", "halcyon open my downloads", "hey hall sion cancel that",
                       "all season tyres", "hello there"):
            b = QPushButton(sample)
            b.clicked.connect(lambda _=False, s=sample: self.phrase.setText(s))
            right.addWidget(b)
        self.prefix_box = QCheckBox("Require hey, ok or yo before Halcyon")
        self.prefix_box.setChecked(bool(self.config.get("wake_prefix_required")))
        self.prefix_box.toggled.connect(self.set_prefix_required)
        right.addWidget(self.prefix_box)
        right.addWidget(row_label("Wake history (what the microphone heard)", "h"))
        self.wake_hist = QPlainTextEdit()
        self.wake_hist.setReadOnly(True)
        right.addWidget(self.wake_hist, 1)
        row.addLayout(right, 1)
        lay.addLayout(row, 1)
        QTimer(self, interval=2000, timeout=self.refresh_wake_hist).start()
        return w

    def set_prefix_required(self, on):
        self.config.set("wake_prefix_required", bool(on))
        self.try_phrase(self.phrase.text())

    def refresh_wake_hist(self):
        from .config import config_dir
        try:
            lines = (config_dir() / "wake_log.txt").read_text(encoding="utf-8", errors="replace").splitlines()[-40:]
        except OSError:
            lines = []
        text = "\n".join(reversed(lines)) or "Nothing yet. Say hey halcyon."
        if text != self.wake_hist.toPlainText():
            self.wake_hist.setPlainText(text)

    def page_lights(self):
        w, lay = self.page("Lights", "Default colour and per-device colour balance.")
        c = card()
        cl = QVBoxLayout(c)
        cl.addWidget(row_label("Default colour", "h"))
        cl.addWidget(row_label("Every light returns to this after an event. Pick a preset or choose any colour.", "sub"))
        self.sw = QPushButton()
        self.sw.setMinimumHeight(54)
        self.sw.clicked.connect(self.pick_colour)
        cl.addWidget(self.sw)
        row = QHBoxLayout()
        for label, rgb in (("Blue", (30, 80, 255)), ("Cyan", (0, 200, 255)), ("Purple", (150, 60, 255)),
                           ("Pink", (255, 60, 160)), ("Orange", (255, 110, 0)), ("Green", (40, 220, 90)),
                           ("White", (255, 240, 220))):
            b = QPushButton(label)
            b.clicked.connect(lambda _=False, v=rgb: self.set_default(v))
            row.addWidget(b)
        cl.addLayout(row)
        self.lt_note = row_label("")
        cl.addWidget(self.lt_note)
        lay.addWidget(c)
        c2 = card()
        l2 = QVBoxLayout(c2)
        l2.addWidget(row_label("Colour balance", "h"))
        l2.addWidget(row_label("If a device looks washed out or tinted, lower the channel that is too strong.", "sub"))
        dr = QHBoxLayout()
        self.lt_dev = QComboBox()
        self.lt_dev.currentTextChanged.connect(self.load_balance)
        fb = QPushButton("Find devices")
        fb.clicked.connect(self.find_lights)
        dr.addWidget(self.lt_dev, 1)
        dr.addWidget(fb)
        l2.addLayout(dr)
        gr = QHBoxLayout()
        self.lt_gain = []
        for lab in ("Red", "Green", "Blue"):
            box = QSpinBox()
            box.setRange(0, 100)
            box.setSuffix(" %")
            box.setValue(100)
            box.valueChanged.connect(self.save_balance)
            self.lt_gain.append(box)
            gr.addWidget(QLabel(lab))
            gr.addWidget(box)
        l2.addLayout(gr)
        lay.addWidget(c2)
        cg = card()
        lg = QVBoxLayout(cg)
        lg.addWidget(row_label("Govee lights", "h"))
        lg.addWidget(row_label("Turn on LAN Control for each light in the Govee Home app. Halcyon scans for them. If none are found, type their IP addresses (see your router), separated by commas.", "sub"))
        gr2 = QHBoxLayout()
        self.gv_ips = QLineEdit(self.config.get("govee_ips") or "")
        self.gv_ips.setPlaceholderText("for example 192.168.8.144")
        gb = QPushButton("Save and use")
        gb.clicked.connect(self.save_govee)
        gr2.addWidget(self.gv_ips, 1)
        gr2.addWidget(gb)
        lg.addLayout(gr2)
        lay.addWidget(cg)
        c3 = card()
        l3 = QVBoxLayout(c3)
        l3.addWidget(row_label("Status", "h"))
        self.lt_status = QPlainTextEdit()
        self.lt_status.setReadOnly(True)
        self.lt_status.setMaximumHeight(110)
        l3.addWidget(self.lt_status)
        sr = QHBoxLayout()
        rb = QPushButton("Refresh lights")
        rb.clicked.connect(self.refresh_lights)
        ab = QPushButton("Apply default now")
        ab.clicked.connect(lambda: self.set_default(self.current_default()))
        sr.addWidget(rb)
        sr.addWidget(ab)
        sr.addStretch(1)
        l3.addLayout(sr)
        lay.addWidget(c3)
        lay.addStretch(1)
        self.paint_colour()
        QTimer(self, interval=2000, timeout=self.refresh_lights_status).start()
        return w

    def save_govee(self):
        self.config.set("govee_ips", self.gv_ips.text().strip())
        if self.lights is not None:
            self.lights.set_govee_ips(self.gv_ips.text())
            self.lights.apply_default()
        self.lt_note.setText("Govee addresses saved.")

    def refresh_lights(self):
        if self.lights is not None and hasattr(self.lights, "refresh"):
            self.lights.refresh()
            self.lt_note.setText("Reconnecting and repainting all lights...")
        self.refresh_lights_status()

    def refresh_lights_status(self):
        if self.lights is None or not hasattr(self.lights, "status"):
            return
        self.lt_status.setPlainText("\n".join("%s: %s" % kv for kv in self.lights.status().items()))

    def lan_ip(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("10.255.255.255", 1))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except OSError:
            return "127.0.0.1"

    def webhook_url(self):
        url = "http://%s:%s/" % (self.lan_ip(), self.config.get("webhook_port"))
        tok = self.config.get("webhook_token")
        return url + ("?token=%s" % tok if tok else "")

    def webhook_body(self):
        keys = ("NotificationType", "NotificationUsername", "DeviceName", "RemoteEndPoint", "ClientName", "Name")
        return "{\n" + ",\n".join('  "%s": "{{%s}}"' % (k, k) for k in keys) + "\n}"

    def page_webhook(self):
        w, lay = self.page("Webhook", "Lets Jellyfin tell Halcyon about logins, playback and new items.")
        c = card()
        cl = QVBoxLayout(c)
        cl.addWidget(row_label("1. Webhook URL", "h"))
        cl.addWidget(row_label("Paste this as the Webhook Url in the Jellyfin Generic Destination.", "sub"))
        self.wh_url = QLineEdit(self.webhook_url())
        self.wh_url.setReadOnly(True)
        cl.addWidget(self.wh_url)
        r = QHBoxLayout()
        cb = QPushButton("Copy URL")
        cb.clicked.connect(lambda: self.copy_text(self.wh_url.text(), "URL copied."))
        gb = QPushButton("Make a secret token")
        gb.clicked.connect(self.make_token)
        r.addWidget(cb)
        r.addWidget(gb)
        r.addStretch(1)
        cl.addLayout(r)
        cl.addWidget(row_label("2. Template", "h"))
        cl.addWidget(row_label("In the same Jellyfin destination, choose Add Generic Destination, tick the events you want, and paste this as the Template.", "sub"))
        self.wh_body = QPlainTextEdit(self.webhook_body())
        self.wh_body.setReadOnly(True)
        self.wh_body.setMaximumHeight(140)
        cl.addWidget(self.wh_body)
        r2 = QHBoxLayout()
        cb2 = QPushButton("Copy template")
        cb2.clicked.connect(lambda: self.copy_text(self.wh_body.toPlainText(), "Template copied."))
        r2.addWidget(cb2)
        r2.addStretch(1)
        cl.addLayout(r2)
        cl.addWidget(row_label("3. Header", "h"))
        cl.addWidget(row_label("Add a header named Content-Type with the value application/json.", "sub"))
        cl.addWidget(row_label("4. Firewall (only if Jellyfin cannot reach this PC)", "h"))
        self.wh_fw = QLineEdit('netsh advfirewall firewall add rule name="Halcyon webhook" dir=in action=allow protocol=TCP localport=%s' % self.config.get("webhook_port"))
        self.wh_fw.setReadOnly(True)
        cl.addWidget(self.wh_fw)
        r3 = QHBoxLayout()
        cb3 = QPushButton("Copy firewall command")
        cb3.clicked.connect(lambda: self.copy_text(self.wh_fw.text(), "Copied. Run it in an admin Command Prompt."))
        tb = QPushButton("Send a test login")
        tb.clicked.connect(self.test_webhook)
        r3.addWidget(cb3)
        r3.addWidget(tb)
        r3.addStretch(1)
        cl.addLayout(r3)
        self.wh_note = row_label("")
        cl.addWidget(self.wh_note)
        lay.addWidget(c)
        lay.addStretch(1)
        return w

    def copy_text(self, text, note):
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText(text)
        self.wh_note.setText(note)

    def make_token(self):
        import secrets
        self.config.set("webhook_token", secrets.token_urlsafe(9))
        self.wh_url.setText(self.webhook_url())
        self.wh_note.setText("Token set. Copy the URL again. Restart is not needed.")

    def test_webhook(self):
        url = self.webhook_url()
        body = json.dumps({"NotificationType": "AuthenticationSuccess", "NotificationUsername": "test",
                           "DeviceName": "Halcyon test", "RemoteEndPoint": "127.0.0.1", "ClientName": "Halcyon"}).encode()

        def job():
            try:
                req = urllib.request.Request(url.replace(self.lan_ip(), "127.0.0.1"), data=body,
                                             headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=4) as r:
                    self.bus.log.emit("webhook test: HTTP %s" % r.status)
                self.bus.chat.emit("Halcyon", "Webhook test received.")
            except Exception as exc:
                self.bus.log.emit("webhook test failed: %s" % exc)
        self.run(job)
        self.wh_note.setText("Test sent. If the server is listening you will see it on the Overview page.")

    def current_default(self):
        try:
            return tuple(int(x) for x in self.config.get("rgb_default"))
        except Exception:
            return (30, 80, 255)

    def paint_colour(self):
        r, g, b = self.current_default()
        self.sw.setText("#%02X%02X%02X   click to choose any colour" % (r, g, b))
        self.sw.setStyleSheet("background:rgb(%d,%d,%d);color:%s;font-size:15px;" % (
            r, g, b, "#000" if r + g + b > 380 else "#fff"))

    def set_default(self, rgb):
        self.config.set("rgb_default", [int(rgb[0]), int(rgb[1]), int(rgb[2])])
        self.paint_colour()
        if self.lights is not None:
            self.lights.apply_default()
            self.lt_note.setText("Applied to your lights.")
        else:
            self.lt_note.setText("Saved. Lights are not available right now.")

    def pick_colour(self):
        from PySide6.QtGui import QColor
        col = QColorDialog.getColor(QColor(*self.current_default()), self, "Default colour")
        if col.isValid():
            self.set_default((col.red(), col.green(), col.blue()))

    def find_lights(self):
        if self.lights is None:
            return
        self.lt_note.setText("Looking for devices...")

        def job():
            try:
                names = self.lights.device_names()
            except Exception:
                names = []
            self.bus.lights.emit(names)
        self.run(job)

    def got_lights(self, names):
        tweaks = self.config.get("rgb_tweaks") or {}
        self.lt_dev.clear()
        self.lt_dev.addItems(sorted(set(names) | set(tweaks)))
        self.lt_note.setText("Found %d devices." % len(names) if names else "No devices found. Is OpenRGB or iCUE running?")

    def load_balance(self, name):
        vals = (self.config.get("rgb_tweaks") or {}).get(name, [100, 100, 100])
        for box, v in zip(self.lt_gain, vals):
            box.blockSignals(True)
            box.setValue(int(v))
            box.blockSignals(False)

    def save_balance(self):
        name = self.lt_dev.currentText()
        if not name:
            return
        tweaks = dict(self.config.get("rgb_tweaks") or {})
        vals = [b.value() for b in self.lt_gain]
        if vals == [100, 100, 100]:
            tweaks.pop(name, None)
        else:
            tweaks[name] = vals
        self.config.set("rgb_tweaks", tweaks)
        if self.lights is not None:
            self.lights.apply_default()

    def add_log(self, text):
        self.log.appendPlainText("[%s] %s" % (time.strftime("%H:%M:%S"), text))

    def add_chat(self, who, text):
        self.chat.appendPlainText("%s: %s" % (who, text))

    def set_state(self, name):
        self.core.set_state(name)

    def set_claude(self, which, text):
        {"path": self.cl_path, "ver": self.cl_ver, "test": self.cl_test}[which].setText(text)

    def set_voice(self, which, text):
        {"whisper": self.v_whisper, "model": self.v_model, "tts": self.v_tts}[which].setText(text)

    def run(self, fn):
        threading.Thread(target=fn, daemon=True).start()

    def refresh_claude(self):
        exe = find_claude()
        if not exe:
            self.set_claude("path", "Claude Code not found. Install it, then run `claude` once to sign in.")
            self.cl_path.setObjectName("bad")
            self.cl_path.setStyleSheet("color:#ff6b6b;")
            self.set_claude("ver", "")
            return
        self.cl_path.setStyleSheet("color:#4ade80;")
        self.set_claude("path", "Found: %s" % exe)

        def job():
            try:
                res = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=15,
                                     creationflags=0x08000000 if sys.platform == "win32" else 0)
                self.bus.claude.emit("ver", "Version: %s" % (res.stdout.strip() or res.stderr.strip() or "unknown"))
            except Exception as exc:
                self.bus.claude.emit("ver", "Could not read version: %s" % exc)
        self.run(job)

    def test_claude(self):
        self.set_claude("test", "Asking Claude...")
        self.cl_test.setStyleSheet("color:#ffb84d;")

        def job():
            try:
                out = ask_model(self.config.get("router_model"), "Reply with only the word ok.", 60)
                self.bus.claude.emit("test", "Claude answered: %s" % out[:80])
                self.cl_test.setStyleSheet("color:#4ade80;")
            except BrainError as exc:
                self.bus.claude.emit("test", str(exc))
                self.cl_test.setStyleSheet("color:#ff6b6b;")
        self.run(job)

    def sign_in(self):
        exe = find_claude()
        if not exe:
            self.set_claude("test", "Claude Code not found, nothing to sign in to.")
            return
        if sys.platform == "win32":
            subprocess.Popen(["cmd", "/c", "start", "Claude sign in", "cmd", "/k", '"%s"' % exe])
        else:
            self.set_claude("test", "Run `claude` in a terminal to sign in.")

    def refresh_voice(self):
        srv = W.find_server()
        self.v_whisper.setText("Speech engine: %s" % (srv if srv else "not found (the installer includes it)"))
        self.v_whisper.setStyleSheet("color:%s;" % ("#4ade80" if srv else "#ffb84d"))
        names = {self.config.get("wake_model"), self.config.get("command_model")}
        have = []
        for n in sorted(names):
            try:
                have.append("%s: %s" % (n, "ready" if W.have_model(n) else "missing"))
            except Exception:
                have.append("%s: unknown" % n)
        self.v_model.setText("Models: " + ",  ".join(have))
        self.v_tts.setText("Voice: checking...")

        def job():
            url = self.config.get("chatterbox_url")
            try:
                with urllib.request.urlopen(url + "/health", timeout=1.5):
                    self.bus.voice.emit("tts", "Voice: Chatterbox is running at %s" % url)
                    return
            except Exception:
                pass
            from . import chatterbox_setup as CB
            note = "Voice: Chatterbox is not running, Windows voice will be used instead"
            if not CB.installed():
                note += " (press Install Chatterbox below)"
            self.bus.voice.emit("tts", note)
        self.run(job)

    def cb_log(self, text):
        self.bus.log.emit(text)

    def install_chatterbox(self):
        from . import chatterbox_setup as CB
        self.cb_install.setEnabled(False)
        self.cb_install.setText("Installing...")

        def job():
            ok = False
            try:
                again = CB.installed()
                if again and self.chatterbox is not None:
                    self.chatterbox.stop()
                ok = CB.install(self.cb_log, clean=again)
                if ok and CB.has_amd_gpu():
                    CB.install_gpu(self.cb_log)
                if ok and self.chatterbox is not None:
                    self.chatterbox.start(self.cb_log)
            except Exception as exc:
                self.cb_log("Install error: %s" % exc)
            self.bus.cbdone.emit(ok)
        self.run(job)

    def cb_finished(self, ok):
        self.cb_install.setEnabled(True)
        self.cb_install.setText("Reinstall Chatterbox" if ok else "Install Chatterbox")
        self.refresh_voice()

    def gpu_chatterbox(self):
        from . import chatterbox_setup as CB
        self.cb_gpu.setEnabled(False)
        self.cb_gpu.setText("Installing GPU support...")

        def job():
            ok = False
            try:
                if self.chatterbox is not None:
                    self.chatterbox.stop()
                ok = CB.install_gpu(self.cb_log)
                self.config.set("chatterbox_device", "cuda" if ok else "cpu")
                if self.chatterbox is not None:
                    self.chatterbox.start(self.cb_log)
            except Exception as exc:
                self.cb_log("GPU setup error: %s" % exc)
            self.bus.gpudone.emit(ok)
        self.run(job)

    def gpu_finished(self, ok):
        self.cb_gpu.setEnabled(True)
        self.cb_gpu.setText("GPU on" if ok else "Use my AMD GPU")

    def start_chatterbox(self):
        if self.chatterbox is not None:
            self.run(lambda: self.chatterbox.start(self.cb_log))

    def stop_chatterbox(self):
        if self.chatterbox is not None:
            self.chatterbox.stop()
            self.voice.chatterbox_ok = False
            self.cb_log("Chatterbox stopped.")

    def speak_test(self):
        def job():
            self.bus.state.emit("speaking")
            self.bus.log.emit("Speaking...")
            try:
                self.voice.speak("Hello. This is Halcyon. Voice test complete.", on_amp=lambda v: self.bus.amp.emit(v))
                self.bus.log.emit("Done, engine: %s" % self.voice.engine_name)
                if self.voice.engine_name != "chatterbox" and getattr(self.voice, "last_error", ""):
                    self.bus.log.emit(self.voice.last_error)
            except Exception as exc:
                self.bus.log.emit("Voice failed: %s" % exc)
            self.bus.amp.emit(0)
            self.bus.state.emit("idle")
        self.run(job)

    def toggle_mic(self):
        if self.mic_stream is not None:
            self.mic_stream.stop()
            self.mic_stream.close()
            self.mic_stream = None
            self.mic_btn.setText("Start mic test")
            self.meter.set_value(0)
            self.core.set_state("idle")
            return
        try:
            import numpy as np
            import sounddevice as sd

            def cb(data, frames, tinfo, status):
                self.mic_level = float(np.sqrt(np.mean(np.square(data)))) * 8

            self.mic_stream = sd.InputStream(device=self.dev.currentData(), channels=1, samplerate=16000, callback=cb)
            self.mic_stream.start()
            self.mic_btn.setText("Stop mic test")
            self.core.set_state("listening")
        except Exception as exc:
            self.add_log("Mic failed: %s" % exc)

    def poll_mic(self):
        if self.mic_stream is not None:
            v = min(1.0, self.mic_level)
            self.meter.set_value(v)
            self.core.set_amp(v)
        elif self.core.state in ("idle",):
            self.core.set_amp(0)

    def filter_words(self, text):
        for i in range(self.words.count()):
            it = self.words.item(i)
            it.setHidden(bool(text) and text.lower() not in it.text().lower())

    def try_phrase(self, text):
        if not text.strip():
            self.result.setText("Type something.")
            self.result.setStyleSheet("")
            return
        rest = match_wake(text, self.config.get("wake_words"), bool(self.config.get("wake_prefix_required")),
                          self.config.get("wake_prefixes"))
        if rest is None:
            self.result.setText("Would NOT wake Halcyon.")
            self.result.setStyleSheet("color:#ff6b6b;")
        else:
            self.result.setText("Wakes Halcyon.  Command heard: %s" % (rest if rest else "(nothing yet, it would listen)"))
            self.result.setStyleSheet("color:#4ade80;")

    def closeEvent(self, e):
        e.ignore()
        self.hide()
        self.stop_mic()

    def stop_mic(self):
        if self.mic_stream is not None:
            try:
                self.mic_stream.stop()
                self.mic_stream.close()
            except Exception:
                pass
            self.mic_stream = None
            self.mic_btn.setText("Start mic test")
            self.meter.set_value(0)
