import difflib
import glob
import os
import random
import re
import shutil
import subprocess
import sys
import time
import traceback

import psutil

from . import safety
from .config import actions_dir

BUILTIN = {}


def action(name, description, confirm=False):
    def deco(fn):
        BUILTIN[name] = {"description": description, "fn": fn, "confirm": confirm, "builtin": True}
        return fn
    return deco


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", str(s).lower()).strip()


def start_menu_apps():
    roots = []
    if sys.platform == "win32":
        roots = [os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs"),
                 os.path.join(os.environ.get("PROGRAMDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs")]
    found = {}
    for root in roots:
        for path in glob.glob(os.path.join(root, "**", "*.lnk"), recursive=True):
            found[_norm(os.path.splitext(os.path.basename(path))[0])] = path
    return found


def steam_roots():
    roots = []
    if sys.platform == "win32":
        for env in ("PROGRAMFILES(X86)", "PROGRAMFILES"):
            base = os.environ.get(env)
            if base:
                roots.append(os.path.join(base, "Steam"))
    else:
        roots.append(os.path.expanduser("~/.steam/steam"))
    return [r for r in roots if os.path.isdir(r)]


def steam_games():
    libs = []
    for root in steam_roots():
        libs.append(os.path.join(root, "steamapps"))
        vdf = os.path.join(root, "steamapps", "libraryfolders.vdf")
        try:
            text = open(vdf, encoding="utf-8", errors="replace").read()
            for m in re.finditer(r'"path"\s+"([^"]+)"', text):
                libs.append(os.path.join(m.group(1).replace("\\\\", "\\"), "steamapps"))
        except OSError:
            pass
    games = {}
    for lib in dict.fromkeys(libs):
        for path in glob.glob(os.path.join(lib, "appmanifest_*.acf")):
            try:
                text = open(path, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            name = re.search(r'"name"\s+"([^"]+)"', text)
            appid = re.search(r'"appid"\s+"(\d+)"', text)
            last = re.search(r'"LastPlayed"\s+"(\d+)"', text)
            if name and appid and name.group(1) not in ("Steamworks Common Redistributables",):
                games[appid.group(1)] = {"name": name.group(1), "id": appid.group(1),
                                         "last": int(last.group(1)) if last else 0}
    return list(games.values())


def best_match(query, names):
    q = _norm(query)
    if not q:
        return None
    best = None
    best_score = 0.0
    for n in names:
        nn = _norm(n)
        if nn == q:
            return n
        score = difflib.SequenceMatcher(None, q, nn).ratio()
        if q in nn or nn in q:
            score = max(score, .85)
        if score > best_score:
            best, best_score = n, score
    return best if best_score >= .6 else None


def launch_path(path):
    if sys.platform == "win32":
        os.startfile(path)
    else:
        subprocess.Popen(["xdg-open", path])


def resolve_app(ctx, name):
    apps = ctx.config.get("apps") or {}
    key = best_match(name, list(apps.keys()))
    if key:
        return ("path", apps[key], key)
    menu = start_menu_apps()
    key = best_match(name, list(menu.keys()))
    if key:
        return ("path", menu[key], key)
    games = steam_games()
    by_name = {g["name"]: g for g in games}
    key = best_match(name, list(by_name.keys()))
    if key:
        return ("steam", by_name[key]["id"], key)
    return None


@action("open_app", "Open an app, program, folder shortcut or game by name. args: app")
def open_app(ctx, args):
    name = str(args.get("app", "")).strip()
    found = resolve_app(ctx, name)
    if not found:
        return {"say": "I couldn't find %s, sir." % name, "expr": "sad", "level": "info"}
    kind, target, label = found
    try:
        if kind == "steam":
            launch_path("steam://rungameid/%s" % target)
        else:
            launch_path(target)
    except Exception as exc:
        return {"say": "I couldn't open %s, sir." % label, "expr": "sad", "detail": str(exc)}
    return {"say": "Opening %s, sir." % label.title() if kind == "path" else "Launching %s, sir." % label,
            "expr": "happy"}


def find_procs(ctx, name):
    q = _norm(name)
    matches = []
    for p in psutil.process_iter(["name", "memory_info"]):
        try:
            pn = _norm(os.path.splitext(p.info["name"] or "")[0])
        except Exception:
            continue
        if pn and (q == pn or q in pn or pn in q and len(pn) > 3):
            matches.append(p)
    return matches


@action("close_app", "Close a running app by name. args: app", confirm=True)
def close_app(ctx, args):
    name = str(args.get("app", "")).strip()
    procs = find_procs(ctx, name)
    if not procs:
        return {"say": "%s isn't running, sir." % name.title(), "expr": "neutral"}
    for p in procs:
        try:
            p.terminate()
        except Exception:
            pass
    return {"say": "Closing %s." % name.title(), "expr": "neutral"}


@action("app_status", "Say whether an app is running and how much memory it uses. args: app")
def app_status(ctx, args):
    name = str(args.get("app", "")).strip()
    procs = find_procs(ctx, name)
    if not procs:
        return {"say": "%s is not running, sir." % name.title(), "expr": "neutral"}
    mem = 0
    for p in procs:
        try:
            mem += p.info["memory_info"].rss
        except Exception:
            pass
    return {"say": "%s is running, using %d megabytes." % (name.title(), mem // (1024 * 1024)), "expr": "neutral"}


@action("system_status", "Show system status: CPU, memory and top programs. No args.")
def system_status(ctx, args):
    cpu = psutil.cpu_percent(interval=.3)
    mem = psutil.virtual_memory().percent
    return {"say": "System is healthy, sir. CPU at %d percent, memory at %d percent." % (cpu, mem),
            "panel": "system", "expr": "happy" if cpu < 80 else "suspicious"}


@action("set_screen", "Set the small screen's expression: neutral, happy, curious, suspicious, worried, surprised, "
                      "sleepy, or sleep and wake. args: state")
def set_screen(ctx, args):
    state = str(args.get("state", "neutral")).lower()
    if state in ("sleep", "asleep", "sleeping"):
        ctx.screen.eyes.sleep_now()
        return {"say": "Small screen is asleep, sir.", "keep_screen": True}
    if state in ("drowsy", "tired", "sleepy"):
        ctx.screen.eyes.drowse()
        return {"say": "Getting sleepy, sir. Small screen settling down.", "keep_screen": True}
    if state in ("wake", "awake", "wake up"):
        ctx.screen.eyes.wake()
        return {"say": "Small screen is awake, sir.", "keep_screen": True}
    ctx.screen.set_expression(state)
    ctx.screen.set_activity("none")
    return {"say": "Small screen is feeling %s, sir." % state, "keep_screen": True}


@action("volume", "Change the PC volume: up, down or mute. args: direction")
def volume(ctx, args):
    d = str(args.get("direction", "up")).lower()
    if sys.platform != "win32":
        return {"say": "Volume control only works on Windows, sir."}
    import ctypes
    codes = {"up": 0xAF, "down": 0xAE, "mute": 0xAD}
    code = codes.get(d, 0xAF)
    reps = 5 if d in ("up", "down") else 1
    for _ in range(reps):
        ctypes.windll.user32.keybd_event(code, 0, 0, 0)
        ctypes.windll.user32.keybd_event(code, 0, 2, 0)
    return {"say": "Volume %s." % d, "expr": "neutral"}


@action("mute_voice", "Mute or unmute Halcyon's own voice. args: on (true/false)")
def mute_voice(ctx, args):
    on = args.get("on", True)
    on = on if isinstance(on, bool) else str(on).lower() in ("true", "yes", "1", "on")
    ctx.set_muted(on)
    return {"say": "" if on else "Voice restored, sir.", "text": "Voice muted." if on else "Voice restored.",
            "expr": "neutral"}


@action("time_now", "Tell the current time and date. No args.")
def time_now(ctx, args):
    now = time.localtime()
    return {"say": "It's %s, sir." % time.strftime("%H:%M on %A the %d of %B", now).replace(" 0", " "),
            "expr": "neutral"}


@action("random_game", "Pick a random game to play from recently played Steam games and favourites, and launch it. "
                       "No args.")
def random_game(ctx, args):
    games = sorted(steam_games(), key=lambda g: -g["last"])
    recent = [g for g in games if g["last"] > 0][:20]
    pool = [("steam", g["id"], g["name"]) for g in recent]
    apps = ctx.config.get("apps") or {}
    for fav in ctx.config.get("favourite_games") or []:
        if fav in apps:
            pool.append(("path", apps[fav], fav))
        else:
            found = resolve_app(ctx, fav)
            if found:
                pool.append(found)
    if not pool:
        return {"say": "I couldn't find any games to pick from, sir.", "expr": "sad"}
    kind, target, name = random.choice(pool)
    try:
        launch_path("steam://rungameid/%s" % target if kind == "steam" else target)
    except Exception:
        return {"say": "I picked %s but couldn't launch it, sir." % name, "expr": "sad"}
    return {"say": "How about %s, sir?" % name, "expr": "happy"}


@action("wake_server", "Wake the home server using the configured wake URL. No args.")
def wake_server(ctx, args):
    url = ctx.config.get("wake_url")
    if not url:
        return {"say": "There's no wake link set up yet, sir.", "expr": "sad"}
    import urllib.request
    try:
        urllib.request.urlopen(url, timeout=8).read()
    except Exception:
        return {"say": "The server didn't answer the wake request, sir.", "expr": "sad", "level": "warn"}
    return {"say": "Wake signal sent to the server, sir.", "expr": "happy"}


class Registry:
    def __init__(self, config):
        self.config = config
        self.user = {}
        self.sig = None
        self.errors = {}
        self.load_user()

    def folder_sig(self):
        out = []
        for path in sorted(glob.glob(os.path.join(str(actions_dir()), "*.py"))):
            try:
                out.append((path, os.path.getmtime(path)))
            except OSError:
                pass
        return out

    def load_user(self):
        sig = self.folder_sig()
        if sig == self.sig:
            return False
        self.sig = sig
        self.user = {}
        self.errors = {}
        for path, _ in sig:
            try:
                source = open(path, encoding="utf-8").read()
                if safety.action_code_is_spend(source):
                    self.errors[path] = "blocked: touches payments"
                    continue
                ns = {"__name__": "halcyon_action_" + os.path.basename(path)[:-3]}
                exec(compile(source, path, "exec"), ns)
                name = ns["NAME"]
                self.user[name] = {"description": ns.get("DESCRIPTION", ""), "fn": ns["run"],
                                   "confirm": bool(ns.get("CONFIRM", False)), "builtin": False, "path": path}
            except Exception:
                self.errors[path] = traceback.format_exc(limit=2)
        return True

    def all(self):
        self.load_user()
        merged = dict(BUILTIN)
        merged.update(self.user)
        return merged

    def describe(self):
        lines = []
        for name, a in self.all().items():
            lines.append("- %s: %s" % (name, a["description"]))
        return "\n".join(lines)

    def run(self, name, ctx, args):
        acts = self.all()
        a = acts.get(name)
        if not a:
            return None
        res = a["fn"](ctx, args or {})
        if isinstance(res, str):
            res = {"say": res}
        return res or {"say": "Done, sir."}

    def commit(self, message):
        git = shutil.which("git")
        if not git:
            return False
        folder = str(actions_dir())
        flags = 0x08000000 if sys.platform == "win32" else 0
        try:
            if not os.path.isdir(os.path.join(folder, ".git")):
                subprocess.run([git, "init"], cwd=folder, capture_output=True, creationflags=flags)
            subprocess.run([git, "add", "-A"], cwd=folder, capture_output=True, creationflags=flags)
            res = subprocess.run([git, "-c", "user.name=Halcyon", "-c", "user.email=halcyon@localhost",
                                  "-c", "commit.gpgsign=false", "commit", "-m", message],
                                 cwd=folder, capture_output=True, creationflags=flags)
            return res.returncode == 0
        except OSError:
            return False
