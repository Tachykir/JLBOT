import datetime as dt
import random
import tempfile
import unittest
from pathlib import Path

from dustbot2.schedule import (
    DailyPlayTarget, PlayLedger, PlayScheduler, plan_window, window_containing_or_next, window_for,
)

DAY = dt.date(2026, 10, 19)  # poniedziałek
TARGET = DailyPlayTarget("09:00", "23:30", play_hours=6.0)


def total_minutes(sessions) -> float:
    return sum(s.minutes for s in sessions)


class PlanWindowTests(unittest.TestCase):
    def test_sum_matches_target_and_fits_window(self):
        start, end = window_for(TARGET, DAY)
        for seed in range(50):
            sessions = plan_window(TARGET, start, end, random.Random(seed))
            with self.subTest(seed=seed):
                self.assertAlmostEqual(total_minutes(sessions), 360, delta=0.1)
                self.assertGreaterEqual(sessions[0].start, start)
                self.assertLessEqual(sessions[-1].end, end + dt.timedelta(seconds=1))

    def test_breaks_respect_minimum_and_are_irregular(self):
        start, end = window_for(TARGET, DAY)
        sessions = plan_window(TARGET, start, end, random.Random(7))
        breaks = [(b.start - a.end).total_seconds() / 60 for a, b in zip(sessions, sessions[1:])]
        self.assertGreater(len(breaks), 2)
        self.assertGreaterEqual(min(breaks), TARGET.min_break_minutes - 1e-6)
        self.assertGreater(max(breaks) - min(breaks), 10)  # nieregularne odstępy

    def test_session_lengths_stay_in_range(self):
        start, end = window_for(TARGET, DAY)
        for seed in range(30):
            lengths = [s.minutes for s in plan_window(TARGET, start, end, random.Random(seed))]
            self.assertTrue(all(20 - 1e-6 <= m <= 90 + 1e-6 for m in lengths), lengths)

    def test_different_days_differ(self):
        start, end = window_for(TARGET, DAY)
        a = plan_window(TARGET, start, end, random.Random("a"))
        b = plan_window(TARGET, start, end, random.Random("b"))
        self.assertNotEqual([s.start for s in a], [s.start for s in b])

    def test_target_longer_than_window_plays_whole_window(self):
        target = DailyPlayTarget("10:00", "12:00", play_hours=5.0)
        start, end = window_for(target, DAY)
        sessions = plan_window(target, start, end, random.Random(1))
        self.assertEqual(len(sessions), 1)
        self.assertAlmostEqual(total_minutes(sessions), 120, delta=0.1)

    def test_already_played_reduces_plan(self):
        start, end = window_for(TARGET, DAY)
        sessions = plan_window(TARGET, start, end, random.Random(1), played_seconds=5 * 3600)
        self.assertAlmostEqual(total_minutes(sessions), 60, delta=0.1)
        self.assertEqual(plan_window(TARGET, start, end, random.Random(1), played_seconds=6 * 3600), [])


class WindowTests(unittest.TestCase):
    def test_window_across_midnight_belongs_to_start_day(self):
        target = DailyPlayTarget("20:00", "02:00", play_hours=3)
        day, start, end = window_containing_or_next(target, dt.datetime(2026, 10, 20, 1, 0))
        self.assertEqual(day, dt.date(2026, 10, 19))
        self.assertEqual(end, dt.datetime(2026, 10, 20, 2, 0))

    def test_days_of_week(self):
        weekend = DailyPlayTarget("10:00", "20:00", play_hours=2, days=(5, 6))
        day, start, _ = window_containing_or_next(weekend, dt.datetime(2026, 10, 19, 12, 0))
        self.assertEqual(day.weekday(), 5)


class SchedulerTests(unittest.TestCase):
    def test_play_time_follows_plan(self):
        scheduler = PlayScheduler(TARGET, "inst-1")
        morning = dt.datetime.combine(DAY, dt.time(8, 0))
        self.assertFalse(scheduler.is_play_time(morning))
        first = scheduler.next_session(morning)
        self.assertGreaterEqual(first.start, dt.datetime.combine(DAY, dt.time(9, 0)))
        self.assertTrue(scheduler.is_play_time(first.start + dt.timedelta(minutes=1)))
        self.assertFalse(scheduler.is_play_time(first.end + dt.timedelta(seconds=30)))

    def test_restart_mid_day_plans_only_the_rest(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = PlayLedger(Path(tmp) / "state.json", "inst-1")
            ledger.add(DAY, 4 * 3600)
            ledger.save()
            reloaded = PlayLedger(Path(tmp) / "state.json", "inst-1")
            scheduler = PlayScheduler(TARGET, "inst-1", reloaded)
            noon = dt.datetime.combine(DAY, dt.time(12, 0))
            sessions = scheduler.sessions(noon)
            self.assertAlmostEqual(total_minutes(sessions), 120, delta=0.1)
            self.assertGreaterEqual(sessions[0].start, noon)

    def test_target_round_trip(self):
        target = DailyPlayTarget("09:00", "23:30", 6.0, (15.0, 60.0), 8.0, (0, 1, 2))
        self.assertEqual(DailyPlayTarget.from_dict(target.to_dict()), target)


class BotScheduleTests(unittest.TestCase):
    def test_game_loop_yields_outside_planned_sessions(self):
        from bot import BotConfig, DustMineBot

        class NoScreenADB:
            serial = "emulator-test"

            def screenshot(self):
                raise AssertionError("poza sesją bot nie powinien nawet robić zrzutu")

        later = dt.datetime.now() + dt.timedelta(hours=3)
        target = DailyPlayTarget(f"{later:%H:%M}", f"{later + dt.timedelta(minutes=30):%H:%M}", play_hours=0.4)
        with tempfile.TemporaryDirectory() as tmp:
            bot = DustMineBot(NoScreenADB(), BotConfig(name="inst-1", capture_dir=tmp,
                                                       capture_unknown_screens=False, daily_play=target))
            self.assertFalse(bot._is_within_schedule())
            self.assertFalse(bot._idle_loop())
            self.assertIn("przerwa do", bot._break_message())


if __name__ == "__main__":
    unittest.main()
