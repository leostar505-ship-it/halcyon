import threading
import time

GAIN = (1.0, 0.8, 1.0)
SAT = 0.45


def correct(rgb):
    lo = min(rgb)
    c = [v - SAT * lo for v in rgb]
    return tuple(max(0, min(255, int(round(v * g)))) for v, g in zip(c, GAIN))


class Corsair:
    def __init__(self):
        self.sdk = None
        self.ok = False
        self.error = ""
        self.next_try = 0.0

    def connect(self, timeout=3.0):
        if self.ok and self.sdk is not None:
            return True
        if time.monotonic() < self.next_try:
            return False
        try:
            from cuesdk import CorsairSessionState, CueSdk
        except Exception as exc:
            self.error = "cuesdk not available: %s" % exc
            self.next_try = time.monotonic() + 300
            return False
        ev = threading.Event()

        def on_state(e):
            if e.state == CorsairSessionState.CSS_Connected:
                ev.set()

        try:
            self.sdk = CueSdk()
            self.sdk.connect(on_state)
        except Exception as exc:
            self.error = "iCUE SDK error: %s" % exc
            self.sdk = None
            self.next_try = time.monotonic() + 60
            return False
        if ev.wait(timeout):
            self.ok = True
            return True
        self.error = "iCUE did not answer. Make sure iCUE is running and its SDK switch is on."
        self.next_try = time.monotonic() + 60
        return False

    def devices(self):
        if not self.connect():
            return []
        from cuesdk import CorsairDeviceFilter
        devs, err = self.sdk.get_devices(CorsairDeviceFilter(0xFFFFFFFF))
        return devs or []

    def status(self):
        if self.ok:
            return "connected"
        return self.error or "not connected"

    def names(self):
        return [d.model for d in self.devices()]

    def colour(self, rgb, tint=None):
        if not self.connect():
            return
        from cuesdk import CorsairAccessLevel, CorsairLedColor
        for d in self.devices():
            c = correct(rgb)
            gains = tint(d.model) if tint else None
            if gains:
                c = tuple(max(0, min(255, int(round(v * g / 100.0)))) for v, g in zip(c, gains))
            self.sdk.request_control(d.device_id, CorsairAccessLevel.CAL_ExclusiveLightingControl)
            pos, _ = self.sdk.get_led_positions(d.device_id)
            leds = [CorsairLedColor(p.id, c[0], c[1], c[2], 255) for p in (pos or [])]
            self.sdk.set_led_colors(d.device_id, leds)

    def close(self):
        try:
            if self.sdk is not None:
                self.sdk.disconnect()
        except Exception:
            pass
        self.sdk = None
        self.ok = False
