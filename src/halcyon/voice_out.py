import io
import json
import math
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.request
import wave

import numpy as np

def split_chunks(text, first_max=45, max_chars=160):
    text = text.strip()
    sentences = [x.strip() for x in re.split(r"(?<=[.!?])\s+", text) if x.strip()]
    if not sentences:
        return [text]
    first = sentences[0]
    rest = sentences[1:]
    if len(first) > first_max:
        m = None
        for m in re.finditer(r"[,;:]\s+", first):
            if m.end() >= 18 and len(first) - m.end() >= 12:
                break
        if m is not None and m.end() >= 18 and len(first) - m.end() >= 12:
            rest.insert(0, first[m.end():].strip())
            first = first[:m.end()].strip()
    chunks = [first]
    for sent in rest:
        if len(chunks) > 1 and len(chunks[-1]) + 1 + len(sent) <= max_chars:
            chunks[-1] += " " + sent
        else:
            chunks.append(sent)
    return chunks


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
            with urllib.request.urlopen(self.config.get("chatterbox_url") + "/health", timeout=5):
                self.chatterbox_ok = True
        except Exception as exc:
            busy = self.chatterbox_ok is True and "timed out" in str(exc).lower()
            if not busy:
                self.chatterbox_ok = False
                self.note("Chatterbox health check failed: %s" % exc)
        return self.chatterbox_ok

    def note(self, text):
        self.last_error = text
        try:
            from .config import config_dir
            with open(config_dir() / "voice.log", "a", encoding="utf-8") as f:
                f.write("%s %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), text))
        except Exception:
            pass

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
            for attempt in (1, 2):
                try:
                    self._speak_chatterbox(text, on_amp)
                    return
                except Exception as exc:
                    self.note("Chatterbox speak failed (try %d): %s: %s" % (attempt, type(exc).__name__, exc))
                    if self.stop_flag.is_set():
                        return
                    time.sleep(0.4)
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

    def _fetch_chatterbox(self, text):
        req = urllib.request.Request(self.config.get("chatterbox_url") + "/tts",
                                     data=json.dumps({"text": text}).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            return ensure_pcm16(resp.read())

    def _speak_chatterbox(self, text, on_amp):
        self.engine_name = "chatterbox"
        parts = split_chunks(text)
        slots = [{"done": threading.Event(), "audio": None, "err": None} for _ in parts]

        def fetcher():
            for part, slot in zip(parts, slots):
                if self.stop_flag.is_set():
                    slot["done"].set()
                    continue
                try:
                    slot["audio"] = self._fetch_chatterbox(part)
                except Exception as exc:
                    slot["err"] = exc
                slot["done"].set()

        threading.Thread(target=fetcher, daemon=True).start()
        for i, slot in enumerate(slots):
            slot["done"].wait()
            if self.stop_flag.is_set():
                return
            if slot["err"] is not None:
                if i == 0:
                    raise slot["err"]
                return
            self._play_chatterbox(slot["audio"], on_amp)

    def _play_chatterbox(self, audio, on_amp):
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
