import os
import queue
import tempfile
import threading
import time

from .cues import NullCues
from .rgb import NullLights
from . import brain, safety, webhook
from .actions import Registry


class Ctx:
    def __init__(self, controller):
        self._c = controller
        self.config = controller.config
        self.screen = controller.screen
        self.registry = controller.registry

    def set_muted(self, value):
        self._c.voice.set_muted(value)

    def clipboard(self, text):
        self._c.clipboard(text)

    def screenshot(self):
        return self._c.screenshot()


class Controller:
    def __init__(self, config, hud, screen, voice, lights=None, clipboard_cb=None, open_folder_cb=None, cues=None):
        self.config = config
        self.cues = cues or NullCues()
        self.hud = hud
        self.screen = screen
        self.voice = voice
        self.lights = lights or NullLights()
        self.light_gen = 0
        self.listener = None
        self.registry = Registry(config)
        self.clipboard_cb = clipboard_cb or (lambda text: None)
        self.lock = threading.Lock()
        self.suppressor = webhook.Suppressor(config.get("webhook_cooldown"), config.get("webhook_escalate"), config.get("admin_users"))
        self.answers = queue.Queue()
        self.asking = False
        self.task = None
        self.status_cb = lambda s: None
        self.ctx = Ctx(self)

    def clipboard(self, text):
        self.clipboard_cb(text)

    def screenshot(self):
        try:
            import mss
            path = os.path.join(tempfile.gettempdir(), "halcyon_screen.png")
            with mss.mss() as sct:
                sct.shot(mon=1, output=path)
            return path
        except Exception:
            return None

    def attach_listener(self, listener):
        self.listener = listener

    def status(self, text):
        self.status_cb(text)

    def answer(self, value):
        if self.asking:
            self.answers.put(bool(value))

    def on_command(self, text):
        threading.Thread(target=self.run_command, args=(text,), daemon=True).start()

    def on_wake(self):
        self.cues.play("wake")
        self.light_on("info")
        self.hud.begin(False)
        self.hud.state("listening")
        self.screen.set_activity("none")
        self.screen.set_expression("curious")

    def say(self, text, show=None, reveal=None):
        shown = text if show is None else show
        if self.listener:
            self.listener.paused = True

        def start():
            if reveal:
                reveal()
            self.hud.said(shown)
            self.hud.state("speaking")
            self.screen.set_activity("speak")
        try:
            self.voice.start_hook = start
            self.voice.speak(text, on_amp=lambda v: self.hud.amp(v))
        finally:
            self.voice.start_hook = None
            self.hud.state("idle")
            self.screen.set_activity("none")
            if self.listener:
                self.listener.paused = False

    def settle(self, expr="neutral", delay=2.0):
        self.screen.eyes.after(delay, lambda: (self.screen.set_expression(expr), self.screen.set_level("info")))

    def ask_yes_no(self, question, bullet):
        self.cues.play("ask")
        jid = self.hud.job_add(bullet, "ask")
        self.hud.level("warn")
        self.screen.set_level("warn")
        self.screen.set_expression("curious")
        self.screen.footer("Say yes or no", "warn")
        self.say(question)
        self.hud.state("listening")
        self.asking = True
        while not self.answers.empty():
            self.answers.get_nowait()
        if self.listener and self.listener.engine_ok:
            def heard():
                text = self.listener.wait_answer(15)
                self.answers.put(safety.parse_yes_no(text) if text is not None else False)
            threading.Thread(target=heard, daemon=True).start()
        try:
            result = self.answers.get(timeout=16)
        except queue.Empty:
            result = False
        self.asking = False
        if self.listener:
            self.listener.answer_wanted = False
        if result is None:
            result = False
        self.hud.level("info")
        self.screen.set_level("info")
        self.screen.footer("")
        self.hud.job_set(jid, bullet + (" - approved" if result else " - declined"), "done" if result else "fail")
        return bool(result)

    def light_on(self, level):
        self.light_gen += 1
        self.lights.show(level)

    def light_off(self, delay=0.0):
        gen = self.light_gen
        if delay <= 0:
            self.lights.restore()
            return

        def later():
            if gen == self.light_gen:
                self.lights.restore()
        threading.Timer(delay, later).start()

    def finish_hud(self, seconds=5.0):
        self.cues.play("done")
        self.light_off(min(seconds, 3.0))
        self.hud.state("idle")
        self.hud.close_in(seconds)

    def run_command(self, text):
        text = (text or "").strip()
        if not text:
            self.light_off()
            self.hud.close()
            self.screen.set_expression("neutral")
            return
        if safety.CANCEL_RE.match(text):
            self.cues.play("nevermind")
            self.light_off()
            self.hud.close()
            self.screen.set_expression("neutral")
            return
        if not self.lock.acquire(blocking=False):
            return
        try:
            self._run(text)
        except Exception as exc:
            self.hud.level("alert")
            self.screen.set_level("alert")
            self.screen.set_expression("sad")
            self.cues.play("error")
            self.say("Something went wrong, sir. %s" % type(exc).__name__)
            self.finish_hud()
            self.settle()
        finally:
            self.lock.release()

    def _run(self, text):
        self.hud.begin(False)
        self.hud.transcript("YOU > %s" % text.lower())
        self.hud.state("thinking")
        self.screen.set_activity("think")
        if safety.is_spend(text):
            self.screen.set_activity("none")
            self.hud.level("warn")
            self.screen.set_level("warn")
            self.screen.set_expression("suspicious")
            self.say(safety.REFUSAL)
            self.finish_hud()
            self.settle()
            return
        decision = brain.route(text, self.registry.describe(), self.config.get("router_model"))
        kind = decision.get("route")
        if kind == "chat":
            self.screen.set_activity("none")
            self.screen.set_expression("happy")
            self.say(str(decision.get("say") or "Done, sir."))
            self.finish_hud()
            self.settle()
            return
        if kind == "action":
            self.run_action(decision)
            return
        self.run_task(decision.get("task") or text, bool(decision.get("screenshot")))

    def run_action(self, decision):
        name = decision.get("action")
        args = decision.get("args") or {}
        acts = self.registry.all()
        info = acts.get(name)
        if not info:
            self.run_task(decision.get("task") or str(name), False)
            return
        if info.get("confirm"):
            self.screen.set_activity("none")
            self.hud.send("mini", True)
            self.hud.job_start()
            what = "%s %s" % (name.replace("_", " "), args.get("app", "")) if args else name.replace("_", " ")
            ok = self.ask_yes_no("Shall I %s? Say yes or no." % what.strip(), what.strip().capitalize() + "?")
            if not ok:
                self.screen.set_expression("neutral")
                self.say("Cancelled, sir.")
                self.hud.job_end()
                self.finish_hud(3)
                self.settle()
                return
            self.hud.job_end()
            self.hud.send("mini", False)
        self.hud.state("thinking")
        try:
            res = self.registry.run(name, self.ctx, args)
        except Exception as exc:
            res = {"say": "That action failed, sir.", "expr": "sad", "level": "warn", "detail": str(exc)}
        res = res or {"say": "Done, sir."}
        level = res.get("level", "info")
        self.hud.level(level)
        self.screen.set_level(level)
        if res.get("panel"):
            self.hud.panel(res["panel"])
        if not res.get("keep_screen"):
            self.screen.set_activity("none")
            self.screen.set_expression(res.get("expr", "neutral"))
        if res.get("say"):
            self.say(res["say"])
        elif res.get("text"):
            self.hud.said(res["text"])
            time.sleep(1.2)
        self.finish_hud(6 if res.get("panel") else 4)
        if not res.get("keep_screen"):
            self.settle()

    def run_task(self, task, want_shot):
        self.hud.send("mini", True)
        self.hud.job_start()
        self.screen.set_activity("think")
        self.hud.job_add("Heard: %s" % safety.short(task, 80), "done")
        shot = None
        if want_shot:
            jid = self.hud.job_add("Taking a screenshot", "run")
            shot = self.screenshot()
            self.hud.job_set(jid, "Screenshot taken" if shot else "Screenshot failed", "done" if shot else "fail")
        current = {"id": None}

        def on_tool(name, inp):
            if current["id"] is not None:
                self.hud.job_set(current["id"], None, "done")
            verb, detail = safety.describe_tool(name, inp)
            label = detail if name == "Bash" else "%s: %s" % (name, detail)
            current["id"] = self.hud.job_add(safety.short(label, 70), "run")
            self.screen.footer(safety.short(label, 40))

        task_runner = brain.ClaudeTask(self.config, on_tool=on_tool)
        self.task = task_runner
        before = self.registry.sig
        result = task_runner.run(task, image=shot)
        approved = []
        rounds = 0
        while result.denials and rounds < 4 and not result.error:
            rounds += 1
            new = []
            for d in result.denials:
                name = d.get("tool_name", "")
                inp = d.get("tool_input") or {}
                if safety.denial_is_spend(name, inp):
                    self.hud.job_add("Blocked: this would involve spending money", "fail")
                    continue
                pattern = safety.allow_pattern(name, inp)
                if pattern in approved or pattern in new:
                    continue
                verb, detail = safety.describe_tool(name, inp)
                self.screen.set_activity("none")
                ok = self.ask_yes_no("May I %s? Say yes or no." % verb, "Allow: %s" % safety.short(detail, 52))
                if ok:
                    new.append(pattern)
                else:
                    approved.append("DECLINED")
            if not new:
                break
            approved += new
            self.screen.set_activity("think")
            current["id"] = None
            result = task_runner.run("The user approved those actions. Continue and finish the task.",
                                     allow_extra=[a for a in approved if a != "DECLINED"], resume=result.session)
        if current["id"] is not None:
            self.hud.job_set(current["id"], None, "done")
        self.screen.footer("")
        if result.error:
            self.hud.level("alert")
            self.screen.set_level("alert")
            self.screen.set_activity("none")
            self.screen.set_expression("sad")
            self.hud.job_add(safety.short(result.error, 70), "fail")
            self.say("I couldn't finish that, sir. %s" % safety.short(result.error, 90))
        elif result.cancelled:
            self.screen.set_activity("none")
            self.say("Cancelled, sir.")
        else:
            say, clip, steps = brain.parse_final(result.text)
            if clip:
                self.clipboard(clip)
                self.hud.job_add("Fix copied to clipboard", "done")
                self.hud.level("warn")
                self.screen.set_level("warn")
            for step in steps[:5]:
                self.hud.job_add(safety.short(step, 70), "done")
            if self.registry.load_user() or self.registry.sig != before:
                self.registry.commit("action update from Halcyon task")
                self.hud.job_add("New action saved and switched on", "done")
            declined = "DECLINED" in approved
            self.screen.set_activity("none")
            self.screen.set_expression("suspicious" if declined else "sad" if clip else "happy")
            self.say(say)
        self.hud.job_end()
        self.finish_hud(6)
        self.settle(delay=3.0)

    def cancel(self):
        if self.task:
            self.task.cancel()
        self.voice.stop()

    def on_webhook(self, ev):
        announce, ev = self.suppressor.check(ev)
        if not announce:
            return
        level = ev.get("level") or webhook.LEVEL.get(ev["kind"], "info")
        expr = ev.get("expr") or webhook.EXPR.get(ev["kind"], "curious")
        self.cues.play("alert" if level == "alert" else "info")
        self.light_on(level)
        self.screen.card(webhook.card_lines(ev), level, expr)
        def reveal():
            self.hud.begin(True)
            self.hud.level(level)
            self.hud.transcript("WEBHOOK > jellyfin / %s" % (ev["raw"] or ev["kind"]).lower())
        with self.lock:
            line = brain.event_line(ev, self.config.get("router_model"))
            self.say(line, reveal=reveal)
            self.finish_hud(1.5)
