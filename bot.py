"""
bot.py
======
Logika jednej instancji bota działającej na jednej instancji LDPlayer.

Cykl działania:
  1. Klikaj punkt idle_point co idle_click_interval, dopóki nie pojawi
     się ekran z kulkami (wykryty przez vision.detect + has_game_screen).
  2. Gdy ekran z kulkami się pojawi: co pętlę na nowo wykrywaj pozycję
     DROP (bo się przemieszcza) i listę pozostałych kulek, przeciągaj
     JEDNĄ kulkę do DROP, poczekaj chwilę na animację/aktualizację UI,
     powtórz aż kulek nie będzie już widać.
  3. Wróć do kroku 1.

Oprócz "Dust Mine" w trybie idle mogą się pojawić minigry:
  - "Depth Collapse" (brązowe kafelki TAP) - bot klika wszystkie
    kafelki przed upływem czasu,
  - "Fishing Hole" (przycisk CAST) - bot od razu klika CAST, żeby
    pominąć minigrę.

Cała logika jest niezależna od danej instancji ADB - dostaje
skonfigurowany ADBController i działa na nim w pętli `run()`,
co pozwala odpalać wiele instancji równolegle (patrz manager.py).
"""

from __future__ import annotations

import time
import random
import threading
import logging
import datetime as dt
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

from adb_controller import ADBController, ADBError
import humanize
import vision
from packs import PackOpener
# re-eksport dla zgodności (GUI i manager.py importują je z bot.py)
from scheduler import (  # noqa: F401
    Decision, RandomRange, SchedulePlanner, ScheduleWindow, schedule_from_list, schedule_to_list,
)

logger = logging.getLogger("bot")

# Wywoływany po każdym screenshot+detect z (nazwa_instancji, frame_bgr, DetectionResult, tryb).
# tryb: "idle" | "collecting". Używane przez GUI do podglądu na żywo bez
# duplikowania pętli detekcji.
FrameCallback = Callable[[str, np.ndarray, "vision.DetectionResult", str], None]

# Wywoływany przy każdej zmianie stanu harmonogramu (sesja / przerwa /
# poza oknem / limit) z (nazwa_instancji, Decision).
ScheduleCallback = Callable[[str, Decision], None]


@dataclass
class BotConfig:
    name: str

    # Punkt (środek), w który bot klika w oczekiwaniu na pojawienie się
    # gry (np. przycisk otwierający "eksplozję kopalni pyłu").
    # Domyślna wartość skalibrowana dla Just Loot.
    idle_point: tuple[int, int] = (436, 1488)

    # Promień (px) obszaru wokół idle_point, w którym bot losuje
    # faktyczny punkt kliknięcia za każdym razem - zamiast klikać
    # dokładnie w ten sam piksel w kółko (co jest łatwo wykrywalnym
    # wzorcem), symuluje niedokładność ludzkiego dotyku. Domyślnie 50px
    # (czyli obszar o średnicy 100px, zgodnie z ustaleniami).
    idle_point_radius: float = 50.0

    # Jak często klikać punkt idle, gdy nie widać jeszcze ekranu gry.
    # Losowany zakres (s) zamiast stałej wartości - patrz RandomRange.
    idle_click_interval: RandomRange = field(default_factory=lambda: RandomRange(0.8, 1.4))

    # Region (x1, y1, x2, y2) planszy gry na ekranie - ogranicza
    # wykrywanie do obszaru czarnego prostokąta z kulkami, żeby
    # uniknąć fałszywych trafień gdzie indziej w UI. Jeśli None,
    # przeszukiwany jest cały ekran.
    # Domyślna wartość skalibrowana dla Just Loot.
    board_region: tuple[int, int, int, int] | None = (29, 333, 861, 1466)

    # Czas trwania gestu przeciągania kulki (ms). Losowany zakres.
    drag_duration_ms: RandomRange = field(default_factory=lambda: RandomRange(800, 1300))

    # Styl przeciągania:
    #   "human"    - po losowej krzywej z przyspieszaniem/zwalnianiem,
    #                drżeniem i czasem lekkim przestrzeleniem celu; kulka
    #                łapana i puszczana w losowym miejscu, kolejność
    #                zbierania kulek losowa (patrz humanize.py),
    #   "straight" - prosta linia od środka kulki do środka DROP, kulki
    #                po kolei (dawne zachowanie).
    # "human" wymaga `input motionevent` (nowsze Androidy); bez niego
    # tor jest prosty, ale punkty chwytu i kolejność nadal są losowe.
    drag_style: str = "human"

    # Odstęp między kolejnymi przeciągnięciami kulek (żeby gra
    # zdążyła zarejestrować i zaktualizować UI przed kolejną akcją).
    # Losowany zakres (s).
    between_drags_delay: RandomRange = field(default_factory=lambda: RandomRange(0.5, 1.1))

    # Ile razy z rzędu detekcja może nie znaleźć kulek/DROP zanim bot
    # uzna, że ekran gry się zakończył i wraca do klikania idle_point.
    empty_detections_to_exit: int = 2

    # Maksymalny czas (s) na "wyczyszczenie" jednego ekranu gry, zanim
    # bot uzna to za błąd i mimo wszystko wróci do trybu idle
    # (zabezpieczenie przed zawieszeniem w pętli).
    max_seconds_per_game_screen: float = 60.0

    # Interwał odświeżania screenshotu w trybie idle (osobny od
    # idle_click_interval, na wypadek gdyby klikanie miało być rzadsze
    # niż sprawdzanie ekranu).
    idle_poll_interval: float = 0.5

    # --- Harmonogram pracy --- #
    # Lista okien czasowych, w których bot MOŻE działać. Pusta lista
    # lub None oznacza "brak harmonogramu - bot działa zawsze", czyli
    # zachowanie identyczne jak przed dodaniem tej funkcji (pełna
    # wsteczna kompatybilność ze starymi configami).
    #
    # Każde okno może mieć własne losowe sesje i przerwy - patrz
    # scheduler.py. Przykład:
    #   schedule = [
    #       ScheduleWindow("09:00", "13:00", days=(0,1,2,3,4),
    #                      session_minutes=RandomRange(40, 90),
    #                      break_minutes=RandomRange(10, 30), jitter_minutes=15),
    #       ScheduleWindow("11:00", "16:00", days=(5,6)),   # cały czas, bez przerw
    #   ]
    schedule: list[ScheduleWindow] = field(default_factory=list)

    # Dzienny limit czasu gry (godziny), losowany raz na dzień z zakresu.
    # None = bez limitu. Po wyczerpaniu bot jest offline do północy.
    daily_limit_hours: RandomRange | None = None

    # Co ile sekund sprawdzać, czy nadal jesteśmy w oknie harmonogramu
    # (podczas oczekiwania poza godzinami pracy - nie ma sensu sprawdzać
    # częściej niż raz na kilkanaście sekund).
    schedule_poll_interval: float = 15.0

    # Czy zamykać grę (force-stop) na czas przerw - zarówno między
    # oknami harmonogramu, jak i między sesjami wewnątrz okna oraz po
    # wyczerpaniu limitu dziennego - i uruchamiać ją ponownie na starcie
    # kolejnej sesji. Wymaga ustawionego game_package - bez niego ta
    # opcja jest cicho ignorowana (nie ma czego zamykać/uruchamiać).
    # True domyślnie - jeśli ktoś już konfiguruje harmonogram, zwykle
    # zależy mu też na tym, żeby gra faktycznie nie działała w tle
    # podczas przerwy (oszczędność zasobów emulatora).
    close_game_during_break: bool = True

    # --- Watchdog czarnego ekranu (auto-restart gry) --- #
    # Nazwa pakietu gry (np. "com.justloot.game") - wymagana, żeby
    # watchdog mógł zrobić force-stop + start. Jeśli None, watchdog
    # jest wyłączony niezależnie od pozostałych ustawień poniżej.
    game_package: str | None = None

    # Opcjonalna nazwa głównej activity (format "pakiet/.MainActivity").
    # Jeśli None, restart używa `monkey` (auto-wykrycie ekranu startowego).
    game_activity: str | None = None

    # Ile sekund NIEPRZERWANIE czarnego ekranu musi minąć, zanim
    # watchdog uzna to za zawieszenie i zrestartuje grę.
    black_screen_timeout: float = 20.0

    # Próg jasności przekazywany do vision.is_black_screen.
    black_screen_brightness_threshold: float = 10.0

    # Ile sekund odczekać po restarcie gry, zanim bot wróci do
    # normalnego działania (czas na załadowanie się gry).
    restart_settle_delay: float = 10.0

    # --- Watchdog "wypadnięcia z gry" (reklama -> Google Play itp.) --- #
    # Lista pakietów, które NIE POWINNY być na pierwszym planie, gdy
    # bot pracuje - jeśli sprawdzenie foreground_package wykryje jeden
    # z nich, restart następuje NATYCHMIAST (bez odliczania jak przy
    # czarnym ekranie), bo to jednoznaczny sygnał, że kliknięcie w
    # reklamę wyrzuciło grę do sklepu/przeglądarki.
    #
    # com.android.vending = Google Play Store
    # com.android.chrome  = Chrome (część reklam otwiera się w przeglądarce)
    # com.android.browser = domyślna przeglądarka systemowa (starsze Androidy)
    foreign_app_packages: tuple[str, ...] = (
        "com.android.vending",
        "com.android.chrome",
        "com.android.browser",
    )

    # Jak często (co ile sekund) sprawdzać, jaka aplikacja jest na
    # pierwszym planie. To osobne, rzadsze wywołanie ADB (dumpsys) niż
    # screenshot, żeby nie obciążać niepotrzebnie emulatora/ADB przy
    # każdej pojedynczej klatce.
    foreground_check_interval: float = 3.0

    # Ile sekund gra może NIEPRZERWANIE nie być na pierwszym planie
    # (np. wysypała się do pulpitu Androida albo otworzyło się coś
    # spoza listy foreign_app_packages), zanim watchdog ją zrestartuje.
    # 0 wyłącza ten mechanizm (zostaje tylko natychmiastowy restart dla
    # foreign_app_packages).
    foreground_lost_timeout: float = 15.0

    # Ile razy z rzędu bot może przeciągać "tę samą" kulkę (w tym samym
    # miejscu), zanim uzna ją za niezbieralną / fałszywe wykrycie i
    # pominie do końca bieżącego ekranu gry.
    max_drag_attempts_per_ball: int = 3

    # --- Minigry --- #
    # "Depth Collapse": klikanie wszystkich kafelków TAP.
    play_debris: bool = True
    # Odstęp między kolejnymi kliknięciami kafelków (s). Losowany zakres.
    # Na planszę jest ~8 s, a samo kliknięcie przez ADB trwa ~0.1-0.3 s.
    tile_tap_delay: RandomRange = field(default_factory=lambda: RandomRange(0.05, 0.25))
    # "Fishing Hole": od razu klikaj CAST, żeby pominąć minigrę.
    skip_fishing: bool = True

    # --- Automatyczne otwieranie paczek (patrz packs.py) --- #
    # Domyślnie wyłączone, dopóki rozpoznawanie ekranów nie zostanie
    # potwierdzone na danym emulatorze.
    auto_packs: bool = False
    # Po ilu porażkach z rzędu otwierać paczki.
    packs_defeat_streak: int = 5
    # Co ile minut sprawdzać w Profile, czy gear nie jest za słaby.
    packs_check_interval_min: float = 30.0
    # Maksymalna liczba rund (max paczek + OPEN) w jednym podejściu.
    packs_max_rounds: int = 10
    # Ile razy kliknąć "+" (gra sama ogranicza do maksimum / dostępnego pyłu).
    pack_plus_taps: int = 25
    # Pozycje względne (x, y jako ułamek szerokości/wysokości ekranu),
    # zmierzone na LDPlayer.
    nav_profile: tuple[float, float] = (0.11, 0.93)
    nav_craft: tuple[float, float] = (0.67, 0.93)
    craft_loot_packs_tab: tuple[float, float] = (0.18, 0.142)

    # ------------------------------------------------------------------ #
    # Serializacja do/z JSON (jedno miejsce dla GUI i manager.py)
    # ------------------------------------------------------------------ #
    @classmethod
    def from_dict(cls, data: dict) -> "BotConfig":
        """Buduje BotConfig ze słownika instancji z config.json. Brakujące
        pola dostają wartości domyślne z tej klasy."""
        d = cls(name=data["name"])
        if "idle_point" in data:
            d.idle_point = tuple(int(v) for v in data["idle_point"])
        if "idle_point_radius" in data:
            d.idle_point_radius = float(data["idle_point_radius"])
        if "idle_click_interval" in data:
            d.idle_click_interval = RandomRange.from_value(data["idle_click_interval"])
        # brak board_region w configu = przeszukiwanie całego ekranu (jak dotychczas)
        region = data.get("board_region")
        d.board_region = tuple(int(v) for v in region) if region else None
        if "drag_duration_ms" in data:
            d.drag_duration_ms = RandomRange.from_value(data["drag_duration_ms"])
        if "between_drags_delay" in data:
            d.between_drags_delay = RandomRange.from_value(data["between_drags_delay"])
        if "schedule" in data:
            d.schedule = schedule_from_list(data["schedule"])
        if data.get("daily_limit_hours") is not None:
            d.daily_limit_hours = RandomRange.from_value(data["daily_limit_hours"])
        if "foreign_app_packages" in data:
            d.foreign_app_packages = tuple(data["foreign_app_packages"] or ())
        for key, conv in (
            ("empty_detections_to_exit", int),
            ("max_seconds_per_game_screen", float),
            ("idle_poll_interval", float),
            ("schedule_poll_interval", float),
            ("close_game_during_break", bool),
            ("black_screen_timeout", float),
            ("black_screen_brightness_threshold", float),
            ("restart_settle_delay", float),
            ("foreground_check_interval", float),
            ("foreground_lost_timeout", float),
            ("max_drag_attempts_per_ball", int),
            ("play_debris", bool),
            ("skip_fishing", bool),
            ("auto_packs", bool),
            ("packs_defeat_streak", int),
            ("packs_check_interval_min", float),
            ("packs_max_rounds", int),
            ("pack_plus_taps", int),
        ):
            if key in data and data[key] is not None:
                setattr(d, key, conv(data[key]))
        for key in ("nav_profile", "nav_craft", "craft_loot_packs_tab"):
            if data.get(key):
                setattr(d, key, tuple(float(v) for v in data[key]))
        if "tile_tap_delay" in data:
            d.tile_tap_delay = RandomRange.from_value(data["tile_tap_delay"])
        if data.get("drag_style"):
            if data["drag_style"] not in ("human", "straight"):
                raise ValueError(f"[{d.name}] drag_style musi być 'human' albo 'straight': {data['drag_style']!r}")
            d.drag_style = data["drag_style"]
        d.game_package = data.get("game_package") or None
        d.game_activity = data.get("game_activity") or None
        if d.board_region is not None:
            x1, y1, x2, y2 = d.board_region
            if x2 <= x1 or y2 <= y1:
                raise ValueError(f"[{d.name}] board_region musi mieć x2 > x1 i y2 > y1: {d.board_region}")
        return d

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "idle_point": list(self.idle_point),
            "idle_point_radius": self.idle_point_radius,
            "board_region": list(self.board_region) if self.board_region else None,
            "idle_click_interval": self.idle_click_interval.to_dict(),
            "drag_duration_ms": self.drag_duration_ms.to_dict(),
            "drag_style": self.drag_style,
            "between_drags_delay": self.between_drags_delay.to_dict(),
            "empty_detections_to_exit": self.empty_detections_to_exit,
            "max_seconds_per_game_screen": self.max_seconds_per_game_screen,
            "idle_poll_interval": self.idle_poll_interval,
            "schedule": schedule_to_list(self.schedule),
            "daily_limit_hours": self.daily_limit_hours.to_dict() if self.daily_limit_hours else None,
            "schedule_poll_interval": self.schedule_poll_interval,
            "close_game_during_break": self.close_game_during_break,
            "game_package": self.game_package,
            "game_activity": self.game_activity,
            "black_screen_timeout": self.black_screen_timeout,
            "black_screen_brightness_threshold": self.black_screen_brightness_threshold,
            "restart_settle_delay": self.restart_settle_delay,
            "foreign_app_packages": list(self.foreign_app_packages),
            "foreground_check_interval": self.foreground_check_interval,
            "foreground_lost_timeout": self.foreground_lost_timeout,
            "max_drag_attempts_per_ball": self.max_drag_attempts_per_ball,
            "play_debris": self.play_debris,
            "tile_tap_delay": self.tile_tap_delay.to_dict(),
            "skip_fishing": self.skip_fishing,
            "auto_packs": self.auto_packs,
            "packs_defeat_streak": self.packs_defeat_streak,
            "packs_check_interval_min": self.packs_check_interval_min,
            "packs_max_rounds": self.packs_max_rounds,
            "pack_plus_taps": self.pack_plus_taps,
            "nav_profile": list(self.nav_profile),
            "nav_craft": list(self.nav_craft),
            "craft_loot_packs_tab": list(self.craft_loot_packs_tab),
        }


@dataclass
class BotStats:
    cycles: int = 0
    # Szacunkowa liczba faktycznie zebranych kulek (spadek liczby
    # wykrytych kulek po przeciągnięciu), a nie liczba samych przeciągnięć.
    balls_collected: int = 0
    drags: int = 0
    errors: int = 0
    restarts: int = 0
    debris_games: int = 0
    fishing_skipped: int = 0
    defeats: int = 0
    pack_rounds: int = 0


class DustMineBot:
    def __init__(self, adb: ADBController, config: BotConfig,
                 on_frame: Optional[FrameCallback] = None,
                 on_schedule: Optional[ScheduleCallback] = None):
        self.adb = adb
        self.config = config
        self.stats = BotStats()
        # Event zamiast zwykłej flagi: wszystkie pauzy w pętli czekają na
        # nim (self._sleep), więc stop() przerywa je natychmiast, zamiast
        # czekać np. 10-15 s do końca restart_settle_delay / poll harmonogramu.
        self._stop_event = threading.Event()
        self._paused = False
        self._on_frame = on_frame
        self._on_schedule = on_schedule
        self._planner = SchedulePlanner(config)
        self._packs = PackOpener(self)
        self._last_decision: Decision | None = None

        # Watchdog czarnego ekranu: znacznik czasu (time.monotonic()),
        # od kiedy nieprzerwanie widzimy czarny ekran. None = ekran
        # aktualnie nie jest czarny (licznik zresetowany).
        self._black_screen_since: float | None = None
        self._last_black_log: float = 0.0
        # Watchdog pierwszego planu: od kiedy gra NIE jest na pierwszym planie.
        self._game_away_since: float | None = None
        self._watchdog_disabled_logged = False
        self._manual_restart_requested = False
        self._last_foreground_check: float = 0.0

    def stop(self) -> None:
        self._stop_event.set()

    @property
    def _stop(self) -> bool:
        return self._stop_event.is_set()

    def _sleep(self, seconds: float) -> bool:
        """Śpi `seconds` sekund albo do wywołania stop(). Zwraca True, jeśli
        przerwano przez stop()."""
        return self._stop_event.wait(max(0.0, seconds))

    def pause(self) -> None:
        """Wstrzymuje klikanie/przeciąganie, ale wątek nadal robi
        screenshoty i wywołuje on_frame - przydatne w GUI do podglądu
        na żywo bez ingerowania w grę."""
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    @property
    def is_paused(self) -> bool:
        return self._paused

    def request_restart_app(self) -> None:
        """
        Zgłasza żądanie ręcznego restartu gry (force-stop + start).
        Sam restart wykona się bezpiecznie wewnątrz wątku bota przy
        najbliższym przejściu pętli - nie wykonuje ADB bezpośrednio
        tutaj, żeby uniknąć wywołania z innego wątku (np. GUI)
        równolegle do screenshot()/tap() już trwających w pętli bota.
        """
        self._manual_restart_requested = True

    # ------------------------------------------------------------------ #
    def _random_point_near(self, center: tuple[int, int], radius: float) -> tuple[int, int]:
        """
        Losuje punkt w kole o danym promieniu wokół `center`, z
        rozkładem normalnym (gaussowskim) - punkty bliżej środka są
        znacznie bardziej prawdopodobne niż punkty przy samej krawędzi,
        co przypomina naturalną niedokładność ludzkiego dotyku.

        Odległość od środka losowana z |N(0, radius/2.5)|, przycięta do
        `radius`. Kąt losowany równomiernie z [0, 2*pi).
        """
        if radius <= 0:
            return center

        sigma = radius / 2.5
        distance = min(abs(random.gauss(0.0, sigma)), radius)
        angle = random.uniform(0.0, 2 * np.pi)

        dx = distance * np.cos(angle)
        dy = distance * np.sin(angle)
        return (int(round(center[0] + dx)), int(round(center[1] + dy)))

    # ------------------------------------------------------------------ #
    def _check_schedule(self) -> Decision:
        """Pyta planer o bieżącą decyzję; loguje i zgłasza do GUI każdą
        zmianę stanu (nowa sesja, przerwa, poza oknem, limit dzienny)."""
        decision = self._planner.decide(dt.datetime.now())
        last = self._last_decision
        if last is None or (decision.play, decision.reason, decision.until) != (last.play, last.reason, last.until):
            self._last_decision = decision
            if decision.reason != "always" or last is not None:
                logger.info("[%s] harmonogram: %s", self.config.name, decision.describe())
            if self._on_schedule is not None:
                self._on_schedule(self.config.name, decision)
        return decision

    def _wait_for_schedule(self) -> None:
        """
        Blokuje, dopóki harmonogram nie pozwoli grać (albo do stop()) -
        poza oknem, w przerwie między sesjami i po wyczerpaniu limitu
        dziennego. Jeśli `close_game_during_break` jest włączone i mamy
        skonfigurowany game_package, gra jest zamykana (force-stop) na
        czas przerwy i ponownie uruchamiana na start kolejnej sesji.
        """
        cfg = self.config
        decision = self._check_schedule()
        if decision.play:
            return

        game_was_closed = False
        if cfg.close_game_during_break and cfg.game_package:
            try:
                self.adb.stop_app(cfg.game_package)
                game_was_closed = True
                logger.info("[%s] zamknięto grę na czas przerwy", cfg.name)
            except ADBError as e:
                logger.warning("[%s] nie udało się zamknąć gry na przerwę: %s", cfg.name, e)

        while not self._stop:
            decision = self._check_schedule()
            if decision.play:
                break
            wait = cfg.schedule_poll_interval
            if decision.until is not None:
                # obudź się dokładnie na koniec przerwy, a nie do 15 s później
                wait = min(wait, max(1.0, (decision.until - dt.datetime.now()).total_seconds()))
            self._sleep(wait)

        if self._stop:
            return

        # Gra była celowo zamknięta - nie traktuj tego czasu jako "wypadnięcia".
        self._game_away_since = None
        self._black_screen_since = None
        if game_was_closed:
            try:
                self.adb.start_app(cfg.game_package, cfg.game_activity)
                logger.info("[%s] uruchomiono grę na start sesji", cfg.name)
                self._sleep(cfg.restart_settle_delay)
            except ADBError as e:
                logger.error("[%s] nie udało się uruchomić gry po przerwie: %s", cfg.name, e)

    # ------------------------------------------------------------------ #
    def run(self) -> None:
        logger.info("[%s] start bota", self.config.name)
        while not self._stop:
            try:
                self._wait_for_schedule()
                if self._stop:
                    break
                kind = self._idle_loop()
                if kind is None:
                    # koniec sesji (albo stop) - wróć do harmonogramu
                    continue
                if kind == "dust":
                    self._collect_loop()
                    self.stats.cycles += 1
                elif kind == "debris":
                    self._debris_loop()
                elif kind == "fishing":
                    self._skip_fishing()
            except ADBError as e:
                logger.error("[%s] błąd ADB: %s", self.config.name, e)
                self.stats.errors += 1
                self._sleep(2.0)
            except Exception:
                logger.exception("[%s] nieoczekiwany błąd", self.config.name)
                self.stats.errors += 1
                self._sleep(2.0)
        logger.info("[%s] bot zatrzymany", self.config.name)

    # ------------------------------------------------------------------ #
    def _do_restart_app(self, reason: str) -> bool:
        """
        Wykonuje faktyczny restart gry przez ADB (force-stop + start) i
        czyści stan watchdogów. Współdzielone przez watchdogi i ręczne
        żądanie restartu z GUI. Zwraca True przy powodzeniu.
        """
        cfg = self.config
        if not cfg.game_package:
            logger.warning(
                "[%s] nie można zrestartować gry - brak 'game_package' w konfiguracji tej instancji.",
                cfg.name,
            )
            return False

        logger.warning("[%s] restartuję grę (%s) - powód: %s", cfg.name, cfg.game_package, reason)
        try:
            self.adb.restart_app(cfg.game_package, cfg.game_activity)
        except ADBError as e:
            logger.error("[%s] restart gry nie powiódł się: %s", cfg.name, e)
            return False

        self._black_screen_since = None
        self._game_away_since = None
        self.stats.restarts += 1
        self._sleep(cfg.restart_settle_delay)
        # po restarcie od razu sprawdź pierwszy plan przy kolejnej klatce
        self._last_foreground_check = 0.0
        return True

    # ------------------------------------------------------------------ #
    def _check_watchdogs(self, frame: np.ndarray) -> bool:
        """
        Obsługuje ręczne żądanie restartu i oba watchdogi. Zwraca True,
        jeśli gra została zrestartowana (wywołujący powinien wtedy
        porzucić bieżącą klatkę i zacząć od nowa).
        """
        if self._manual_restart_requested:
            self._manual_restart_requested = False
            if self._do_restart_app("ręczne żądanie z GUI"):
                return True
        if self._check_foreground_watchdog():
            return True
        return self._check_black_screen_watchdog(frame)

    def _check_foreground_watchdog(self) -> bool:
        """
        Sprawdza (co `foreground_check_interval` sekund), czy gra jest na
        pierwszym planie:
          - jeśli na wierzchu jest aplikacja z `foreign_app_packages`
            (np. Google Play po kliknięciu w reklamę) - restart NATYCHMIAST,
          - jeśli na wierzchu jest cokolwiek innego niż gra (np. pulpit
            Androida po crashu gry) nieprzerwanie dłużej niż
            `foreground_lost_timeout` sekund - restart. Krótkie, przejściowe
            ekrany nie wywołują restartu.

        Zwraca True, jeśli wykonano restart.
        """
        cfg = self.config
        if not cfg.game_package:
            return False

        now = time.monotonic()
        if now - self._last_foreground_check < cfg.foreground_check_interval:
            return False
        self._last_foreground_check = now

        try:
            foreground = self.adb.get_foreground_package()
        except ADBError as e:
            logger.debug("[%s] nie udało się sprawdzić foreground_package: %s", cfg.name, e)
            return False

        if foreground is None:
            return False

        if foreground == cfg.game_package:
            self._game_away_since = None
            return False

        if foreground in cfg.foreign_app_packages:
            logger.warning(
                "[%s] wykryto obcą aplikację na pierwszym planie (%s) zamiast gry - "
                "prawdopodobnie kliknięcie w reklamę wyrzuciło do sklepu/przeglądarki",
                cfg.name, foreground,
            )
            return self._do_restart_app(f"wykryto {foreground} zamiast gry")

        if cfg.foreground_lost_timeout <= 0:
            return False

        if self._game_away_since is None:
            self._game_away_since = now
            logger.info("[%s] gra nie jest na pierwszym planie (%s) - restart za %.0fs, jeśli nie wróci",
                        cfg.name, foreground, cfg.foreground_lost_timeout)
            return False

        away = now - self._game_away_since
        if away < cfg.foreground_lost_timeout:
            return False
        return self._do_restart_app(f"gra poza pierwszym planem przez {away:.0f}s (na wierzchu: {foreground})")

    # ------------------------------------------------------------------ #
    def _check_black_screen_watchdog(self, frame: np.ndarray) -> bool:
        """
        Aktualizuje licznik czarnego ekranu na podstawie bieżącej klatki
        i - jeśli przekroczono `black_screen_timeout` - restartuje grę.
        Zwraca True, jeśli wykonano restart.
        """
        cfg = self.config

        if not cfg.game_package:
            if not self._watchdog_disabled_logged:
                logger.warning(
                    "[%s] watchdogi (czarny ekran / wypadnięcie z gry) WYŁĄCZONE - brak "
                    "'game_package' w konfiguracji tej instancji. Ustaw go w GUI albo w config.json.",
                    cfg.name,
                )
                self._watchdog_disabled_logged = True
            return False

        # Sprawdzamy jasność TYLKO w obrębie board_region, jeśli jest
        # ustawiony - pasek statusu / nawigacja Androida mogą być jasne
        # nawet gdy sama plansza gry jest czarna (zawieszona).
        if cfg.board_region is not None:
            x1, y1, x2, y2 = cfg.board_region
            region_frame = frame[y1:y2, x1:x2]
            if region_frame.size == 0:
                region_frame = frame
        else:
            region_frame = frame

        if not vision.is_black_screen(region_frame, cfg.black_screen_brightness_threshold):
            self._black_screen_since = None
            return False

        now = time.monotonic()
        if self._black_screen_since is None:
            self._black_screen_since = now
            self._last_black_log = now
            logger.info("[%s] wykryto czarny ekran, start odliczania watchdoga (limit %.0fs)",
                        cfg.name, cfg.black_screen_timeout)
            return False

        elapsed = now - self._black_screen_since
        if elapsed < cfg.black_screen_timeout:
            # log co ~30s, żeby było widać że odliczanie faktycznie trwa
            if now - self._last_black_log >= 30:
                self._last_black_log = now
                logger.info("[%s] czarny ekran trwa %.0fs / %.0fs", cfg.name, elapsed, cfg.black_screen_timeout)
            return False

        return self._do_restart_app(f"czarny ekran przez {elapsed:.0f}s (limit {cfg.black_screen_timeout:.0f}s)")

    # ------------------------------------------------------------------ #
    def _enabled_kind(self, det: vision.DetectionResult) -> str | None:
        kind = vision.screen_kind(det)
        if kind == "debris" and not self.config.play_debris:
            return None
        if kind == "fishing" and not self.config.skip_fishing:
            return None
        return kind

    def _idle_loop(self) -> str | None:
        """Klika idle_point dopóki nie pojawi się ekran gry. Zwraca rodzaj
        ekranu ("dust" | "debris" | "fishing"), albo None przy stop()
        albo gdy harmonogram każe skończyć sesję."""
        cfg = self.config
        last_click = 0.0
        next_click_delay = cfg.idle_click_interval.sample()
        while not self._stop:
            # Koniec sesji sprawdzamy tylko tutaj (w idle), a nie w trakcie
            # zbierania kulek - rozpoczęta plansza zawsze jest dokończona.
            if not self._check_schedule().play:
                return None

            frame = self.adb.screenshot()

            if self._check_watchdogs(frame):
                # gra została zrestartowana - zacznij od nowa ze świeżym
                # screenshotem zamiast działać na starej klatce
                last_click = 0.0
                continue

            det = vision.detect_all(frame, region=cfg.board_region)

            if self._on_frame is not None:
                self._on_frame(cfg.name, frame, det, "idle")

            kind = self._enabled_kind(det)
            if kind is None and not self._paused:
                reason = self._packs.observe(frame)
                if reason is not None:
                    self._packs.run(reason)
                    last_click = 0.0
                    continue

            if kind == "dust":
                logger.info("[%s] wykryto ekran gry: DROP=%s, kulki=%d",
                            cfg.name, det.drop.center if det.drop else None, len(det.balls))
                return kind
            if kind == "debris":
                logger.info("[%s] wykryto minigrę Depth Collapse: %d kafelków", cfg.name, len(det.tile_taps))
                return kind
            if kind == "fishing":
                logger.info("[%s] wykryto minigrę Fishing Hole - pomijam (CAST)", cfg.name)
                return kind

            now = time.monotonic()
            if not self._paused and now - last_click >= next_click_delay:
                click_point = self._random_point_near(cfg.idle_point, cfg.idle_point_radius)
                self.adb.tap(*click_point)
                last_click = now
                next_click_delay = cfg.idle_click_interval.sample()

            self._sleep(cfg.idle_poll_interval)
        return None

    # ------------------------------------------------------------------ #
    @staticmethod
    def _tap_order(points: list[tuple[int, int]]) -> list[tuple[int, int]]:
        """Kolejność klikania kafelków jak u człowieka: zaczyna od
        losowego, potem zwykle najbliższy kolejny (czasem drugi najbliższy)."""
        remaining = list(points)
        order = [remaining.pop(random.randrange(len(remaining)))]
        while remaining:
            lx, ly = order[-1]
            remaining.sort(key=lambda p: (p[0] - lx) ** 2 + (p[1] - ly) ** 2)
            i = 1 if len(remaining) > 1 and random.random() < 0.2 else 0
            order.append(remaining.pop(i))
        return order

    def _debris_loop(self) -> None:
        """Minigra "Depth Collapse": klika wszystkie kafelki, potem
        sprawdza ekran ponownie (nachodzące kafelki, nietrafione)."""
        cfg = self.config
        start_time = time.monotonic()
        empty_count = 0
        passes = 0
        tapped = 0

        while not self._stop:
            if time.monotonic() - start_time > cfg.max_seconds_per_game_screen:
                logger.warning("[%s] przekroczono max czas minigry - wracam do idle", cfg.name)
                return

            frame = self.adb.screenshot()
            if self._check_watchdogs(frame):
                return
            det = vision.detect_all(frame, region=cfg.board_region, in_debris_game=True)
            if self._on_frame is not None:
                self._on_frame(cfg.name, frame, det, "debris")

            if not det.tile_taps:
                empty_count += 1
                if empty_count >= cfg.empty_detections_to_exit:
                    if tapped:
                        self.stats.debris_games += 1
                        logger.info("[%s] Depth Collapse zakończone (%d kliknięć)", cfg.name, tapped)
                    return
                self._sleep(0.2)
                continue
            empty_count = 0

            if self._paused:
                start_time = time.monotonic()
                self._sleep(0.3)
                continue

            passes += 1
            if passes > 6:
                logger.warning("[%s] kafelki nie znikają po %d rundach klikania - wracam do idle",
                               cfg.name, passes - 1)
                return

            radius = det.tile_size * 0.25
            for x, y in self._tap_order(det.tile_taps):
                if self._stop or self._paused:
                    break
                self.adb.tap(*humanize.random_point_in_circle(x, y, radius))
                tapped += 1
                self._sleep(cfg.tile_tap_delay.sample())

    def _skip_fishing(self) -> None:
        """Minigra "Fishing Hole": od razu klika CAST (bez celowania),
        co kończy minigrę."""
        cfg = self.config
        frame = self.adb.screenshot()
        det = vision.detect_all(frame, region=cfg.board_region)
        if det.cast_button is None or self._paused:
            return
        if self._on_frame is not None:
            self._on_frame(cfg.name, frame, det, "fishing")
        b = det.cast_button
        # krótka "ludzka" chwila zastanowienia przed kliknięciem
        self._sleep(random.uniform(0.3, 1.0))
        cx, cy = b.center
        self.adb.tap(*humanize.random_point_in_circle(cx, cy, min(b.w, b.h) * 0.3))
        self.stats.fishing_skipped += 1
        self._sleep(random.uniform(1.0, 2.0))

    # ------------------------------------------------------------------ #
    @staticmethod
    def _same_ball(a: vision.Circle, b: vision.Circle) -> bool:
        return float(np.hypot(a.x - b.x, a.y - b.y)) < max(a.r, b.r)

    @staticmethod
    def _pick_ball(candidates: list[vision.Circle],
                   finger: tuple[int, int] | None) -> vision.Circle:
        """
        Wybiera kolejną kulkę tak, jak robiłby to człowiek - bez stałej
        kolejności: najczęściej najbliższą miejsca, gdzie jest palec (po
        poprzednim upuszczeniu), czasem "od góry planszy", czasem losową.
        """
        roll = random.random()
        if finger is not None and roll < 0.45:
            fx, fy = finger
            ranked = sorted(candidates, key=lambda b: (b.x - fx) ** 2 + (b.y - fy) ** 2)
            # zwykle najbliższa, czasem druga najbliższa
            if len(ranked) > 1 and random.random() < 0.2:
                return ranked[1]
            return ranked[0]
        if roll < 0.65:
            return min(candidates, key=lambda b: b.y)
        return random.choice(candidates)

    def _drag_ball(self, ball: vision.Circle, drop: vision.Circle) -> tuple[int, int]:
        """Przeciąga kulkę do DROP zgodnie z drag_style. Zwraca punkt,
        w którym puszczono palec."""
        cfg = self.config
        duration = cfg.drag_duration_ms.sample()
        if cfg.drag_style == "straight":
            self.adb.swipe(ball.x, ball.y, drop.x, drop.y, duration_ms=int(duration))
            return drop.x, drop.y

        start = humanize.random_point_in_circle(ball.x, ball.y, ball.r * 0.4)
        end = humanize.random_point_in_circle(drop.x, drop.y, drop.r * 0.35)
        path = humanize.drag_path(start, end)
        self.adb.drag_path(path, duration)
        return end

    def _collect_loop(self) -> None:
        """Przeciąga kolejno wszystkie kulki do (aktualnej) pozycji DROP."""
        cfg = self.config
        start_time = time.monotonic()
        empty_count = 0

        # Kulki, które mimo kilku prób nie dały się zebrać (fałszywe
        # wykrycie, element UI itp.) - pomijane do końca tego ekranu gry.
        skipped: list[vision.Circle] = []
        # Ile razy przeciągano kulkę w danym miejscu, a ona nadal jest.
        attempts: list[tuple[vision.Circle, int]] = []
        # Liczba wykrytych kulek tuż przed ostatnim przeciągnięciem -
        # służy do policzenia, ile kulek faktycznie zniknęło.
        balls_before_drag: int | None = None
        finger: tuple[int, int] | None = None

        while not self._stop:
            if time.monotonic() - start_time > cfg.max_seconds_per_game_screen:
                logger.warning("[%s] przekroczono max czas ekranu gry - wracam do idle", cfg.name)
                return

            frame = self.adb.screenshot()

            if self._check_watchdogs(frame):
                logger.info("[%s] restart gry przerwał zbieranie - wracam do idle", cfg.name)
                return

            det = vision.detect(frame, region=cfg.board_region)

            if self._on_frame is not None:
                self._on_frame(cfg.name, frame, det, "collecting")

            if balls_before_drag is not None:
                collected = balls_before_drag - len(det.balls)
                if det.drop is None:
                    # plansza zniknęła w całości - nie wiemy, ile kulek
                    # zebrało ostatnie przeciągnięcie; liczymy najwyżej jedną
                    collected = min(collected, 1)
                if collected > 0:
                    self.stats.balls_collected += collected
                balls_before_drag = None

            # kulki, które zniknęły, nie mają już "nieudanych prób"
            attempts = [(c, n) for c, n in attempts if any(self._same_ball(c, b) for b in det.balls)]

            candidates = [b for b in det.balls if not any(self._same_ball(b, s) for s in skipped)]

            if det.drop is None or not candidates:
                empty_count += 1
                if empty_count >= cfg.empty_detections_to_exit:
                    if skipped and det.balls:
                        logger.info("[%s] ekran gry zakończony - pominięto %d niezbieralnych kulek",
                                    cfg.name, len(skipped))
                    else:
                        logger.info("[%s] ekran gry zakończony (kulki zebrane)", cfg.name)
                    return
                self._sleep(0.3)
                continue

            empty_count = 0

            if self._paused:
                # czas pauzy nie liczy się do limitu max_seconds_per_game_screen
                start_time = time.monotonic()
                self._sleep(0.3)
                continue

            if cfg.drag_style == "straight":
                ball = candidates[0]
            else:
                ball = self._pick_ball(candidates, finger)
            drop = det.drop

            idx = next((i for i, (c, _) in enumerate(attempts) if self._same_ball(c, ball)), None)
            count = (attempts[idx][1] if idx is not None else 0) + 1
            if count > cfg.max_drag_attempts_per_ball:
                logger.warning("[%s] kulka (%d,%d) nie znika po %d próbach - pomijam ją",
                               cfg.name, ball.x, ball.y, cfg.max_drag_attempts_per_ball)
                skipped.append(ball)
                continue
            if idx is not None:
                attempts[idx] = (ball, count)
            else:
                attempts.append((ball, count))

            logger.debug("[%s] przeciągam kulkę (%d,%d) -> DROP (%d,%d)",
                         cfg.name, ball.x, ball.y, drop.x, drop.y)

            finger = self._drag_ball(ball, drop)
            self.stats.drags += 1
            balls_before_drag = len(det.balls)

            self._sleep(cfg.between_drags_delay.sample())
