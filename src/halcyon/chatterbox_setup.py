import os
import pathlib
import shutil
import subprocess
import sys
import urllib.request

from .config import config_dir

FLAGS = 0x08000000 if sys.platform == "win32" else 0
GOOD = ("3.11", "3.10", "3.12")


def root():
    path = config_dir() / "chatterbox"
    path.mkdir(parents=True, exist_ok=True)
    return path


def venv_python():
    sub = ("Scripts", "python.exe") if sys.platform == "win32" else ("bin", "python")
    return root() / "venv" / sub[0] / sub[1]


def installed():
    py = venv_python()
    if not py.exists():
        return False
    return (root() / "ready").exists()


def server_script():
    here = pathlib.Path(__file__).resolve()
    meipass = getattr(sys, "_MEIPASS", None)
    cands = []
    if meipass:
        cands.append(pathlib.Path(meipass) / "tools" / "chatterbox_server.py")
    cands.append(pathlib.Path(sys.executable).parent / "_internal" / "tools" / "chatterbox_server.py")
    cands.append(here.parents[2] / "tools" / "chatterbox_server.py")
    for c in cands:
        if c.exists():
            return c
    return None


def find_python():
    if sys.platform != "win32":
        for v in GOOD:
            exe = shutil.which("python%s" % v)
            if exe:
                return [exe]
        return None
    for v in GOOD:
        try:
            r = subprocess.run(["py", "-%s" % v, "-c", "import sys;print(sys.version)"], capture_output=True,
                               text=True, creationflags=FLAGS, timeout=20)
            if r.returncode == 0:
                return ["py", "-%s" % v]
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def stream(cmd, log, cwd=None):
    log("> " + " ".join(cmd))
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                         errors="replace", cwd=cwd, creationflags=FLAGS)
    last = ""
    for line in p.stdout:
        line = line.strip()
        if line and line != last and (line.startswith(("Collecting", "Downloading", "Installing", "Successfully", "ERROR", "Using cached")) or "error" in line.lower()):
            log(line[:140])
            last = line
    return p.wait()


def install(log):
    py = find_python()
    if not py and sys.platform == "win32":
        log("Python 3.11 not found. Installing it with winget (this can take a few minutes)...")
        code = stream(["winget", "install", "-e", "--id", "Python.Python.3.11", "--silent",
                       "--accept-package-agreements", "--accept-source-agreements"], log)
        if code != 0:
            log("winget could not install Python. Install Python 3.11 from python.org, then press Install again.")
            return False
        py = find_python()
    if not py:
        log("Python 3.10 to 3.12 is required but was not found.")
        return False
    vdir = root() / "venv"
    if not venv_python().exists():
        log("Creating a private Python environment...")
        if stream(py + ["-m", "venv", str(vdir)], log) != 0:
            log("Could not create the environment.")
            return False
    vp = str(venv_python())
    log("Installing Chatterbox. This downloads about 2 GB and can take 5 to 15 minutes.")
    if stream([vp, "-m", "pip", "install", "--upgrade", "pip"], log) != 0:
        return False
    if stream([vp, "-m", "pip", "install", "chatterbox-tts"], log) != 0:
        log("Install failed. See the lines above.")
        return False
    (root() / "ready").write_text("ok")
    log("Chatterbox installed.")
    return True


class Server:
    def __init__(self, config):
        self.config = config
        self.proc = None

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, log=lambda s: None):
        if self.running():
            return True
        script = server_script()
        if not installed() or not script:
            log("Chatterbox is not installed yet.")
            return False
        port = self.config.get("chatterbox_url").rsplit(":", 1)[-1].strip("/")
        logf = open(root() / "server.log", "a", encoding="utf-8")
        self.proc = subprocess.Popen([str(venv_python()), str(script), "--device", "cpu", "--port", port, "--threads", "4"],
                                     stdout=logf, stderr=logf, creationflags=FLAGS)
        log("Chatterbox is starting. The first start loads the model and can take a minute or two.")
        return True

    def stop(self):
        if self.proc is not None:
            try:
                self.proc.terminate()
            except OSError:
                pass
        self.proc = None

    def healthy(self):
        try:
            with urllib.request.urlopen(self.config.get("chatterbox_url") + "/health", timeout=1.5):
                return True
        except Exception:
            return False
