import difflib
import queue
import re
import threading
import time

import numpy as np

from .vendor import whispercpp as W

CHUNK = 1600
RATE = 16000
LISTEN_TIMEOUT = 7.0
DEAF_AFTER_WAKE = 0.35
MAX_COMMAND_CHUNKS = 120
LEAD_INS = {"hey", "hi", "hello", "okay", "ok", "yo", "oi", "hay", "heya", "hiya", "hei", "o", "k"}
CANCEL_RE = re.compile(r"^(never ?mind|cancel|forget it|stop)\b", re.I)


def wake_check(text, wake_words, require_prefix=False, prefixes=None):
    leads = {p.lower() for p in prefixes} if prefixes else LEAD_INS
    tokens = list(re.finditer(r"[A-Za-z']+", text))
    if not tokens:
        return None, None
    start = 0
    while start < len(tokens) and tokens[start].group(0).lower() in leads:
        start += 1
    targets = [w.replace(" ", "").lower() for w in wake_words]
    best = (0.0, 0)
    for n in (1, 2, 3):
        if start + n > len(tokens):
            break
        head = "".join(t.group(0).lower() for t in tokens[start:start + n])
        for target in targets:
            score = 1.0 if head == target else difflib.SequenceMatcher(None, head, target).ratio()
            if score >= .74 and score > best[0] and head[0] == target[0]:
                best = (score, n)
    if not best[1]:
        return None, None
    end = tokens[start + best[1] - 1].end()
    rest = text[end:].lstrip(" ,.:;!?-")
    if require_prefix and start == 0:
        return "noprefix", rest
    return "ok", rest


def match_wake(text, wake_words, require_prefix=False, prefixes=None):
    kind, rest = wake_check(text, wake_words, require_prefix, prefixes)
    return rest if kind == "ok" else None


def log_wake(kind, text):
    try:
        from .config import config_dir
        path = config_dir() / "wake_log.txt"
        if path.exists() and path.stat().st_size > 100000:
            path.write_text("")
        label = "WOKE   " if kind == "ok" else "IGNORED (no hey/ok/yo before it)"
        with open(path, "a", encoding="utf-8") as f:
            f.write("%s  %s  heard: %s\n" % (time.strftime("%H:%M:%S"), label, text.strip()[:160]))
    except Exception:
        pass


class Listener:
    def __init__(self, config, on_command, on_level=None, on_status=None, on_wake=None, source=None):
        self.config = config
        self.on_command = on_command
        self.on_level = on_level or (lambda v: None)
        self.on_status = on_status or (lambda s: None)
        self.on_wake = on_wake or (lambda: None)
        self.source = source
        self.paused = False
        self.stop_flag = threading.Event()
        self.thread = None
        self.mode = "wake"
        self.dictate_requested = False
        self.answers = queue.Queue()
        self.answer_wanted = False
        self.engine_ok = False

    def start(self):
        if self.thread is not None:
            return
        self.stop_flag.clear()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_flag.set()

    def request_dictation(self):
        self.dictate_requested = True

    def wait_answer(self, timeout):
        while not self.answers.empty():
            try:
                self.answers.get_nowait()
            except queue.Empty:
                break
        self.answer_wanted = True
        try:
            return self.answers.get(timeout=timeout)
        except queue.Empty:
            return None
        finally:
            self.answer_wanted = False

    def _chunks(self):
        import sounddevice as sd
        with sd.RawInputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=CHUNK) as stream:
            while not self.stop_flag.is_set():
                data, _ = stream.read(CHUNK)
                yield bytes(data)

    def _prepare(self):
        names = {self.config.get("wake_model"), self.config.get("command_model")}
        for name in names:
            if name not in W.MODELS:
                raise W.WhisperError("Unknown Whisper model %s" % name)
            if not W.have_model(name):
                self.on_status("Downloading the %s voice model (%d MB)..." % (name, W.model_size_mb(name)))
                W.download_model(name)
        exe = W.find_server()
        if not exe:
            raise W.WhisperError("The Whisper program was not found.")
        gpu = bool(self.config.get("use_gpu"))
        wake = W.Server(exe, W.model_path(self.config.get("wake_model")), gpu)
        wake.extra = {"prompt": "Halcyon."}
        if self.config.get("command_model") == self.config.get("wake_model"):
            talk = wake
        else:
            talk = W.Server(exe, W.model_path(self.config.get("command_model")), gpu)
            talk.extra = {"prompt": "Halcyon."}
        wake.start()
        if talk is not wake:
            talk.start()
        self.engine_gpu = bool(wake.used_gpu)
        self.on_status("Speech recognition running on the %s." % ("GPU" if wake.used_gpu else "CPU"))
        return wake, talk

    def _run(self):
        try:
            self.on_status("Loading the voice engine...")
            wake_srv, talk_srv = self._prepare()
            self.engine_ok = True
            self.on_status("Listening for the wake word.")
        except Exception as exc:
            self.on_status("Voice engine problem: %s" % exc)
            return
        wake_seg = W.Segmenter(end_quiet=5, min_voiced=4, max_chunks=50, preroll=5)
        talk_seg = W.Segmenter(end_quiet=6, min_voiced=2, max_chunks=MAX_COMMAND_CHUNKS, floor=120.0, preroll=5)
        started = 0.0
        deaf_until = 0.0
        words = self.config.get("wake_words")
        try:
            chunks = self.source if self.source is not None else self._chunks()
            for data in chunks:
                if self.stop_flag.is_set():
                    break
                now = time.monotonic()
                samples = np.frombuffer(data, dtype="<i2").astype(np.float64)
                rms = float(np.sqrt(np.mean(samples * samples))) if len(samples) else 0.0
                self.on_level(min(1.0, rms / 3500.0))
                if self.paused or now < deaf_until:
                    continue
                if self.answer_wanted and self.mode != "answer":
                    self.mode = "answer"
                    talk_seg.reset()
                    started = now
                if self.mode == "wake":
                    if self.dictate_requested:
                        self.dictate_requested = False
                        self.mode = "dictate"
                        talk_seg.reset()
                        started = now
                        self.on_wake()
                        continue
                    seg = wake_seg.feed(data)
                    if seg is None:
                        continue
                    try:
                        text = wake_srv.transcribe(seg, timeout=30)
                    except W.WhisperError as exc:
                        self.on_status("Voice engine problem: %s" % exc)
                        continue
                    kind, rest = wake_check(text, words, bool(self.config.get("wake_prefix_required")),
                                            self.config.get("wake_prefixes"))
                    if kind is None:
                        continue
                    log_wake(kind, text)
                    if kind != "ok":
                        continue
                    self.on_wake()
                    if len(rest.split()) >= 2 or CANCEL_RE.match(rest):
                        self.on_command(rest)
                        continue
                    self.mode = "dictate"
                    talk_seg.reset()
                    started = time.monotonic()
                    deaf_until = started + DEAF_AFTER_WAKE
                    continue
                seg = talk_seg.feed(data)
                if seg is None:
                    if not talk_seg.active and now - started > LISTEN_TIMEOUT:
                        was = self.mode
                        self.mode = "wake"
                        wake_seg.reset()
                        if was == "answer":
                            self.answers.put("")
                        else:
                            self.on_command("")
                    continue
                try:
                    final = talk_srv.transcribe(seg)
                except W.WhisperError as exc:
                    self.on_status("Voice engine problem: %s" % exc)
                    final = ""
                was = self.mode
                self.mode = "wake"
                wake_seg.reset()
                if was == "answer":
                    self.answers.put(final)
                else:
                    self.on_command(final)
        except Exception as exc:
            self.on_status("Microphone problem: %s" % exc)
        finally:
            for srv in {wake_srv, talk_srv}:
                try:
                    srv.stop()
                except Exception:
                    pass
