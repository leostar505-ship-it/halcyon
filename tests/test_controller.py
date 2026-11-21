import os
import stat
import sys
import tempfile
import threading
import time

import pytest

from halcyon import config as cfgmod

FAKE = r'''#!/usr/bin/env python3
import json, sys
args = sys.argv[1:]
stdin = sys.stdin.read()
if "--output-format" in args:
    if "--resume" in args:
        print(json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "scp report.docx leo@192.168.8.50:~"}}]}}))
        print(json.dumps({"type": "result", "result": "Copied it.\nSAY: The report is on the server.", "session_id": "s1", "permission_denials": []}))
    else:
        print(json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}}))
        print(json.dumps({"type": "result", "result": "Blocked.", "session_id": "s1", "permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "scp report.docx leo@192.168.8.50:~"}}]}))
elif "router for Halcyon" in stdin:
    if "report" in stdin:
        print(json.dumps({"route": "claude", "task": "copy report.docx to the server", "screenshot": False}))
    else:
        print(json.dumps({"route": "chat", "say": "It is a lovely day, sir."}))
else:
    print("Sir, Alex has just logged on.")
'''


class FakeHud:
    def __init__(self):
        self.log = []

    def __getattr__(self, name):
        def fn(*a):
            self.log.append((name, a))
            return len(self.log)
        return fn


class FakeScreen:
    class Eyes:
        def after(self, *a):
            pass

        def drowse(self):
            pass

        def sleep_now(self):
            pass

        def wake(self):
            pass

    def __init__(self):
        self.eyes = self.Eyes()
        self.log = []

    def __getattr__(self, name):
        def fn(*a):
            self.log.append((name, a))
        return fn


class FakeVoice:
    muted = False

    def __init__(self):
        self.spoken = []

    def speak(self, text, on_amp=None):
        self.spoken.append(text)

    def stop(self):
        pass

    def set_muted(self, v):
        self.muted = v


@pytest.fixture
def env(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "fake_claude.py"
    script.write_text(FAKE)
    if sys.platform == "win32":
        exe = bindir / "claude.cmd"
        exe.write_text('@"%s" "%s" %%*\r\n' % (sys.executable, script))
    else:
        exe = bindir / "claude"
        exe.write_text("#!/bin/sh\nexec %s %s \"$@\"\n" % (sys.executable, script))
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("HALCYON_CLAUDE", str(exe))
    monkeypatch.setattr(cfgmod, "config_dir", lambda: tmp_path)
    monkeypatch.setattr("halcyon.config.config_dir", lambda: tmp_path)
    (tmp_path / "actions").mkdir(exist_ok=True)
    monkeypatch.setattr("halcyon.config.actions_dir", lambda: tmp_path / "actions")
    monkeypatch.setattr("halcyon.actions.actions_dir", lambda: tmp_path / "actions")
    monkeypatch.setattr("halcyon.brain.actions_dir", lambda: tmp_path / "actions")
    from halcyon.config import Config
    from halcyon.controller import Controller
    cfg = Config()
    hud, screen, voice = FakeHud(), FakeScreen(), FakeVoice()
    clip = []
    c = Controller(cfg, hud, screen, voice, clipboard_cb=clip.append)
    return c, hud, screen, voice, clip


def test_chat_route(env):
    c, hud, screen, voice, _ = env
    c.run_command("tell me something nice")
    assert voice.spoken == ["It is a lovely day, sir."]


def test_money_blocked(env):
    c, hud, screen, voice, _ = env
    c.run_command("buy me a new skin")
    assert "can't spend money" in voice.spoken[0]


def test_task_with_approval(env):
    c, hud, screen, voice, _ = env

    def approve():
        for _ in range(100):
            if c.asking:
                c.answer(True)
                return
            time.sleep(.05)
    threading.Thread(target=approve, daemon=True).start()
    c.run_command("copy the report to the server")
    joined = " ".join(voice.spoken)
    assert "May I copy a file" in joined
    assert voice.spoken[-1] == "The report is on the server."


def test_task_declined(env):
    c, hud, screen, voice, _ = env

    def deny():
        for _ in range(100):
            if c.asking:
                c.answer(False)
                return
            time.sleep(.05)
    threading.Thread(target=deny, daemon=True).start()
    c.run_command("copy the report to the server")
    assert not any("on the server" in s for s in voice.spoken if s.startswith("The report"))


def test_webhook_line(env):
    c, hud, screen, voice, _ = env
    ev = {"kind": "login", "raw": "AuthenticationSuccess", "user": "Alex", "device": "iPhone", "ip": "1.1.1.1",
          "app": "Safari", "item": "", "message": "", "at": time.time()}
    c.on_webhook(ev)
    assert voice.spoken == ["Sir, Alex has just logged on."]
    c.on_webhook(dict(ev))
    assert len(voice.spoken) == 1

def test_ctx_exposes_ask(env):
    c, hud, screen, voice, clip = env
    assert callable(c.ctx.ask_yes_no)


def test_history_flows_and_clears(env):
    c, hud, screen, voice, clip = env
    c.run_command("tell me something nice")
    assert c.history.text() == "User: tell me something nice\nHalcyon: It is a lovely day, sir."
    c.run_command("clear history")
    assert c.history.recent() == []
    assert voice.spoken[-1] == "Conversation cleared, sir."


def test_run_command_asks_first(env):
    import sys
    c, hud, screen, voice, clip = env

    def answer(value):
        for _ in range(100):
            if c.asking:
                c.answer(value)
                return
            time.sleep(.05)
    threading.Thread(target=answer, args=(True,), daemon=True).start()
    c.run_command('run command "%s" -c "print(7)"' % sys.executable)
    assert voice.spoken[0].startswith("Shall I run command")
    assert voice.spoken[-1] == "7"
    voice.spoken.clear()
    threading.Thread(target=answer, args=(False,), daemon=True).start()
    c.run_command('run command "%s" -c "print(8)"' % sys.executable)
    assert voice.spoken[-1] == "Cancelled, sir."
    assert "8" not in voice.spoken


def test_asleep_screen_stays_asleep_while_speaking(env):
    c, hud, screen, voice, clip = env
    from halcyon.screen import Eyes
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    eyes = Eyes()
    screen.eyes = eyes
    screen.set_activity = eyes.set_activity
    screen.set_expression = eyes.set_expression
    eyes.sleep_now()
    c.say("Hello there, sir.")
    assert eyes.activity == "sleep"


def test_webhook_leaves_sleeping_screen_and_lights_alone(env):
    c, hud, screen, voice, clip = env
    from halcyon.screen import Eyes
    from PySide6.QtWidgets import QApplication
    QApplication.instance() or QApplication([])
    eyes = Eyes()
    screen.eyes = eyes
    eyes.sleep_now()
    shown = []
    c.light_on = lambda level: shown.append(level)
    screen.card = lambda *a, **k: shown.append("card")
    ev = {"kind": "login", "raw": "AuthenticationSuccess", "user": "Sam", "device": "iPhone", "ip": "1.1.1.1",
          "app": "Safari", "item": "", "message": "", "at": time.time()}
    c.on_webhook(ev)
    assert shown == [] and eyes.activity == "sleep"


def test_unknown_app_goes_back_to_the_router(env):
    c, hud, screen, voice, clip = env
    c.run_command("close zzqxv")
    assert voice.spoken == ["It is a lovely day, sir."]


def test_about_self_answers_from_facts(env):
    c, hud, screen, voice, clip = env
    res = c.registry.run("about_self", c.ctx, {"question": "what processor is chatterbox using"})
    assert res["say"] == "Sir, Alex has just logged on."
    assert any("Chatterbox" in f for f in c.self_facts())


def test_guess_is_confirmed_before_running(env, monkeypatch):
    c, hud, screen, voice, clip = env
    from halcyon import brain
    real = brain.route
    monkeypatch.setattr(brain, "route", lambda text, d, m, history="", note="":
                        {"route": "action", "action": "close_app", "args": {"app": "Fortnite"}} if note else real(text, d, m, history=history))
    asked = []
    c.ask_yes_no = lambda q, b: asked.append(q) or False
    c.run_command("close zzqxv")
    assert asked == ["Did you mean Fortnite, sir? Shall I close it? Say yes or no."]
    assert voice.spoken[-1] == "Cancelled, sir."


def test_low_risk_command_runs_without_asking(env):
    c, hud, screen, voice, clip = env
    asked = []
    c.ask_yes_no = lambda *a, **k: asked.append(a) or True
    c.registry.run = lambda name, ctx, args: {"say": "Done, sir."}
    c.run_action({"route": "action", "action": "run_command", "args": {"command": "echo hello"}})
    assert asked == [] and voice.spoken[-1] == "Done, sir."


def test_high_risk_command_warns_in_red_then_asks(env):
    c, hud, screen, voice, clip = env
    asked = []
    c.ask_yes_no = lambda q, b, level="warn", expr="curious": asked.append((q, level, expr)) or False
    c.run_action({"route": "action", "action": "run_command", "args": {"command": "Remove-Item C:\\a.txt"}})
    assert voice.spoken[0].startswith("Careful, sir.")
    assert asked and asked[0][1] == "alert" and asked[0][2] == "worried"
    assert voice.spoken[-1] == "Cancelled, sir."


def test_update_question_shows_thinking_steps(env, monkeypatch):
    c, hud, screen, voice, clip = env
    from halcyon import actions
    monkeypatch.setattr(actions, "github_updates", lambda: ["fix lights"])
    steps = []
    monkeypatch.setattr(c, "think", lambda text, pause=0.7: steps.append(text))
    c.registry.run("about_self", c.ctx, {"question": "what was in the last update"})
    assert steps[0] == "You want the update log"
    assert "GitHub" in steps[1] and steps[2] == "Checking GitHub"


def test_think_adds_and_finishes_job_lines(env):
    c, hud, screen, voice, clip = env
    c.think("one", 0)
    c.think("two", 0)
    c.end_think()
    assert c._thinking is False
