import io
import json
import math
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
    "[Console]::Out.WriteLine('S');[Console]::Out.Flush();"
    "$s.Speak([Console]::In.ReadToEnd())"
)


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
        if sys.platform == "win32":
            try:
                import winsound
                winsound.PlaySound(None, winsound.SND_PURGE)
            except Exception:
                pass

    def _chatterbox_up(self):
        now = time.monotonic()
        if self.chatterbox_ok is not None and now - self.checked < 30:
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
            except Exception:
                self.chatterbox_ok = False
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
            audio = resp.read()
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

    def _speak_sapi(self, text, on_amp):
        self.engine_name = "sapi"
        self.proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", SAPI_SCRIPT],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=FLAGS)
        proc = self.proc

        def wait_start():
            try:
                proc.stdout.readline()
                self.started()
            except Exception:
                pass
        threading.Thread(target=wait_start, daemon=True).start()
        try:
            self.proc.stdin.write(text.encode("utf-8"))
            self.proc.stdin.close()
        except OSError:
            pass
        n = 0
        while self.proc.poll() is None and not self.stop_flag.is_set():
            n += 1
            on_amp(.35 + .65 * abs(math.sin(n * 1.7)) * abs(math.sin(n * .63 + 1)))
            time.sleep(.09)
        self.proc = None
