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
    bad = webhook.normalise({"event": "AuthenticationFailure", "user": "Alex"})
    out = [sup.check(dict(bad), now=base + i)[0] for i in range(6)]
    assert out[0] is True and out[4] is True and out.count(True) == 2


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
