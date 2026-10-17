import json
import tempfile
import time
import unittest
from pathlib import Path

import cv2
import numpy as np

from bot import BotConfig, DustMineBot
from recorder import MinigameRecorder, UnknownScreenSaver

SCREENS = Path(__file__).resolve().parent.parent / "Unknown"
BOARD = cv2.imread(str(SCREENS / "20261010_154356_700420_Vexaro.png"))


def noise_frame(seed: int) -> np.ndarray:
    return np.random.default_rng(seed).integers(0, 255, size=(960, 540, 3), dtype=np.uint8)


class FakeProc:
    def __init__(self):
        self.terminated = False

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True


class FakeADB:
    serial = "emulator-test"

    def __init__(self, frame=None):
        self.frame = frame if frame is not None else noise_frame(0)
        self.shell_calls: list[tuple] = []
        self.popen_calls: list[list[str]] = []
        self.pulled: list[tuple[str, str]] = []
        self.taps = 0

    def shell(self, *args, timeout=10.0):
        self.shell_calls.append(args)
        return "12345.67 890.12\n" if args[:2] == ("cat", "/proc/uptime") else ""

    def popen(self, args, stdout=None):
        self.popen_calls.append(args)
        if stdout is not None:
            stdout.write(b"[   12345.700000] /dev/input/event2: EV_KEY BTN_TOUCH DOWN\n")
        return FakeProc()

    def pull(self, remote, local, timeout=120.0):
        self.pulled.append((remote, local))
        Path(local).write_bytes(b"mp4")

    def get_screen_size(self):
        return 540, 960

    def screenshot(self):
        time.sleep(0.01)
        return self.frame.copy()

    def tap(self, x, y):
        self.taps += 1


class UnknownScreenSaverTests(unittest.TestCase):
    def test_saves_once_for_repeated_frame_and_again_for_new_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            saver = UnknownScreenSaver(Path(tmp), "inst-1", min_interval=0.0)
            self.assertIsNotNone(saver.offer(noise_frame(1)))
            self.assertIsNone(saver.offer(noise_frame(1)))
            self.assertIsNotNone(saver.offer(BOARD))
            names = sorted(p.name for p in Path(tmp).iterdir())
            self.assertEqual(len(names), 2)
            self.assertTrue(all(n.endswith("_inst-1.png") for n in names))

    def test_respects_min_interval(self):
        with tempfile.TemporaryDirectory() as tmp:
            saver = UnknownScreenSaver(Path(tmp), "inst-1", min_interval=60.0)
            self.assertIsNotNone(saver.offer(noise_frame(1)))
            self.assertIsNone(saver.offer(BOARD))


class MinigameRecorderTests(unittest.TestCase):
    def test_record_writes_frames_touches_video_and_meta(self):
        adb = FakeADB()
        with tempfile.TemporaryDirectory() as tmp:
            folder = MinigameRecorder(adb, Path(tmp), "inst 1").record("fishing", 0.2)
            self.assertTrue(folder.name.endswith("_inst_1_fishing"))
            self.assertGreater(len(list((folder / "frames").glob("*.png"))), 0)
            self.assertIn(b"BTN_TOUCH", (folder / "touches.txt").read_bytes())
            self.assertTrue((folder / "video.mp4").exists())
            meta = json.loads((folder / "meta.json").read_text(encoding="utf-8"))
            self.assertEqual(meta["label"], "fishing")
            self.assertEqual(meta["device_uptime_at_start"], 12345.67)
            self.assertEqual(meta["screen_size"], [540, 960])
            self.assertTrue(meta["has_video"])
        self.assertTrue(any(c[0] == "screenrecord" for c in (a[1:] for a in adb.popen_calls)))
        self.assertIn(("pkill", "-INT", "screenrecord"), adb.shell_calls)


class BotCaptureTests(unittest.TestCase):
    def make_bot(self, tmp: str, **overrides) -> tuple[DustMineBot, FakeADB]:
        adb = FakeADB()
        cfg = BotConfig(name="inst-1", capture_dir=tmp, **overrides)
        return DustMineBot(adb, cfg), adb

    def test_unknown_screen_is_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, _ = self.make_bot(tmp)
            self.assertFalse(bot._handle_capture(noise_frame(2)))
            self.assertEqual(len(list((Path(tmp) / "Unknown").glob("*.png"))), 1)

    def test_known_screen_is_not_saved(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, _ = self.make_bot(tmp)
            self.assertFalse(bot._handle_capture(BOARD))
            self.assertFalse((Path(tmp) / "Unknown").exists())

    def test_unknown_screen_triggers_recording_without_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, adb = self.make_bot(tmp, record_unknown_seconds=0.2)
            self.assertTrue(bot._handle_capture(noise_frame(3)))
            self.assertEqual(len(list((Path(tmp) / "recordings").iterdir())), 1)
            self.assertEqual(adb.taps, 0)
            # cooldown - drugi nieznany ekran od razu nie uruchamia kolejnego nagrania
            self.assertFalse(bot._handle_capture(noise_frame(4)))

    def test_manual_request_records_any_screen(self):
        with tempfile.TemporaryDirectory() as tmp:
            bot, _ = self.make_bot(tmp)
            bot.request_recording(0.2, "fishing")
            self.assertTrue(bot._handle_capture(BOARD))
            folders = list((Path(tmp) / "recordings").iterdir())
            self.assertEqual(len(folders), 1)
            self.assertTrue(folders[0].name.endswith("_fishing"))


if __name__ == "__main__":
    unittest.main()
