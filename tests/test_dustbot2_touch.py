import random
import tempfile
import unittest
from pathlib import Path

from dustbot2.geometry import Screen
from dustbot2.touch.humanize import SHAPES, DragProfile, plan_drag, straightness, to_commands
from dustbot2.touch.minitouch import Banner, MinitouchClient, MinitouchSession, TouchError, parse_banner


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

    def test_tap_commits_down_and_up_separately(self):
        transport = FakeTransport()
        make_client(transport).tap(100, 200, hold_ms=60)
        self.assertEqual(transport.sent, [b"d 0 100 200 50\nc\nw 60\nu 0\nc\n"])

    def test_screen_pixels_are_scaled_to_touch_range(self):
        banner = Banner(version=1, max_contacts=10, max_x=32767, max_y=32767, max_pressure=255)
        client = MinitouchClient(FakeTransport(), banner, Screen(540, 960))
        self.assertEqual(client.down(0, 0, 0), "d 0 0 0 50")
        self.assertEqual(client.down(0, 539, 959), "d 0 32767 32767 50")
        x, y = client.to_touch(269.5, 479.5)
        self.assertAlmostEqual(x, 16384, delta=1)
        self.assertAlmostEqual(y, 16384, delta=1)

    def test_zero_max_pressure_sends_zero(self):
        banner = Banner(version=1, max_contacts=10, max_x=100, max_y=100, max_pressure=0)
        self.assertEqual(MinitouchClient(FakeTransport(), banner).down(0, 1, 1, 50), "d 0 1 1 0")

    def test_transport_error_is_wrapped(self):
        class Broken:
            def sendall(self, data):
                raise OSError("pipe")

        with self.assertRaises(TouchError):
            make_client(Broken()).tap(1, 1)


class HumanizeTests(unittest.TestCase):
    def test_drag_starts_and_ends_near_requested_points(self):
        for shape in SHAPES:
            if shape == "flick":
                continue
            with self.subTest(shape=shape):
                steps = plan_drag((100, 100), (400, 900), rng=random.Random(1), shape=shape).steps
                self.assertLess(abs(steps[0].x - 100), 20)
                self.assertLess(abs(steps[0].y - 100), 20)
                self.assertLess(abs(steps[-1].x - 400), 30)
                self.assertLess(abs(steps[-1].y - 900), 30)

    def test_duration_stays_in_clamp(self):
        profile = DragProfile()
        for seed in range(200):
            total = plan_drag((0, 0), (300, 600), profile, random.Random(seed)).duration_ms
            self.assertGreaterEqual(total, profile.duration_clamp[0] - 1)
            self.assertLessEqual(total, profile.duration_clamp[1] + 1)

    def test_fixed_duration_range_is_respected(self):
        profile = DragProfile(duration_ms=(500, 900))
        for seed in range(20):
            total = plan_drag((0, 0), (300, 300), profile, random.Random(seed), shape="arc").duration_ms
            self.assertGreaterEqual(total, 499)
            self.assertLessEqual(total, 901)

    def test_same_seed_gives_same_path(self):
        a = plan_drag((10, 10), (500, 900), rng=random.Random(42))
        b = plan_drag((10, 10), (500, 900), rng=random.Random(42))
        self.assertEqual(a, b)

    def test_all_shapes_are_used(self):
        rng = random.Random(0)
        seen = {plan_drag((0, 0), (300, 600), rng=rng).shape for _ in range(300)}
        self.assertEqual(seen, set(SHAPES))

    def test_shapes_differ_in_straightness(self):
        def median(shape):
            values = sorted(
                straightness(plan_drag((100, 800), (400, 200), rng=random.Random(s), shape=shape).steps)
                for s in range(40)
            )
            return values[len(values) // 2]

        self.assertGreater(median("straight"), 0.97)
        self.assertLess(median("arc"), median("straight"))
        self.assertLess(median("elbow"), 0.85)
        self.assertLess(median("hook"), 0.95)

    def test_mixed_shapes_cover_human_straightness_range(self):
        # nagrania prawdziwych przeciągnięć: od ok. 0.70 do 0.99
        rng = random.Random(3)
        values = [straightness(plan_drag((100, 800), (400, 200), rng=rng).steps) for _ in range(300)]
        self.assertLess(min(values), 0.8)
        self.assertGreater(max(values), 0.98)

    def test_axis_aligned_elbow_becomes_arc(self):
        # kulka prawie pionowo pod DROP - zakręt "L" nie ma sensu
        for seed in range(20):
            plan = plan_drag((300, 900), (305, 200), rng=random.Random(seed), shape="elbow")
            self.assertEqual(plan.shape, "arc")

    def test_paths_stay_inside_bounds(self):
        bounds = (25, 215, 515, 935)
        rng = random.Random(1)
        for _ in range(300):
            start = (rng.uniform(70, 470), rng.uniform(260, 890))
            end = (rng.uniform(70, 470), rng.uniform(260, 890))
            steps = plan_drag(start, end, rng=rng, bounds=bounds).steps
            for s in steps:
                self.assertTrue(bounds[0] <= s.x <= bounds[2] and bounds[1] <= s.y <= bounds[3], (start, end))

    def test_flick_releases_before_target(self):
        for seed in range(20):
            steps = plan_drag((100, 900), (100, 100), rng=random.Random(seed), shape="flick").steps
            self.assertGreater(steps[-1].y, 100 + 0.1 * 800)

    def test_finger_speeds_up_then_slows_down(self):
        profile = DragProfile(jitter_px=0, easing=(1.0, 1.0), skew=(1.0, 1.0))
        steps = plan_drag((100, 100), (100, 900), profile, random.Random(7), shape="straight").steps
        speeds = [abs(b.y - a.y) / b.delay_ms for a, b in zip(steps, steps[1:])]
        third = len(speeds) // 3
        middle = sum(speeds[third:2 * third]) / third
        self.assertGreater(middle, 1.5 * sum(speeds[:third // 2]) / (third // 2))
        self.assertGreater(middle, 1.5 * sum(speeds[-(third // 2):]) / (third // 2))

    def test_pressure_stays_in_range_and_changes_smoothly(self):
        profile = DragProfile(pressure=(40, 70), pressure_step=2)
        steps = plan_drag((0, 0), (300, 600), profile, random.Random(9)).steps
        self.assertTrue(all(40 <= s.pressure <= 70 for s in steps))
        self.assertTrue(all(abs(b.pressure - a.pressure) <= 2 for a, b in zip(steps, steps[1:])))

    def test_frames_are_evenly_spaced(self):
        steps = plan_drag((0, 0), (300, 600), rng=random.Random(4)).steps
        delays = {round(s.delay_ms, 6) for s in steps[1:]}
        self.assertEqual(len(delays), 1)

    def test_commands_down_moves_then_up(self):
        client = make_client()
        steps = plan_drag((100, 100), (400, 800), rng=random.Random(5)).steps
        commands = to_commands(steps, client)
        self.assertTrue(commands[0].startswith("d 0 "))
        self.assertEqual(commands[-2:], ["u 0", "c"])

    def test_every_commit_has_at_most_one_event_per_contact(self):
        # protokół minitouch: najwyżej jedno d/m/u dla palca w jednym "c"
        client = make_client()
        for seed in range(30):
            commands = to_commands(plan_drag((100, 100), (400, 800), rng=random.Random(seed)).steps, client)
            self.assertEqual(commands[-1], "c")
            group: list[str] = []
            for command in commands:
                if command == "c":
                    self.assertEqual(len(group), 1, group)
                    group = []
                elif command[0] in "dmu":
                    group.append(command)


class FakeSessionADB:
    def __init__(self, abi="x86_64", size=(540, 960)):
        self.abi = abi
        self.size = size
        self.calls: list[tuple] = []

    def shell(self, *args, timeout=10.0):
        self.calls.append(("shell",) + args)
        return self.abi + "\n" if args == ("getprop", "ro.product.cpu.abi") else ""

    def push(self, local, remote, timeout=60.0):
        self.calls.append(("push", Path(local).name, remote))

    def forward(self, *args):
        self.calls.append(("forward",) + args)

    def popen(self, args, stdout=None):
        self.calls.append(("popen",) + tuple(args))

        class Proc:
            def terminate(self):
                pass
        return Proc()

    def get_screen_size(self):
        return self.size


class FakeSocket(FakeTransport):
    def close(self):
        pass


class SessionTests(unittest.TestCase):
    def binaries(self, tmp, abi="x86_64"):
        folder = Path(tmp) / abi
        folder.mkdir(parents=True)
        (folder / "minitouch").write_bytes(b"\x7fELF")
        return Path(tmp)

    def test_start_pushes_runs_as_root_and_forwards(self):
        banner = Banner(1, 10, 32767, 32767, 255)
        adb = FakeSessionADB()
        with tempfile.TemporaryDirectory() as tmp:
            session = MinitouchSession(adb, binaries=self.binaries(tmp),
                                       connect=lambda port, timeout: (FakeSocket(), banner))
            client = session.start()
            self.assertEqual(client.screen, Screen(540, 960))
            self.assertIn(("push", "minitouch", "/data/local/tmp/minitouch"), adb.calls)
            self.assertIn(("popen", "shell", "su", "-c", "/data/local/tmp/minitouch"), adb.calls)
            forward = [c for c in adb.calls if c[0] == "forward"][0]
            self.assertEqual(forward[2], "localabstract:minitouch")
            session.stop()
            self.assertIn(("forward", "--remove", forward[1]), adb.calls)

    def test_missing_binary_for_abi_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = MinitouchSession(FakeSessionADB(abi="mips"), binaries=self.binaries(tmp))
            with self.assertRaises(TouchError):
                session.start()

    def test_unresponsive_minitouch_raises_and_cleans_up(self):
        def refuse(port, timeout):
            raise ConnectionRefusedError("refused")

        adb = FakeSessionADB()
        with tempfile.TemporaryDirectory() as tmp:
            session = MinitouchSession(adb, binaries=self.binaries(tmp), connect=refuse, startup_timeout=0.3)
            with self.assertRaises(TouchError):
                session.start()
            self.assertTrue(any(c[:2] == ("forward", "--remove") for c in adb.calls))

    def test_bundled_x86_64_binary_exists(self):
        from dustbot2.touch.minitouch import vendor_dir
        self.assertTrue((vendor_dir() / "x86_64" / "minitouch").exists())


if __name__ == "__main__":
    unittest.main()
