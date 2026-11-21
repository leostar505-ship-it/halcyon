import io
import json
import math
import queue
import subprocess
import sys
import threading
import time
import urllib.request
import wave

import numpy as np

FLAGS = 0x08000000 if sys.platform == "win32" else 0

SAPI_SCRIPT = (
    "Add-Type -AssemblyName System.Speech;"
    "$s=New-Object System.Speech.Synthesis.SpeechSynthesizer;"
    "$pick=$null;"
    "foreach($v in $s.GetInstalledVoices()){$n=$v.VoiceInfo.Name;"
    "if($n -match 'George|Ryan|Hazel|Susan'){ $pick=$n; if($n -match 'George|Ryan'){break} }};"
    "if($pick){$s.SelectVoice($pick)};"
    "$s.Rate=0;"
    "[Console]::Out.WriteLine('R');[Console]::Out.Flush();"
    "while(($l=[Console]::In.ReadLine()) -ne $null){"
    "if($l.Trim() -eq ''){[Console]::Out.WriteLine('D');[Console]::Out.Flush();continue};"
    "[Console]::Out.WriteLine('S');[Console]::Out.Flush();"
    "$s.Speak($l);"
    "[Console]::Out.WriteLine('D');[Console]::Out.Flush()}"
)


def ensure_pcm16(data):
    try:
        with wave.open(io.BytesIO(data)) as w:
            w.getnframes()
        return data
    except (wave.Error, EOFError):
        pass
    pos = 12
    fmt = None
    raw = b""
    while pos + 8 <= len(data):
        cid = data[pos:pos + 4]
        size = int.from_bytes(data[pos + 4:pos + 8], "little")
        body = data[pos + 8:pos + 8 + size]
        if cid == b"fmt ":
            fmt = (int.from_bytes(body[0:2], "little"), int.from_bytes(body[2:4], "little"),
                   int.from_bytes(body[4:8], "little"), int.from_bytes(body[14:16], "little"))
        elif cid == b"data":
            raw = body
            break
        pos += 8 + size + (size & 1)
    if fmt is None or not raw:
        raise ValueError("unreadable audio from Chatterbox")
    tag, ch, rate, bits = fmt
    if bits == 32:
        arr = np.frombuffer(raw[:len(raw) // 4 * 4], dtype="<f4")
        pcm = (np.clip(arr, -1.0, 1.0) * 32767).astype("<i2")
    elif bits == 16:
        pcm = np.frombuffer(raw[:len(raw) // 2 * 2], dtype="<i2")
    else:
        raise ValueError("unsupported audio format from Chatterbox: %d bit" % bits)
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(ch)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.tobytes())
    return out.getvalue()


def statement(text):
    return " ".join(text.replace("?", ".").split())


class Voice:
    def __init__(self, config):
        self.config = config
        self.muted = bool(config.get("voice_muted"))
        self.proc = None
        self.stop_flag = threading.Event()
        self.chatterbox_ok = None
        self.checked = 0.0
        self.engine_name = "none"
        self.start_hook = None
        self.last_error = ""

    def set_muted(self, value):
        self.muted = bool(value)
        self.config.set("voice_muted", self.muted)

    def stop(self):
        self.stop_flag.set()
        p = self.proc
        if p is not None:
            try:
                p.terminate()
            except Exception:
                pass
            self.proc = None
        if sys.platform == "win32":
            try:
                import winsound
                winsound.PlaySound(None, winsound.SND_PURGE)
            except Exception:
                pass

    def _chatterbox_up(self):
        now = time.monotonic()
        if self.chatterbox_ok is not None and now - self.checked < (30 if self.chatterbox_ok else 3):
            return self.chatterbox_ok
        self.checked = now
        try:
            with urllib.request.urlopen(self.config.get("chatterbox_url") + "/health", timeout=1.5):
                self.chatterbox_ok = True
        except Exception:
            self.chatterbox_ok = False
        return self.chatterbox_ok

    def speak(self, text, on_amp=None):
        text = statement(text)
        if not text:
            return
        self.stop_flag.clear()
        on_amp = on_amp or (lambda v: None)
        if self.muted:
            self._simulate(text, on_amp)
            return
        if self._chatterbox_up():
            try:
                self._speak_chatterbox(text, on_amp)
                return
            except Exception as exc:
                self.chatterbox_ok = False
                self.last_error = "Chatterbox failed: %s" % exc
        if sys.platform == "win32":
            try:
                self._speak_sapi(text, on_amp)
                return
            except Exception:
                pass
        self._simulate(text, on_amp)

    def started(self):
        hook, self.start_hook = self.start_hook, None
        if hook:
            try:
                hook()
            except Exception:
                pass

    def _simulate(self, text, on_amp):
        self.started()
        end = time.monotonic() + max(1.0, len(text) / 15.0)
        n = 0
        while time.monotonic() < end and not self.stop_flag.is_set():
            n += 1
            on_amp(.35 + .65 * abs(math.sin(n * 1.7)) * abs(math.sin(n * .63 + 1)))
            time.sleep(.09)

    def _speak_chatterbox(self, text, on_amp):
        self.engine_name = "chatterbox"
        req = urllib.request.Request(self.config.get("chatterbox_url") + "/tts",
                                     data=json.dumps({"text": text}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            audio = ensure_pcm16(resp.read())
        if self.stop_flag.is_set():
            return
        with wave.open(io.BytesIO(audio)) as w:
            rate = w.getframerate()
            ch = w.getnchannels()
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float64)
        if ch > 1:
            pcm = pcm.reshape(-1, ch).mean(axis=1)
        step = max(1, int(rate * .05))
        env = [float(np.sqrt(np.mean(pcm[i:i + step] ** 2))) / 12000.0 for i in range(0, len(pcm), step)]
        done = threading.Event()

        def feed():
            t0 = time.monotonic()
            while not done.is_set() and not self.stop_flag.is_set():
                i = int((time.monotonic() - t0) / .05)
                if i >= len(env):
                    break
                on_amp(min(1.0, env[i]))
                time.sleep(.04)

        threading.Thread(target=feed, daemon=True).start()
        self.started()
        try:
            if sys.platform == "win32":
                import winsound
                winsound.PlaySound(audio, winsound.SND_MEMORY)
            else:
                time.sleep(len(pcm) / rate)
        finally:
            done.set()

    def _sapi_start(self):
        if self.proc is not None and self.proc.poll() is None:
            return self.proc
        proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", SAPI_SCRIPT],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=FLAGS)
        lines = queue.Queue()

        def reader():
            try:
                for line in proc.stdout:
                    lines.put(line.decode("utf-8", "replace").strip())
            except Exception:
                pass
            lines.put(None)
        threading.Thread(target=reader, daemon=True).start()
        self.proc = proc
        self.sapi_lines = lines
        return proc

    def warm_up(self):
        if sys.platform == "win32" and not self.muted:
            try:
                self._sapi_start()
            except Exception:
                pass

    def _speak_sapi(self, text, on_amp):
        self.engine_name = "sapi"
        proc = self._sapi_start()
        lines = self.sapi_lines
        try:
            proc.stdin.write((" ".join(text.split()) + "\n").encode("utf-8"))
            proc.stdin.flush()
        except OSError:
            self.proc = None
            raise
        n = 0
        while not self.stop_flag.is_set():
            try:
                line = lines.get(timeout=.09)
            except queue.Empty:
                line = ""
            if line is None:
                self.proc = None
                break
            if line == "S":
                self.started()
            elif line == "D":
                break
            elif line == "R":
                continue
            elif proc.poll() is None:
                n += 1
                on_amp(.35 + .65 * abs(math.sin(n * 1.7)) * abs(math.sin(n * .63 + 1)))
