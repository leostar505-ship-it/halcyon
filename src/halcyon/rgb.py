import json
import queue
import re
import socket
import struct
import threading
import time

COLOURS = {"info": (0, 255, 255), "warn": (255, 190, 0), "alert": (255, 0, 0)}
GOVEE_GROUP = "239.255.255.250"


def scale(rgb, brightness):
    k = max(0, min(100, int(brightness))) / 100.0
    return tuple(int(round(c * k)) for c in rgb)


def parse_ips(text):
    return [p for p in re.split(r"[,\s;]+", text or "") if p]


class Govee:
    def __init__(self, ips=()):
        self.ips = list(ips)
        self.saved = {}

    def _listener(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("", 4002))
        try:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                         struct.pack("4sl", socket.inet_aton(GOVEE_GROUP), socket.INADDR_ANY))
        except OSError:
            pass
        s.settimeout(0.4)
        return s

    def scan(self, wait=2.0):
        found = {}
        s = self._listener()
        try:
            msg = json.dumps({"msg": {"cmd": "scan", "data": {"account_topic": "reserved"}}}).encode()
            s.sendto(msg, (GOVEE_GROUP, 4001))
            end = time.time() + wait
            while time.time() < end:
                try:
                    data, _ = s.recvfrom(4096)
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    d = json.loads(data.decode("utf-8", "replace"))["msg"]
                except (ValueError, KeyError):
                    continue
                if d.get("cmd") == "scan" and d.get("data", {}).get("ip"):
                    found[d["data"]["ip"]] = d["data"]
        finally:
            s.close()
        return list(found.values())

    def discover(self):
        if not self.ips:
            self.ips = [d["ip"] for d in self.scan()]
        return self.ips

    def send(self, ip, cmd, data):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.sendto(json.dumps({"msg": {"cmd": cmd, "data": data}}).encode(), (ip, 4003))
        finally:
            s.close()

    def status(self, ip):
        s = self._listener()
        try:
            s.sendto(json.dumps({"msg": {"cmd": "devStatus", "data": {}}}).encode(), (ip, 4003))
            end = time.time() + 1.5
            while time.time() < end:
                try:
                    data, addr = s.recvfrom(4096)
                except socket.timeout:
                    continue
                try:
                    d = json.loads(data.decode("utf-8", "replace"))["msg"]
                except (ValueError, KeyError):
                    continue
                if d.get("cmd") == "devStatus":
                    return d.get("data")
        except OSError:
            return None
        finally:
            s.close()
        return None

    def snapshot(self):
        for ip in self.discover():
            if ip not in self.saved:
                st = self.status(ip)
                if st:
                    self.saved[ip] = st

    def colour(self, rgb):
        for ip in self.discover():
            self.send(ip, "turn", {"value": 1})
            self.send(ip, "colorwc", {"color": {"r": rgb[0], "g": rgb[1], "b": rgb[2]}, "colorTemInKelvin": 0})

    def restore(self):
        for ip, st in list(self.saved.items()):
            if not st.get("onOff", 1):
                self.send(ip, "turn", {"value": 0})
                continue
            kelvin = st.get("colorTemInKelvin", 0)
            c = st.get("color") or {"r": 255, "g": 255, "b": 255}
            self.send(ip, "turn", {"value": 1})
            self.send(ip, "colorwc", {"color": c, "colorTemInKelvin": kelvin})
            if "brightness" in st:
                self.send(ip, "brightness", {"value": st["brightness"]})
        self.saved.clear()


class OpenRGB:
    def __init__(self, host="127.0.0.1", port=6742):
        self.host = host
        self.port = port
        self.client = None
        self.saved = {}

    def connect(self):
        if self.client is not None:
            return True
        try:
            from openrgb import OpenRGBClient
            self.client = OpenRGBClient(self.host, self.port, name="Halcyon")
            return True
        except Exception:
            self.client = None
            return False

    def devices(self):
        return list(self.client.devices) if self.connect() else []

    def snapshot(self):
        if not self.connect():
            return
        for d in self.client.devices:
            if d.name not in self.saved:
                try:
                    self.saved[d.name] = list(d.colors)
                except Exception:
                    pass

    def colour(self, rgb):
        if not self.connect():
            return
        from openrgb.utils import RGBColor
        c = RGBColor(*rgb)
        for d in self.client.devices:
            try:
                try:
                    d.set_mode("direct")
                except Exception:
                    pass
                d.set_color(c)
            except Exception:
                pass

    def restore(self):
        if not self.client:
            return
        for d in self.client.devices:
            cols = self.saved.get(d.name)
            if cols:
                try:
                    d.set_colors(cols)
                except Exception:
                    pass
        self.saved.clear()

    def close(self):
        try:
            if self.client:
                self.client.disconnect()
        except Exception:
            pass
        self.client = None


class NullLights:
    def show(self, level):
        pass

    def restore(self):
        pass

    def close(self):
        pass


class Lights:
    def __init__(self, config, govee=None, openrgb=None):
        self.config = config
        self.backends = []
        if govee is not None or openrgb is not None:
            self.backends = [b for b in (govee, openrgb) if b is not None]
        else:
            self.backends.append(Govee(parse_ips(config.get("govee_ips"))))
            if config.get("openrgb_enabled"):
                self.backends.append(OpenRGB(config.get("openrgb_host"), int(config.get("openrgb_port"))))
        self.q = queue.Queue()
        self.active = False
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def enabled(self):
        return bool(self.config.get("rgb_enabled"))

    def show(self, level):
        if self.enabled():
            self.q.put(("show", level))

    def restore(self):
        if self.enabled():
            self.q.put(("restore", None))

    def close(self):
        self.q.put(("close", None))

    def shutdown(self):
        if self.enabled():
            self.q.put(("restore", None))
        self.q.put(("close", None))
        self.thread.join(4)

    def loop(self):
        while True:
            op, arg = self.q.get()
            if op == "close":
                for b in self.backends:
                    if hasattr(b, "close"):
                        try:
                            b.close()
                        except Exception:
                            pass
                return
            try:
                if op == "show":
                    rgb = scale(COLOURS.get(arg, COLOURS["info"]), self.config.get("rgb_brightness"))
                    for b in self.backends:
                        if not self.active:
                            self.safe(b.snapshot)
                        self.safe(b.colour, rgb)
                    self.active = True
                elif op == "restore" and self.active:
                    for b in self.backends:
                        self.safe(b.restore)
                    self.active = False
            except Exception:
                pass

    def safe(self, fn, *args):
        try:
            fn(*args)
        except Exception:
            pass


def scan_report(config):
    out = ["Halcyon lights scan", ""]
    g = Govee(parse_ips(config.get("govee_ips")))
    try:
        found = g.scan(2.5)
    except OSError as exc:
        found = []
        out.append("Govee scan error: %s" % exc)
    out.append("Govee devices found: %d" % len(found))
    for d in found:
        out.append("  %s  model %s  ip %s" % (d.get("device", "?"), d.get("sku", "?"), d.get("ip", "?")))
    if not found:
        out.append("  none. In the Govee Home app turn on LAN Control for each light (Device settings), and allow Halcyon through the Windows firewall.")
    out.append("")
    o = OpenRGB(config.get("openrgb_host"), int(config.get("openrgb_port")))
    if not o.connect():
        out.append("OpenRGB: could not connect to %s:%s. Open OpenRGB, go to the SDK Server tab and press Start Server." %
                   (o.host, o.port))
    else:
        devs = o.devices()
        out.append("OpenRGB devices found: %d" % len(devs))
        gpu = False
        for d in devs:
            name = str(d.name)
            kind = str(getattr(d, "type", ""))
            leds = len(getattr(d, "leds", []))
            out.append("  %s  type %s  leds %d" % (name, kind, leds))
            if "gpu" in kind.lower() or re.search(r"radeon|9070|gigabyte", name, re.I):
                gpu = True
        out.append("")
        out.append("GPU lighting visible to OpenRGB: %s" % ("yes" if gpu else "no"))
        o.close()
    return "\n".join(out) + "\n"
