from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit,
                               QSpinBox, QTabWidget, QVBoxLayout, QWidget)

from . import autostart

WHISPER_MODELS = ["tiny.en", "base.en", "small.en", "medium.en"]
CLAUDE_MODELS = ["haiku", "sonnet", "opus"]


class SettingsDialog(QDialog):
    def __init__(self, config, on_saved=None, parent=None):
        super().__init__(parent)
        self.config = config
        self.on_saved = on_saved
        self.setWindowTitle("Halcyon settings")
        self.setMinimumWidth(460)
        tabs = QTabWidget()

        general = QWidget()
        g = QFormLayout(general)
        self.start = QCheckBox("Start Halcyon when Windows starts")
        self.start.setChecked(autostart.is_enabled())
        self.hotkey = QLineEdit(config.get("hotkey"))
        self.wake = QLineEdit(", ".join(config.get("wake_words")))
        self.muted = QCheckBox("Mute Halcyon's voice")
        self.muted.setChecked(bool(config.get("voice_muted")))
        self.monitor = QSpinBox()
        self.monitor.setRange(0, 8)
        self.monitor.setValue(int(config.get("hud_monitor") or 0))
        g.addRow(self.start)
        g.addRow("Talk hotkey", self.hotkey)
        g.addRow("Wake words", self.wake)
        g.addRow("HUD monitor (0 = main)", self.monitor)
        g.addRow(self.muted)
        tabs.addTab(general, "General")

        voice = QWidget()
        v = QFormLayout(voice)
        self.wake_model = self.combo(WHISPER_MODELS, config.get("wake_model"))
        self.command_model = self.combo(WHISPER_MODELS, config.get("command_model"))
        self.gpu = QCheckBox("Use the GPU for speech recognition")
        self.gpu.setChecked(bool(config.get("use_gpu")))
        self.chatterbox = QLineEdit(config.get("chatterbox_url"))
        v.addRow("Wake word model", self.wake_model)
        v.addRow("Command model", self.command_model)
        v.addRow(self.gpu)
        v.addRow("Chatterbox server", self.chatterbox)
        tabs.addTab(voice, "Voice")

        brain = QWidget()
        b = QFormLayout(brain)
        self.router = self.combo(CLAUDE_MODELS, config.get("router_model"))
        self.task = self.combo(CLAUDE_MODELS, config.get("task_model"))
        self.timeout = QSpinBox()
        self.timeout.setRange(30, 3600)
        self.timeout.setSuffix(" s")
        self.timeout.setValue(int(config.get("task_timeout")))
        self.host = QLineEdit(config.get("server_host"))
        self.user = QLineEdit(config.get("server_user"))
        b.addRow("Router model", self.router)
        b.addRow("Task model", self.task)
        b.addRow("Task time limit", self.timeout)
        b.addRow("Server address", self.host)
        b.addRow("Server user", self.user)
        tabs.addTab(brain, "Claude")

        link = QWidget()
        k = QFormLayout(link)
        self.port = QSpinBox()
        self.port.setRange(1024, 65535)
        self.port.setValue(int(config.get("webhook_port")))
        self.token = QLineEdit(config.get("webhook_token"))
        self.cooldown = QSpinBox()
        self.cooldown.setRange(0, 3600)
        self.cooldown.setSuffix(" s")
        self.cooldown.setValue(int(config.get("webhook_cooldown")))
        self.screen = QCheckBox("Use the small screen")
        self.screen.setChecked(bool(config.get("screen_enabled")))
        self.serial = QLineEdit(config.get("serial_port"))
        k.addRow("Jellyfin webhook port", self.port)
        k.addRow("Webhook token", self.token)
        k.addRow("Silence repeats for", self.cooldown)
        k.addRow(self.screen)
        k.addRow("Small screen port", self.serial)
        tabs.addTab(link, "Devices")

        lights = QWidget()
        lf = QFormLayout(lights)
        self.rgb = QCheckBox("Change my lights when Halcyon is called or an event arrives")
        self.rgb.setChecked(bool(config.get("rgb_enabled")))
        self.rgb_brightness = QSpinBox()
        self.rgb_brightness.setRange(1, 100)
        self.rgb_brightness.setSuffix(" %")
        self.rgb_brightness.setValue(int(config.get("rgb_brightness")))
        self.govee = QLineEdit(config.get("govee_ips"))
        self.govee.setPlaceholderText("blank = find Govee lights automatically")
        self.orgb = QCheckBox("Control PC lights through OpenRGB")
        self.orgb.setChecked(bool(config.get("openrgb_enabled")))
        self.orgb_host = QLineEdit(config.get("openrgb_host"))
        self.orgb_port = QSpinBox()
        self.orgb_port.setRange(1, 65535)
        self.orgb_port.setValue(int(config.get("openrgb_port")))
        lf.addRow(self.rgb)
        lf.addRow("Brightness", self.rgb_brightness)
        lf.addRow("Govee light addresses", self.govee)
        lf.addRow(self.orgb)
        lf.addRow("OpenRGB address", self.orgb_host)
        lf.addRow("OpenRGB port", self.orgb_port)
        tabs.addTab(lights, "Lights")

        note = QLabel("Voice, Claude and device changes apply after restarting Halcyon.")
        note.setWordWrap(True)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.save)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(tabs)
        layout.addWidget(note)
        layout.addWidget(buttons)

    def combo(self, items, current):
        box = QComboBox()
        box.setEditable(True)
        box.addItems(items)
        box.setCurrentText(str(current))
        return box

    def save(self):
        c = self.config
        words = [w.strip().lower() for w in self.wake.text().split(",") if w.strip()]
        values = {
            "hotkey": self.hotkey.text().strip() or "ctrl+alt+h",
            "wake_words": words or c.get("wake_words"),
            "hud_monitor": self.monitor.value(),
            "voice_muted": self.muted.isChecked(),
            "wake_model": self.wake_model.currentText().strip(),
            "command_model": self.command_model.currentText().strip(),
            "use_gpu": self.gpu.isChecked(),
            "chatterbox_url": self.chatterbox.text().strip(),
            "router_model": self.router.currentText().strip(),
            "task_model": self.task.currentText().strip(),
            "task_timeout": self.timeout.value(),
            "server_host": self.host.text().strip(),
            "server_user": self.user.text().strip(),
            "webhook_port": self.port.value(),
            "webhook_token": self.token.text().strip(),
            "webhook_cooldown": self.cooldown.value(),
            "screen_enabled": self.screen.isChecked(),
            "serial_port": self.serial.text().strip() or "auto",
            "rgb_enabled": self.rgb.isChecked(),
            "rgb_brightness": self.rgb_brightness.value(),
            "govee_ips": self.govee.text().strip(),
            "openrgb_enabled": self.orgb.isChecked(),
            "openrgb_host": self.orgb_host.text().strip() or "127.0.0.1",
            "openrgb_port": self.orgb_port.value(),
        }
        c.data.update(values)
        c.save()
        autostart.set_enabled(self.start.isChecked())
        if self.on_saved:
            self.on_saved(values)
        self.accept()
