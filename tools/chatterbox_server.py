import argparse
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import perth
import torch
import torchaudio as ta
from chatterbox.tts import ChatterboxTTS

SEED = 1
EXAGGERATION = 0.5
CFG_WEIGHT = 0.5

lock = threading.Lock()
model = None


def synth(text):
    with lock:
        torch.manual_seed(SEED)
        wav = model.generate(text, exaggeration=EXAGGERATION, cfg_weight=CFG_WEIGHT)
        buf = io.BytesIO()
        ta.save(buf, wav.cpu(), model.sr, format="wav")
        return buf.getvalue()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok")
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        if self.path != "/tts":
            self.send_response(404)
            self.end_headers()
            return
        n = int(self.headers.get("Content-Length") or 0)
        try:
            text = json.loads(self.rfile.read(n).decode("utf-8")).get("text", "")
        except ValueError:
            text = ""
        if not text.strip():
            self.send_response(400)
            self.end_headers()
            return
        audio = synth(text)
        self.send_response(200)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(audio)))
        self.end_headers()
        self.wfile.write(audio)


def main():
    global model
    if getattr(perth, "PerthImplicitWatermarker", None) is None:
        perth.PerthImplicitWatermarker = perth.DummyWatermarker
    ap = argparse.ArgumentParser()
    ap.add_argument("--device", default="cpu")
    ap.add_argument("--port", type=int, default=8123)
    ap.add_argument("--threads", type=int, default=4)
    args = ap.parse_args()
    torch.set_num_threads(args.threads)
    model = ChatterboxTTS.from_pretrained(device=args.device)
    synth("Ready.")
    print("Chatterbox server ready on port %d" % args.port, flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
