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
    def __init__(self, ips=(), on_found=None):
        self.fixed = list(ips)
        self.ips = list(ips)
        self.on_found = on_found
        self.next_scan = 0.0

    def local_ips(self):
        out = []
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                ip = info[4][0]
                if not ip.startswith(("127.", "169.254.")) and ip not in out:
                    out.append(ip)
        except OSError:
            pass
        try:
            t = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            t.connect(("10.255.255.255", 1))
            ip = t.getsockname()[0]
            t.close()
            if ip not in out and not ip.startswith("127."):
                out.insert(0, ip)
        except OSError:
            pass
        return out

    def _listener(self):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("", 4002))
        try:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                         struct.pack("4sl", socket.inet_aton(GOVEE_GROUP), socket.INADDR_ANY))
        except OSError:
            pass
        for ip in self.local_ips():
            try:
                s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                             struct.pack("4s4s", socket.inet_aton(GOVEE_GROUP), socket.inet_aton(ip)))
            except OSError:
                pass
        s.settimeout(0.3)
        return s

    def collect(self, s, found, seconds):
        end = time.time() + seconds
        while time.time() < end:
            try:
                data, addr = s.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                d = json.loads(data.decode("utf-8", "replace"))["msg"]
            except (ValueError, KeyError):
                continue
            if d.get("cmd") == "scan":
                data_ = d.get("data") or {}
                ip = data_.get("ip") or addr[0]
                data_["ip"] = ip
                found[ip] = data_

    def scan(self, wait=2.0):
        found = {}
        s = self._listener()
        msg = json.dumps({"msg": {"cmd": "scan", "data": {"account_topic": "reserved"}}}).encode()
        try:
            try:
                s.sendto(msg, (GOVEE_GROUP, 4001))
            except OSError:
                pass
            self.collect(s, found, 1.2)
            if not found:
                for ip in self.local_ips():
                    try:
                        s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
                        s.sendto(msg, (GOVEE_GROUP, 4001))
                    except OSError:
                        pass
                self.collect(s, found, wait)
            if not found:
                for ip in self.local_ips():
                    base = ip.rsplit(".", 1)[0]
                    for n in range(1, 255):
                        try:
                            s.sendto(msg, ("%s.%d" % (base, n), 4001))
                        except OSError:
                            pass
                self.collect(s, found, wait + 1.5)
        finally:
            s.close()
        return list(found.values())

    def discover(self):
        if not self.ips and time.monotonic() >= self.next_scan:
            self.ips = [d["ip"] for d in self.scan()]
            if not self.ips:
                self.next_scan = time.monotonic() + 15
            elif self.on_found:
                try:
                    self.on_found(self.ips)
                except Exception:
                    pass
        return self.ips

    def send(self, ip, cmd, data):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.sendto(json.dumps({"msg": {"cmd": cmd, "data": data}}).encode(), (ip, 4003))
        finally:
            s.close()

    def dev_status(self, ip):
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
        self.ips = list(self.fixed)
        self.next_scan = 0.0

    def status(self):
        if self.ips:
            return "%d light(s): %s" % (len(self.ips), ", ".join(self.ips))
        return "no lights found (turn on LAN Control in the Govee Home app and allow Halcyon in the Windows firewall)"


class OpenRGB:
    def __init__(self, host="127.0.0.1", port=6742, zone_leds=0):
        self.host = host
        self.port = port
        self.zone_leds = int(zone_leds or 0)
        self.client = None
        self.sized = False
        self.count = 0
        self.detail = []
        self.error = ""

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
        last = None
        for attempt in (1, 2):
            if not self.connect():
                last = RuntimeError("could not connect to OpenRGB at %s:%s (is OpenRGB open with its SDK server on?)" % (self.host, self.port))
                self.error = str(last)
                time.sleep(0.3)
                continue
            try:
                n = 0
                detail = []
                for d in list(self.client.devices):
                    c = apply_gain(rgb, tint(d.name) if tint else None)
                    try:
                        d.set_color(RGBColor(*c))
                        detail.append("%s ok" % d.name)
                    except Exception as exc1:
                        try:
                            d.set_mode("direct")
                            d.set_color(RGBColor(*c))
                            detail.append("%s ok after direct mode" % d.name)
                        except Exception as exc2:
                            detail.append("%s FAILED %s" % (d.name, exc2 or exc1))
                    n += 1
                self.count = n
                self.detail = detail
                self.error = ""
                return
            except Exception as exc:
                last = exc
                self.close()
        if last:
            raise last

    def status(self):
        if self.client is not None:
            return "connected, %d device(s): %s" % (len(self.client.devices), "; ".join(self.detail) or "no paint yet")
        if self.error:
            return "last error: %s" % self.error
        return "idle, %d device(s) last painted: %s" % (self.count, "; ".join(self.detail) or "nothing yet")

    def close(self):
        try:
            if self.client:
                self.client.disconnect()
        except Exception:
            pass
        self.client = None

    def reset(self):
        import struct
        self.close()
        try:
            s = socket.create_connection((self.host, self.port), 2)
            s.sendall(b"ORGB" + struct.pack("<III", 0, 140, 0))
            s.close()
            time.sleep(1.5)
        except OSError:
            pass
        self.sized = False
        self.error = ""


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
            self.backends.append(Govee(parse_ips(config.get("govee_ips")) or list(config.get("govee_cache") or []), self.remember_govee))
            if config.get("openrgb_enabled"):
                self.backends.append(OpenRGB(config.get("openrgb_host"), int(config.get("openrgb_port")), config.get("rgb_zone_leds")))
            if config.get("icue_enabled"):
                from .corsair import Corsair
                self.backends.append(Corsair())
        self.q = queue.Queue()
        self.active = False
        self.last = None
        self.results = {}
        self.logfile = None
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

    def refresh(self):
        if self.enabled():
            self.q.put(("refresh", None))

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

    def set_govee_ips(self, text):
        for b in self.backends:
            if isinstance(b, Govee):
                ips = parse_ips(text)
                b.fixed = list(ips)
                b.ips = list(ips)
                b.next_scan = 0.0

    def remember_govee(self, ips):
        try:
            self.config.set("govee_cache", list(ips))
        except Exception:
            pass

    def label(self, b):
        return type(b).__name__

    def log(self, text):
        try:
            if self.logfile is None:
                from .config import config_dir
                self.logfile = config_dir() / "lights.log"
            if self.logfile.exists() and self.logfile.stat().st_size > 200000:
                self.logfile.write_text("")
            with open(self.logfile, "a", encoding="utf-8") as f:
                f.write("%s %s\n" % (time.strftime("%H:%M:%S"), text))
        except Exception:
            pass

    def paint_one(self, b, c):
        name = self.label(b)
        t0 = time.monotonic()
        try:
            b.colour(c, self.tint)
            self.results[name] = "ok %.0f ms" % ((time.monotonic() - t0) * 1000)
        except Exception as exc:
            self.results[name] = "error: %s" % exc
        self.log("%s %s %s" % (name, c, self.results[name]))

    def paint(self, rgb, only_local=False):
        c = scale(rgb, self.config.get("rgb_brightness"))
        self.last = rgb
        targets = [b for b in self.backends if not (only_local and isinstance(b, Govee))]
        threads = [threading.Thread(target=self.paint_one, args=(b, c), daemon=True) for b in targets]
        for t in threads:
            t.start()
        for t in threads:
            t.join(8)

    def status(self):
        out = {}
        for b in self.backends:
            name = self.label(b)
            line = self.results.get(name, "not used yet")
            if hasattr(b, "status"):
                try:
                    line = "%s | %s" % (b.status(), line)
                except Exception:
                    pass
            out[name] = line
        return out

    def next_op(self):
        try:
            wait = float(self.config.get("rgb_keepalive") or 0)
        except (KeyError, TypeError, ValueError):
            wait = 0.0
        try:
            op = self.q.get(timeout=wait) if wait > 0 else self.q.get()
        except queue.Empty:
            return ("beat", None)
        paint_ops = ("show", "restore", "default")
        while True:
            try:
                nxt = self.q.get_nowait()
            except queue.Empty:
                return op
            if nxt == op:
                continue
            if op[0] in paint_ops and nxt[0] in paint_ops:
                op = nxt
                continue
            self.q.queue.appendleft(nxt)
            return op

    def loop(self):
        while True:
            op, arg = self.next_op()
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
                elif op == "refresh":
                    for b in self.backends:
                        fn = getattr(b, "reset", None) or getattr(b, "refresh", None)
                        if fn:
                            try:
                                fn()
                            except Exception:
                                pass
                    self.paint(self.last if self.last is not None else self.default_colour())
                elif op == "beat" and self.enabled():
                    self.paint(self.last if self.last is not None else self.default_colour(), only_local=True)
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
