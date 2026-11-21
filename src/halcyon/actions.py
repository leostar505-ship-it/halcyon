import difflib
import glob
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
import traceback
from urllib.parse import quote

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


SHORTCUT_EXTS = (".lnk", ".url")


def start_menu_apps():
    if sys.platform != "win32":
        return {}
    menus = []
    for env in ("APPDATA", "PROGRAMDATA"):
        base = os.environ.get(env)
        if base:
            menus.append(os.path.join(base, "Microsoft", "Windows", "Start Menu", "Programs"))
    desktops = []
    for env, sub in (("USERPROFILE", "Desktop"), ("PUBLIC", "Desktop"), ("OneDrive", "Desktop")):
        base = os.environ.get(env)
        if base:
            desktops.append(os.path.join(base, sub))
    found = {}
    for root in desktops:
        for ext in SHORTCUT_EXTS:
            for path in glob.glob(os.path.join(root, "*" + ext)):
                found[_norm(os.path.splitext(os.path.basename(path))[0])] = path
    for root in menus:
        for ext in SHORTCUT_EXTS:
            for path in glob.glob(os.path.join(root, "**", "*" + ext), recursive=True):
                found[_norm(os.path.splitext(os.path.basename(path))[0])] = path
    return found


def epic_games():
    base = os.environ.get("PROGRAMDATA")
    if not base:
        return []
    games = []
    for path in glob.glob(os.path.join(base, "Epic", "EpicGamesLauncher", "Data", "Manifests", "*.item")):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        name = data.get("DisplayName")
        app = data.get("AppName")
        if not name or not app or data.get("bIsIncompleteInstall"):
            continue
        ns = data.get("CatalogNamespace") or ""
        cid = data.get("CatalogItemId") or ""
        ref = "%s:%s:%s" % (ns, cid, app) if ns and cid else app
        games.append({"name": name,
                      "uri": "com.epicgames.launcher://apps/%s?action=launch&silent=true" % quote(ref, safe=""),
                      "dir": data.get("InstallLocation") or ""})
    return games


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
            folder = re.search(r'"installdir"\s+"([^"]+)"', text)
            if name and appid and name.group(1) not in ("Steamworks Common Redistributables",):
                games[appid.group(1)] = {"name": name.group(1), "id": appid.group(1),
                                         "last": int(last.group(1)) if last else 0,
                                         "dir": os.path.join(lib, "common", folder.group(1)) if folder else ""}
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


RUN_EXTS = (".exe", ".bat", ".cmd", ".py")
SKIP_NAMES = re.compile(r"unins|uninst|crash|helper|setup|install|update|redist|vcredist|easyanticheat|eac|dxsetup|report|bootstrap|cef|webhelper", re.I)


def scan_roots():
    roots = []
    for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        v = os.environ.get(env)
        if v:
            roots.append(v)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(os.path.join(local, "Programs"))
    home = os.environ.get("USERPROFILE")
    if home:
        for sub in ("Desktop", "Downloads", "Documents"):
            roots.append(os.path.join(home, sub))
    if sys.platform == "win32":
        for letter in "CDEFGH":
            for sub in ("Games", "Epic Games", "SteamLibrary", "Program Files\\Epic Games"):
                roots.append("%s:\\%s" % (letter, sub))
    seen = []
    for r in roots:
        r = os.path.normcase(os.path.abspath(r))
        if r not in seen and os.path.isdir(r):
            seen.append(r)
    return seen


def scan_app(name, roots=None, max_depth=6, budget=5.0):
    q = _norm(name)
    if not q:
        return []
    started = time.monotonic()
    out = []
    for root in (roots if roots is not None else scan_roots()):
        base_depth = root.rstrip("\\/").count(os.sep)
        for cur, dirs, files in os.walk(root):
            if time.monotonic() - started > budget:
                return rank_hits(out)
            depth = cur.count(os.sep) - base_depth
            if depth >= max_depth:
                dirs[:] = []
            rel = os.path.relpath(cur, root)
            folders = [] if rel == "." else rel.split(os.sep)
            dir_hit = any(q in _norm(f) for f in folders)
            for f in files:
                stem, ext = os.path.splitext(f)
                if ext.lower() not in RUN_EXTS:
                    continue
                stem_hit = q in _norm(stem)
                if not stem_hit and not dir_hit:
                    continue
                if SKIP_NAMES.search(stem) and not _norm(stem) == q:
                    continue
                out.append({"path": os.path.join(cur, f), "stem_hit": stem_hit, "dir_hit": dir_hit,
                            "depth": depth, "ext": ext.lower(), "exact": _norm(stem) == q,
                            "folders": folders})
    return rank_hits(out)


def rank_hits(hits):
    order = {".exe": 0, ".bat": 1, ".cmd": 2, ".py": 3}
    return sorted(hits, key=lambda h: (not h["exact"], not h["stem_hit"], order[h["ext"]], h["depth"], len(h["path"])))


def epic_uri_for(hit):
    parts = hit["path"].replace("/", "\\").split("\\")
    for i, part in enumerate(parts):
        if part.lower() == "epic games" and i + 1 < len(parts) - 1:
            return "com.epicgames.launcher://apps/%s?action=launch&silent=true" % quote(parts[i + 1], safe="")
    return ""


def resolve_by_scan(name):
    hits = scan_app(name)
    if not hits:
        return None
    best = hits[0]
    uri = epic_uri_for(best)
    if uri:
        return ("epic", uri, _norm(name), hits)
    return ("path", best["path"], _norm(os.path.splitext(os.path.basename(best["path"]))[0]), hits)


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
    epic = {g["name"]: g for g in epic_games()}
    key = best_match(name, list(epic.keys()))
    if key:
        return ("epic", epic[key]["uri"], key)
    scanned = resolve_by_scan(name)
    if scanned:
        ctx_hits = scanned[3]
        try:
            ctx.last_scan = [h["path"] for h in ctx_hits]
        except Exception:
            pass
        return scanned[:3]
    return None


def launch_target(kind, target):
    if kind == "path" and target.lower().endswith(".py"):
        py = shutil.which("py") or shutil.which("python") or shutil.which("python3")
        if py:
            subprocess.Popen([py, target], cwd=os.path.dirname(target))
            return
    if kind == "path" and target.lower().endswith((".bat", ".cmd")):
        subprocess.Popen(["cmd", "/c", target], cwd=os.path.dirname(target)) if sys.platform == "win32" else subprocess.Popen([target], cwd=os.path.dirname(target))
        return
    launch_path("steam://rungameid/%s" % target if kind == "steam" else target)


def game_dir(kind, target):
    if kind == "steam":
        for g in steam_games():
            if g["id"] == target:
                return g.get("dir", "")
    if kind == "epic":
        for g in epic_games():
            if g["uri"] == target:
                return g.get("dir", "")
    return ""


def running_procs(kind, target, label):
    folder = game_dir(kind, target)
    if folder:
        root = os.path.normcase(os.path.abspath(folder)) + os.sep
        found = []
        for p in psutil.process_iter(["exe"]):
            try:
                exe = p.info.get("exe") or ""
            except Exception:
                continue
            if exe and os.path.normcase(os.path.abspath(exe)).startswith(root):
                found.append(p)
        return found
    return find_procs(None, label)


@action("open_app", "Open an app, program, folder shortcut or game by name. args: app")
def open_app(ctx, args):
    name = str(args.get("app", "")).strip()
    found = resolve_app(ctx, name)
    if not found:
        return {"say": "I couldn't find %s, sir." % name, "expr": "sad", "level": "info"}
    kind, target, label = found
    shown = label.title() if kind == "path" else label
    ask = getattr(ctx, "ask_yes_no", None)
    if ask:
        try:
            already = bool(running_procs(kind, target, label))
        except Exception:
            already = False
        if already and not ask("%s is already open, sir. Shall I open it again? Say yes or no." % shown,
                               "%s is already open. Open again?" % shown):
            return {"say": "Leaving %s as it is, sir." % shown, "expr": "neutral"}
    try:
        launch_target(kind, target)
    except Exception as exc:
        return {"say": "I couldn't open %s, sir." % shown, "expr": "sad", "detail": str(exc)}
    hits = getattr(ctx, "last_scan", None) or []
    detail = "\n".join(hits[:15]) if hits else ""
    return {"say": "Opening %s, sir." % shown if kind == "path" else "Launching %s, sir." % shown,
            "expr": "happy", "detail": detail}


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


@action("clear_history", "Forget the recent conversation history so the next request starts fresh. No args.")
def clear_history(ctx, args):
    ctx.clear_history()
    return {"say": "Conversation cleared, sir.", "expr": "neutral"}


NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
DANGEROUS_COMMAND = re.compile(
    r"\bformat\s+[a-z]:|\bdiskpart\b|\bmkfs\b|\brm\s+-\w*\s*/(?:\s|$)|\b(?:rd|rmdir|del)\b[^\n]*\s[a-z]:\\?\*?(?:\s|$)",
    re.I)


@action("run_command", "Run one shell command on this PC after the user approves it, then say what it printed. "
                       "args: command", confirm=True)
def run_command(ctx, args):
    cmd = str(args.get("command", "")).strip()
    if not cmd:
        return {"say": "I didn't catch a command, sir.", "expr": "sad"}
    if DANGEROUS_COMMAND.search(cmd) or safety.is_spend(cmd):
        return {"say": "I won't run that one, sir.", "expr": "suspicious", "level": "warn"}
    try:
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace",
                             timeout=30, cwd=os.path.expanduser("~"), creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return {"say": "That command took too long, sir, so I stopped it.", "expr": "sad", "level": "warn"}
    except OSError as exc:
        return {"say": "I couldn't run that, sir.", "expr": "sad", "detail": str(exc)}
    lines = [l.strip() for l in ((res.stdout or "") + "\n" + (res.stderr or "")).splitlines() if l.strip()]
    first = lines[0][:140] if lines else ""
    if res.returncode != 0:
        return {"say": ("That failed with code %d, sir. %s" % (res.returncode, first)).strip(), "expr": "sad",
                "level": "warn"}
    if not first:
        return {"say": "Done, sir.", "expr": "happy"}
    return {"say": first + (" There is more output, sir." if len(lines) > 1 else ""), "expr": "happy"}


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
        launch_target(kind, target)
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


def app_candidates(ctx):
    names = []
    for p in psutil.process_iter(["name", "exe"]):
        try:
            exe = (p.info.get("exe") or "").lower()
            if exe and "\\windows\\" not in exe:
                n = os.path.splitext(p.info.get("name") or "")[0]
                if n and n not in names:
                    names.append(n)
        except Exception:
            continue
    known = list((ctx.config.get("apps") or {}).keys()) + list(start_menu_apps().keys())
    known += [g["name"] for g in steam_games()] + [g["name"] for g in epic_games()]
    return names[:30], known[:60]


def unresolved_note(ctx, name, args, said):
    app = str(args.get("app", "")).strip()
    if name == "close_app" and not find_procs(ctx, app):
        running, known = app_candidates(ctx)
    elif name == "open_app" and not resolve_app(ctx, app):
        running, known = app_candidates(ctx)
    else:
        return ""
    return ("No app matches %s (speech recognition probably misheard). Running: %s. Installed: %s. "
            "Reply with the corrected route JSON for what they most likely meant, or a one-line chat question if truly unclear."
            % (json.dumps(app), ", ".join(running), ", ".join(known)))


GITHUB_COMMITS = "https://api.github.com/repos/leostar505-ship-it/halcyon/commits?per_page=8"


def github_updates():
    import urllib.request
    req = urllib.request.Request(GITHUB_COMMITS, headers={"User-Agent": "halcyon", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        data = json.loads(r.read().decode("utf-8"))
    return ["%s: %s" % ((c.get("commit", {}).get("author", {}).get("date") or "")[:10],
                        (c.get("commit", {}).get("message") or "").splitlines()[0]) for c in data]


@action("about_self", "Answer questions about Halcyon itself: its settings, what hardware the voice runs on, lights, version, "
        "or what changed in the latest update. args: question")
def about_self(ctx, args):
    from . import brain
    question = str(args.get("question", "")).strip()
    low = question.lower()
    facts = list(ctx.facts())
    if any(w in low for w in ("update", "patch", "change", "new", "latest", "version", "release")):
        ctx.say("Checking GitHub for the latest patch notes, sir.")
        try:
            facts.append("Recent updates, newest first: " + " | ".join(github_updates()))
        except Exception as exc:
            facts.append("GitHub could not be reached: %s" % exc)
    answer = brain.answer_from_facts(question, "\n".join(facts), ctx.config.get("router_model"))
    return {"say": answer or "I couldn't work that out, sir.", "expr": "happy"}
