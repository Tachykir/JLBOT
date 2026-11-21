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
import math
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
    game_package: str = "com.timhilgenstoehlerexpo.justlootapp"
    game_start_wait_s: float = 25.0      # ile czekać po włączeniu gry

    match_threshold: float = 0.85        # 0..1, im wyżej tym ostrzej
    poll_interval: list = field(default_factory=lambda: [1.0, 2.5])
    after_click_wait: list = field(default_factory=lambda: [1.5, 3.0])
    # po kliknięciu RUN 10 MORE nie klikaj go ponownie przez tyle sekund,
    # nawet jeśli gra jeszcze go pokazuje (zawsze jedno tapnięcie)
    reclick_after_s: float = 20.0

    # naturalne odstępy (sekundy: [min, mediana, max])
    reaction_s: list = field(default_factory=lambda: [0.4, 1.1, 6.0])     # zauważenie RUN 10 MORE
    distraction_chance: float = 0.07                                       # czasem "zagapienie się"
    distraction_s: list = field(default_factory=lambda: [4.0, 9.0, 40.0])
    nav_reaction_s: list = field(default_factory=lambda: [0.3, 0.8, 3.0])   # przed ikoną leaderboardu
    look_leaderboard_s: list = field(default_factory=lambda: [1.5, 3.5, 10.0])  # patrzenie na ranking
    tap_hold_ms: list = field(default_factory=lambda: [45, 130])           # jak długo palec dotyka
    # margines od krawędzi przycisku (ułamek szer./wys.), żeby nie klikać w ramkę
    click_margin_x: float = 0.06
    click_margin_y: float = 0.20

    # leaderboard automatycznie co ile minut (losowo z zakresu); [0, 0] = tylko
    # na żądanie (przycisk "Sprawdź teraz" w GUI)
    leaderboard_every_min: list = field(default_factory=lambda: [0, 0])
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

    def tap(self, x: int, y: int, hold_ms: int | None = None) -> None:
        """Tapnięcie. Z hold_ms - jak palec: przytrzymanie przez tyle ms
        z drobnym (0-2 px) przesunięciem zamiast idealnego `input tap`."""
        if not hold_ms:
            self._run(["shell", "input", "tap", str(x), str(y)])
            return
        x2 = x + random.randint(-2, 2)
        y2 = y + random.randint(-2, 2)
        self._run(["shell", "input", "swipe", str(x), str(y), str(x2), str(y2), str(int(hold_ms))])

    def stop_app(self, package: str) -> None:
        self._run(["shell", "am", "force-stop", package])

    def is_running(self, package: str) -> bool:
        out = self._run(["shell", "pidof", package], check=False).strip()
        if out:
            return True
        # starsze Androidy bez pidof
        ps = self._run(["shell", "ps"], check=False).decode(errors="replace")
        return any(line.split()[-1] == package for line in ps.splitlines() if line.split())

    def _launcher_component(self, package: str) -> str | None:
        """Activity startowa gry ("pakiet/.MainActivity") wg systemu.
        `monkey` bywa niedostępny (np. LDPlayer 14), więc startujemy przez
        `am start -n`."""
        if getattr(self, "_component", None):
            return self._component
        try:
            out = self._run(["shell", "cmd", "package", "resolve-activity", "--brief",
                             "-a", "android.intent.action.MAIN",
                             "-c", "android.intent.category.LAUNCHER", package]
                            ).decode(errors="replace")
        except RuntimeError as e:
            log.info("[%s] resolve-activity: %s", self.serial, e)
            return None
        for line in reversed(out.splitlines()):
            if line.strip().startswith(package + "/"):
                self._component = line.strip()
                return self._component
        return None

    def start_app(self, package: str) -> None:
        component = self._launcher_component(package)
        if component:
            out = self._run(["shell", "am", "start", "-n", component]).decode(errors="replace")
            if "Error" not in out:
                return
            log.info("[%s] am start: %s", self.serial, out.strip()[:200])
        self._run(["shell", "monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1"])

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


def human_pause(lo: float, median: float, hi: float) -> float:
    """Czas reakcji człowieka: rozkład log-normalny (najczęściej koło mediany,
    czasem wyraźnie dłużej, nigdy idealnie równo), obcięty do [lo, hi]."""
    return min(hi, max(lo, random.lognormvariate(math.log(median), 0.45)))


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


def gap_change(prev: LeaderboardEvent | None, cur: LeaderboardEvent) -> int | None:
    """O ile zmniejszył się dystans do rywala od poprzedniego udanego odczytu:
    było 800, jest 790 -> +10 (podbiłem o 10 LP); było 800, jest 820 -> −20."""
    if prev is None or not prev.valid or not cur.valid:
        return None
    return prev.missing - cur.missing


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
        self.stop_at: float | None = None  # koniec (time.time()), None = bez
        self.start_at: float | None = None  # "start od" (time.time()), None = od razu
        self._outside = False
        self._next_game_check = 0.0
        self._last_run10 = -1e9
        self.next_leaderboard = time.monotonic() + 20  # pierwszy raz zaraz po starcie
        # ostatni udany odczyt (także z poprzedniego uruchomienia) - do różnic
        hist = [e for e in load_leaderboard_history() if e.serial == cfg.serial]
        self.last_leaderboard: LeaderboardEvent | None = hist[-1] if hist else None
        self.session_start_lb: LeaderboardEvent | None = None  # pierwszy odczyt po Start

    def tap_in(self, key: str, rect, img: np.ndarray | None, mx: float, my: float,
               score: float = 1.0) -> tuple[int, int]:
        """Klik w losowy punkt prostokąta (px urządzenia) + zdarzenie dla GUI."""
        x1, y1, x2, y2 = rect
        fx, fy = self.clickers.setdefault(key, RandomClicker()).pick(mx, my)
        x = int(round(x1 + fx * (x2 - x1)))
        y = int(round(y1 + fy * (y2 - y1)))
        self.adb.tap(x, y, random.randint(*self.cfg.tap_hold_ms))
        if self.on_click:
            crop = img[y1:y2, x1:x2].copy() if img is not None else None
            self.on_click(ClickEvent(self.cfg.serial, key, fx, fy, x, y, score, crop))
        return x, y

    def frac_rect(self, img: np.ndarray, r) -> tuple[int, int, int, int]:
        h, w = img.shape[:2]
        return int(r[0] * w), int(r[1] * h), int(r[2] * w), int(r[3] * h)

    @property
    def auto_leaderboard(self) -> bool:
        return bool(self.cfg.leaderboard_every_min) and max(self.cfg.leaderboard_every_min) > 0

    def schedule_leaderboard(self) -> None:
        lo, hi = self.cfg.leaderboard_every_min
        self.next_leaderboard = time.monotonic() + 60 * random.uniform(lo, hi)

    def pause(self, rng: list) -> None:
        self.stop.wait(human_pause(*rng))

    def check_leaderboard(self) -> None:
        self.pause(self.cfg.nav_reaction_s)
        img = self.adb.screenshot()
        x, y = self.tap_in("leaderboard", self.frac_rect(img, self.cfg.nav_leaderboard), img, 0.1, 0.1)
        log.info("[%s] leaderboard (%d, %d)", self.tag, x, y)
        opened = time.monotonic()
        look = human_pause(*self.cfg.look_leaderboard_s)  # tyle "patrzymy" na ranking
        self.stop.wait(min(look, rand_between([1.2, 2.0])))
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

        rest = look - (time.monotonic() - opened)
        if rest > 0:
            self.stop.wait(rest)
        img = self.adb.screenshot()
        x, y = self.tap_in("profile", self.frac_rect(img, self.cfg.nav_profile), img, 0.1, 0.1)
        log.info("[%s] powrót do Profile (%d, %d)", self.tag, x, y)
        self.pause([0.8, 1.6, 4.0])

        ev = LeaderboardEvent(self.cfg.serial, result.my_lp if result else None,
                              result.rival_lp if result else None, self.cfg.rival_name, time.time())
        fmt = lambda v: f"{v:,}".replace(",", " ") if v is not None else "?"
        if ev.missing is not None:
            msg = (f"brakuje {fmt(ev.missing)} LP" if ev.missing > 0
                   else f"przewaga {fmt(-ev.missing)} LP")
        else:
            msg = "nie udało się odczytać"
        if ev.valid and self.session_start_lb is None:
            self.session_start_lb = ev
        total = gap_change(self.session_start_lb, ev)
        d = gap_change(self.last_leaderboard, ev)
        if total is not None:
            msg += f" | od startu {signed(total)}"
            if d is not None:
                msg += f" (ostatnio {signed(d)})"
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
        if time.monotonic() - self._last_run10 < self.cfg.reclick_after_s:
            return False  # dopiero kliknięty - czekamy, aż gra zareaguje
        # człowiek nie klika w tej samej milisekundzie: czas reakcji,
        # czasem dłuższe zagapienie się; potem sprawdź, czy przycisk dalej jest
        delay = human_pause(*self.cfg.reaction_s)
        if random.random() < self.cfg.distraction_chance:
            delay += human_pause(*self.cfg.distraction_s)
        self.stop.wait(delay)
        if self.stop.is_set():
            return False
        img = self.adb.screenshot()
        score, rect = self.detector.find(img)
        if rect is None or score < self.cfg.match_threshold:
            return False
        self._last_run10 = time.monotonic()
        x, y = self.tap_in("run10", rect, img, self.cfg.click_margin_x,
                           self.cfg.click_margin_y, score)
        self.clicks += 1
        log.info("[%s] RUN 10 MORE #%d (%d, %d) po %.1f s, dopasowanie %.2f",
                 self.tag, self.clicks, x, y, delay, score)
        return True

    # ------------------------------------------------------------ harmonogram
    # start_at / stop_at (time.time()):
    #   - tylko stop_at: minutnik - o tej porze zamknij grę i zakończ,
    #   - start_at (+ ew. stop_at): okno codzienne - przed startem i po
    #     końcu gra jest zamknięta, w oknie bot ją włącza (gdy wyłączona)
    #     i klika; po końcu okno przesuwa się na następny dzień.
    def in_window(self, now: float) -> bool:
        if self.start_at is None:
            return True
        if now < self.start_at:
            return False
        return self.stop_at is None or now < self.stop_at

    def _close_game(self, why: str) -> None:
        try:
            if self.adb.is_running(self.cfg.game_package):
                log.info("[%s] %s - zamykam grę", self.tag, why)
                self.adb.stop_app(self.cfg.game_package)
        except Exception as e:
            log.error("[%s] nie udało się zamknąć gry: %s", self.tag, e)

    def _ensure_game(self) -> None:
        """W oknie: gra ma działać - włącz, jeśli wyłączona (sprawdzane co minutę)."""
        if time.monotonic() < self._next_game_check:
            return
        self._next_game_check = time.monotonic() + 60
        if self.adb.is_running(self.cfg.game_package):
            return
        log.info("[%s] gra wyłączona - włączam", self.tag)
        self.adb.start_app(self.cfg.game_package)
        self.stop.wait(self.cfg.game_start_wait_s)
        self.next_leaderboard = max(self.next_leaderboard, time.monotonic() + 20)

    def _schedule_tick(self) -> bool:
        """True = poza oknem (nic nie klikamy)."""
        now = time.time()
        if self.stop_at is not None and now >= self.stop_at:
            if self.start_at is None:
                self.finish()
                return True
            # okno codzienne: następny dzień
            while self.stop_at <= now:
                self.start_at += 86400
                self.stop_at += 86400
            self._close_game("koniec okna")
            log.info("[%s] następne okno: %s–%s", self.tag,
                     time.strftime("%d.%m %H:%M", time.localtime(self.start_at)),
                     time.strftime("%H:%M", time.localtime(self.stop_at)))
            self._outside = True
            return True
        if not self.in_window(now):
            if not self._outside or time.monotonic() >= self._next_game_check:
                self._close_game("poza oknem")
                self._next_game_check = time.monotonic() + 60
                if not self._outside:
                    log.info("[%s] czekam na start o %s", self.tag,
                             time.strftime("%H:%M", time.localtime(self.start_at)))
            self._outside = True
            return True
        if self._outside:
            log.info("[%s] start okna", self.tag)
            self._outside = False
            self._next_game_check = 0
        self._ensure_game()
        return False

    def run(self) -> None:
        log.info("[%s] start - czekam na RUN 10 MORE", self.tag)
        while not self.stop.is_set():
            try:
                if self._schedule_tick():
                    # poza oknem: sprawdzaj co 5 s, ale wstań dokładnie na start
                    wait = 5.0
                    if self.start_at is not None and self.start_at > time.time():
                        wait = min(wait, max(0.2, self.start_at - time.time()))
                    self.stop.wait(wait)
                    continue
            except Exception as e:
                log.error("[%s] harmonogram: %s", self.tag, e)
                self.stop.wait(10)
                continue
            if self.stop.is_set():
                break
            try:
                lb_on = self.auto_leaderboard
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

    def finish(self) -> None:
        """Minutnik: zamknij grę i przestań klikać."""
        log.info("[%s] minutnik - zamykam grę i kończę", self.tag)
        try:
            self.adb.stop_app(self.cfg.game_package)
        except Exception as e:
            log.error("[%s] nie udało się zamknąć gry: %s", self.tag, e)
        self.stop.set()


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


def parse_stop_time(text: str, now: float | None = None) -> float:
    """Minutnik: "90" / "90m" = za 90 minut, "1h30" = za 1 h 30 min,
    "23:15" = o tej godzinie (jutro, jeśli już minęła). Zwraca time.time()."""
    now = time.time() if now is None else now
    t = text.strip().lower().replace(" ", "")
    if not t:
        raise ValueError("pusty minutnik")
    if "h" in t:
        h, _, m = t.partition("h")
        return now + 3600 * int(h or 0) + 60 * int(m.rstrip("m") or 0)
    if ":" in t:
        h, m = (int(v) for v in t.split(":", 1))
        if not (0 <= h < 24 and 0 <= m < 60):
            raise ValueError(text)
        lt = time.localtime(now)
        target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, m, 0, 0, 0, -1))
        # godzina, która dziś już minęła = jutro
        if target <= now:
            target += 86400
        return target
    minutes = float(t.rstrip("m").replace(",", "."))
    if minutes <= 0:
        raise ValueError(text)
    return now + 60 * minutes


def make_window(start_text: str, stop_text: str,
                now: float | None = None) -> tuple[float | None, float | None]:
    """(start_at, stop_at) z pól "Start od" i "Koniec". Start to godzina
    (HH:MM); koniec to godzina albo czas trwania od startu (90, 1h30).
    Gdy okno trwa właśnie teraz, start jest dzisiejszy (w przeszłości)."""
    now = time.time() if now is None else now
    start_text, stop_text = start_text.strip(), stop_text.strip()
    if not start_text:
        return None, (parse_stop_time(stop_text, now) if stop_text else None)
    if ":" not in start_text:
        raise ValueError("start podaj jako godzinę, np. 8:00")
    h, m = (int(v) for v in start_text.split(":", 1))
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError(start_text)
    lt = time.localtime(now)
    today = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, m, 0, 0, 0, -1))
    # trwające okno: wczorajszy start (okno przez północ) albo dzisiejszy
    for start in (today - 86400, today):
        if start <= now:
            stop = parse_stop_time(stop_text, start) if stop_text else None
            if stop is None and start == today:
                return start, None
            if stop is not None and stop > now:
                return start, stop
    start = today if today > now else today + 86400
    return start, (parse_stop_time(stop_text, start) if stop_text else None)


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
    ap.add_argument("--stop-after", metavar="MIN|HH:MM",
                    help="minutnik: po tylu minutach (albo o tej godzinie) zamknij grę i zakończ")
    ap.add_argument("--start-at", metavar="HH:MM",
                    help="okno: od tej godziny gra włączona (z --stop-after codziennie), wcześniej wyłączona")
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
        if args.stop_after or args.start_at:
            bot.start_at, bot.stop_at = make_window(args.start_at or "", args.stop_after or "")
            hm = lambda t: time.strftime("%d.%m %H:%M", time.localtime(t)) if t else "-"
            log.info("[%s] harmonogram: start %s, koniec %s", bot.tag, hm(bot.start_at), hm(bot.stop_at))
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
