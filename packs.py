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
        self._size = (0, 0)  # (W, H) ostatniej klatki

    # ------------------------------------------------------------------ #
    # Obserwacja w trybie idle
    # ------------------------------------------------------------------ #
    def observe(self, frame) -> str | None:
        """Wywoływane na każdej klatce idle. Liczy porażki z rzędu i mówi,
        czy pora na procedurę: "defeats" | "check" | None."""
        cfg = self.bot.config
        if not cfg.auto_packs:
            return None
        self._size = (frame.shape[1], frame.shape[0])

        button = screens.find_wide_bottom_button(frame)
        if button is not None:
            if not self._on_result_screen:
                if screens.has_defeat_text(frame, button):
                    self.defeat_streak += 1
                    self.bot.stats.defeats += 1
                    logger.info("[%s] porażka w walce (%d z rzędu)", cfg.name, self.defeat_streak)
                elif self.defeat_streak:
                    logger.info("[%s] wygrana walka - zeruję licznik porażek", cfg.name)
                    self.defeat_streak = 0
            self._on_result_screen = True
        else:
            self._on_result_screen = False

        if self.defeat_streak >= cfg.packs_defeat_streak:
            return "defeats"
        if (self._last_check is None
                or time.monotonic() - self._last_check >= cfg.packs_check_interval_min * 60):
            return "check"
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
        self._tap_nav("nav_profile")
        self.bot._sleep(random.uniform(1.2, 1.8))
        warning = screens.frontier_warning(self._frame())
        if warning is None:
            # selektor głębokości może być poniżej - przewiń stronę
            W, H = self._size
            self.bot.adb.swipe(int(W * 0.5), int(H * 0.65), int(W * 0.5), int(H * 0.35), duration_ms=400)
            self.bot._sleep(1.0)
            warning = screens.frontier_warning(self._frame())
        logger.info("[%s] profil: %s", cfg.name,
                    {True: "wrogowie mają bonus - gear za słaby",
                     False: "wrogowie na normalnym poziomie",
                     None: "nie udało się odczytać selektora głębokości"}[warning])
        return warning

    def open_round(self) -> bool:
        """Jedna runda: max paczek, OPEN, wszystkie karty. False = nie
        otworzono (za mało pyłu albo nie znaleziono ekranu)."""
        bot, cfg = self.bot, self.bot.config
        self._tap_nav("nav_craft")
        bot._sleep(random.uniform(1.2, 1.8))
        frame = self._frame()
        buy = screens.find_buy_button(frame)
        if buy is None:
            self._tap_rel(cfg.craft_loot_packs_tab)
            bot._sleep(1.0)
            frame = self._frame()
            buy = screens.find_buy_button(frame)
        if buy is None:
            logger.warning("[%s] nie znaleziono ekranu Loot Packs w zakładce Craft", cfg.name)
            return False

        W, _H = self._size
        plus = screens.find_plus_button(frame, buy) or (int(buy.center[0] + 0.06 * W), int(buy.y - 1.05 * buy.h))
        for _ in range(cfg.pack_plus_taps):
            if self._abort():
                return False
            bot._tap(*humanize.random_point_in_circle(plus[0], plus[1], 0.015 * W))
            bot._sleep(random.uniform(0.05, 0.15))

        bot._sleep(random.uniform(0.4, 0.8))
        frame = self._frame()
        buy = screens.find_buy_button(frame) or buy
        self._tap_rect(buy)
        bot._sleep(random.uniform(1.0, 1.5))
        dialog = screens.find_open_dialog(self._frame())
        if dialog is None:
            logger.info("[%s] brak okna potwierdzenia - prawdopodobnie za mało pyłu", cfg.name)
            return False
        bot._sleep(random.uniform(0.3, 0.8))
        bot._tap(*humanize.random_point_in_circle(*dialog.open_button, 0.012 * W))
        bot._sleep(random.uniform(1.5, 2.0))

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
                logger.info("[%s] otwarto paczki (%d kart)", cfg.name, cards)
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

            button = screens.find_wide_bottom_button(frame)
            if button is not None:
                self._tap_rect(button)
            else:
                self._tap_rel(cfg.card_tap_anywhere)
            cards += 1
            bot._sleep(random.uniform(0.35, 0.8))
        return cards > 0

    # ------------------------------------------------------------------ #
    def run(self, reason: str) -> None:
        bot, cfg = self.bot, self.bot.config
        self._last_check = time.monotonic()
        self._leave_result_screen()

        warning = self.check_profile()
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
