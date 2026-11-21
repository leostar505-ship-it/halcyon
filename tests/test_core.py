import time

from halcyon import brain, safety, webhook
from halcyon.voice_in import match_wake


WAKE = ["halcyon", "halsion", "halcion", "hal see on", "hal sion", "halsyon"]


def test_fast_route_apps():
    r = brain.fast_route("open Chrome")
    assert r["action"] == "open_app" and r["args"]["app"].lower() == "chrome"
    r = brain.fast_route("close the browser")
    assert r["action"] == "close_app"
    assert brain.fast_route("system status")["action"] == "system_status"
    assert brain.fast_route("make the small screen sleepy")["args"]["state"] == "sleepy"
    assert brain.fast_route("pick a random game")["action"] == "random_game"
    assert brain.fast_route("copy report.docx to the server") is None


def test_wake_matching():
    assert match_wake("Halcyon open chrome", WAKE) == "open chrome"
    assert match_wake("Hey Halcyon, what time is it", WAKE) == "what time is it"
    assert match_wake("Hal see on. Open Steam", WAKE) == "Open Steam"
    assert match_wake("Halsion", WAKE) == ""
    assert match_wake("Hallucian close discord", WAKE) == "close discord"
    assert match_wake("thank you for watching", WAKE) is None
    assert match_wake("", WAKE) is None
    from halcyon.config import DEFAULTS
    full = DEFAULTS["wake_words"]
    for phrase in ("Hal-see-on open chrome", "Hell see on what time is it", "Hal sion mute", "Halsey on status",
                   "Hal zion open steam", "How see on, volume up"):
        assert match_wake(phrase, full) is not None, phrase
    for phrase in ("open the halcyon folder", "hello there", "what's the weather", "all season tyres"):
        assert match_wake(phrase, full) is None, phrase


def test_spend_block():
    assert safety.is_spend("buy me a skin on steam")
    assert safety.is_spend("pay for the subscription")
    assert not safety.is_spend("copy my files to the server")
    assert safety.denial_is_spend("Bash", {"command": "curl https://api.stripe.com/v1/charges"})
    assert not safety.denial_is_spend("Bash", {"command": "scp report.docx leo@192.168.8.50:~"})


def test_yes_no():
    assert safety.parse_yes_no("yes please") is True
    assert safety.parse_yes_no("No don't") is False
    assert safety.parse_yes_no("maybe") is None
    assert safety.parse_yes_no("") is None


def test_describe_and_pattern():
    verb, detail = safety.describe_tool("Bash", {"command": "scp a.txt leo@host:~"})
    assert verb == "copy a file"
    assert safety.allow_pattern("Bash", {"command": "scp a.txt leo@host:~"}) == "Bash(scp a.txt leo@host:~)"


def test_parse_final():
    say, clip, steps = brain.parse_final("Tried it.\nCLIPBOARD: ssh-copy-id leo@192.168.8.50\nSTEP: Open PowerShell\n"
                                         "STEP: Paste and press enter\nSAY: The key was rejected.")
    assert say == "The key was rejected."
    assert clip == "ssh-copy-id leo@192.168.8.50"
    assert steps == ["Open PowerShell", "Paste and press enter"]
    assert brain.parse_final("All done.")[0] == "All done."


def test_normalise_and_suppress():
    ev = webhook.normalise({"event": "AuthenticationSuccess", "user": "Alex", "device": "iPhone",
                            "ip": "1.2.3.4:5555", "client": "Safari"})
    assert ev["kind"] == "login" and ev["ip"] == "1.2.3.4"
    sup = webhook.Suppressor(cooldown=120)
    base = time.monotonic()
    results = [sup.check(dict(ev), now=base + i)[0] for i in range(10)]
    assert results[0] is True and not any(results[1:])
    later = sup.check(dict(ev), now=base + 200)[0]
    assert later is True
    assert ev["kind"] == "login" and webhook.LEVEL["login"] == "info" and webhook.EXPR["login"] == "happy"
    bad = webhook.normalise({"event": "AuthenticationFailure", "user": "Alex"})
    sup = webhook.Suppressor(cooldown=120)
    seen = [sup.check(dict(bad), now=base + i) for i in range(6)]
    assert [a for a, _ in seen] == [True, False, True, False, False, False]
    assert seen[0][1].get("level") is None
    assert seen[2][1]["level"] == "alert" and seen[2][1]["expr"] == "angry"
    admin = webhook.normalise({"event": "AuthenticationFailure", "user": "Leo"})
    ok, out = webhook.Suppressor(cooldown=120).check(admin, now=base)
    assert ok and out["level"] == "alert" and out["expr"] == "angry" and out["admin"]


def test_card_lines():
    ev = webhook.normalise({"event": "PlaybackStart", "user": "Alex", "item": "Venom", "device": "TV"})
    labels = [a for a, _ in webhook.card_lines(ev)]
    assert labels == ["User", "Playing", "Device", "App"]


def test_settings_dialog_saves(tmp_path, monkeypatch):
    from PySide6.QtWidgets import QApplication
    from halcyon import config as cfgmod, settings
    monkeypatch.setattr(cfgmod, "config_dir", lambda: tmp_path)
    app = QApplication.instance() or QApplication([])
    cfg = cfgmod.Config()
    dlg = settings.SettingsDialog(cfg)
    dlg.hotkey.setText("ctrl+alt+j")
    dlg.wake.setText("halcyon, hal see on")
    dlg.port.setValue(9001)
    dlg.save()
    assert cfg.get("hotkey") == "ctrl+alt+j"
    assert cfg.get("wake_words") == ["halcyon", "hal see on"]
    assert cfg.get("webhook_port") == 9001
    assert cfgmod.Config().get("webhook_port") == 9001


class FakeBackend:
    def __init__(self):
        self.log = []

    def snapshot(self):
        self.log.append("snap")

    def colour(self, rgb):
        self.log.append(rgb)

    def restore(self):
        self.log.append("restore")


def test_lights_flow():
    from halcyon import rgb

    class Cfg:
        def get(self, k):
            return {"rgb_enabled": True, "rgb_brightness": 50}[k]

    fb = FakeBackend()
    lights = rgb.Lights(Cfg(), govee=fb)
    lights.show("alert")
    lights.show("info")
    lights.restore()
    lights.restore()
    lights.shutdown()
    assert fb.log == ["snap", (128, 0, 0), (0, 128, 128), "restore"]
    assert rgb.parse_ips("192.168.8.20, 192.168.8.21;192.168.8.22") == ["192.168.8.20", "192.168.8.21", "192.168.8.22"]
    assert rgb.scale((255, 190, 0), 0) == (0, 0, 0)


def test_govee_payload(monkeypatch):
    from halcyon import rgb
    sent = []
    g = rgb.Govee(["10.0.0.5"])
    monkeypatch.setattr(g, "send", lambda ip, cmd, data: sent.append((ip, cmd, data)))
    g.colour((0, 255, 255))
    assert sent[0] == ("10.0.0.5", "turn", {"value": 1})
    assert sent[1][1] == "colorwc" and sent[1][2]["color"] == {"r": 0, "g": 255, "b": 255}
    g.saved["10.0.0.5"] = {"onOff": 1, "brightness": 80, "color": {"r": 1, "g": 2, "b": 3}, "colorTemInKelvin": 0}
    sent.clear()
    g.restore()
    assert [c for _, c, _ in sent] == ["turn", "colorwc", "brightness"] and not g.saved
