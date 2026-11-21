import json
import os
import pathlib
import sys

DEFAULTS = {
    "wake_words": ["halcyon", "halsion", "halcion", "halsyon", "halcyan", "halsian", "halsean", "halseon", "halsee on", "halsy on", "halsi on", "halsey on", "halsie on", "halsea on", "halzion", "halzian", "halcian", "halcean", "halcyone", "halsyun", "hal see on", "hal sea on", "hal si on", "hal sy on", "hal sigh on", "hal sion", "hal sian", "hal zion", "hal cion", "hal cyan", "hal seon", "hal sean", "hal seen", "hal see an", "hal see in", "hal sea an", "hal c on", "hal c in", "hal cee on", "hal sie on", "hal sy an", "hal sigh an", "how see on", "how sion", "hell see on", "hello see on", "hallo see on", "hall see on", "hall sion", "hull see on", "hal sy", "hals ion", "hals eon", "hal's ion", "hal's see on", "halls ion", "hal siren", "hal syrin", "hal saion", "hal xion", "hal shun", "hal sun", "hal sin", "hal sinn", "hallcyon", "hallcion", "hallsion", "hallsyon", "hallseon", "halcyon's"],
    "wake_model": "small.en",
    "command_model": "small.en",
    "use_gpu": True,
    "hotkey": "ctrl+alt+h",
    "voice_muted": False,
    "chatterbox_url": "http://127.0.0.1:8123",
    "router_model": "haiku",
    "task_model": "sonnet",
    "task_timeout": 600,
    "webhook_port": 8765,
    "webhook_token": "",
    "webhook_cooldown": 120,
    "webhook_escalate": 3,
    "rgb_enabled": True,
    "rgb_brightness": 100,
    "govee_ips": "",
    "openrgb_enabled": True,
    "openrgb_host": "127.0.0.1",
    "openrgb_port": 6742,
    "admin_users": ["admin", "leo"],
    "server_host": "192.168.8.50",
    "server_user": "leo",
    "wake_url": "",
    "serial_port": "auto",
    "screen_enabled": True,
    "hud_monitor": 0,
    "apps": {},
    "favourite_games": [],
    "auto_allow": ["Read", "Glob", "Grep", "WebSearch", "WebFetch", "Bash(ls:*)", "Bash(pwd)", "Bash(cat:*)",
                   "Bash(head:*)", "Bash(tail:*)", "Bash(find:*)", "Bash(grep:*)", "Bash(echo:*)", "Bash(whoami)",
                   "Bash(ssh-add -l)", "Bash(ssh-keygen -l:*)", "Bash(Get-ChildItem:*)", "Bash(Get-Content:*)",
                   "Bash(Test-Path:*)"],
}


def config_dir():
    base = os.environ.get("APPDATA") if sys.platform == "win32" else None
    root = pathlib.Path(base) if base else pathlib.Path.home() / ".config"
    path = root / "Halcyon"
    path.mkdir(parents=True, exist_ok=True)
    return path


def actions_dir():
    path = config_dir() / "actions"
    path.mkdir(parents=True, exist_ok=True)
    return path


class Config:
    def __init__(self):
        self.path = config_dir() / "config.json"
        self.data = dict(DEFAULTS)
        self.load()

    def load(self):
        try:
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                self.data.update(loaded)
        except (OSError, ValueError):
            pass
        if not self.path.exists():
            self.save()

    def save(self):
        try:
            self.path.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
        except OSError:
            pass

    def get(self, key):
        return self.data.get(key, DEFAULTS.get(key))

    def set(self, key, value):
        self.data[key] = value
        self.save()
