import json
import queue
import re
import socket
import struct
import threading
import time

COLOURS = {"info": (0, 255, 255), "warn": (255, 110, 0), "alert": (255, 0, 0)}
GOVEE_GROUP = "239.255.255.250"


DEFAULT_COLOUR = (30, 80, 255)


def apply_gain(rgb, gains):
    if not gains:
        return tuple(rgb)
    return tuple(max(0, min(255, int(round(c * g / 100.0)))) for c, g in zip(rgb, gains))


def scale(rgb, brightness):
    k = max(0, min(100, int(brightness))) / 100.0
    return tuple(int(round(c * k)) for c in rgb)


def parse_ips(text):
    return [p for p in re.split(r"[,\s;]+", text or "") if p]


class Govee:
    def __init__(self, ips=()):
        self.fixed = list(ips)
        self.ips = list(ips)

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

    def names(self):
        return ["Govee %s" % ip for ip in self.discover()]

    def colour(self, rgb, tint=None):
        for ip in self.discover():
            c = apply_gain(rgb, tint("Govee %s" % ip) if tint else None)
            self.send(ip, "turn", {"value": 1})
            self.send(ip, "colorwc", {"color": {"r": c[0], "g": c[1], "b": c[2]}, "colorTemInKelvin": 0})

    def refresh(self):
        self.ips = []


class OpenRGB:
    def __init__(self, host="127.0.0.1", port=6742, zone_leds=0):
        self.host = host
        self.port = port
        self.zone_leds = int(zone_leds or 0)
        self.client = None
        self.sized = False

    def grow_zones(self):
        if self.sized or self.zone_leds <= 0 or not self.connect():
            return
        self.sized = True
        try:
            from openrgb.utils import DeviceType, ZoneType
            for d in list(self.client.devices):
                if d.type != DeviceType.MOTHERBOARD:
                    continue
                for z in d.zones:
                    if z.type == ZoneType.LINEAR and len(z.leds) < self.zone_leds:
                        try:
                            z.resize(self.zone_leds)
                        except Exception:
                            pass
        except Exception:
            pass
        self.close()

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
        self.close()
        return list(self.client.devices) if self.connect() else []

    def names(self):
        out = [d.name for d in self.devices()]
        self.close()
        return out

    def colour(self, rgb, tint=None):
        from openrgb.utils import RGBColor
        self.grow_zones()
        for attempt in (1, 2):
            self.close()
            if not self.connect():
                time.sleep(0.4)
                continue
            try:
                for d in list(self.client.devices):
                    c = apply_gain(rgb, tint(d.name) if tint else None)
                    try:
                        d.set_color(RGBColor(*c))
                    except Exception:
                        try:
                            d.set_mode("direct")
                            d.set_color(RGBColor(*c))
                        except Exception:
                            pass
                return
            except Exception:
                continue
            finally:
                self.close()

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

    def apply_default(self):
        pass

    def restore(self):
        pass

    def close(self):
        pass


class Lights:
    def __init__(self, config, govee=None, openrgb=None, corsair=None):
        self.config = config
        self.backends = []
        explicit = [b for b in (govee, openrgb, corsair) if b is not None]
        if explicit:
            self.backends = explicit
        else:
            self.backends.append(Govee(parse_ips(config.get("govee_ips"))))
            if config.get("openrgb_enabled"):
                self.backends.append(OpenRGB(config.get("openrgb_host"), int(config.get("openrgb_port")), config.get("rgb_zone_leds")))
            if config.get("icue_enabled"):
                from .corsair import Corsair
                self.backends.append(Corsair())
        self.q = queue.Queue()
        self.active = False
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def enabled(self):
        return bool(self.config.get("rgb_enabled"))

    def default_colour(self):
        try:
            v = tuple(int(x) for x in self.config.get("rgb_default"))
            return v if len(v) == 3 else DEFAULT_COLOUR
        except (TypeError, ValueError):
            return DEFAULT_COLOUR

    def tint(self, name):
        tweaks = self.config.get("rgb_tweaks") or {}
        return tweaks.get(name)

    def show(self, level):
        if self.enabled():
            self.q.put(("show", level))

    def restore(self):
        if self.enabled():
            self.q.put(("restore", None))

    def apply_default(self):
        if self.enabled():
            self.q.put(("default", None))

    def device_names(self):
        out = []
        for b in self.backends:
            if hasattr(b, "names"):
                try:
                    out.extend(b.names())
                except Exception:
                    pass
        return out

    def close(self):
        self.q.put(("close", None))

    def shutdown(self):
        if self.enabled():
            self.q.put(("default", None))
        self.q.put(("close", None))
        self.thread.join(6)

    def paint(self, rgb):
        c = scale(rgb, self.config.get("rgb_brightness"))
        for b in self.backends:
            self.safe(b.colour, c, self.tint)

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
                    self.paint(COLOURS.get(arg, COLOURS["info"]))
                    self.active = True
                elif op == "restore" and self.active:
                    self.paint(self.default_colour())
                    self.active = False
                elif op == "default":
                    self.paint(self.default_colour())
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
    out.append("")
    try:
        from .corsair import Corsair
        c = Corsair()
        cdevs = c.devices()
        if cdevs:
            out.append("iCUE devices found: %d" % len(cdevs))
            for d in cdevs:
                out.append("  %s  leds %d" % (d.model, d.led_count))
        else:
            out.append("iCUE: not reachable or no devices. %s" % (c.error or ""))
        c.close()
    except Exception as exc:
        out.append("iCUE check failed: %s" % exc)
    return "\n".join(out) + "\n"
