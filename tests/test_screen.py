import time

from PySide6.QtWidgets import QApplication

from halcyon.screen import Eyes, Framer, image_to_565

app = QApplication.instance() or QApplication([])


def advance(e, t, secs):
    for _ in range(int(secs * 60)):
        t += 1 / 60
        e.step(t, 1 / 60)
    return t


def test_sleep_and_wake_sequence():
    e = Eyes()
    t = time.monotonic()
    t = advance(e, t, 1)
    e.drowse()
    t = advance(e, t, 6)
    assert e.activity == "sleep"
    e.open_card([("User", "Alex")], "warn", "curious")
    assert e.frantic > time.monotonic()
    assert e.activity == "none"
    t = advance(e, t, .1)
    assert all(v == v for v in e.cur.values())


def test_frame_encoding():
    e = Eyes()
    f = Framer()
    t = time.monotonic()
    sizes = []
    for _ in range(30):
        t = advance(e, t, 1 / 20)
        data = f.encode(image_to_565(e.render(t)))
        sizes.append(len(data))
    assert max(sizes) < 40000
    assert sum(sizes) > 0
