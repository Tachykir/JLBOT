import time
import unittest
from pathlib import Path

import cv2
import numpy as np

from bot import BotConfig, DustMineBot
from dustbot2.minigames.depth_collapse import find_tiles
from dustbot2.minigames.fishing import detect

MATERIAL = Path(__file__).resolve().parent.parent / "material" / "2026-10-17"
SHOTS = MATERIAL / "screenshots"
ZONE = (530.0, 687.0)


def video_frames(name: str) -> list[tuple[float, np.ndarray]]:
    cap = cv2.VideoCapture(str(MATERIAL / name))
    frames = []
    while True:
        t = cap.get(cv2.CAP_PROP_POS_MSEC)
        ok, frame = cap.read()
        if not ok:
            return frames
        frames.append((t, frame))


class FakeADB:
    serial = "emulator-test"

    def __init__(self, frames=()):
        self.frames = list(frames)
        self.taps: list[tuple[int, int]] = []

    def screenshot(self):
        frame = self.frames.pop(0) if len(self.frames) > 1 else self.frames[0]
        return frame.copy()

    def tap(self, x, y):
        self.taps.append((x, y))


def make_bot(adb, **config) -> DustMineBot:
    cfg = BotConfig(name="inst-1", capture_unknown_screens=False, **config)
    bot = DustMineBot(adb, cfg)
    bot._classifier.ui_scale = 1.28
    return bot


class ScreenHandlerTests(unittest.TestCase):
    def handle(self, file_name: str):
        frame = cv2.imread(str(SHOTS / file_name))
        adb = FakeADB([frame])
        bot = make_bot(adb)
        state = bot._classifier.classify(frame).state
        handled = bot._handle_screen(frame, state)
        return state, handled, adb.taps, frame.shape

    def test_proceed_after_combat(self):
        state, handled, taps, (h, w, _) = self.handle("20261017_235122_233729_instance-1.png")
        self.assertEqual(state, "combat_done")
        self.assertTrue(handled)
        (x, y), = taps
        self.assertLess(abs(x - w / 2), 60)
        self.assertGreater(y, 0.9 * h)

    def test_item_popup_keeps_current(self):
        state, handled, taps, (h, w, _) = self.handle("20261017_235222_242463_instance-1.png")
        self.assertEqual(state, "item_popup")
        (x, y), = taps
        # "Keep Current" - ostatni przycisk na dole (x 380-696, y 2170-2216)
        self.assertTrue(380 <= x <= 696 and 2160 <= y <= 2226, (x, y))

    def test_reveal_taps_card(self):
        state, handled, taps, (h, w, _) = self.handle("20261017_235207_794917_instance-1.png")
        self.assertEqual(state, "reveal")
        (x, y), = taps
        self.assertTrue(0.3 * w < x < 0.7 * w and 0.35 * h < y < 0.6 * h)

    def test_home_taps_just_loot(self):
        _, home = video_frames("fishing.mp4")[-1]
        bot = make_bot(FakeADB([home]))
        self.assertEqual(bot._classifier.classify(home).state, "home")
        x, y = bot._idle_click_point(home)
        self.assertLess(abs(x - 541), 50)
        self.assertLess(abs(y - 2153), 50)

    def test_dust_mine_uses_drop_template(self):
        frame = cv2.imread(str(SHOTS / "20261017_235303_981096_instance-1.png"))
        bot = make_bot(FakeADB([frame]))
        det = bot._detect_board(frame)
        self.assertIsNotNone(det.drop)
        self.assertGreater(len(det.balls), 0)
        for ball in det.balls:
            self.assertGreater(np.hypot(ball.x - det.drop.x, ball.y - det.drop.y), det.drop.r)


class DepthCollapseTests(unittest.TestCase):
    def test_taps_every_tile_group(self):
        frames = video_frames("depth_collapse.mp4")
        board = frames[2][1]
        _, home = frames[-8]  # po minigrze - już inny ekran
        groups = find_tiles(board)
        adb = FakeADB([board, home])  # po jednej rundzie gra pokazuje już inny ekran
        bot = make_bot(adb)
        bot._play_depth_collapse()
        self.assertEqual(len(adb.taps), len(groups))
        for (x, y), group in zip(adb.taps, groups):
            self.assertLess(np.hypot(x - group.x, y - group.y), 20)


class VideoADB(FakeADB):
    """'Emulator' odtwarzający nagranie w czasie rzeczywistym."""

    def __init__(self, frames, start_ms=500.0, capture_ms=90.0):
        super().__init__()
        self.video = frames
        self.start_ms = start_ms
        self.capture_ms = capture_ms
        self.t0: float | None = None
        self.tap_times: list[float] = []

    def video_time(self, host_ms: float) -> float:
        return host_ms - self.t0 * 1000 + self.start_ms

    def screenshot(self):
        now = time.monotonic()
        if self.t0 is None:
            self.t0 = now
        middle = self.video_time(now * 1000 + self.capture_ms / 2)
        time.sleep(self.capture_ms / 1000)
        return min(self.video, key=lambda item: abs(item[0] - middle))[1].copy()

    def tap(self, x, y):
        super().tap(x, y)
        self.tap_times.append(self.video_time(time.monotonic() * 1000))


class FishingBotTests(unittest.TestCase):
    def test_cast_lands_in_zone_on_recording(self):
        frames = [(t, f) for t, f in video_frames("fishing.mp4") if t < 3500]
        positions = [(t, d.indicator_x) for t, d in ((t, detect(f)) for t, f in frames) if d is not None]
        adb = VideoADB(frames)
        bot = make_bot(adb, fishing_latency_ms=0.0, fishing_aim_spread=0.0)
        plan = bot._play_fishing()

        self.assertIsNotNone(plan)
        self.assertEqual(len(adb.taps), 1)
        hit_video_ms = adb.video_time(plan.hit_at_ms)
        self.assertLess(hit_video_ms, 3450, "rzut po końcu nagrania - test nic nie sprawdza")
        times = [t for t, _ in positions]
        xs = [x for _, x in positions]
        actual = float(np.interp(hit_video_ms, times, xs))
        self.assertTrue(ZONE[0] - 10 <= actual <= ZONE[1] + 10, (hit_video_ms, actual))
        # tap poszedł w przycisk CAST
        (x, y), = adb.taps
        self.assertLess(abs(x - 540), 30)
        self.assertLess(abs(y - 1413), 30)


if __name__ == "__main__":
    unittest.main()
