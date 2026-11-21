import os
import sys
import tempfile
import threading
import time

from PySide6.QtCore import QLockFile, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QGuiApplication, QIcon
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QSystemTrayIcon

from . import EXPANSION, NAME, __version__
from .actions import Registry
from .config import Config, actions_dir, config_dir
from .controller import Controller
from .hud import Hud
from .screen import Screen
from .settings import SettingsDialog
from .rgb import Lights, scan_report
from .voice_in import Listener
from .voice_out import Voice
from .webhook import WebhookServer


def resource(rel):
    base = getattr(sys, "_MEIPASS", None) or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
    return os.path.normpath(os.path.join(base, rel))


class Bridge(QObject):
    status = Signal(str)
    clip = Signal(str)


def open_path(path):
    if sys.platform == "win32":
        os.startfile(path)
    else:
        import subprocess
        subprocess.Popen(["xdg-open", path])


SAMPLE_EVENTS = {
    "login": {"kind": "login", "raw": "AuthenticationSuccess", "user": "Alex", "device": "iPhone", "ip": "192.0.2.44",
              "app": "Safari", "item": "", "message": "", "at": 0},
    "failed": {"kind": "failed", "raw": "AuthenticationFailure", "user": "Alex", "device": "Android",
               "ip": "203.0.113.9", "app": "Chrome", "item": "", "message": "", "at": 0},
    "playback": {"kind": "playback", "raw": "PlaybackStart", "user": "Alex", "device": "Smart TV", "ip": "",
                 "app": "Jellyfin", "item": "Venom", "message": "", "at": 0},
    "shutdown": {"kind": "shutdown", "raw": "ServerShutdown", "user": "", "device": "", "ip": "", "app": "",
                 "item": "", "message": "", "at": 0},
}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    selftest = "--selftest" in argv
    trace = None
    if selftest:
        import faulthandler
        trace = open(os.path.join(tempfile.gettempdir(), "halcyon_selftest.log"), "w", buffering=1)
        faulthandler.enable(trace)
        faulthandler.dump_traceback_later(45, file=trace, exit=True)
        trace.write("start\n")
    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    app.setApplicationName(NAME)
    icon_path = resource("assets/icon.png")
    icon = QIcon(icon_path) if os.path.exists(icon_path) else QIcon()
    app.setWindowIcon(icon)

    lock = QLockFile(os.path.join(tempfile.gettempdir(), "halcyon.lock"))
    lock.setStaleLockTime(0)
    if not selftest and not lock.tryLock(100):
        return 0

    config = Config()
    bridge = Bridge()
    hud = Hud(config.get("hud_monitor"))
    screen = Screen(config, on_status=bridge.status.emit)
    voice = Voice(config)
    clipboard = QGuiApplication.clipboard()
    bridge.clip.connect(lambda text: clipboard.setText(text))
    lights = Lights(config)
    from .cues import Cues
    cues = Cues(config)
    controller = Controller(config, hud.bridge, screen, voice, lights=lights, clipboard_cb=bridge.clip.emit, cues=cues)
    controller.status_cb = bridge.status.emit

    tray = QSystemTrayIcon(icon, app)
    tray.setToolTip("%s %s" % (NAME, __version__))
    menu = QMenu()

    def act(label, fn, checkable=False, checked=False):
        a = QAction(label, menu)
        a.setCheckable(checkable)
        if checkable:
            a.setChecked(checked)
            a.toggled.connect(fn)
        else:
            a.triggered.connect(lambda: fn())
        menu.addAction(a)
        return a

    listener = Listener(config, controller.on_command, on_level=lambda v: hud.bridge.amp(v) if controller.asking or
                        hud.state == "listening" else None, on_status=bridge.status.emit, on_wake=controller.on_wake)
    controller.attach_listener(listener)
    from .chatterbox_setup import Server as ChatterboxServer, installed as chatterbox_installed
    chatterbox = ChatterboxServer(config)
    if not selftest:
        threading.Thread(target=voice.warm_up, daemon=True).start()
    if not selftest and chatterbox_installed():
        threading.Thread(target=chatterbox.start, daemon=True).start()
    webhook_server = WebhookServer(config, controller.on_webhook, on_status=bridge.status.emit)

    def talk():
        controller.on_wake()
        listener.request_dictation()

    act("Talk now", talk)
    act("Yes", lambda: controller.answer(True))
    act("No", lambda: controller.answer(False))
    act("Cancel current task", controller.cancel)
    menu.addSeparator()
    mute = act("Mute voice", lambda on: voice.set_muted(on), True, voice.muted)
    sub = menu.addMenu("Test webhook")
    for key in SAMPLE_EVENTS:
        a = QAction(key.capitalize(), sub)
        a.triggered.connect(lambda _=False, k=key: threading.Thread(
            target=controller.on_webhook, args=(dict(SAMPLE_EVENTS[k], at=time.time()),), daemon=True).start())
        sub.addAction(a)
    sub2 = menu.addMenu("Small screen")
    for label, fn in (("Drowsy", screen.eyes.drowse), ("Sleep", screen.eyes.sleep_now), ("Wake", screen.eyes.wake)):
        a = QAction(label, sub2)
        a.triggered.connect(lambda _=False, f=fn: f())
        sub2.addAction(a)
    panel_holder = []

    def open_panel():
        if not panel_holder:
            from .panel import Panel
            p = Panel(config, voice, hud, controller, SAMPLE_EVENTS, lights, chatterbox)
            bridge.status.connect(p.note)
            panel_holder.append(p)
        panel_holder[0].show()
        panel_holder[0].raise_()
        panel_holder[0].activateWindow()

    act("Open Halcyon", open_panel)
    tray.activated.connect(lambda reason: open_panel() if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick) else None)
    menu.addSeparator()
    act("Open actions folder", lambda: open_path(str(actions_dir())))
    dialogs = []

    def settings():
        if dialogs and dialogs[0].isVisible():
            dialogs[0].raise_()
            dialogs[0].activateWindow()
            return
        dlg = SettingsDialog(config, on_saved=lambda v: (voice.set_muted(bool(v["voice_muted"])), lights.apply_default()),
                             device_finder=lights.device_names)
        dlg.setWindowIcon(icon)
        dialogs[:] = [dlg]
        dlg.show()

    def lights_scan():
        def work():
            text = scan_report(config)
            path = config_dir() / "lights_scan.txt"
            path.write_text(text, encoding="utf-8")
            open_path(str(path))
        threading.Thread(target=work, daemon=True).start()

    lights_menu = menu.addMenu("Lights test")
    for label, level in (("Cyan", "info"), ("Yellow", "warn"), ("Red", "alert")):
        a = lights_menu.addAction(label)
        a.triggered.connect(lambda checked=False, lv=level: lights.show(lv))
    lights_menu.addAction("Back to default colour").triggered.connect(lambda checked=False: lights.apply_default())
    lights_menu.addAction("Scan lights and devices").triggered.connect(lambda checked=False: lights_scan())

    act("Settings...", settings)
    act("Open settings file", lambda: open_path(str(config.path)))
    app.aboutToQuit.connect(lights.shutdown)
    QTimer.singleShot(2500, lights.apply_default)
    def about():
        box = QMessageBox()
        box.setWindowTitle("About %s" % NAME)
        box.setWindowIcon(icon)
        box.setText("%s %s\n\n%s" % (NAME, __version__, EXPANSION))
        dialogs.append(box)
        box.show()

    act("About", about)
    act("Quit", app.quit)
    tray.setContextMenu(menu)
    tray.show()
    if config.get("open_panel_on_start"):
        QTimer.singleShot(600, open_panel)

    def on_status(text):
        tray.setToolTip("%s: %s" % (NAME, text))
        if any(w in text.lower() for w in ("problem", "unavailable", "not found", "downloading", "connected")):
            tray.showMessage(NAME, text, QSystemTrayIcon.Information, 4000)

    bridge.status.connect(on_status)

    hotkey = None
    try:
        from .vendor.hotkey import Hotkey
        hotkey = Hotkey()
        hotkey.start(config.get("hotkey"), lambda: QTimer.singleShot(0, talk))
    except Exception:
        hotkey = None

    if selftest:
        screen.start()
        webhook_server.start()
        QTimer.singleShot(1500, lambda: threading.Thread(
            target=controller.on_webhook, args=(dict(SAMPLE_EVENTS["login"], at=time.time()),), daemon=True).start())
        QTimer.singleShot(9000, app.quit)
        trace.write("running\n")
    else:
        screen.start()
        webhook_server.start()
        listener.start()
        tray.showMessage(NAME, "Running. Say \"hal-see-on\" or press %s." % config.get("hotkey").upper(),
                         QSystemTrayIcon.Information, 4000)
    code = app.exec()
    if selftest:
        trace.write("exited\n")
        trace.flush()
        os._exit(0)
    def shut():
        for fn in (hotkey.stop if hotkey else None, listener.stop, screen.stop, webhook_server.stop, chatterbox.stop, voice.stop):
            if fn is None:
                continue
            try:
                fn()
            except Exception:
                pass

    worker = threading.Thread(target=shut, daemon=True)
    worker.start()
    worker.join(4)
    try:
        chatterbox.stop()
    except Exception:
        pass
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(code)
