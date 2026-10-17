import random
import unittest

from dustbot2.geometry import Screen
from dustbot2.touch.humanize import DragProfile, plan_drag, to_commands
from dustbot2.touch.minitouch import Banner, MinitouchClient, TouchError, parse_banner


class FakeTransport:
    def __init__(self):
        self.sent: list[bytes] = []

    def sendall(self, data: bytes) -> None:
        self.sent.append(data)


def make_client(transport=None) -> MinitouchClient:
    banner = Banner(version=1, max_contacts=10, max_x=719, max_y=1559, max_pressure=100)
    return MinitouchClient(transport or FakeTransport(), banner)


class BannerTests(unittest.TestCase):
    def test_parses_standard_header(self):
        banner = parse_banner(["v 1\n", "^ 10 719 1559 100\n", "$ 1234\n"])
        self.assertEqual(banner, Banner(1, 10, 719, 1559, 100))

    def test_missing_limits_raises(self):
        with self.assertRaises(TouchError):
            parse_banner(["v 1\n", "$ 1\n"])


class ScreenTests(unittest.TestCase):
    def test_fraction_to_pixels_and_back(self):
        screen = Screen(720, 1560)
        x, y = screen.to_px(0.5, 0.5)
        self.assertEqual((x, y), (360, 780))
        fx, fy = screen.to_frac(x, y)
        self.assertAlmostEqual(fx, 0.5, places=2)
        self.assertAlmostEqual(fy, 0.5, places=2)

    def test_out_of_range_fraction_is_clamped(self):
        self.assertEqual(Screen(720, 1560).to_px(1.5, -0.2), (719, 0))


class CommandTests(unittest.TestCase):
    def test_coordinates_are_clamped_to_banner(self):
        client = make_client()
        self.assertEqual(client.down(0, 9999, -5), "d 0 719 0 50")

    def test_tap_sends_down_wait_up_then_commit(self):
        transport = FakeTransport()
        make_client(transport).tap(100, 200, hold_ms=60)
        self.assertEqual(transport.sent, [b"d 0 100 200 50\nw 60\nu 0\nc\n"])

    def test_transport_error_is_wrapped(self):
        class Broken:
            def sendall(self, data):
                raise OSError("pipe")

        with self.assertRaises(TouchError):
            make_client(Broken()).tap(1, 1)


class HumanizeTests(unittest.TestCase):
    def test_drag_starts_and_ends_near_requested_points(self):
        profile = DragProfile(endpoint_px=2.0)
        steps = plan_drag((100, 100), (600, 1000), profile, random.Random(1))
        first, last = steps[0], steps[-1]
        self.assertLess(abs(first.x - 100), 10)
        self.assertLess(abs(first.y - 100), 10)
        self.assertLess(abs(last.x - 600), 10)
        self.assertLess(abs(last.y - 1000), 10)

    def test_duration_is_within_profile_range(self):
        profile = DragProfile(duration_ms=(500, 900))
        for seed in range(20):
            steps = plan_drag((0, 0), (300, 300), profile, random.Random(seed))
            total = sum(s.delay_ms for s in steps)
            self.assertGreaterEqual(total, 499)
            self.assertLessEqual(total, 901)

    def test_same_seed_gives_same_path(self):
        a = plan_drag((10, 10), (500, 900), rng=random.Random(42))
        b = plan_drag((10, 10), (500, 900), rng=random.Random(42))
        self.assertEqual(a, b)

    def test_pressure_stays_in_profile_range(self):
        profile = DragProfile(pressure=(40, 70))
        steps = plan_drag((0, 0), (300, 300), profile, random.Random(3))
        self.assertTrue(all(40 <= s.pressure <= 70 for s in steps))

    def test_commands_down_moves_then_up(self):
        client = make_client()
        steps = plan_drag((100, 100), (400, 800), DragProfile(), random.Random(5))
        commands = to_commands(steps, client)
        self.assertTrue(commands[0].startswith("d 0 "))
        self.assertEqual(commands[-1], "u 0")
        self.assertTrue(all(c.startswith(("w ", "m ")) for c in commands[1:-1]))


if __name__ == "__main__":
    unittest.main()
