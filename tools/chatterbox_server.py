import argparse
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import perth
import numpy as np
import torch
import wave
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
        arr = wav.detach().float().cpu().numpy().reshape(-1)
        pcm = (np.clip(arr, -1.0, 1.0) * 32767).astype("<i2")
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(int(model.sr))
            w.writeframes(pcm.tobytes())
        return buf.getvalue()


INFO = ["ok device=cpu"]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(INFO[0].encode())
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
    device = args.device
    if device == "cuda":
        if not torch.cuda.is_available():
            print("GPU not available, using CPU", flush=True)
            device = "cpu"
        else:
            pick = 0
            for i in range(torch.cuda.device_count()):
                name = torch.cuda.get_device_name(i)
                print("GPU %d: %s" % (i, name), flush=True)
                if "Graphics" not in name:
                    pick = i
            torch.cuda.set_device(pick)
            print("Using GPU %d: %s" % (pick, torch.cuda.get_device_name(pick)), flush=True)
            INFO[0] = "ok device=gpu name=%s" % torch.cuda.get_device_name(pick)
    model = ChatterboxTTS.from_pretrained(device=device)
    synth("Ready.")
    print("Chatterbox server ready on port %d" % args.port, flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
