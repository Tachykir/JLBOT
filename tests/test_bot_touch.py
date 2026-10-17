import unittest

from bot import BotConfig, DustMineBot
from dustbot2.geometry import Screen
from dustbot2.touch.minitouch import Banner, MinitouchClient


class Recorder:
    def __init__(self, fail=False):
        self.sent: list[bytes] = []
        self.fail = fail

    def sendall(self, data):
        if self.fail:
            raise OSError("broken pipe")
        self.sent.append(data)


class FakeADB:
    serial = "emulator-test"

    def __init__(self):
        self.taps: list[tuple] = []
        self.swipes: list[tuple] = []

    def tap(self, x, y):
        self.taps.append((x, y))

    def swipe(self, x1, y1, x2, y2, duration_ms=400):
        self.swipes.append((x1, y1, x2, y2, duration_ms))


def make_bot(transport=None):
    adb = FakeADB()
    bot = DustMineBot(adb, BotConfig(name="inst-1", capture_unknown_screens=False))
    if transport is not None:
        banner = Banner(1, 10, 539, 959, 100)
        bot._touch = MinitouchClient(transport, banner, Screen(540, 960))
    return bot, adb


class BotTouchTests(unittest.TestCase):
    def test_adb_backend_by_default(self):
        bot, adb = make_bot()
        bot._tap(10, 20)
        bot._drag((100, 800), (300, 300))
        self.assertEqual(adb.taps, [(10, 20)])
        self.assertEqual(len(adb.swipes), 1)

    def test_minitouch_drag_sends_gesture_instead_of_swipe(self):
        transport = Recorder()
        bot, adb = make_bot(transport)
        bot._drag_rng.seed(1)
        bot._drag((100, 800), (300, 300))
        self.assertEqual(adb.swipes, [])
        payload = transport.sent[0].decode()
        self.assertTrue(payload.startswith("d 0 "))
        self.assertTrue(payload.endswith("u 0\nc\n"))
        self.assertGreater(payload.count("\nm 0 "), 10)

    def test_minitouch_tap(self):
        transport = Recorder()
        bot, adb = make_bot(transport)
        bot._tap(100, 200)
        self.assertEqual(adb.taps, [])
        self.assertTrue(transport.sent[0].decode().startswith("d 0 100 200 "))

    def test_falls_back_to_adb_when_minitouch_breaks(self):
        bot, adb = make_bot(Recorder(fail=True))
        bot._drag((100, 800), (300, 300))
        self.assertEqual(len(adb.swipes), 1)
        self.assertIsNone(bot._touch)
        bot._tap(5, 5)
        self.assertEqual(adb.taps, [(5, 5)])


if __name__ == "__main__":
    unittest.main()
