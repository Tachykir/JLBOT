"""
packs.py
========
Automatyczne otwieranie paczek (Craft -> Loot Packs), gdy gear jest za
słaby na wybraną głębokość.

Kiedy:
  - co `packs_check_interval_min` minut bot zagląda do Profile i czyta
    linię pod "CHOOSE DEPTH": kolorowe "enemies +N% stronger - find newer
    gear" = wrogowie mają bonus (depth > gear level + 55), szare
    "enemies at normal strength" = OK;
  - albo po `packs_defeat_streak` porażkach z rzędu ("Defeated.").

Co:
  1. Craft -> Loot Packs, "+" aż do maksimum (gra sama ogranicza do
     liczby paczek, na którą starcza pyłu), przycisk z ceną, OPEN.
  2. Karty aż się skończą (wraca ekran paczek): "Next Card" przy
     przedmiocie z auto-upgrade, klik w pusty obszar przy zwykłym dropie
     ("TAP ANYWHERE TO CONTINUE").
  3. Profile - jeśli nadal ostrzeżenie, kolejna runda (max
     `packs_max_rounds`). Brak okna potwierdzenia po kliknięciu ceny =
     za mało pyłu -> koniec, bot wraca do walki.
"""

from __future__ import annotations

import logging
import random
import time

import cv2
import numpy as np

import humanize
import screens

logger = logging.getLogger("bot")

# (import z bot.py opóźniony do użycia - bot.py importuje ten moduł)
def _point_defaults():
    from bot import POINT_DEFAULTS
    return POINT_DEFAULTS


class PackOpener:
    def __init__(self, bot):
        self.bot = bot
        self.defeat_streak = 0
        self._on_result_screen = False
        self._last_check: float | None = None
        self._last_depth_check: float | None = None
        self._depth_target_seen = None  # zmiana celu = sprawdź od razu
        self._known_max: int | None = None        # maks. odblokowana (tryb max_pct)
        self._known_max_time = 0.0
        self._size = (0, 0)  # (W, H) ostatniej klatki

    # ------------------------------------------------------------------ #
    # Obserwacja w trybie idle
    # ------------------------------------------------------------------ #
    def observe(self, frame) -> str | None:
        """Wywoływane na każdej klatce idle. Liczy porażki z rzędu i mówi,
        czy pora na procedurę: "defeats" | "check" | None."""
        cfg = self.bot.config
        if not cfg.auto_packs and not cfg.depth_enabled:
            return None
        self._size = (frame.shape[1], frame.shape[0])

        # Wynik walki liczymy raz na ekran i tylko wtedy, gdy log kończy się
        # napisem "Defeated." / "Victory." (sam szeroki przycisk na dole
        # albo czerwony tekst w trakcie walki to jeszcze nie koniec).
        button = screens.find_wide_bottom_button(frame) if cfg.auto_packs else None
        if button is None:
            self._on_result_screen = False
        elif not self._on_result_screen:
            # najpierw "Victory." - wygrana ma pierwszeństwo przed czerwonymi
            # wierszami logu
            if screens.has_victory_text(frame, button):
                self._on_result_screen = True
                if self.defeat_streak:
                    logger.info("[%s] wygrana walka - zeruję licznik porażek", cfg.name)
                self.defeat_streak = 0
            elif screens.has_defeat_text(frame, button):
                self._on_result_screen = True
                self.defeat_streak += 1
                self.bot.stats.defeats += 1
                logger.info("[%s] porażka w walce (%d z rzędu)", cfg.name, self.defeat_streak)

        # procedury zaczynamy tylko na ekranie z paskiem nawigacji (nie w
        # trakcie walki, dropu czy minigry)
        if screens.find_just_loot(frame) is None:
            return None
        now = time.monotonic()
        if cfg.auto_packs:
            if self.defeat_streak >= cfg.packs_defeat_streak:
                return "defeats"
            if self._last_check is None or now - self._last_check >= cfg.packs_check_interval_min * 60:
                return "check"
        if cfg.depth_enabled and (self._last_depth_check is None
                                   or self._depth_goal() != self._depth_target_seen
                                   or now - self._last_depth_check >= cfg.depth_check_interval_min * 60):
            return "depth"
        return None

    # ------------------------------------------------------------------ #
    # Pomocnicze
    # ------------------------------------------------------------------ #
    def _abort(self) -> bool:
        return self.bot._stop or self.bot.is_paused

    def _frame(self):
        frame = self.bot.adb.screenshot()
        self._size = (frame.shape[1], frame.shape[0])
        return frame

    def _tap_rel(self, point: tuple[float, float, float]) -> None:
        """Kliknięcie w losowe miejsce obszaru (x, y, promień) - ułamki ekranu."""
        W, H = self._size
        x, y = humanize.random_point_in_circle(point[0] * W, point[1] * H, point[2] * W)
        self.bot._tap(x, y)

    def _tap_nav(self, key: str) -> None:
        """
        Kliknięcie zakładki w pasku nawigacji (PROFILE / CRAFT) w obszarze
        ustawionym w konfiguracji. Dla domyślnych (niekalibrowanych)
        punktów wysokość bierzemy od wykrytego przycisku JUST LOOT - leży
        w tym samym pasku, więc trafia przy dowolnych proporcjach ekranu
        (zakładki są pół promienia koła nad jego środkiem: iOS i Android).
        Punkt wskazany w GUI jest używany dokładnie tak, jak ustawiono.
        """
        point = getattr(self.bot.config, key)
        frame = self._frame()
        W, H = self._size
        y = point[1] * H
        if tuple(point) == _point_defaults()[key]:
            button = screens.find_just_loot(frame)
            if button is not None:
                y = button.y - 0.5 * button.r
        x, y = humanize.random_point_in_circle(point[0] * W, y, point[2] * W)
        self.bot._tap(x, y)

    def _tap_rect(self, rect) -> None:
        cx, cy = rect.center
        self.bot._tap(*humanize.random_point_in_circle(cx, cy, min(rect.w, rect.h) * 0.3))

    def _leave_result_screen(self) -> None:
        """Jeśli widać ekran końca walki (PROCEED), kliknij go - pasek
        nawigacji jest widoczny dopiero na ekranach głównych."""
        for _ in range(3):
            frame = self._frame()
            button = screens.find_wide_bottom_button(frame)
            if button is None:
                return
            self._tap_rect(button)
            self.bot._sleep(random.uniform(1.0, 1.6))

    # ------------------------------------------------------------------ #
    def check_profile(self) -> bool | None:
        """Otwiera Profile i sprawdza, czy wrogowie mają bonus. True =
        za słaby gear, False = OK, None = nie udało się odczytać."""
        cfg = self.bot.config
        self._open_profile()
        warning = screens.frontier_warning(self._frame())
        logger.info("[%s] profil: %s", cfg.name,
                    {True: "wrogowie mają bonus - gear za słaby",
                     False: "wrogowie na normalnym poziomie",
                     None: "nie udało się odczytać selektora głębokości"}[warning])
        return warning

    def _open_profile(self) -> None:
        """Otwiera Profile i w razie potrzeby przewija do "CHOOSE DEPTH"."""
        self._tap_nav("nav_profile")
        self.bot._sleep(random.uniform(0.8, 1.2))
        frame = self._frame()
        if screens.find_depth_selector(frame) is None and screens.frontier_warning(frame) is None:
            # selektor głębokości może być poniżej - przewiń stronę
            W, H = self._size
            self.bot.adb.swipe(int(W * 0.5), int(H * 0.65), int(W * 0.5), int(H * 0.35),
                               duration_ms=random.randint(300, 450))
            self.bot._sleep(random.uniform(0.7, 1.0))

    def _depth_goal(self):
        """Cel ustawiania (zmiana = sprawdź od razu)."""
        cfg = self.bot.config
        if cfg.depth_mode == "max_pct":
            return ("max_pct", cfg.depth_below_max_pct)
        return cfg.depth_mode if cfg.depth_mode in ("max", "max_normal") else cfg.farm_depth

    def _read_depth(self):
        return screens.find_depth_selector(self._frame())

    def ensure_depth(self, already_on_profile: bool = False) -> bool | None:
        """
        Ustawia głębokość w Profile ("CHOOSE DEPTH") wg `depth_mode`:
          - "fixed": `farm_depth`,
          - "max": najwyższa odblokowana (strzałka "›" aż przestanie
            cokolwiek zmieniać / zgaśnie),
          - "max_normal": jak "max", a potem "‹" aż zniknie bonus wrogów
            ("enemies at normal strength").
        Kafelki (do 79): klik w kafelek z numerem albo strzałka w stronę
        celu; duża liczba (80+): strzałka co 1. True = ustawiona, False =
        nie da się dokładnie (cel poza odblokowanym zakresem), None = nie
        odczytano selektora.
        """
        cfg = self.bot.config
        self._last_depth_check = time.monotonic()
        self._depth_target_seen = self._depth_goal()
        if not cfg.depth_enabled:
            return True
        if not already_on_profile:
            self._open_profile()
        sel = self._read_depth()
        if sel is None:
            logger.warning("[%s] nie znaleziono wyboru głębokości (CHOOSE DEPTH) w Profile", cfg.name)
            return None
        start = sel.current

        if cfg.depth_mode == "max":
            result, sel = self._move_depth(sel, None)
            if result is None:
                return None
            final = sel.current if sel is not None else None
            if final != start:
                logger.info("[%s] głębokość: %s -> %s (maks. odblokowana)", cfg.name, start, final)
            return True
        if cfg.depth_mode == "max_pct":
            # maksimum rośnie tylko przy walce na samym maksimum, więc przy
            # farmieniu niżej wystarczy je sprawdzać raz na godzinę
            if self._known_max is None or time.monotonic() - self._known_max_time > 3600:
                result, sel = self._move_depth(sel, None)
                if result is None or sel is None or sel.current is None:
                    return None
                self._known_max, self._known_max_time = sel.current, time.monotonic()
            target = max(1, int(self._known_max * (1 - cfg.depth_below_max_pct / 100.0)))
            result, sel = self._move_depth(sel, target)
            if result is True and start != target:
                logger.info("[%s] głębokość: %s -> %d (maks. %d minus %.0f%%)", cfg.name, start, target,
                            self._known_max, cfg.depth_below_max_pct)
            return result
        if cfg.depth_mode == "max_normal":
            sel = self._climb_normal(sel)
            if sel is None:
                return None
            if sel.current != start:
                logger.info("[%s] głębokość: %s -> %s (najwyższa bez bonusu wrogów)", cfg.name, start, sel.current)
            return True

        target = cfg.farm_depth
        result, sel = self._move_depth(sel, target)
        if result is True and start != target:
            logger.info("[%s] głębokość ustawiona: %s -> %d", cfg.name, start, target)
        elif result is False:
            logger.warning("[%s] głębokości %d nie da się wybrać - zostaje %s (poza odblokowanym zakresem?)",
                           cfg.name, target, sel.current if sel is not None else None)
        return result

    def _move_depth(self, sel, target: int | None):
        """Przesuwa wybór do `target` (None = jak najwyżej). Zwraca
        (True/False/None, ostatni odczyt selektora)."""
        cfg = self.bot.config
        stuck = 0
        prev_state = None
        for _ in range(400):
            if self._abort():
                return None, sel
            if sel is None:
                logger.warning("[%s] zgubiono wybór głębokości w Profile", cfg.name)
                return None, sel
            numbers = [t.number for t in sel.tiles if t.number is not None]
            current = sel.current
            if not numbers:
                logger.warning("[%s] nie udało się odczytać numeru głębokości", cfg.name)
                return None, sel
            if target is not None and current == target:
                return True, sel
            state = (tuple(numbers), current)
            at_end = target is None or target > max(numbers)
            # nieaktywna "›" przy dużej liczbie = koniec zakresu, bez klikania
            if at_end and sel.kind == "big" and not sel.right_found:
                stuck = 3
            elif state == prev_state:
                stuck += 1
            else:
                stuck = 0
            prev_state = state
            if stuck >= 3:
                # koniec zakresu - przy kafelkach wybierz skrajny widoczny
                if sel.kind == "tiles":
                    edge_number = max(numbers) if at_end else min(numbers)
                    edge = next(t for t in sel.tiles if t.number == edge_number)
                    if current != edge_number:
                        self._tap_rect(edge.rect)
                        self.bot._sleep(humanize.human_delay(0.2, 0.6))
                        sel = self._read_depth()
                return (True if target is None else False), sel

            tile = next((t for t in sel.tiles if t.number == target), None) if target is not None else None
            if sel.kind == "tiles" and tile is not None:
                self._tap_rect(tile.rect)
            else:
                if target is None:
                    up = True
                else:
                    ref = current if current is not None else (numbers[0] if target < numbers[0] else numbers[-1])
                    up = target > ref
                self._tap_rect(sel.right if up else sel.left)
            self.bot._sleep(humanize.human_delay(0.2, 0.6))
            sel = self._read_depth()
        return False, sel

    def _climb_normal(self, sel):
        """Najwyższa głębokość bez bonusu wrogów, szukana od obecnej: z
        bonusem - w dół, bez - w górę po 1, dopóki bonus się nie pojawi
        (wtedy o 1 z powrotem) albo nie skończy się zakres. Przy kolejnych
        sprawdzeniach to zwykle 1-2 kliknięcia."""
        cfg = self.bot.config
        warning = screens.frontier_warning(self._frame())
        if warning is None:
            logger.warning("[%s] nie odczytano siły wrogów w Profile", cfg.name)
            return None
        if warning:
            return self._step_down_until_normal(sel)
        for _ in range(400):
            if self._abort() or sel is None:
                return None
            before = sel.current
            if before is None:
                return sel
            result, after = self._move_depth(sel, before + 1)
            if result is not True:
                return after if result is False else None   # koniec zakresu / błąd
            frame = self._frame()
            warning = screens.frontier_warning(frame)
            if warning:
                result, after = self._move_depth(screens.find_depth_selector(frame), before)
                return after
            sel = after
        return sel

    def _step_down_until_normal(self, sel):
        """Z najwyższej głębokości schodzi "‹" aż zniknie ostrzeżenie o
        bonusie wrogów. Zwraca ostatni odczyt selektora (None = błąd)."""
        cfg = self.bot.config
        for _ in range(200):
            if self._abort():
                return None
            frame = self._frame()
            warning = screens.frontier_warning(frame)
            if warning is None:
                logger.warning("[%s] nie odczytano siły wrogów w Profile", cfg.name)
                return None
            sel = screens.find_depth_selector(frame)
            if sel is None or not warning:
                return sel
            before = sel.current
            if before is None or before <= 1:
                return sel
            result, after = self._move_depth(sel, before - 1)
            if result is None:
                return None
            if result is False:
                logger.warning("[%s] nie da się zejść poniżej głębokości %s", cfg.name, before)
                return after
        return sel

    def open_round(self) -> bool:
        """Jedna runda: max paczek, OPEN, wszystkie karty. False = nie
        otworzono (za mało pyłu albo nie znaleziono ekranu)."""
        bot, cfg = self.bot, self.bot.config
        self._tap_nav("nav_craft")
        bot._sleep(random.uniform(0.8, 1.2))
        frame = self._frame()
        buy = screens.find_buy_button(frame)
        if buy is None:
            self._tap_rel(cfg.craft_loot_packs_tab)
            bot._sleep(random.uniform(0.6, 0.9))
            frame = self._frame()
            buy = screens.find_buy_button(frame)
        if buy is None:
            logger.warning("[%s] nie znaleziono ekranu Loot Packs w zakładce Craft", cfg.name)
            return False

        W, _H = self._size
        plus = screens.find_plus_button(frame, buy) or (int(buy.center[0] + 0.06 * W), int(buy.y - 1.05 * buy.h))
        if self._abort():
            return False
        # wszystkie "+" jedną serią (jedno polecenie ADB zamiast 25)
        bot._tap_many([humanize.random_point_in_circle(plus[0], plus[1], 0.015 * W)
                       for _ in range(cfg.pack_plus_taps)], (0.04, 0.12))

        bot._sleep(random.uniform(0.25, 0.5))
        frame = self._frame()
        buy = screens.find_buy_button(frame) or buy
        self._tap_rect(buy)
        dialog = None
        for _ in range(3):  # okno pojawia się po krótkiej animacji
            bot._sleep(random.uniform(0.4, 0.6))
            dialog = screens.find_open_dialog(self._frame())
            if dialog is not None:
                break
        if dialog is None:
            logger.info("[%s] brak okna potwierdzenia - prawdopodobnie za mało pyłu", cfg.name)
            return False
        bot._sleep(random.uniform(0.2, 0.5))
        bot._tap(*humanize.random_point_in_circle(*dialog.open_button, 0.012 * W))
        bot._sleep(random.uniform(0.8, 1.2))

        # Karty: przedmiot z auto-upgrade ma przycisk "Next Card", zwykły
        # drop - napis "TAP ANYWHERE TO CONTINUE" bez przycisku. Klikamy
        # przycisk, jeśli jest, a w przeciwnym razie pusty obszar u góry
        # ekranu - aż wróci ekran paczek.
        cards = 0
        unchanged = 0
        prev = None
        deadline = time.monotonic() + 1800
        while time.monotonic() < deadline and not self._abort():
            frame = self._frame()
            if screens.find_buy_button(frame) is not None and screens.find_open_dialog(frame) is None:
                logger.info("[%s] otwarto paczki (%d kliknięć w karty)", cfg.name, cards)
                return True
            if cards >= 1000:
                logger.warning("[%s] ponad %d kart - przerywam", cfg.name, cards)
                return True
            # zabezpieczenie: obraz przestał się zmieniać mimo klikania
            small = cv2.resize(frame, (64, 64), interpolation=cv2.INTER_AREA).astype(np.int16)
            if prev is not None and float(np.abs(small - prev).mean()) < 1.0:
                unchanged += 1
                if unchanged >= 15:
                    logger.warning("[%s] ekran kart nie reaguje na kliknięcia (%d kart) - przerywam",
                                   cfg.name, cards)
                    return False
            else:
                unchanged = 0
            prev = small

            done = screens.find_center_button(frame)
            if done is not None:
                # "Pack Opened" - "Done" po ostatniej karcie
                self._tap_rect(done)
                bot._sleep(random.uniform(0.5, 0.8))
                continue
            button = screens.find_wide_bottom_button(frame)
            if button is not None:
                self._tap_rect(button)
                cards += 1
                bot._sleep(random.uniform(0.15, 0.35))
            else:
                # "tap anywhere": 2-3 szybkie kliknięcia na jeden zrzut
                # ekranu - ten obszar jest pusty także na ekranie paczek,
                # więc nadmiarowe kliknięcie po ostatniej karcie nic nie robi
                W, H = self._size
                px, py, pr = cfg.card_tap_anywhere
                n = random.randint(2, 3)
                bot._tap_many([humanize.random_point_in_circle(px * W, py * H, pr * W) for _ in range(n)],
                              (0.12, 0.3))
                cards += n
                bot._sleep(random.uniform(0.05, 0.15))
        return cards > 0

    # ------------------------------------------------------------------ #
    def run(self, reason: str) -> None:
        bot, cfg = self.bot, self.bot.config
        self._leave_result_screen()
        if reason == "depth":
            self.ensure_depth()
            return
        self._last_check = time.monotonic()

        warning = self.check_profile()
        if cfg.depth_enabled and self.ensure_depth(already_on_profile=True) is not None:
            # po zmianie głębokości zmienia się też ostrzeżenie o sile wrogów
            warning = screens.frontier_warning(self._frame())
        if reason == "check" and warning is not True:
            return
        if reason == "defeats":
            logger.warning("[%s] %d porażek z rzędu - otwieram paczki", cfg.name, self.defeat_streak)
        else:
            logger.warning("[%s] gear za słaby na tę głębokość - otwieram paczki", cfg.name)

        rounds = 0
        while rounds < cfg.packs_max_rounds and not self._abort():
            if not self.open_round():
                break
            rounds += 1
            bot.stats.pack_rounds += 1
            warning = self.check_profile()
            if warning is not True:
                break
        logger.info("[%s] koniec otwierania paczek (%d rund) - wracam do walki", cfg.name, rounds)
        self.defeat_streak = 0
        self._last_check = time.monotonic()
        if warning is None:
            self._tap_nav("nav_profile")
            bot._sleep(1.0)
