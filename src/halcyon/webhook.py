import collections
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

KIND_MAP = {
    "authenticationsuccess": "login", "userauthenticated": "login", "sessionstart": "login", "login": "login",
    "authenticationfailure": "failed", "authenticationfailed": "failed", "failedlogin": "failed", "failed": "failed",
    "playbackstart": "playback", "playback": "playback",
    "itemadded": "added", "added": "added",
    "serverrestartrequired": "shutdown", "servershutdown": "shutdown", "server_shutdown": "shutdown",
    "shutdown": "shutdown", "serverstop": "shutdown",
}

LEVEL = {"login": "warn", "failed": "alert", "shutdown": "alert", "playback": "info", "added": "info", "other": "info"}
EXPR = {"login": "curious", "failed": "suspicious", "shutdown": "sad", "playback": "happy", "added": "happy",
        "other": "curious"}


def first(payload, *keys):
    for k in keys:
        v = payload.get(k)
        if v not in (None, ""):
            return str(v)
    return ""


def normalise(payload):
    if not isinstance(payload, dict):
        return None
    raw = first(payload, "event", "NotificationType", "notification_type", "type")
    kind = KIND_MAP.get(raw.replace(" ", "").replace("-", "").lower(), "other")
    ip = first(payload, "ip", "RemoteEndPoint", "remote_end_point", "address")
    ip = ip.split(":")[0] if ip.count(":") == 1 else ip
    return {
        "kind": kind,
        "raw": raw,
        "user": first(payload, "user", "NotificationUsername", "Username", "username"),
        "device": first(payload, "device", "DeviceName", "device_name"),
        "ip": ip,
        "app": first(payload, "app", "client", "ClientName", "client_name"),
        "item": first(payload, "item", "ItemName", "Name", "title"),
        "message": first(payload, "message", "Description"),
        "at": time.time(),
    }


class Suppressor:
    def __init__(self, cooldown=120, escalate=5):
        self.cooldown = cooldown
        self.escalate = escalate
        self.seen = collections.defaultdict(collections.deque)
        self.escalated = {}
        self.lock = threading.Lock()

    def check(self, ev, now=None):
        now = now if now is not None else time.monotonic()
        key = (ev["kind"], ev["user"].lower(), ev["device"].lower() if ev["kind"] == "playback" else "")
        with self.lock:
            q = self.seen[key]
            while q and now - q[0] > self.cooldown:
                q.popleft()
            first_in_window = not q
            q.append(now)
            if first_in_window:
                self.escalated.pop(key, None)
                return True, ev
            if ev["kind"] == "failed" and len(q) >= self.escalate and key not in self.escalated:
                self.escalated[key] = True
                ev = dict(ev)
                ev["message"] = "%d failed logins in the last %d minutes" % (len(q), max(1, self.cooldown // 60))
                ev["repeated"] = len(q)
                return True, ev
            return False, ev


def card_lines(ev):
    kind = ev["kind"]
    clock = time.strftime("%H:%M", time.localtime(ev.get("at", time.time())))
    lines = []
    if kind == "shutdown":
        lines = [("Server", "shut down"), ("Since", clock), ("Status", "offline")]
    elif kind == "failed":
        lines = [("Login", "failed"), ("User", ev["user"] or "unknown"), ("Device", ev["device"] or "unknown"),
                 ("IP", ev["ip"] or "unknown")]
    elif kind == "playback":
        lines = [("User", ev["user"] or "unknown"), ("Playing", ev["item"] or "a film"),
                 ("Device", ev["device"] or "unknown"), ("App", ev["app"] or "Jellyfin")]
    elif kind == "added":
        lines = [("Added", ev["item"] or "new item"), ("Library", "Jellyfin")]
    else:
        lines = [("User", ev["user"] or "unknown"), ("Device", ev["device"] or "unknown"),
                 ("IP", ev["ip"] or "unknown"), ("App", ev["app"] or "unknown")]
    return [(a, b) for a, b in lines if b]


class WebhookServer:
    def __init__(self, config, on_event, on_status=None):
        self.config = config
        self.on_event = on_event
        self.on_status = on_status or (lambda s: None)
        self.httpd = None
        self.thread = None

    def start(self):
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, code, body=b"ok"):
                self.send_response(code)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def authorised(self):
                token = outer.config.get("webhook_token")
                if not token:
                    return True
                q = parse_qs(urlparse(self.path).query)
                return self.headers.get("X-Halcyon-Token") == token or (q.get("token") or [""])[0] == token

            def do_GET(self):
                self.reply(200, b"halcyon webhook ready")

            def do_POST(self):
                if not self.authorised():
                    self.reply(401, b"unauthorised")
                    return
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(min(n, 65536)) if n else b""
                try:
                    payload = json.loads(body.decode("utf-8", "replace"))
                except ValueError:
                    self.reply(400, b"bad json")
                    return
                ev = normalise(payload)
                if ev is None:
                    self.reply(400, b"bad payload")
                    return
                self.reply(200)
                threading.Thread(target=outer.on_event, args=(ev,), daemon=True).start()

        port = int(self.config.get("webhook_port"))
        try:
            self.httpd = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        except OSError as exc:
            self.on_status("Webhook port %d unavailable: %s" % (port, exc))
            return False
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.on_status("Webhook listening on port %d" % port)
        return True

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
        self.httpd = None
