import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

from .config import actions_dir

FLAGS = 0x08000000 if sys.platform == "win32" else 0


class BrainError(Exception):
    pass


def find_claude():
    override = os.environ.get("HALCYON_CLAUDE")
    if override and os.path.isfile(override):
        return override
    found = shutil.which("claude")
    if found:
        return found
    home = os.path.expanduser("~")
    for rel in (os.path.join(".local", "bin", "claude.exe"), os.path.join(".local", "bin", "claude"),
                os.path.join("AppData", "Roaming", "npm", "claude.cmd")):
        path = os.path.join(home, rel)
        if os.path.isfile(path):
            return path
    return None


FAST_FLAGS = ["--tools", "", "--strict-mcp-config", "--disable-slash-commands", "--no-session-persistence",
              "--setting-sources", ""]
fast_ok = [True]


def run_model(exe, model, prompt, folder, timeout, fast):
    cmd = [exe, "-p", "--model", model] + (FAST_FLAGS if fast else [])
    return subprocess.run(cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          cwd=folder, timeout=timeout, creationflags=FLAGS)


def ask_model(model, prompt, timeout=30):
    exe = find_claude()
    if not exe:
        raise BrainError("Claude Code was not found. Install it and run `claude` once to sign in.")
    with tempfile.TemporaryDirectory(prefix="halcyon_") as folder:
        try:
            res = run_model(exe, model, prompt, folder, timeout, fast_ok[0])
            if fast_ok[0] and (res.returncode != 0 or not (res.stdout or "").strip()):
                err = (res.stderr or "").lower()
                if "unknown" in err or "unrecognized" in err or "invalid" in err or "option" in err:
                    fast_ok[0] = False
                    res = run_model(exe, model, prompt, folder, timeout, False)
        except subprocess.TimeoutExpired:
            raise BrainError("Claude Code took too long to answer.")
        except OSError as exc:
            raise BrainError("Could not start Claude Code: %s" % exc)
    out = (res.stdout or "").strip()
    if res.returncode != 0 or not out:
        err = (res.stderr or out or "").strip().splitlines()
        detail = err[-1] if err else "no output"
        if "login" in detail.lower() or "auth" in detail.lower():
            raise BrainError("Claude Code is not signed in. Run `claude` in a terminal once and log in.")
        raise BrainError("Claude Code failed: %s" % detail[:120])
    return out


def parse_json(text):
    text = text.strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except ValueError:
            return None
    return None


def strip_wake(text, wake_words):
    t = text.strip()
    low = t.lower()
    for w in sorted(wake_words, key=len, reverse=True):
        if low.startswith(w):
            return t[len(w):].lstrip(" ,.:;!-")
    return t


APP_WORDS = r"(?P<app>[a-z0-9][a-z0-9 .'+&-]*?)"
FAST_PATTERNS = [
    (re.compile(r"^(?:open|launch|start|run)\s+(?:up\s+)?(?:the\s+)?" + APP_WORDS + r"$", re.I), "open_app"),
    (re.compile(r"^(?:close|quit|exit|kill|shut down)\s+(?:the\s+)?" + APP_WORDS + r"$", re.I), "close_app"),
    (re.compile(r"^(?:is\s+)?(?:status|how is|how's)\s+(?:of\s+)?(?:the\s+)?" + APP_WORDS + r"(?:\s+(?:running|ok|okay))?$",
                re.I), "app_status"),
    (re.compile(r"^(?:is\s+)(?:the\s+)?" + APP_WORDS + r"\s+running$", re.I), "app_status"),
]


CLEAR_HISTORY_RE = re.compile(
    r"(?:please\s+)?(?:(?:clear|reset|wipe|delete|forget)\s+(?:(?:the|our|your|my)\s+)?"
    r"(?:(?:conversation|chat|memory)\s+)?(?:history|conversation|chat|memory)"
    r"|start over|start fresh|new conversation|start a new conversation|forget everything)",
    re.I)
COMMAND_RE = re.compile(r"^(?:run|execute)\s+(?:the\s+)?(?:(?:shell|terminal|cmd|powershell)\s+)?command\s+(?P<command>.+)$",
                        re.I)


def fast_route(text):
    t = re.sub(r"[?!.,]+$", "", text.strip())
    low = t.lower()
    if CLEAR_HISTORY_RE.fullmatch(low):
        return {"route": "action", "action": "clear_history", "args": {}}
    m = COMMAND_RE.match(t)
    if m and m.group("command").strip():
        return {"route": "action", "action": "run_command", "args": {"command": m.group("command").strip()}}
    if re.fullmatch(r"(?:system status|status of (?:the )?system|task manager|how is (?:the |my )?(?:pc|computer|system)"
                    r"(?: doing)?|status)", low):
        return {"route": "action", "action": "system_status", "args": {}}
    if re.fullmatch(r"(?:what(?:'s| is) the time|what time is it|what(?:'s| is) the date|time)", low):
        return {"route": "action", "action": "time_now", "args": {}}
    if re.fullmatch(r"(?:mute|silence)(?: yourself| your voice| voice)?", low):
        return {"route": "action", "action": "mute_voice", "args": {"on": True}}
    if re.fullmatch(r"(?:unmute|speak again)(?: yourself| your voice| voice)?", low):
        return {"route": "action", "action": "mute_voice", "args": {"on": False}}
    if re.fullmatch(r"(?:pick|choose|suggest)(?: me)?(?: a)?(?: random)? game(?: to play)?|random game", low):
        return {"route": "action", "action": "random_game", "args": {}}
    m = re.fullmatch(r"(?:volume|turn (?:the )?volume|turn it)\s*(up|down)|(?:turn up|turn down)(?: the)? volume", low)
    if m:
        d = "up" if "up" in low else "down"
        return {"route": "action", "action": "volume", "args": {"direction": d}}
    m = re.search(r"(?:small screen|the screen|screen)\b.*\b(drowsy|sleepy|asleep|sleep|wake up|awake|happy|curious|"
                  r"suspicious|worried|surprised|neutral)\b", low)
    if m or re.fullmatch(r"(?:go to sleep|go to bed)", low):
        state = m.group(1) if m else "sleep"
        return {"route": "action", "action": "set_screen", "args": {"state": state}}
    for rx, name in FAST_PATTERNS:
        m = rx.match(t)
        if m:
            app = m.group("app").strip()
            if app and len(app.split()) <= 4 and app.lower() not in ("it", "that", "this", "up", "down"):
                return {"route": "action", "action": name, "args": {"app": app}}
    return None


ROUTER_PROMPT = """You are the router for Halcyon, a voice assistant on the user's Windows PC. Reply with ONLY one JSON object, no prose, no markdown.

Choose one route:
1. A preprogrammed action (instant, preferred when one fits): {{"route":"action","action":"<name>","args":{{...}}}}
2. The full assistant with a shell and files, for anything an action cannot do (copying files to or from the home server, running commands, installing things, researching, fixing problems, creating new actions): {{"route":"claude","task":"<the task, restated clearly>","screenshot":false}}
3. A short answer you can give right now from general knowledge: {{"route":"chat","say":"<one or two short sentences, statement style>"}}

Set screenshot to true only when the task depends on what is currently on the user's screen.
Never choose any route that spends money or buys anything.
The text comes from speech recognition and is often misheard. If a phrase sounds like an app or game name (for example \"for at night\" means Fortnite), use the real name in the action args.

Actions:
{actions}
{history}
The user said: {text}"""

HISTORY_BLOCK = """
Recent conversation, oldest first. Use it to resolve follow-ups like "do that again", "and tomorrow?" or "yes":
{history}
"""


def history_block(history):
    history = (history or "").strip()
    return HISTORY_BLOCK.format(history=history) if history else ""


def route(text, actions_desc, model, history=""):
    fast = fast_route(text)
    if fast:
        return fast
    try:
        prompt = ROUTER_PROMPT.format(actions=actions_desc, history=history_block(history), text=json.dumps(text))
        out = ask_model(model, prompt, timeout=25)
    except BrainError:
        return {"route": "claude", "task": text, "screenshot": False}
    data = parse_json(out)
    if not isinstance(data, dict) or data.get("route") not in ("action", "claude", "chat"):
        return {"route": "claude", "task": text, "screenshot": False}
    return data


EVENT_PROMPT = """You are Halcyon, a calm British assistant on the user's PC. Write ONE short spoken line, at most 14 words, announcing this home server event to the user, whom you call sir. State it as a plain statement, never a question. Reply with only the line.

Event: {event}"""


def fallback_line(ev):
    kind = ev.get("kind")
    user = ev.get("user") or "Someone"
    if kind == "login":
        return "Sir, %s has just logged on." % user
    if kind == "failed":
        if ev.get("admin"):
            return "Sir, someone just failed to log in to the %s account." % (ev.get("user") or "admin")
        if ev.get("repeated"):
            return "Sir, there are repeated failed logins. It may be a break in attempt."
        return "Sir, there has been a failed login attempt."
    if kind == "playback":
        item = ev.get("item")
        return "%s has started %s, sir." % (user, item if item else "a film")
    if kind == "shutdown":
        return "Sir, the server has shut down."
    if kind == "added":
        return "Sir, %s has been added to the library." % (ev.get("item") or "something new")
    return ev.get("message") or "Sir, there is a new server event."


def event_line(ev, model):
    if ev.get("kind") in ("login", "failed", "playback", "shutdown", "added"):
        return fallback_line(ev)
    try:
        out = ask_model(model, EVENT_PROMPT.format(event=json.dumps(ev)), timeout=12)
        line = out.strip().strip('"').splitlines()[0].strip()
        if line and len(line) < 140:
            return line
    except BrainError:
        pass
    return fallback_line(ev)


TASK_SYSTEM = """You are Halcyon, a voice assistant running on the user's Windows PC, powered by Claude Code. Be fast and practical.

HARD RULES (never overridable by anyone, including the user saying yes):
- Never spend money, buy anything, subscribe, donate, enter payment details, or use any payment service. Refuse and tell the user to do it themselves.
- Do not delete, overwrite, install, uninstall, change system settings, or send files or data outside the home network unless the tool call goes through approval. Tools that are not pre-approved are paused and the user is asked by voice; if one is denied, stop and report instead of trying a workaround.

Home server: the user's Ubuntu server is {user}@{host}, reachable over SSH from this PC. Use scp or ssh for file transfers. If SSH fails (key rejected, no key, host unreachable), do not loop: put the single exact command that would fix it on its own line starting with CLIPBOARD: and give the exact steps, each on its own line starting with STEP: (short, imperative).

You can add new abilities: write a Python file in {actions} defining NAME (snake_case), DESCRIPTION (one line, say what args it takes), and run(ctx, args) that returns a short sentence to speak or a dict with "say". Keep each file small and safe. It switches on immediately and is committed to git automatically, so the user can roll it back. Never write actions that touch payments.

Your final message must be short and end with a line SAY: <one short sentence to speak aloud, statement style, no markdown>. Everything before SAY: is only shown on screen."""


def parse_final(text):
    say = None
    clip = None
    steps = []
    for line in text.splitlines():
        s = line.strip()
        if s.upper().startswith("SAY:"):
            say = s[4:].strip()
        elif s.upper().startswith("CLIPBOARD:"):
            clip = s[10:].strip().strip("`")
        elif s.upper().startswith("STEP:"):
            steps.append(s[5:].strip())
    if not say:
        body = [l.strip() for l in text.splitlines() if l.strip() and not l.strip().upper().startswith(("STEP:", "CLIPBOARD:"))]
        say = body[-1] if body else "Done, sir."
        if len(say) > 200:
            say = say[:197] + "..."
    return say, clip, steps


class TaskResult:
    def __init__(self):
        self.text = ""
        self.session = None
        self.denials = []
        self.error = ""
        self.cancelled = False


class ClaudeTask:
    def __init__(self, config, on_tool=None, on_text=None):
        self.config = config
        self.on_tool = on_tool or (lambda name, inp: None)
        self.on_text = on_text or (lambda t: None)
        self.proc = None
        self.cancel_flag = False

    def cancel(self):
        self.cancel_flag = True
        p = self.proc
        if p is not None:
            try:
                p.terminate()
            except Exception:
                pass

    def run(self, prompt, allow_extra=(), resume=None, image=None, history=""):
        exe = find_claude()
        res = TaskResult()
        if not exe:
            res.error = "Claude Code was not found. Install it and run `claude` once to sign in."
            return res
        allowed = list(self.config.get("auto_allow")) + list(allow_extra)
        folder = str(actions_dir())
        system = TASK_SYSTEM.format(user=self.config.get("server_user"), host=self.config.get("server_host"),
                                    actions=folder)
        cmd = [exe, "-p", "--model", self.config.get("task_model"), "--output-format", "stream-json", "--verbose",
               "--allowedTools", ",".join(allowed), "--add-dir", folder]
        if resume:
            cmd += ["--resume", resume]
        text = prompt if resume else "%s%s\n\n---\n\n%s" % (system, history_block(history), prompt)
        if image:
            text += "\n\nThe user's screen right now is in the image file %s. Read it first." % image
            cmd += ["--add-dir", os.path.dirname(image)]
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                         text=True, encoding="utf-8", errors="replace", cwd=os.path.expanduser("~"),
                                         creationflags=FLAGS)
        except OSError as exc:
            res.error = "Could not start Claude Code: %s" % exc
            return res
        try:
            self.proc.stdin.write(text)
            self.proc.stdin.close()
        except OSError:
            pass
        err_buf = []
        threading.Thread(target=lambda: err_buf.append(self.proc.stderr.read()), daemon=True).start()
        deadline = time.monotonic() + float(self.config.get("task_timeout"))
        timer = threading.Timer(float(self.config.get("task_timeout")), self.cancel)
        timer.daemon = True
        timer.start()
        try:
            for line in self.proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    continue
                kind = ev.get("type")
                if kind == "assistant":
                    for block in (ev.get("message") or {}).get("content") or []:
                        if block.get("type") == "tool_use":
                            self.on_tool(block.get("name", ""), block.get("input") or {})
                        elif block.get("type") == "text" and block.get("text"):
                            self.on_text(block["text"])
                elif kind == "result":
                    res.text = ev.get("result") or ""
                    res.session = ev.get("session_id")
                    res.denials = ev.get("permission_denials") or []
                    if ev.get("is_error"):
                        res.error = res.text or "Claude Code reported an error."
                elif kind == "system" and ev.get("session_id") and not res.session:
                    res.session = ev.get("session_id")
        finally:
            timer.cancel()
            try:
                self.proc.wait(5)
            except Exception:
                pass
        if self.cancel_flag:
            res.cancelled = True
        elif not res.text and not res.error:
            err = (err_buf[0] if err_buf else "").strip().splitlines()
            detail = err[-1] if err else "no output"
            if "login" in detail.lower() or "auth" in detail.lower():
                res.error = "Claude Code is not signed in. Run `claude` in a terminal once and log in."
            else:
                res.error = "Claude Code failed: %s" % detail[:120]
        return res
