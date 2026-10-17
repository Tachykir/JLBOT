import json
import unittest
from pathlib import Path

import cv2
import numpy as np

from dustbot2.screens import BLACK, UNKNOWN, ScreenClassifier

ROOT = Path(__file__).resolve().parent.parent
SCREENS = ROOT / "Unknown"
LABELS = json.loads((Path(__file__).parent / "screen_labels.json").read_text(encoding="utf-8"))

# Pozycje DROP sprawdzone wzrokowo na zrzutach 540x960.
DROP_CENTERS = {
    "20261009_192113_882449_Vexaro.png": (140, 312),
    "20261009_212432_511659_KriNox.png": (172, 810),
    "20261010_154356_700420_Vexaro.png": (380, 322),
}
# DROP przykryty kulką - napisu nie widać.
DROP_HIDDEN = "20261009_183135_232305_ZimnyPixel.png"


def load(name: str) -> np.ndarray:
    image = cv2.imread(str(SCREENS / name))
    assert image is not None, name
    return image


class ScreenClassifierTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.clf = ScreenClassifier.load()

    def test_all_labeled_screenshots(self):
        misses = {}
        for name, expected in LABELS.items():
            got = self.clf.classify(load(name)).state
            if got != expected:
                misses[name] = (expected, got)
        self.assertEqual(misses, {})

    def test_same_state_at_other_resolutions(self):
        samples = {}
        for name, state in LABELS.items():
            samples.setdefault(state, name)
        for width, height in [(720, 1280), (1080, 1920)]:
            for state, name in samples.items():
                frame = cv2.resize(load(name), (width, height), interpolation=cv2.INTER_LINEAR)
                with self.subTest(state=state, size=(width, height)):
                    self.assertEqual(self.clf.classify(frame).state, state)

    def test_state_changes_between_consecutive_frames(self):
        # klasyfikator zapamiętuje poprzedni stan - nie może to blokować zmiany
        names = list(LABELS)
        for name in names + names[::-1]:
            self.assertEqual(self.clf.classify(load(name)).state, LABELS[name], name)

    def test_popup_wins_over_dimmed_screen_below(self):
        board = load("20261010_154356_700420_Vexaro.png")
        popup = load("20261010_112048_973111_ZimnyPixel.png")
        frame = (board * 0.4).astype(np.uint8)
        frame[600:, :] = popup[600:, :]
        self.assertEqual(self.clf.classify(frame).state, "item_popup")

    def test_noise_frame_is_unknown(self):
        rng = np.random.default_rng(0)
        frame = rng.integers(0, 255, size=(960, 540, 3), dtype=np.uint8)
        self.assertEqual(self.clf.classify(frame).state, UNKNOWN)

    def test_black_frame(self):
        self.assertEqual(self.clf.classify(np.zeros((960, 540, 3), np.uint8)).state, BLACK)


class DropLocatorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.clf = ScreenClassifier.load()

    def assertNear(self, actual, expected, tolerance):
        self.assertLessEqual(abs(actual[0] - expected[0]), tolerance, (actual, expected))
        self.assertLessEqual(abs(actual[1] - expected[1]), tolerance, (actual, expected))

    def test_drop_found_at_known_position(self):
        for name, center in DROP_CENTERS.items():
            with self.subTest(name=name):
                match = self.clf.locate(load(name), "drop_text")
                self.assertIsNotNone(match)
                self.assertNear((match.x, match.y), center, 4)

    def test_drop_found_on_every_visible_board(self):
        for name, state in LABELS.items():
            if state != "dust_mine" or name == DROP_HIDDEN:
                continue
            with self.subTest(name=name):
                self.assertIsNotNone(self.clf.locate(load(name), "drop_text"))

    def test_hidden_drop_is_not_reported(self):
        self.assertIsNone(self.clf.locate(load(DROP_HIDDEN), "drop_text"))

    def test_drop_position_scales_with_resolution(self):
        name, (cx, cy) = next(iter(DROP_CENTERS.items()))
        frame = cv2.resize(load(name), (1080, 1920), interpolation=cv2.INTER_LINEAR)
        match = self.clf.locate(frame, "drop_text")
        self.assertIsNotNone(match)
        self.assertNear((match.x, match.y), (cx * 2, cy * 2), 6)


NEW_MATERIAL = ROOT / "material" / "2026-10-17" / "screenshots"
NEW_LABELS = {
    "20261017_235025_836554_instance-1.png": "dust_mine",
    "20261017_235122_233729_instance-1.png": "combat_done",
    "20261017_235207_794917_instance-1.png": "reveal",
    "20261017_235222_242463_instance-1.png": "item_popup",
    "20261017_235303_981096_instance-1.png": "dust_mine",
    "20261017_235912_438551_instance-1.png": "item_popup",
}


class NewResolutionTests(unittest.TestCase):
    """LDPlayer 1080x2320 - interfejs ok. 1.28 raza większy niż na 540x960."""

    def test_ui_scale_is_detected_automatically(self):
        clf = ScreenClassifier.load()
        self.assertIsNone(clf.ui_scale)
        frame = cv2.imread(str(NEW_MATERIAL / "20261017_235025_836554_instance-1.png"))
        self.assertEqual(clf.classify(frame).state, "dust_mine")
        self.assertAlmostEqual(clf.ui_scale, 1.28, delta=0.03)

    def test_new_screens(self):
        clf = ScreenClassifier.load(ui_scale=1.28)
        for name, expected in NEW_LABELS.items():
            with self.subTest(name=name):
                self.assertEqual(clf.classify(cv2.imread(str(NEW_MATERIAL / name))).state, expected)

    def test_drop_found_on_dust_mine_video(self):
        # DROP szukamy tylko na ekranie Dust Mine - karty przedmiotów mają
        # napis "DROPPED AT DEPTH", który szablon słusznie dopasowuje.
        clf = ScreenClassifier.load(ui_scale=1.28)
        cap = cv2.VideoCapture(str(ROOT / "material" / "2026-10-17" / "dust_mine.mp4"))
        boards = found = 0
        while True:
            ok, frame = cap.read()
            if not ok:
                break
            if clf.classify(frame).state != "dust_mine":
                continue
            boards += 1
            if clf.locate(frame, "drop_text_v2") is not None:
                found += 1
        self.assertGreater(boards, 20)
        # część klatek ma DROP zasłonięty przez przeciąganą kulkę
        self.assertGreaterEqual(found / boards, 0.6)

if __name__ == "__main__":
    unittest.main()
