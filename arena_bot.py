"""
arena_bot.py
============
Minimalny bot do Just Loot (LDPlayer, ADB):
  - gdy na ekranie głównym pojawi się przycisk **RUN 10 MORE** (arena
    skończyła 10 walk), bot klika go (wykrywany na obrazie, wzorzec
    templates/run10more.png),
  - co jakiś czas wchodzi na leaderboard, odczytuje LP swoje i MossG,
    wypisuje, ile brakuje, i wraca do Profile.
Każde kliknięcie trafia w losowy punkt przycisku (nigdy dwa razy w to samo
miejsce).

Uruchomienie:
    python gui.py                        # okno z podglądem i mapą kliknięć
    python arena_bot.py                  # bez okna, instancje z config.json
    python arena_bot.py --serial emulator-5566
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import random
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

import lp_reader

log = logging.getLogger("arena")

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
TEMPLATE_PATH = BASE_DIR / "templates" / "run10more.png"
TEMPLATE_REF_WIDTH = 547  # szerokość ekranu, z którego wycięto wzorzec


@dataclass
class Config:
    serial: str = "127.0.0.1:5555"
    name: str = ""

    match_threshold: float = 0.85        # 0..1, im wyżej tym ostrzej
    poll_interval: list = field(default_factory=lambda: [1.0, 2.5])
    after_click_wait: list = field(default_factory=lambda: [1.5, 3.0])
    # margines od krawędzi przycisku (ułamek szer./wys.), żeby nie klikać w ramkę
    click_margin_x: float = 0.06
    click_margin_y: float = 0.20

    # leaderboard: co ile minut (losowo z zakresu), 0 = wyłączone
    leaderboard_every_min: list = field(default_factory=lambda: [10, 20])
    rival_name: str = "MossG"            # tylko do opisu - szukany wzorcem templates/mossg.png
    # przyciski paska nawigacji (ułamki ekranu x1, y1, x2, y2)
    nav_leaderboard: list = field(default_factory=lambda: [0.775, 0.945, 0.835, 0.980])
    nav_profile: list = field(default_factory=lambda: [0.025, 0.940, 0.085, 0.975])

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        known = {f.name for f in fields(cls)}
        unknown = set(d) - known
        if unknown:
            log.warning("Nieznane pola w configu (pomijam): %s", ", ".join(sorted(unknown)))
        return cls(**{k: v for k, v in d.items() if k in known})


# --------------------------------------------------------------------- #
# ADB
# --------------------------------------------------------------------- #
def default_adb_path() -> str:
    bundled = BASE_DIR / "vendor" / "adb" / "adb.exe"
    if sys.platform == "win32" and bundled.exists():
        return str(bundled)
    return "adb"


def list_devices(adb_path: str) -> list[str]:
    try:
        p = subprocess.run([adb_path, "devices"], capture_output=True, timeout=15,
                           creationflags=_NO_WINDOW)
    except Exception as e:
        log.error("adb devices: %s", e)
        return []
    out = []
    for line in p.stdout.decode(errors="replace").splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            out.append(parts[0])
    return out


_RAW_FORMATS = {1: cv2.COLOR_RGBA2BGR, 2: cv2.COLOR_RGBA2BGR, 5: cv2.COLOR_BGRA2BGR}


def decode_raw(data: bytes) -> np.ndarray | None:
    """Wynik `screencap` bez -p: nagłówek (w, h, format; Android 9+ jeszcze
    4 bajty) i piksele RGBA. None, gdy format się nie zgadza."""
    if len(data) < 16:
        return None
    w, h, fmt = (int(v) for v in np.frombuffer(data[:12], dtype="<u4"))
    if fmt not in _RAW_FORMATS or not (0 < w < 10000 and 0 < h < 10000):
        return None
    header = len(data) - w * h * 4
    if header not in (12, 16):
        return None
    px = np.frombuffer(data, dtype=np.uint8, offset=header).reshape(h, w, 4)
    return cv2.cvtColor(px, _RAW_FORMATS[fmt])


class ADB:
    def __init__(self, serial: str, adb_path: str):
        self.serial = serial
        self.adb = adb_path
        self._png_only = False
        if ":" in serial:
            self._run(["connect", serial], with_serial=False, check=False)

    def _run(self, args, with_serial=True, check=True, timeout=20.0) -> bytes:
        cmd = [self.adb] + (["-s", self.serial] if with_serial else []) + list(args)
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=_NO_WINDOW)
        if check and p.returncode != 0:
            raise RuntimeError(f"adb {' '.join(args)}: {p.stderr.decode(errors='replace').strip()}")
        return p.stdout

    def tap(self, x: int, y: int) -> None:
        self._run(["shell", "input", "tap", str(x), str(y)])

    def screenshot(self) -> np.ndarray:
        # surowe piksele zamiast PNG - emulator nie traci czasu na kompresję
        if not self._png_only:
            img = decode_raw(self._run(["exec-out", "screencap"]))
            if img is not None:
                return img
            self._png_only = True
            log.info("[%s] surowy screencap nieobsługiwany - używam PNG", self.serial)
        data = self._run(["exec-out", "screencap", "-p"])
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise RuntimeError("Nie udało się odczytać zrzutu ekranu")
        return img


# --------------------------------------------------------------------- #
# Wykrywanie RUN 10 MORE
# --------------------------------------------------------------------- #
class RunButtonDetector:
    def __init__(self, template_path: Path = TEMPLATE_PATH):
        tpl = cv2.imread(str(template_path), cv2.IMREAD_GRAYSCALE)
        if tpl is None:
            raise FileNotFoundError(f"Brak wzorca {template_path}")
        self.templates = [cv2.resize(tpl, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
                          for s in np.linspace(0.9, 1.1, 9)]

    def find(self, img: np.ndarray) -> tuple[float, tuple[int, int, int, int] | None]:
        """(wynik 0..1, prostokąt przycisku w px urządzenia). Ekran jest
        sprowadzany do szerokości wzorca, więc działa w każdej rozdzielczości."""
        h, w = img.shape[:2]
        k = TEMPLATE_REF_WIDTH / w
        g = cv2.cvtColor(cv2.resize(img, (TEMPLATE_REF_WIDTH, int(round(h * k))),
                                    interpolation=cv2.INTER_AREA), cv2.COLOR_BGR2GRAY)
        best_score, best_rect = 0.0, None
        for t in self.templates:
            if t.shape[0] > g.shape[0] or t.shape[1] > g.shape[1]:
                continue
            r = cv2.matchTemplate(g, t, cv2.TM_CCOEFF_NORMED)
            _, mv, _, ml = cv2.minMaxLoc(r)
            if mv > best_score:
                best_score = float(mv)
                best_rect = (int(ml[0] / k), int(ml[1] / k),
                             int((ml[0] + t.shape[1]) / k), int((ml[1] + t.shape[0]) / k))
        return best_score, best_rect


# --------------------------------------------------------------------- #
# Losowe kliknięcie
# --------------------------------------------------------------------- #
class RandomClicker:
    """Losowy punkt w prostokącie (gęściej przy środku); nigdy (prawie) ten
    sam punkt co poprzednio."""

    def __init__(self):
        self._last: tuple[float, float] | None = None

    def pick(self, mx: float, my: float) -> tuple[float, float]:
        """Zwraca (fx, fy) - położenie względem całego przycisku, 0..1."""
        for _ in range(30):
            fx = mx + (1 - 2 * mx) * min(max(random.gauss(0.5, 0.22), 0.0), 1.0)
            fy = my + (1 - 2 * my) * min(max(random.gauss(0.5, 0.22), 0.0), 1.0)
            if self._last is None or abs(fx - self._last[0]) + abs(fy - self._last[1]) > 0.03:
                break
        self._last = (fx, fy)
        return fx, fy


def rand_between(rng: list) -> float:
    lo, hi = rng
    # zwykle bliżej dolnej granicy, czasem dłużej - jak człowiek
    return lo + (hi - lo) * random.betavariate(2.0, 4.0)


# --------------------------------------------------------------------- #
# Bot
# --------------------------------------------------------------------- #
@dataclass
class ClickEvent:
    serial: str
    key: str            # "run10" | "leaderboard" | "profile"
    fx: float           # 0..1 względem prostokąta przycisku
    fy: float
    x: int              # px urządzenia
    y: int
    score: float
    button: np.ndarray  # wycinek ekranu z przyciskiem (BGR)


@dataclass
class LeaderboardEvent:
    serial: str
    my_lp: int | None
    rival_lp: int | None
    rival_name: str
    when: float

    @property
    def missing(self) -> int | None:
        if self.my_lp is None or self.rival_lp is None:
            return None
        return self.rival_lp - self.my_lp

    @property
    def valid(self) -> bool:
        return self.missing is not None


def signed(v: int | None) -> str:
    """+54 / −42 / ±0 (spacja jako separator tysięcy)."""
    if v is None:
        return "?"
    if v == 0:
        return "±0"
    return ("+" if v > 0 else "−") + f"{abs(v):,}".replace(",", " ")


def leaderboard_deltas(prev: LeaderboardEvent | None, cur: LeaderboardEvent):
    """Zmiana (moje LP, LP rywala, brakuje) od poprzedniego udanego odczytu."""
    if prev is None or not prev.valid or not cur.valid:
        return None
    return cur.my_lp - prev.my_lp, cur.rival_lp - prev.rival_lp, cur.missing - prev.missing


@dataclass
class FrameEvent:
    serial: str
    frame: np.ndarray
    score: float
    rect: tuple | None
    visible: bool


class ArenaBot:
    def __init__(self, cfg: Config, adb_path: str, stop: threading.Event,
                 on_click: Callable[[ClickEvent], None] | None = None,
                 on_frame: Callable[[FrameEvent], None] | None = None,
                 on_leaderboard: Callable[[LeaderboardEvent], None] | None = None):
        self.cfg = cfg
        self.stop = stop
        self.adb = ADB(cfg.serial, adb_path)
        self.detector = RunButtonDetector()
        self.clickers: dict[str, RandomClicker] = {}
        self.on_click = on_click
        self.on_frame = on_frame
        self.on_leaderboard = on_leaderboard
        self.tag = cfg.name or cfg.serial
        self.clicks = 0
        self.lb_reader = lp_reader.LeaderboardReader()
        self.check_leaderboard_now = threading.Event()
        self.next_leaderboard = time.monotonic() + 20  # pierwszy raz zaraz po starcie
        # ostatni udany odczyt (także z poprzedniego uruchomienia) - do różnic
        hist = [e for e in load_leaderboard_history() if e.serial == cfg.serial]
        self.last_leaderboard: LeaderboardEvent | None = hist[-1] if hist else None

    def tap_in(self, key: str, rect, img: np.ndarray | None, mx: float, my: float,
               score: float = 1.0) -> tuple[int, int]:
        """Klik w losowy punkt prostokąta (px urządzenia) + zdarzenie dla GUI."""
        x1, y1, x2, y2 = rect
        fx, fy = self.clickers.setdefault(key, RandomClicker()).pick(mx, my)
        x = int(round(x1 + fx * (x2 - x1)))
        y = int(round(y1 + fy * (y2 - y1)))
        self.adb.tap(x, y)
        if self.on_click:
            crop = img[y1:y2, x1:x2].copy() if img is not None else None
            self.on_click(ClickEvent(self.cfg.serial, key, fx, fy, x, y, score, crop))
        return x, y

    def frac_rect(self, img: np.ndarray, r) -> tuple[int, int, int, int]:
        h, w = img.shape[:2]
        return int(r[0] * w), int(r[1] * h), int(r[2] * w), int(r[3] * h)

    def schedule_leaderboard(self) -> None:
        lo, hi = self.cfg.leaderboard_every_min
        self.next_leaderboard = time.monotonic() + 60 * random.uniform(lo, hi)

    def check_leaderboard(self) -> None:
        img = self.adb.screenshot()
        x, y = self.tap_in("leaderboard", self.frac_rect(img, self.cfg.nav_leaderboard), img, 0.1, 0.1)
        log.info("[%s] leaderboard (%d, %d)", self.tag, x, y)
        self.stop.wait(rand_between([1.5, 2.5]))
        result = None
        for _ in range(5):
            if self.stop.is_set():
                return
            frame = self.adb.screenshot()
            if self.on_frame:
                self.on_frame(FrameEvent(self.cfg.serial, frame, 0.0, None, False))
            lb = self.lb_reader.read(frame)
            if lb.my_lp is not None and lb.rival_lp is not None:
                result = lb
                break
            result = result or lb
            self.stop.wait(1.0)

        img = self.adb.screenshot()
        x, y = self.tap_in("profile", self.frac_rect(img, self.cfg.nav_profile), img, 0.1, 0.1)
        log.info("[%s] powrót do Profile (%d, %d)", self.tag, x, y)
        self.stop.wait(rand_between([1.0, 2.0]))

        ev = LeaderboardEvent(self.cfg.serial, result.my_lp if result else None,
                              result.rival_lp if result else None, self.cfg.rival_name, time.time())
        fmt = lambda v: f"{v:,}".replace(",", " ") if v is not None else "?"
        if ev.missing is not None:
            msg = (f"brakuje {fmt(ev.missing)} LP" if ev.missing > 0
                   else f"przewaga {fmt(-ev.missing)} LP")
        else:
            msg = "nie udało się odczytać"
        d = leaderboard_deltas(self.last_leaderboard, ev)
        if d:
            mins = (ev.when - self.last_leaderboard.when) / 60
            msg += (f" | od ostatniego ({mins:.0f} min): ja {signed(d[0])}, "
                    f"{ev.rival_name} {signed(d[1])}, różnica {signed(d[2])}")
        log.info("[%s] %s %s LP | ja %s LP | %s", self.tag, ev.rival_name, fmt(ev.rival_lp),
                 fmt(ev.my_lp), msg)
        if ev.valid:
            self.last_leaderboard = ev
        save_leaderboard(ev)
        if self.on_leaderboard:
            self.on_leaderboard(ev)

    def step(self) -> bool:
        """Jedno sprawdzenie ekranu; True = kliknięto."""
        img = self.adb.screenshot()
        score, rect = self.detector.find(img)
        visible = rect is not None and score >= self.cfg.match_threshold
        if self.on_frame:
            self.on_frame(FrameEvent(self.cfg.serial, img, score, rect, visible))
        if not visible:
            return False
        x, y = self.tap_in("run10", rect, img, self.cfg.click_margin_x,
                           self.cfg.click_margin_y, score)
        self.clicks += 1
        log.info("[%s] RUN 10 MORE #%d (%d, %d) dopasowanie %.2f", self.tag, self.clicks, x, y, score)
        return True

    def run(self) -> None:
        log.info("[%s] start - czekam na RUN 10 MORE", self.tag)
        while not self.stop.is_set():
            try:
                lb_on = self.cfg.leaderboard_every_min and max(self.cfg.leaderboard_every_min) > 0
                if self.check_leaderboard_now.is_set() or (
                        lb_on and time.monotonic() >= self.next_leaderboard):
                    self.check_leaderboard_now.clear()
                    self.schedule_leaderboard()
                    self.check_leaderboard()
                    continue
                clicked = self.step()
                self.stop.wait(rand_between(self.cfg.after_click_wait if clicked
                                            else self.cfg.poll_interval))
            except Exception as e:
                log.error("[%s] błąd: %s", self.tag, e)
                self.stop.wait(5)
        log.info("[%s] zatrzymany", self.tag)


LEADERBOARD_CSV = BASE_DIR / "leaderboard.csv"


def save_leaderboard(ev: LeaderboardEvent) -> None:
    """Historia odczytów: czas, urządzenie, moje LP, LP rywala, brakuje."""
    try:
        new = not LEADERBOARD_CSV.exists()
        with open(LEADERBOARD_CSV, "a", encoding="utf-8") as f:
            if new:
                f.write("time;serial;my_lp;rival;rival_lp;missing\n")
            f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ev.when))};{ev.serial};"
                    f"{ev.my_lp if ev.my_lp is not None else ''};{ev.rival_name};"
                    f"{ev.rival_lp if ev.rival_lp is not None else ''};"
                    f"{ev.missing if ev.missing is not None else ''}\n")
    except OSError as e:
        log.warning("nie zapisano %s: %s", LEADERBOARD_CSV, e)


def load_leaderboard_history() -> list[LeaderboardEvent]:
    """Udane odczyty z leaderboard.csv (najstarsze pierwsze)."""
    out = []
    try:
        lines = LEADERBOARD_CSV.read_text(encoding="utf-8").strip().splitlines()[1:]
    except OSError:
        return out
    for line in lines:
        try:
            t, serial, my, rival_name, rival, _m = line.split(";")
            ev = LeaderboardEvent(serial, int(my) if my else None, int(rival) if rival else None,
                                  rival_name, time.mktime(time.strptime(t, "%Y-%m-%d %H:%M:%S")))
        except ValueError:
            continue
        if ev.valid:
            out.append(ev)
    return out


# --------------------------------------------------------------------- #
def load_instances(path: Path) -> list[dict]:
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    defaults = {k: v for k, v in data.items() if k != "instances"}
    return [{**defaults, **inst} for inst in data.get("instances", [])]


def main() -> None:
    ap = argparse.ArgumentParser(description="Just Loot - klikanie RUN 10 MORE")
    ap.add_argument("--config", default=str(BASE_DIR / "config.json"))
    ap.add_argument("--serial", help="tylko ta instancja")
    ap.add_argument("--adb", default=os.environ.get("ADB_PATH") or default_adb_path())
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    instances = load_instances(Path(args.config))
    if args.serial:
        instances = [i for i in instances if i.get("serial") == args.serial] or [{"serial": args.serial}]
    if not instances:
        instances = [{"serial": s} for s in list_devices(args.adb)]
    if not instances:
        log.error("Brak urządzeń - sprawdź `adb devices` albo config.json")
        sys.exit(1)

    stop = threading.Event()
    threads = []
    for inst in instances:
        cfg = Config.from_dict(inst)
        bot = ArenaBot(cfg, args.adb, stop)
        t = threading.Thread(target=bot.run, name=cfg.serial, daemon=True)
        t.start()
        threads.append(t)
    try:
        while any(t.is_alive() for t in threads):
            time.sleep(0.5)
    except KeyboardInterrupt:
        log.info("Zatrzymuję...")
        stop.set()
        for t in threads:
            t.join(timeout=10)


if __name__ == "__main__":
    main()
