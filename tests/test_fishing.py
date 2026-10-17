import random
import unittest
from pathlib import Path

import cv2
import numpy as np

from dustbot2.minigames.fishing import (
    AimProfile, CastPlan, FishingTracker, LatencyCalibrator, detect, plan_cast, stopped_position,
)
from dustbot2.screens import ScreenClassifier

VIDEO = Path(__file__).resolve().parent.parent / "material" / "2026-10-17" / "fishing.mp4"
ZONE = (530.0, 687.0)          # strefa na nagraniu (px, 1080x2320)
ZONE_CENTER = sum(ZONE) / 2
CAST_AT_MS = 3550              # po tej chwili wskaźnik stoi (rzut gracza)


def load_video() -> list[tuple[float, np.ndarray]]:
    cap = cv2.VideoCapture(str(VIDEO))
    frames = []
    while True:
        t = cap.get(cv2.CAP_PROP_POS_MSEC)
        ok, frame = cap.read()
        if not ok:
            return frames
        frames.append((t, frame))


class FishingVideoTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.frames = load_video()
        cls.detections = [(t, detect(f)) for t, f in cls.frames]
        found = [(t, d) for t, d in cls.detections if d is not None]
        cls.found = found
        moving = [(t, d.indicator_x) for t, d in found if t < CAST_AT_MS]
        cls.crossings = [
            t0 + (ZONE_CENTER - x0) / (x1 - x0) * (t1 - t0)
            for (t0, x0), (t1, x1) in zip(moving, moving[1:])
            if (x0 - ZONE_CENTER) * (x1 - ZONE_CENTER) < 0
        ]

    def test_indicator_found_on_every_fishing_frame(self):
        clf = ScreenClassifier.load(ui_scale=1.28)
        for t, frame in self.frames[::5]:
            with self.subTest(t=t):
                is_fishing = clf.classify(frame).state == "fishing"
                self.assertEqual(detect(frame) is not None, is_fishing)

    def test_zone_is_found_exactly(self):
        full = [d.zone for _, d in self.found if d.zone is not None and not d.zone_partial]
        self.assertGreater(len(full), 30)
        self.assertEqual(set(full), {ZONE})

    def test_bar_edges(self):
        lefts = np.median([d.bar[0] for _, d in self.found])
        rights = np.median([d.bar[1] for _, d in self.found])
        self.assertAlmostEqual(lefts, 102, delta=3)
        self.assertAlmostEqual(rights, 977, delta=3)

    def test_predicts_next_zone_crossing(self):
        # obserwacje 0.5-1.5 s, co trzecia klatka (jak rzadsze zrzuty bota)
        tracker = FishingTracker()
        for t, d in [p for p in self.found if 500 <= p[0] <= 1500][::3]:
            tracker.update(t, d)
        self.assertAlmostEqual(tracker.speed, 0.99, delta=0.05)
        when, _ = tracker.next_time_at(ZONE_CENTER, 1500)
        error = min(abs(when - c) for c in self.crossings)
        self.assertLess(error, 40)

    def test_prediction_accuracy_over_many_windows(self):
        errors = []
        for start in range(500, 2200, 100):
            tracker = FishingTracker()
            for t, d in [p for p in self.found if start <= p[0] <= start + 800][::3]:
                tracker.update(t, d)
            if tracker.fit() is None:
                continue
            when, _ = tracker.next_time_at(ZONE_CENTER, start + 800)
            if when < CAST_AT_MS - 150:
                errors.append(min(abs(when - c) for c in self.crossings))
        self.assertGreater(len(errors), 10)
        # strefa daje ok. +-79 ms zapasu
        self.assertLess(np.median(errors), 40)
        self.assertGreaterEqual(np.mean(np.array(errors) < 79), 0.85)

    def test_detects_stop_after_cast(self):
        after = [(t, d.indicator_x) for t, d in self.found if t > CAST_AT_MS]
        stopped = stopped_position(after)
        self.assertIsNotNone(stopped)
        self.assertTrue(ZONE[0] <= stopped <= ZONE[1])  # ten rzut trafił
        moving = [(t, d.indicator_x) for t, d in self.found if 1000 < t < 1300]
        self.assertIsNone(stopped_position(moving))


class PlanAndCalibrationTests(unittest.TestCase):
    def tracker(self) -> FishingTracker:
        # syntetyczny ruch: granice 100-900, 1 px/ms, start w 100 w prawo
        tracker = FishingTracker(speed_per_width=1.0 / 1080)

        class Det:
            def __init__(self, x):
                self.indicator_x, self.indicator_w = x, 30
                self.bar, self.zone, self.zone_partial = (85.0, 915.0), (450.0, 550.0), False

        for t in range(0, 600, 120):
            tracker.update(float(t), Det(100.0 + t))
        return tracker

    def test_plan_targets_zone_and_subtracts_latency(self):
        tracker = self.tracker()
        plan = plan_cast(tracker, now_ms=480, latency_ms=100, rng=random.Random(1))
        self.assertTrue(450 <= plan.target_x <= 550)
        self.assertAlmostEqual(plan.hit_at_ms - plan.tap_at_ms, 100)
        self.assertGreaterEqual(plan.tap_at_ms, 480)
        self.assertAlmostEqual(tracker.position_at(plan.hit_at_ms), plan.target_x, delta=3)

    def test_deliberate_miss_aims_outside_zone(self):
        plan = plan_cast(self.tracker(), now_ms=480, latency_ms=100,
                         aim=AimProfile(miss_rate=1.0), rng=random.Random(2))
        self.assertFalse(450 <= plan.target_x <= 550)

    def test_calibrator_increases_latency_when_tap_was_late(self):
        plan = CastPlan(tap_at_ms=1000, target_x=500, hit_at_ms=1100, direction=1)
        calibrator = LatencyCalibrator(latency_ms=100, gain=1.0)
        # wskaźnik jechał w prawo i stanął 30 px za celem - tap był 30 ms za późno
        self.assertAlmostEqual(calibrator.update(plan, stopped_x=530, speed=1.0), 30)
        self.assertAlmostEqual(calibrator.latency_ms, 130)
        # jadąc w lewo, stanięcie na prawo od celu oznacza tap za wcześnie
        left = CastPlan(tap_at_ms=1000, target_x=500, hit_at_ms=1100, direction=-1)
        calibrator.update(left, stopped_x=520, speed=1.0)
        self.assertAlmostEqual(calibrator.latency_ms, 110)


if __name__ == "__main__":
    unittest.main()
