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

    def colour(self, rgb, tint=None):
        self.log.append(rgb)


def test_wake_prefix():
    from halcyon.voice_in import wake_check
    pre = ["hey", "ok", "yo"]
    assert wake_check("Hey Halcyon open chrome", WAKE, True, pre) == ("ok", "open chrome")
    assert wake_check("OK halcyon", WAKE, True, pre) == ("ok", "")
    assert wake_check("Yo, Halcyon what time is it", WAKE, True, pre) == ("ok", "what time is it")
    assert wake_check("Halcyon open chrome", WAKE, True, pre)[0] == "noprefix"
    assert match_wake("Halcyon open chrome", WAKE, True, pre) is None
    assert match_wake("Halcyon open chrome", WAKE) == "open chrome"
    assert wake_check("all season tyres", WAKE, True, pre) == (None, None)


def test_lights_flow():
    from halcyon import rgb

    class Cfg:
        def get(self, k):
            return {"rgb_enabled": True, "rgb_brightness": 50, "rgb_default": [30, 80, 255], "rgb_tweaks": {}}[k]

    fb = FakeBackend()
    lights = rgb.Lights(Cfg(), govee=fb)
    lights.show("alert")
    lights.show("info")
    lights.restore()
    lights.restore()
    lights.shutdown()
    assert fb.log[:2] == [(128, 0, 0), (0, 128, 128)]
    assert fb.log[2:] and set(fb.log[2:]) == {(15, 40, 128)}
    assert rgb.parse_ips("192.168.8.20, 192.168.8.21;192.168.8.22") == ["192.168.8.20", "192.168.8.21", "192.168.8.22"]
    assert rgb.scale((255, 190, 0), 0) == (0, 0, 0)


def test_lights_refresh():
    from halcyon import rgb

    class Cfg:
        def get(self, k):
            return {"rgb_enabled": True, "rgb_brightness": 100, "rgb_default": [10, 20, 30], "rgb_tweaks": {}}[k]

    class Resettable(FakeBackend):
        resets = 0

        def reset(self):
            Resettable.resets += 1

    fb = Resettable()
    lights = rgb.Lights(Cfg(), govee=fb)
    lights.show("alert")
    lights.refresh()
    lights.shutdown()
    assert Resettable.resets == 1
    assert fb.log[0] == fb.log[1] == rgb.COLOURS["alert"]


def test_govee_payload(monkeypatch):
    from halcyon import rgb
    sent = []
    g = rgb.Govee(["10.0.0.5"])
    monkeypatch.setattr(g, "send", lambda ip, cmd, data: sent.append((ip, cmd, data)))
    g.colour((0, 255, 255))
    assert sent[0] == ("10.0.0.5", "turn", {"value": 1})
    assert sent[1][1] == "colorwc" and sent[1][2]["color"] == {"r": 0, "g": 255, "b": 255}
    sent.clear()
    g.colour((0, 200, 200), lambda name: [100, 50, 100])
    assert sent[1][2]["color"] == {"r": 0, "g": 100, "b": 200}


def test_gain_and_default():
    from halcyon import rgb
    assert rgb.apply_gain((200, 100, 50), [50, 100, 0]) == (100, 100, 0)
    assert rgb.apply_gain((1, 2, 3), None) == (1, 2, 3)
    assert rgb.COLOURS["warn"][1] < 140


def test_corsair_correction():
    from halcyon import corsair
    assert corsair.correct((255, 0, 0)) == (255, 0, 0)
    r, g, b = corsair.correct((255, 123, 35))
    assert g < 123 and b < 35


def test_cues_render():
    from halcyon import cues
    for name in cues.RECIPES:
        assert cues.render(name)[:4] == b"RIFF"


class FakeCfg:
    def get(self, key, default=None):
        return {} if key == "apps" else default


class AskCtx:
    def __init__(self, answer):
        self.config = FakeCfg()
        self.answer = answer
        self.asked = []

    def ask_yes_no(self, question, bullet):
        self.asked.append(question)
        return self.answer


class FakeProc:
    def __init__(self, exe):
        self.info = {"exe": exe}


def epic_env(tmp_path, monkeypatch):
    import json
    from halcyon import actions
    manifests = tmp_path / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"
    manifests.mkdir(parents=True)
    game = tmp_path / "Games" / "Fortnite"
    (manifests / "a.item").write_text(json.dumps({
        "DisplayName": "Fortnite", "AppName": "Fortnite", "CatalogNamespace": "fn",
        "CatalogItemId": "4fe75bbc5a674f4f9b356b5c90567da5", "InstallLocation": str(game)}))
    (manifests / "b.item").write_text(json.dumps({"DisplayName": "Half Installed", "AppName": "X",
                                                  "bIsIncompleteInstall": True}))
    (manifests / "c.item").write_text("not json")
    monkeypatch.setenv("PROGRAMDATA", str(tmp_path))
    monkeypatch.setattr(actions, "start_menu_apps", lambda: {})
    monkeypatch.setattr(actions, "steam_games", lambda: [])
    return game


def test_epic_fortnite_found(tmp_path, monkeypatch):
    from halcyon import actions
    epic_env(tmp_path, monkeypatch)
    found = actions.resolve_app(AskCtx(True), "fortnite")
    assert found[0] == "epic" and found[2] == "Fortnite"
    assert found[1] == ("com.epicgames.launcher://apps/fn%3A4fe75bbc5a674f4f9b356b5c90567da5%3AFortnite"
                        "?action=launch&silent=true")
    assert actions.resolve_app(AskCtx(True), "half installed") is None


def test_open_app_not_running_launches_without_asking(tmp_path, monkeypatch):
    from halcyon import actions
    epic_env(tmp_path, monkeypatch)
    launched = []
    monkeypatch.setattr(actions, "launch_path", launched.append)
    monkeypatch.setattr(actions.psutil, "process_iter", lambda attrs=None: [FakeProc(str(tmp_path / "other.exe"))])
    ctx = AskCtx(True)
    res = actions.open_app(ctx, {"app": "fortnite"})
    assert ctx.asked == [] and len(launched) == 1 and launched[0].startswith("com.epicgames.launcher://")
    assert "Launching Fortnite" in res["say"]


def test_open_app_already_open_asks(tmp_path, monkeypatch):
    from halcyon import actions
    game = epic_env(tmp_path, monkeypatch)
    exe = str(game / "FortniteGame" / "Binaries" / "Win64" / "FortniteClient-Win64-Shipping.exe")
    monkeypatch.setattr(actions.psutil, "process_iter", lambda attrs=None: [FakeProc(exe)])
    launched = []
    monkeypatch.setattr(actions, "launch_path", launched.append)
    ctx = AskCtx(False)
    res = actions.open_app(ctx, {"app": "fortnite"})
    assert len(ctx.asked) == 1 and "already open" in ctx.asked[0]
    assert launched == [] and "Leaving Fortnite" in res["say"]
    ctx = AskCtx(True)
    res = actions.open_app(ctx, {"app": "fortnite"})
    assert len(ctx.asked) == 1 and len(launched) == 1 and "Launching Fortnite" in res["say"]


def test_open_app_already_open_by_name(monkeypatch):
    from halcyon import actions
    monkeypatch.setattr(actions, "resolve_app", lambda ctx, name: ("path", "chrome.lnk", "chrome"))
    monkeypatch.setattr(actions, "find_procs", lambda ctx, name: [object()])
    launched = []
    monkeypatch.setattr(actions, "launch_path", launched.append)
    ctx = AskCtx(False)
    res = actions.open_app(ctx, {"app": "chrome"})
    assert "already open" in ctx.asked[0] and launched == [] and "Leaving Chrome" in res["say"]

