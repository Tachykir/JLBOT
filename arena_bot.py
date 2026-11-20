"""
arena_bot.py
============
Minimalny bot do Just Loot (LDPlayer, ADB). Jedna pętla:

  1. klik w "..." (prawy dolny róg paska nawigacji),
  2. klik "Arena" w wysuniętym menu,
  3. przewinięcie w dół o ok. 1,5 ekranu,
  4. klik FIND MATCH (wykrywany na obrazie),
  5. restart aplikacji i od nowa.

Każde kliknięcie trafia w losowy punkt obszaru (nigdy dwa razy w to samo
miejsce), przerwy i przewijanie też są losowe.

Uruchomienie:
    python arena_bot.py                  # wszystkie instancje z config.json
    python arena_bot.py --serial 127.0.0.1:5555
    python arena_bot.py --loops 5        # tylko 5 pętli
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

import cv2
import numpy as np

log = logging.getLogger("arena")

_NO_WINDOW = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
BASE_DIR = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))


# --------------------------------------------------------------------- #
# Konfiguracja. Obszary kliknięć są ułamkami ekranu [x1, y1, x2, y2],
# więc działają w każdej rozdzielczości (zmierzone na zrzutach 9:16).
# --------------------------------------------------------------------- #
@dataclass
class Config:
    serial: str = "127.0.0.1:5555"
    name: str = ""
    game_package: str = "com.timhilgenstoehlerexpo.justlootapp"

    more_button: list = field(default_factory=lambda: [0.885, 0.940, 0.965, 0.978])
    arena_menu_item: list = field(default_factory=lambda: [0.50, 0.705, 0.93, 0.765])
    # zapasowy obszar FIND MATCH, gdyby nie udało się go wykryć na obrazie
    find_match_fallback: list = field(default_factory=lambda: [0.12, 0.485, 0.88, 0.540])

    scroll_screens: float = 1.5          # ile ekranów przewinąć (łącznie)
    scroll_jitter: float = 0.15          # +- losowo do scroll_screens
    swipe_ms: list = field(default_factory=lambda: [450, 800])

    delay_after_more: list = field(default_factory=lambda: [0.7, 1.6])
    delay_after_arena: list = field(default_factory=lambda: [2.0, 3.5])
    delay_after_scroll: list = field(default_factory=lambda: [0.8, 1.8])
    delay_after_find_match: list = field(default_factory=lambda: [1.5, 3.0])
    delay_between_loops: list = field(default_factory=lambda: [0.5, 2.0])

    # po restarcie: czekaj aż widać pasek nawigacji (koło JUST LOOT),
    # najmniej startup_min_s, najwyżej startup_timeout_s
    startup_min_s: float = 6.0
    startup_timeout_s: float = 60.0
    find_match_attempts: int = 4

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


class ADB:
    def __init__(self, serial: str, adb_path: str):
        self.serial = serial
        self.adb = adb_path
        if ":" in serial:
            self._run(["connect", serial], with_serial=False, check=False)
        self.width, self.height = self._screen_size()

    def _run(self, args, with_serial=True, check=True, timeout=20.0) -> bytes:
        cmd = [self.adb] + (["-s", self.serial] if with_serial else []) + list(args)
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=_NO_WINDOW)
        if check and p.returncode != 0:
            raise RuntimeError(f"adb {' '.join(args)}: {p.stderr.decode(errors='replace').strip()}")
        return p.stdout

    def shell(self, *args) -> str:
        return self._run(["shell", *args]).decode(errors="replace")

    def _screen_size(self) -> tuple[int, int]:
        out = self.shell("wm", "size")
        # "Physical size: 1080x1920" (+ ewentualnie "Override size: ...")
        size = out.strip().splitlines()[-1].split(":")[-1].strip()
        w, h = (int(v) for v in size.split("x"))
        return w, h

    def tap(self, x: int, y: int) -> None:
        self.shell("input", "tap", str(x), str(y))

    def swipe(self, x1, y1, x2, y2, ms) -> None:
        self.shell("input", "swipe", str(x1), str(y1), str(x2), str(y2), str(ms))

    def screenshot(self) -> np.ndarray:
        data = self._run(["exec-out", "screencap", "-p"])
        img = cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise RuntimeError("Nie udało się odczytać zrzutu ekranu")
        return img

    def restart_app(self, package: str) -> None:
        self.shell("am", "force-stop", package)
        time.sleep(random.uniform(0.8, 1.6))
        self.shell("monkey", "-p", package, "-c", "android.intent.category.LAUNCHER", "1")


# --------------------------------------------------------------------- #
# Losowość
# --------------------------------------------------------------------- #
class RandomClicker:
    """Losowy punkt w obszarze; pilnuje, żeby nie trafić w (prawie) ten sam
    punkt co poprzednio w tym samym obszarze."""

    def __init__(self, adb: ADB):
        self.adb = adb
        self._last: dict[str, tuple[int, int]] = {}

    def point_in(self, rect_px, key: str) -> tuple[int, int]:
        x1, y1, x2, y2 = rect_px
        last = self._last.get(key)
        min_dist = max(3, min(x2 - x1, y2 - y1) // 8)
        for _ in range(20):
            # trochę gęściej przy środku, ale na całym obszarze
            fx = min(max(random.gauss(0.5, 0.22), 0.0), 1.0)
            fy = min(max(random.gauss(0.5, 0.22), 0.0), 1.0)
            p = (int(round(x1 + fx * (x2 - x1))), int(round(y1 + fy * (y2 - y1))))
            if last is None or abs(p[0] - last[0]) + abs(p[1] - last[1]) >= min_dist:
                break
        self._last[key] = p
        return p

    def frac_rect(self, rect) -> tuple[int, int, int, int]:
        w, h = self.adb.width, self.adb.height
        return int(rect[0] * w), int(rect[1] * h), int(rect[2] * w), int(rect[3] * h)

    def click(self, rect_px, key: str) -> tuple[int, int]:
        x, y = self.point_in(rect_px, key)
        self.adb.tap(x, y)
        return x, y


def pause(rng: list) -> None:
    lo, hi = rng
    # zwykle bliżej dolnej granicy, czasem dłużej - jak człowiek
    time.sleep(lo + (hi - lo) * random.betavariate(2.0, 4.0))


# --------------------------------------------------------------------- #
# Wykrywanie na obrazie
# --------------------------------------------------------------------- #
def find_white_button(img: np.ndarray) -> tuple[int, int, int, int] | None:
    """FIND MATCH = jedyny szeroki, wypełniony biały przycisk na ekranie
    areny (RE-LOCK i AUTO mają tylko obramowanie). Zwraca bbox w px."""
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    mask = (gray > 225).astype(np.uint8) * 255
    # zamknij dziury po czarnym napisie
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best = None
    for c in contours:
        x, y, bw, bh = cv2.boundingRect(c)
        if bw < 0.70 * w or not (0.045 * h <= bh <= 0.12 * h):
            continue
        if y + bh > 0.88 * h:  # nie pasek nawigacji
            continue
        fill = cv2.contourArea(c) / float(bw * bh)
        if fill < 0.85:
            continue
        if best is None or bw * bh > best[2] * best[3]:
            best = (x, y, bw, bh)
    if best is None:
        return None
    x, y, bw, bh = best
    # bez krawędzi (zaokrąglone rogi)
    mx, my = int(bw * 0.08), int(bh * 0.2)
    return x + mx, y + my, x + bw - mx, y + bh - my


def nav_bar_visible(img: np.ndarray) -> bool:
    """Białe koło JUST LOOT na dole środka = gra załadowana, pasek widoczny."""
    h, w = img.shape[:2]
    roi = cv2.cvtColor(img[int(0.925 * h):int(0.97 * h), int(0.44 * w):int(0.56 * w)],
                       cv2.COLOR_BGR2GRAY)
    return roi.size > 0 and float((roi > 200).mean()) > 0.55


# --------------------------------------------------------------------- #
# Pętla
# --------------------------------------------------------------------- #
class ArenaBot:
    def __init__(self, cfg: Config, adb_path: str, stop: threading.Event):
        self.cfg = cfg
        self.stop = stop
        self.adb = ADB(cfg.serial, adb_path)
        self.clicker = RandomClicker(self.adb)
        self.tag = cfg.name or cfg.serial
        log.info("[%s] ekran %dx%d", self.tag, self.adb.width, self.adb.height)

    def _wait(self, rng) -> None:
        lo, hi = rng
        self.stop.wait(lo + (hi - lo) * random.betavariate(2.0, 4.0))

    def wait_for_game(self) -> None:
        start = time.monotonic()
        self.stop.wait(self.cfg.startup_min_s)
        while not self.stop.is_set() and time.monotonic() - start < self.cfg.startup_timeout_s:
            try:
                if nav_bar_visible(self.adb.screenshot()):
                    self._wait([1.0, 2.5])  # niech UI się ustabilizuje
                    return
            except Exception as e:
                log.debug("[%s] zrzut: %s", self.tag, e)
            self.stop.wait(1.0)
        log.warning("[%s] gra nie wstała w %.0f s - próbuję dalej",
                    self.tag, self.cfg.startup_timeout_s)

    def scroll_down(self, screens: float) -> None:
        w, h = self.adb.width, self.adb.height
        remaining = screens
        while remaining > 0.05 and not self.stop.is_set():
            step = min(remaining, random.uniform(0.40, 0.55))
            x1 = int(w * random.uniform(0.25, 0.75))
            y1 = int(h * random.uniform(0.70, 0.80))
            x2 = x1 + int(w * random.uniform(-0.05, 0.05))
            y2 = y1 - int(h * step)
            self.adb.swipe(x1, y1, x2, y2, random.randint(*self.cfg.swipe_ms))
            remaining -= step
            self._wait([0.25, 0.7])

    def click_find_match(self) -> bool:
        for attempt in range(self.cfg.find_match_attempts):
            if self.stop.is_set():
                return False
            rect = find_white_button(self.adb.screenshot())
            if rect:
                x, y = self.clicker.click(rect, "find_match")
                log.info("[%s] FIND MATCH (%d, %d)", self.tag, x, y)
                return True
            log.info("[%s] nie widzę FIND MATCH (próba %d) - przewijam", self.tag, attempt + 1)
            # gra mogła przewinąć za mało/za dużo: raz w dół, raz w górę
            w, h = self.adb.width, self.adb.height
            x = int(w * random.uniform(0.3, 0.7))
            if attempt % 2 == 0:
                self.scroll_down(0.25)
            else:
                self.adb.swipe(x, int(h * random.uniform(0.33, 0.38)), x,
                               int(h * random.uniform(0.55, 0.62)), random.randint(*self.cfg.swipe_ms))
            self._wait([0.6, 1.2])
        x, y = self.clicker.click(self.clicker.frac_rect(self.cfg.find_match_fallback), "find_match_fb")
        log.warning("[%s] FIND MATCH nie wykryty - klik w obszar zapasowy (%d, %d)", self.tag, x, y)
        return True

    def one_loop(self) -> None:
        c = self.cfg
        x, y = self.clicker.click(self.clicker.frac_rect(c.more_button), "more")
        log.info("[%s] ... (%d, %d)", self.tag, x, y)
        self._wait(c.delay_after_more)

        x, y = self.clicker.click(self.clicker.frac_rect(c.arena_menu_item), "arena")
        log.info("[%s] Arena (%d, %d)", self.tag, x, y)
        self._wait(c.delay_after_arena)

        self.scroll_down(c.scroll_screens + random.uniform(-c.scroll_jitter, c.scroll_jitter))
        self._wait(c.delay_after_scroll)

        self.click_find_match()
        self._wait(c.delay_after_find_match)

        log.info("[%s] restart aplikacji", self.tag)
        self.adb.restart_app(c.game_package)
        self.wait_for_game()
        self._wait(c.delay_between_loops)

    def run(self, loops: int = 0, restart_first: bool = True) -> None:
        if restart_first:
            self.adb.restart_app(self.cfg.game_package)
            self.wait_for_game()
        n = 0
        while not self.stop.is_set() and (loops <= 0 or n < loops):
            try:
                self.one_loop()
                n += 1
                log.info("[%s] pętla %d gotowa", self.tag, n)
            except Exception as e:
                log.error("[%s] błąd: %s - restart i dalej", self.tag, e)
                self.stop.wait(5)
                try:
                    self.adb.restart_app(self.cfg.game_package)
                    self.wait_for_game()
                except Exception as e2:
                    log.error("[%s] restart nieudany: %s", self.tag, e2)
                    self.stop.wait(15)


# --------------------------------------------------------------------- #
def load_instances(path: Path) -> list[dict]:
    if not path.exists():
        log.warning("Brak %s - używam domyślnej instancji 127.0.0.1:5555", path)
        return [{}]
    data = json.loads(path.read_text(encoding="utf-8"))
    defaults = {k: v for k, v in data.items() if k != "instances"}
    return [{**defaults, **inst} for inst in data.get("instances", [{}])]


def main() -> None:
    ap = argparse.ArgumentParser(description="Just Loot - pętla areny")
    ap.add_argument("--config", default=str(BASE_DIR / "config.json"))
    ap.add_argument("--serial", help="tylko ta instancja")
    ap.add_argument("--loops", type=int, default=0, help="liczba pętli (0 = bez końca)")
    ap.add_argument("--no-initial-restart", action="store_true",
                    help="nie restartuj gry przed pierwszą pętlą")
    ap.add_argument("--adb", default=os.environ.get("ADB_PATH") or default_adb_path())
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")

    instances = load_instances(Path(args.config))
    if args.serial:
        instances = [i for i in instances if i.get("serial") == args.serial] or [{"serial": args.serial}]

    stop = threading.Event()
    threads = []
    for inst in instances:
        cfg = Config.from_dict(inst)
        try:
            bot = ArenaBot(cfg, args.adb, stop)
        except Exception as e:
            log.error("[%s] nie mogę połączyć: %s", cfg.serial, e)
            continue
        t = threading.Thread(target=bot.run, args=(args.loops, not args.no_initial_restart),
                             name=cfg.serial, daemon=True)
        t.start()
        threads.append(t)
        time.sleep(random.uniform(1, 3))  # instancje nie w tym samym rytmie

    if not threads:
        sys.exit(1)
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
