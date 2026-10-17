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


if __name__ == "__main__":
    unittest.main()
