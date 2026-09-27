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
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Callable, Optional

import cv2
# Kilka instancji bota działa równolegle w wątkach - wewnętrzna
# wielowątkowość OpenCV tylko by się z nimi przepychała (więcej CPU na
# synchronizację niż zysku); jeden wątek na wywołanie wystarcza.
cv2.setNumThreads(1)
import numpy as np

from adb_controller import ADBController, ADBError
import humanize
import screens
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


# Punkty do klikania (patrz BotConfig): klucz -> domyślne (x, y, promień)
POINT_DEFAULTS: dict[str, tuple[float, float, float]] = {
    "nav_profile": (0.11, 0.93, 0.03),
    "nav_craft": (0.67, 0.93, 0.03),
    "craft_loot_packs_tab": (0.18, 0.142, 0.03),
    "card_tap_anywhere": (0.5, 0.22, 0.08),
    "nav_inventory": (0.31, 0.93, 0.03),
}

# Wybór na ekranie dropu: klucz -> (etykieta, numer wiersza w panelu od góry)
DROP_CHOICES: dict[str, tuple[str, int]] = {
    "keep": ("Keep Current", 3),
    "replace": ("Replace", 0),
    "equip_salvage": ("Equip & Salvage", 1),
    "salvage": ("Salvage", 2),
}

# Ustawienia osobne dla każdej instancji - wszystkie pozostałe są w GUI
# wspólne (zmiana na jednej instancji ustawia je na wszystkich).
DEPTH_AUTO_MODES = ("max", "max_normal", "max_pct")
DEPTH_MODES = {
    "fixed": "Stała głębokość (wpisana)",
    "max": "Maksymalna odblokowana",
    "max_normal": "Maksymalna bez bonusu wrogów",
    "max_pct": "Maksymalna minus X%",
}

PER_INSTANCE_FIELDS = ("name", "schedule", "daily_limit_hours", "close_game_during_break",
                       "schedule_poll_interval", "farm_depth", "depth_mode",
                       "depth_below_max_pct")


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

    # Gdzie klikać w trybie idle:
    #   "just_loot" - przycisk JUST LOOT wykrywany na każdej klatce (koło
    #                 na dole ekranu, dowolny motyw/rozdzielczość); gdy go
    #                 nie widać, a jest PROCEED - PROCEED; w ostateczności
    #                 idle_point,
    #   "fixed"     - zawsze idle_point (dawne zachowanie).
    idle_target: str = "just_loot"

    # Czy w trybie "just_loot" klikać punkt awaryjny (idle_point), gdy bot
    # nie rozpozna żadnego ekranu. Domyślnie wyłączone - klikanie "na
    # ślepo" na nieznanym ekranie może trafić w coś przypadkowego.
    idle_fallback_enabled: bool = False

    # Zapisywanie zrzutów ekranów, których bot nie rozpoznaje dłużej niż
    # `unknown_screen_after_s` sekund (folder unknown_screens obok programu)
    # - żeby można je było przesłać i dodać ich obsługę.
    save_unknown_screens: bool = True
    unknown_screen_after_s: float = 30.0

    # Co wybrać na ekranie dropu po wygranej walce (panel z przyciskami
    # Replace / Equip & Salvage / Salvage / Keep Current):
    # "keep" | "replace" | "equip_salvage" | "salvage".
    drop_choice: str = "keep"

    # Pełny ekwipunek ("Inventory Full" po Keep Current): OK, przytrzymaj
    # Salvage na ekranie dropu, potem w Inventory przytrzymaj "+x DUST" na
    # `salvage_items` przedmiotach Z GÓRY LISTY (sortowanie ustaw w grze
    # tak, żeby na górze były najsłabsze, np. Item Level rosnąco).
    salvage_when_full: bool = True
    salvage_items: int = 10
    # Jak długo przytrzymać przycisk rozebrania (ms).
    salvage_hold_ms: RandomRange = field(default_factory=lambda: RandomRange(1200, 1800))

    # Jak często klikać punkt idle, gdy nie widać jeszcze ekranu gry.
    # Losowany zakres (s) zamiast stałej wartości - patrz RandomRange.
    idle_click_interval: RandomRange = field(default_factory=lambda: RandomRange(0.8, 1.4))

    # Region (x1, y1, x2, y2) planszy gry na ekranie - ogranicza
    # wykrywanie do obszaru czarnego prostokąta z kulkami, żeby
    # uniknąć fałszywych trafień gdzie indziej w UI. Jeśli None,
    # przeszukiwany jest cały ekran (domyślnie - wykrywanie działa na
    # całym ekranie i nie zależy od rozdzielczości).
    board_region: tuple[int, int, int, int] | None = None
    # Oszczędzanie CPU: zrzuty ekranu szersze niż tyle pikseli są przed
    # analizą pomniejszane (np. 1080x1920 -> 540x960, ~3x mniej pracy);
    # kliknięcia są przeliczane z powrotem na pełną rozdzielczość.
    # 0 = pełna rozdzielczość.
    processing_width: int = 540

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

    # --- "Ludzkie" zachowanie --- #
    # Czas reakcji (s) od pojawienia się planszy / minigry do pierwszego
    # ruchu - człowiek potrzebuje chwili, żeby zauważyć zmianę ekranu.
    reaction_delay: RandomRange = field(default_factory=lambda: RandomRange(0.4, 1.8))
    # Jak długo palec trzyma tapnięcie (ms). Zwykłe `input tap` to dotyk
    # 0 ms, zawsze identyczny; prawdziwy trwa ~50-150 ms i lekko się przesuwa.
    tap_hold_ms: RandomRange = field(default_factory=lambda: RandomRange(50, 150))
    # Szansa (0-1) na niegroźną pomyłkę: puszczenie kulki tuż obok DROP albo
    # kliknięcie obok kafelka. Kosztuje tylko chwilę (bot poprawia), a
    # 100% skuteczności wygląda nieludzko. 0 = wyłączone.
    mistake_chance: float = 0.03

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
    # Ile razy kliknąć "+", gdy nie da się odczytać ustawionej liczby paczek
    # (normalnie bot dobija do maksimum 20; gra sama ogranicza też do pyłu).
    pack_plus_taps: int = 20
    # Farmienie na wybranej głębokości: bot ustawia ją w Profile ("CHOOSE
    # DEPTH") strzałkami / kafelkiem i pilnuje co depth_check_interval_min
    # minut. 0 = nie zmieniaj głębokości.
    farm_depth: int = 0
    # "fixed" - głębokość farm_depth; "max" - najwyższa odblokowana;
    # "max_normal" - najwyższa, na której wrogowie nie mają bonusu
    # ("enemies at normal strength"); "max_pct" - maksymalna odblokowana
    # minus depth_below_max_pct procent. Dla "max*" farm_depth jest pomijane.
    depth_mode: str = "fixed"
    depth_below_max_pct: float = 10.0
    # Gdy wybrana głębokość spadnie do 1 (np. gra przełączyła loadout na
    # HUNT), sprawdź w Profile, czy wybrany jest loadout o tej nazwie, i w
    # razie potrzeby go wybierz. Pusta nazwa = nie sprawdzaj.
    loadout_name: str = "MAIN"
    depth_check_interval_min: float = 15.0
    # Punkty do klikania jako obszary: (x, y, promień) - x i promień jako
    # ułamek szerokości ekranu, y jako ułamek wysokości. Bot klika w losowe
    # miejsce wewnątrz koła. Domyślne wartości zmierzone na LDPlayer;
    # w GUI (zakładka Punkty) ustawisz je kliknięciem na podglądzie.
    nav_profile: tuple[float, float, float] = POINT_DEFAULTS["nav_profile"]
    nav_craft: tuple[float, float, float] = POINT_DEFAULTS["nav_craft"]
    nav_inventory: tuple[float, float, float] = POINT_DEFAULTS["nav_inventory"]
    craft_loot_packs_tab: tuple[float, float, float] = POINT_DEFAULTS["craft_loot_packs_tab"]
    # Gdzie klikać na karcie z paczki bez przycisku ("TAP ANYWHERE TO
    # CONTINUE") - pusty obszar, który na ekranie Craft też jest pusty.
    card_tap_anywhere: tuple[float, float, float] = POINT_DEFAULTS["card_tap_anywhere"]

    # ------------------------------------------------------------------ #
    # Serializacja do/z JSON (jedno miejsce dla GUI i manager.py)
    # ------------------------------------------------------------------ #
    @property
    def depth_enabled(self) -> bool:
        return self.depth_mode in DEPTH_AUTO_MODES or self.farm_depth > 0 or bool(self.loadout_name.strip())

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
            ("mistake_chance", float),
            ("salvage_when_full", bool),
            ("idle_fallback_enabled", bool),
            ("save_unknown_screens", bool),
            ("unknown_screen_after_s", float),
            ("salvage_items", int),
            ("play_debris", bool),
            ("skip_fishing", bool),
            ("auto_packs", bool),
            ("packs_defeat_streak", int),
            ("packs_check_interval_min", float),
            ("packs_max_rounds", int),
            ("pack_plus_taps", int),
            ("farm_depth", int),
            ("depth_mode", str),
            ("processing_width", int),
            ("depth_below_max_pct", float),
            ("loadout_name", str),
            ("depth_check_interval_min", float),
        ):
            if key in data and data[key] is not None:
                setattr(d, key, conv(data[key]))
        for key in POINT_DEFAULTS:
            if data.get(key):
                vals = [float(v) for v in data[key]]
                if len(vals) == 2:  # stary format bez promienia
                    vals.append(POINT_DEFAULTS[key][2])
                setattr(d, key, tuple(vals[:3]))
        for key in ("reaction_delay", "tap_hold_ms", "salvage_hold_ms"):
            if data.get(key) is not None:
                setattr(d, key, RandomRange.from_value(data[key]))
        if "tile_tap_delay" in data:
            d.tile_tap_delay = RandomRange.from_value(data["tile_tap_delay"])
        if data.get("idle_target"):
            if data["idle_target"] not in ("just_loot", "fixed"):
                raise ValueError(f"[{d.name}] idle_target musi być 'just_loot' albo 'fixed': {data['idle_target']!r}")
            d.idle_target = data["idle_target"]
        if data.get("drop_choice"):
            if data["drop_choice"] not in DROP_CHOICES:
                raise ValueError(f"[{d.name}] drop_choice musi być jednym z {list(DROP_CHOICES)}: {data['drop_choice']!r}")
            d.drop_choice = data["drop_choice"]
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

    def copy_shared_from(self, other: "BotConfig") -> None:
        """Kopiuje z `other` wszystkie ustawienia poza PER_INSTANCE_FIELDS."""
        import copy
        import dataclasses
        for f in dataclasses.fields(self):
            if f.name not in PER_INSTANCE_FIELDS:
                setattr(self, f.name, copy.deepcopy(getattr(other, f.name)))

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "idle_point": list(self.idle_point),
            "idle_point_radius": self.idle_point_radius,
            "idle_target": self.idle_target,
            "idle_fallback_enabled": self.idle_fallback_enabled,
            "save_unknown_screens": self.save_unknown_screens,
            "unknown_screen_after_s": self.unknown_screen_after_s,
            "drop_choice": self.drop_choice,
            "salvage_when_full": self.salvage_when_full,
            "salvage_items": self.salvage_items,
            "salvage_hold_ms": self.salvage_hold_ms.to_dict(),
            "nav_inventory": list(self.nav_inventory),
            "board_region": list(self.board_region) if self.board_region else None,
            "idle_click_interval": self.idle_click_interval.to_dict(),
            "drag_duration_ms": self.drag_duration_ms.to_dict(),
            "drag_style": self.drag_style,
            "reaction_delay": self.reaction_delay.to_dict(),
            "tap_hold_ms": self.tap_hold_ms.to_dict(),
            "mistake_chance": self.mistake_chance,
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
            "farm_depth": self.farm_depth,
            "depth_mode": self.depth_mode,
            "processing_width": self.processing_width,
            "depth_below_max_pct": self.depth_below_max_pct,
            "loadout_name": self.loadout_name,
            "depth_check_interval_min": self.depth_check_interval_min,
            "nav_profile": list(self.nav_profile),
            "nav_craft": list(self.nav_craft),
            "craft_loot_packs_tab": list(self.craft_loot_packs_tab),
            "card_tap_anywhere": list(self.card_tap_anywhere),
        }


def unknown_screens_dir() -> Path:
    """Folder na zrzuty nierozpoznanych ekranów - obok programu (obok .exe
    w wersji zbudowanej, obok bot.py przy uruchamianiu ze źródeł)."""
    base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    return base / "unknown_screens"


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
    drops: int = 0
    salvaged: int = 0
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
        self._fishing_frames = 0
        self._apply_performance()
        # ostatnia znana pozycja DROP (z poprzednich plansz) - pomaga
        # rozpoznać planszę, na której kulki mocno zasłaniają DROP
        self._last_drop: vision.Circle | None = None
        self._idle_point_warned = False
        self._last_drop_choice = 0.0
        self._unknown_since: float | None = None   # od kiedy ekran nierozpoznany
        self._unknown_saved = False
        self._inventory_full = False            # po "Inventory Full": Salvage na ekranie dropu
        self._inventory_salvage_pending = False # potem: rozebrać przedmioty w Inventory
        self._forced_salvage: int | None = None # ręczne żądanie z GUI: ile przedmiotów
        self._forced_packs: int | None = None   # ręczne żądanie z GUI: ile paczek otworzyć
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

    def request_salvage(self, count: int | None = None) -> None:
        """Ręczne żądanie rozebrania `count` przedmiotów z góry listy w
        Inventory (domyślnie config.salvage_items). Wykonywane w wątku bota
        przy najbliższym ekranie z paskiem nawigacji (JUST LOOT)."""
        self._forced_salvage = int(count) if count else self.config.salvage_items

    def request_packs(self, count: int) -> None:
        """Ręczne żądanie otwarcia `count` paczek (Craft -> Loot Packs).
        Wykonywane w wątku bota przy najbliższym ekranie z paskiem nawigacji."""
        self._forced_packs = max(1, int(count))

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
                self._react()
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
            x1, y1, x2, y2 = self._region()
            region_frame = frame[y1:y2, x1:x2]
            if region_frame.size == 0:
                region_frame = frame
        else:
            region_frame = frame

        black = vision.is_black_screen(region_frame, cfg.black_screen_brightness_threshold)
        # pusty ekran gry (np. zawieszona gra z samym "+1 RAID XP") -
        # traktowany jak czarny, o ile to nie zakryta karta "?" / karta z paczki
        empty = (not black and vision.is_empty_screen(frame)
                 and screens.find_reveal_card(frame) is None and screens.find_item_card(frame) is None)
        if not (black or empty):
            self._black_screen_since = None
            return False

        now = time.monotonic()
        if self._black_screen_since is None:
            self._black_screen_since = now
            self._last_black_log = now
            logger.info("[%s] wykryto %s, start odliczania watchdoga (limit %.0fs)",
                        cfg.name, "czarny ekran" if black else "pusty ekran (gra się zawiesiła?)",
                        cfg.black_screen_timeout)
            return False

        elapsed = now - self._black_screen_since
        if elapsed < cfg.black_screen_timeout:
            # log co ~30s, żeby było widać że odliczanie faktycznie trwa
            if now - self._last_black_log >= 30:
                self._last_black_log = now
                logger.info("[%s] czarny ekran trwa %.0fs / %.0fs", cfg.name, elapsed, cfg.black_screen_timeout)
            return False

        return self._do_restart_app(f"czarny/pusty ekran przez {elapsed:.0f}s (limit {cfg.black_screen_timeout:.0f}s)")

    # ------------------------------------------------------------------ #
    def _enabled_kind(self, det: vision.DetectionResult, frame: np.ndarray | None = None) -> str | None:
        # ekrany dropu po walce mają pierwszeństwo - obsługuje je zwykłe
        # klikanie idle (Keep Current / odkrycie karty), a ich elementy
        # (np. zakładki RING, tekst przedmiotu) nie mogą udawać minigry
        if frame is not None and (screens.find_drop_choice(frame) or screens.find_reveal_card(frame)
                                  or screens.find_item_card(frame) or screens.find_center_button(frame)):
            return None
        kind = vision.screen_kind(det)
        # plansza Dust Mine i minigry zajmują cały ekran - gdy widać pasek
        # nawigacji z JUST LOOT (Profile, Craft, Inventory...), to nie jest
        # żadna z nich, nawet jeśli coś na ekranie przypomina kulki / DROP
        if kind is not None and frame is not None and screens.find_just_loot(frame) is not None:
            return None
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
        self._apply_performance()  # zmiana ustawienia w GUI działa od następnej pętli
        last_click = 0.0
        next_click_delay = humanize.human_delay(cfg.idle_click_interval.min, cfg.idle_click_interval.max)
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

            # karty (zakryta "?" po walce, odkryta z dropu / paczki): szybka
            # ścieżka - bez wykrywania planszy i z krótką przerwą, jak
            # człowiek szybko przeklikujący karty
            # To samo dla panelu wyboru dropu (Keep Current / ...): cały ciąg
            # "?" -> odkrycie -> Keep Current bez zwykłej przerwy idle.
            choice = screens.find_drop_choice(frame)
            card = (choice or screens.find_reveal_card(frame) or screens.find_item_card(frame)
                    or screens.find_center_button(frame))
            if card and not self._paused:
                if self._on_frame is not None:
                    self._on_frame(cfg.name, frame, vision.DetectionResult(None, []), "idle")
                self._note_unknown(None)
                # po wyborze na panelu dropu nie klikaj go drugi raz, zanim
                # gra zdąży zamknąć ekran (drugie kliknięcie trafiłoby w
                # następny ekran)
                if not (choice and time.monotonic() - self._last_drop_choice < 1.2):
                    if not self._handle_inventory_flow(frame):
                        point = self._idle_click_point(frame)
                        if point is not None:
                            self._tap(*point)
                last_click = time.monotonic()
                self._sleep(humanize.human_delay(0.15, 0.45))
                continue

            det = vision.detect_all(frame, region=self._region(), drop_hint=self._last_drop)

            if self._on_frame is not None:
                self._on_frame(cfg.name, frame, det, "idle")

            kind = self._enabled_kind(det, frame)
            if kind is not None:
                self._note_unknown(None)
            if kind is None and not self._paused and self._forced_packs and screens.find_just_loot(frame):
                count, self._forced_packs = self._forced_packs, None
                logger.warning("[%s] zlecono otwarcie %d paczek", cfg.name, count)
                opened = self._packs.open_count(count)
                logger.info("[%s] ręczne otwieranie paczek zakończone: %d/%d", cfg.name, opened, count)
                last_click = 0.0
                continue
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
                # potwierdzenie na drugiej klatce - pojedyncza klatka walki
                # (animacje, obramowane przyciski) nie uruchomi minigry
                self._fishing_frames += 1
                if self._fishing_frames >= 2:
                    self._fishing_frames = 0
                    logger.info("[%s] wykryto minigrę Fishing Hole - pomijam (CAST)", cfg.name)
                    return kind
                continue
            self._fishing_frames = 0

            now = time.monotonic()
            if not self._paused and now - last_click >= next_click_delay:
                if not self._handle_inventory_flow(frame):
                    point = self._idle_click_point(frame)
                    if point is not None:
                        self._tap(*point)
                last_click = now
                next_click_delay = humanize.human_delay(cfg.idle_click_interval.min, cfg.idle_click_interval.max)

            self._sleep(cfg.idle_poll_interval)
        return None

    def _region(self) -> tuple[int, int, int, int] | None:
        """board_region (piksele urządzenia) w układzie pomniejszonego zrzutu."""
        r = self.config.board_region
        scale = getattr(self.adb, "scale", 1.0) or 1.0
        if r is None or scale == 1.0:
            return r
        return tuple(int(round(v / scale)) for v in r)

    def _apply_performance(self) -> None:
        if hasattr(self.adb, "max_width"):
            self.adb.max_width = max(0, int(self.config.processing_width))

    @staticmethod
    def _non_board_screen(frame: np.ndarray) -> str | None:
        """Nazwa rozpoznanego ekranu, który na pewno nie jest planszą Dust
        Mine (żadnego z tych elementów plansza nie ma), albo None."""
        if screens.find_just_loot(frame) is not None:
            return "widać pasek nawigacji"
        if screens.find_wide_bottom_button(frame) is not None:
            return "ekran końca walki"
        if screens.find_drop_choice(frame) or screens.find_reveal_card(frame) or screens.find_item_card(frame):
            return "ekran dropu"
        if screens.find_center_button(frame) is not None:
            return "ekran z przyciskiem"
        return None

    def _tap(self, x: int, y: int) -> None:
        """Tapnięcie "jak palcem": dotyk trwa tap_hold_ms i lekko się
        przesuwa (swipe o ~1 px z czasem trwania = przytrzymanie). Działa
        na każdej wersji Androida."""
        hold = self.config.tap_hold_ms.sample()
        if hold <= 0:
            self.adb.tap(x, y)
            return
        dx = int(round(max(-2.0, min(2.0, random.gauss(0.0, 0.8)))))
        dy = int(round(max(-2.0, min(2.0, random.gauss(0.0, 0.8)))))
        self.adb.swipe(x, y, x + dx, y + dy, duration_ms=int(hold))

    def _tap_many(self, points: list[tuple[int, int]], gap: tuple[float, float]) -> None:
        """Seria szybkich tapnięć (np. "+" albo kolejne karty) jednym
        poleceniem ADB; przerwy losowe z zakresu `gap` (sekundy)."""
        if not points:
            return
        holds = [max(1, int(self.config.tap_hold_ms.sample())) for _ in points]
        gaps = [humanize.human_delay(*gap) for _ in points[1:]]
        self.adb.tap_sequence([(x, y, h) for (x, y), h in zip(points, holds)], gaps)

    def _hold(self, x: int, y: int, hold_ms: float) -> None:
        """Przytrzymanie palcem (long press) z lekkim przesunięciem."""
        dx = int(round(max(-2.0, min(2.0, random.gauss(0.0, 0.8)))))
        dy = int(round(max(-2.0, min(2.0, random.gauss(0.0, 0.8)))))
        self.adb.swipe(x, y, x + dx, y + dy, duration_ms=int(hold_ms))

    def _handle_inventory_flow(self, frame: np.ndarray) -> bool:
        """
        Obsługa pełnego ekwipunku w trybie czekania. Zwraca True, jeśli
        wykonano akcję (wtedy zwykłe kliknięcie idle jest pomijane):
          1. okno "Inventory Full" tuż po wyborze na ekranie dropu -> OK,
          2. ekran dropu -> przytrzymanie Salvage (zamiast Keep Current),
          3. z powrotem na ekranie z paskiem nawigacji -> Inventory i
             rozebranie przedmiotów z góry listy.
        """
        cfg = self.config
        # ręczne (wymuszone) rozbieranie - niezależnie od ustawień
        if self._forced_salvage and screens.find_just_loot(frame) is not None:
            count, self._forced_salvage = self._forced_salvage, None
            logger.warning("[%s] wymuszone rozbieranie: %d przedmiotów z góry listy", cfg.name, count)
            self._salvage_inventory(count)
            return True
        if not cfg.salvage_when_full:
            return False
        W = frame.shape[1]
        dialog = screens.find_open_dialog(frame)
        if dialog is not None:
            # jedyne okno spodziewane w trybie czekania - "Inventory Full"
            # zaraz po kliknięciu opcji na ekranie dropu; innych nie ruszamy
            if time.monotonic() - self._last_drop_choice < 20:
                logger.warning("[%s] ekwipunek pełny - rozbiorę nowy drop i %d przedmiotów z Inventory",
                               cfg.name, cfg.salvage_items)
                self._inventory_full = True
                self._tap(*humanize.random_point_in_circle(*dialog.open_button, 0.012 * W))
                return True
            return False
        if self._inventory_full:
            rows = screens.find_drop_choice(frame)
            if rows and len(rows) == 4:
                cx, cy = rows[DROP_CHOICES["salvage"][1]].center
                logger.info("[%s] drop po walce - przytrzymuję Salvage (pełny ekwipunek)", cfg.name)
                self._hold(*humanize.random_point_in_circle(cx, cy, 0.03 * W), cfg.salvage_hold_ms.sample())
                self._inventory_full = False
                self._inventory_salvage_pending = True
                self._last_drop_choice = time.monotonic()
                return True
        if self._inventory_salvage_pending and screens.find_just_loot(frame) is not None:
            self._inventory_salvage_pending = False
            self._salvage_inventory()
            return True
        return False

    def _salvage_inventory(self, count: int | None = None) -> None:
        """Zakładka Inventory: przytrzymuje "+x DUST" na przedmiotach z góry
        listy (po każdym lista przesuwa się w górę)."""
        cfg = self.config
        target = count if count else cfg.salvage_items
        self._packs._tap_nav("nav_inventory")
        self._sleep(random.uniform(1.2, 1.8))
        done = misses = 0
        while done < target and not self._stop and not self._paused:
            frame = self.adb.screenshot()
            buttons = screens.find_salvage_buttons(frame)
            if not buttons:
                misses += 1
                if misses >= 3:
                    if done == 0:
                        logger.warning("[%s] nie znaleziono przycisków „+x DUST” w Inventory", cfg.name)
                    break
                self._sleep(0.7)
                continue
            misses = 0
            x, y = buttons[0]
            self._hold(*humanize.random_point_in_circle(x, y, 0.012 * frame.shape[1]), cfg.salvage_hold_ms.sample())
            done += 1
            self.stats.salvaged += 1
            self._sleep(humanize.human_delay(0.6, 1.4))
        logger.info("[%s] rozebrano %d przedmiotów z Inventory", cfg.name, done)

    def _react(self) -> None:
        """Czas reakcji na nowy ekran (plansza / minigra)."""
        rd = self.config.reaction_delay
        self._sleep(humanize.human_delay(rd.min, rd.max))

    def _mistake(self) -> bool:
        return random.random() < self.config.mistake_chance

    def _note_unknown(self, frame: np.ndarray | None) -> None:
        """Liczy, jak długo ekran pozostaje nierozpoznany (frame=None =
        rozpoznany); po `unknown_screen_after_s` zapisuje jeden zrzut."""
        cfg = self.config
        if frame is None:
            self._unknown_since = None
            self._unknown_saved = False
            return
        now = time.monotonic()
        if self._unknown_since is None:
            self._unknown_since = now
            return
        if (not cfg.save_unknown_screens or self._unknown_saved
                or now - self._unknown_since < cfg.unknown_screen_after_s):
            return
        self._unknown_saved = True
        try:
            folder = unknown_screens_dir()
            folder.mkdir(parents=True, exist_ok=True)
            # nie zaśmiecaj dysku - zostaw najwyżej 50 ostatnich zrzutów
            old = sorted(folder.glob("*.png"))
            for f in old[:-49]:
                f.unlink(missing_ok=True)
            safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in cfg.name)
            path = folder / f"{dt.datetime.now():%Y%m%d_%H%M%S_%f}_{safe_name}.png"
            cv2.imwrite(str(path), frame)
            logger.warning("[%s] nie rozpoznaję ekranu od %.0f s - zapisano zrzut: %s "
                           "(prześlij go, żeby dodać obsługę tego ekranu)",
                           cfg.name, now - self._unknown_since, path)
        except Exception as e:
            logger.warning("[%s] nie udało się zapisać zrzutu nierozpoznanego ekranu: %s", cfg.name, e)

    def _idle_click_point(self, frame: np.ndarray) -> tuple[int, int] | None:
        """Punkt kliknięcia w trybie idle - patrz BotConfig.idle_target.
        Ekrany dropu po wygranej walce są obsługiwane zawsze."""
        self._unknown_hit = False
        point = self._idle_click_point_inner(frame)
        if not self._unknown_hit:
            self._note_unknown(None)  # ekran rozpoznany - zeruj licznik
        return point

    def _idle_click_point_inner(self, frame: np.ndarray) -> tuple[int, int] | None:
        cfg = self.config

        # wybór przedmiotu z dropu (Replace / ... / Keep Current)
        rows = screens.find_drop_choice(frame)
        if rows:
            label, index = DROP_CHOICES.get(cfg.drop_choice, DROP_CHOICES["keep"])
            if len(rows) != 4:
                # nietypowy układ - bezpiecznie: ostatni wiersz = Keep Current
                label, index = DROP_CHOICES["keep"][0], len(rows) - 1
            row = rows[index]
            now = time.monotonic()
            if now - self._last_drop_choice > 5.0:  # licz raz na ekran, nie na każde ponowienie
                self.stats.drops += 1
                logger.info("[%s] drop po walce - wybieram: %s", cfg.name, label)
            self._last_drop_choice = now
            cx, cy = row.center
            return humanize.random_point_in_circle(cx, cy, min(row.w, row.h) * 0.3)
        # zakryta karta dropu - "tap anywhere to reveal"
        card = screens.find_reveal_card(frame)
        if card is not None:
            cx, cy = card.center
            return humanize.random_point_in_circle(cx, cy, min(card.w, card.h) * 0.3)
        # odkryta karta z paczki - "tap anywhere to continue" (np. gdy
        # otwieranie paczek zostało przerwane albo paczki otwarto ręcznie)
        item = screens.find_item_card(frame)
        if item is not None:
            h, w = frame.shape[:2]
            px, py, pr = cfg.card_tap_anywhere
            x, y = humanize.random_point_in_circle(px * w, py * h, pr * w)
            if item.x <= x <= item.x + item.w and item.y <= y <= item.y + item.h:
                # punkt wypadł na karcie - kliknij nad nią
                x, y = humanize.random_point_in_circle(w / 2, item.y / 2, 0.08 * w)
            return x, y
        # okno informacyjne z samym "OK" (np. "Not Enough Dust") - zamknij
        info = screens.find_open_dialog(frame)
        if info is not None and info.buttons == 1:
            logger.info("[%s] okno informacyjne - klikam OK", cfg.name)
            return humanize.random_point_in_circle(*info.open_button, 0.012 * frame.shape[1])
        # "Pack Opened" -> "Done" (ekran po kartach z paczki)
        done = screens.find_center_button(frame)
        if done is not None:
            cx, cy = done.center
            return humanize.random_point_in_circle(cx, cy, min(done.w, done.h) * 0.3)
        if cfg.idle_target == "just_loot":
            button = screens.find_just_loot(frame)
            if button is not None:
                return humanize.random_point_in_circle(button.x, button.y, button.r * 0.45)
            proceed = screens.find_wide_bottom_button(frame)
            if proceed is not None:
                cx, cy = proceed.center
                return humanize.random_point_in_circle(cx, cy, min(proceed.w, proceed.h) * 0.3)
            # nic nie rozpoznano
            self._unknown_hit = True
            self._note_unknown(frame)
            if not cfg.idle_fallback_enabled:
                return None
        k = getattr(self.adb, "scale", 1.0) or 1.0   # idle_point jest w pikselach urządzenia
        x, y = self._random_point_near((cfg.idle_point[0] / k, cfg.idle_point[1] / k), cfg.idle_point_radius / k)
        h, w = frame.shape[:2]
        if not (0 <= x < w and 0 <= y < h):
            # punkt z configu dla innej rozdzielczości - nie klikaj w próżnię
            if not self._idle_point_warned:
                logger.warning("[%s] punkt awaryjny (%d,%d) leży poza ekranem %dx%d - ustaw go na podglądzie",
                               cfg.name, cfg.idle_point[0], cfg.idle_point[1], w, h)
                self._idle_point_warned = True
            return None
        return x, y

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
            det = vision.detect_all(frame, region=self._region(), in_debris_game=True)
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
                if self._mistake():
                    # kliknięcie obok kafelka - następna runda go poprawi
                    a = random.uniform(0, 2 * np.pi)
                    off = det.tile_size * random.uniform(0.6, 0.8)
                    x, y = int(x + off * np.cos(a)), int(y + off * np.sin(a))
                    self._tap(x, y)
                else:
                    self._tap(*humanize.random_point_in_circle(x, y, radius))
                tapped += 1
                self._sleep(humanize.human_delay(cfg.tile_tap_delay.min, cfg.tile_tap_delay.max))

    def _skip_fishing(self) -> None:
        """Minigra "Fishing Hole": od razu klika CAST (bez celowania),
        co kończy minigrę."""
        cfg = self.config
        frame = self.adb.screenshot()
        det = vision.detect_all(frame, region=self._region())
        if det.cast_button is None or self._paused or screens.find_just_loot(frame) is not None:
            return
        if self._on_frame is not None:
            self._on_frame(cfg.name, frame, det, "fishing")
        b = det.cast_button
        # (czas reakcji na minigrę jest już odczekany w run())
        cx, cy = b.center
        self._tap(*humanize.random_point_in_circle(cx, cy, min(b.w, b.h) * 0.3))
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
        if cfg.drag_style == "straight" and np.hypot(ball.x - drop.x, ball.y - drop.y) >= drop.r:
            self.adb.swipe(ball.x, ball.y, drop.x, drop.y, duration_ms=int(duration))
            return drop.x, drop.y

        start = humanize.random_point_in_circle(ball.x, ball.y, ball.r * 0.4)
        end = humanize.random_point_in_circle(drop.x, drop.y, drop.r * 0.35)
        if self._mistake():
            # puszczenie kulki tuż obok DROP - zostanie, bot spróbuje ponownie
            a = random.uniform(0, 2 * np.pi)
            off = drop.r * random.uniform(1.2, 1.5)
            end = (int(drop.x + off * np.cos(a)), int(drop.y + off * np.sin(a)))
        dist = float(np.hypot(ball.x - drop.x, ball.y - drop.y))
        if dist < drop.r:
            # Kulka leży na DROP-ie - ruch "z DROP na DROP" byłby za krótki
            # (gra może go uznać za tapnięcie). Wyciągamy ją poza okrąg i
            # wrzucamy z powrotem jednym ciągłym ruchem.
            if dist > 1:
                ux, uy = (ball.x - drop.x) / dist, (ball.y - drop.y) / dist
            else:
                a = random.uniform(0, 2 * np.pi)
                ux, uy = float(np.cos(a)), float(np.sin(a))
            out_d = drop.r + ball.r * random.uniform(1.0, 1.5)
            out = (int(drop.x + ux * out_d), int(drop.y + uy * out_d))
            path = humanize.drag_path(start, out) + humanize.drag_path(out, end)[1:]
            self.adb.drag_path(path, duration * 1.5)
            return end
        path = humanize.drag_path(start, end)
        self.adb.drag_path(path, duration)
        return end

    @staticmethod
    def _patch(frame: np.ndarray, x: int, y: int, r: int) -> np.ndarray:
        """Wycinek (skala szarości, stały rozmiar) wokół miejsca chwytu."""
        half = max(4, int(r * 1.2))
        h, w = frame.shape[:2]
        x1, y1 = min(max(0, x - half), w - 1), min(max(0, y - half), h - 1)
        patch = frame[y1:y1 + 2 * half, x1:x1 + 2 * half]
        if patch.size == 0:
            return np.zeros((16, 16), np.float32)
        gray = cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY)
        return cv2.resize(gray, (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32)

    def _collect_loop(self) -> None:
        """Przeciąga kolejno wszystkie kulki do (aktualnej) pozycji DROP."""
        cfg = self.config
        start_time = time.monotonic()
        empty_count = 0

        # Kulki, które mimo kilku prób nie dały się zebrać (fałszywe
        # wykrycie, element UI itp.) - pomijane do końca tego ekranu gry.
        skipped: list[vision.Circle] = []
        # Nieudane próby w danym miejscu: (kulka, liczba prób, wycinek obrazu
        # wokół miejsca chwytu sprzed przeciągnięcia). Próba jest nieudana
        # tylko wtedy, gdy obraz w tym miejscu praktycznie się nie zmienił.
        # Zdjęcie kulki ze stosu (odsłania inną, przesuniętą - nawet w tym
        # samym kolorze) albo upuszczenie jej obok DROP (kulka się przesuwa)
        # zmienia obraz i liczy się jako postęp.
        attempts: list[tuple[vision.Circle, int, np.ndarray]] = []
        # Liczba wykrytych kulek tuż przed ostatnim przeciągnięciem -
        # służy do policzenia, ile kulek faktycznie zniknęło.
        balls_before_drag: int | None = None
        finger: tuple[int, int] | None = None
        # ostatnia znana pozycja DROP - gdy kulki go chwilowo zasłonią
        last_drop: vision.Circle | None = self._last_drop

        while not self._stop:
            if time.monotonic() - start_time > cfg.max_seconds_per_game_screen:
                logger.warning("[%s] przekroczono max czas ekranu gry - wracam do idle", cfg.name)
                return

            frame = self.adb.screenshot()

            if self._check_watchdogs(frame):
                logger.info("[%s] restart gry przerwał zbieranie - wracam do idle", cfg.name)
                return

            other = self._non_board_screen(frame)
            if other:
                # plansza się skończyła - z podpowiedzią położenia DROP
                # detektor mógłby brać elementy UI (np. PROCEED) za kulki
                if balls_before_drag is not None:
                    self.stats.balls_collected += min(balls_before_drag, 1)  # jak przy zniknięciu planszy
                logger.info("[%s] plansza zamknięta (%s) - wracam do idle", cfg.name, other)
                return

            det = vision.detect(frame, region=self._region(), drop_hint=last_drop)
            if det.drop is not None:
                last_drop = det.drop
                self._last_drop = det.drop

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

            # próby zostają tylko tam, gdzie obraz się nie zmienił
            kept = []
            for c, n, before in attempts:
                if not any(self._same_ball(c, b) for b in det.balls):
                    continue
                if float(np.abs(self._patch(frame, c.x, c.y, c.r) - before).mean()) > 6.0:
                    continue
                kept.append((c, n, before))
            attempts = kept

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

            idx = next((i for i, (c, _n, _col) in enumerate(attempts) if self._same_ball(c, ball)), None)
            count = (attempts[idx][1] if idx is not None else 0) + 1
            if count > cfg.max_drag_attempts_per_ball:
                logger.warning("[%s] kulka (%d,%d) nie znika po %d próbach - pomijam ją",
                               cfg.name, ball.x, ball.y, cfg.max_drag_attempts_per_ball)
                skipped.append(ball)
                continue
            entry = (ball, count, self._patch(frame, ball.x, ball.y, ball.r))
            if idx is not None:
                attempts[idx] = entry
            else:
                attempts.append(entry)

            logger.debug("[%s] przeciągam kulkę (%d,%d) -> DROP (%d,%d)",
                         cfg.name, ball.x, ball.y, drop.x, drop.y)

            finger = self._drag_ball(ball, drop)
            self.stats.drags += 1
            balls_before_drag = len(det.balls)

            self._sleep(humanize.human_delay(cfg.between_drags_delay.min, cfg.between_drags_delay.max))
