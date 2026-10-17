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

Cała logika jest niezależna od danej instancji ADB - dostaje
skonfigurowany ADBController i działa na nim w pętli `run()`,
co pozwala odpalać wiele instancji równolegle (patrz manager.py).
"""

from __future__ import annotations

import time
import random
import logging
import datetime as dt
from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np

from adb_controller import ADBController, ADBError
import vision

logger = logging.getLogger("bot")

# Wywoływany po każdym screenshot+detect z (nazwa_instancji, frame_bgr, DetectionResult, tryb).
# tryb: "idle" | "collecting". Używane przez GUI do podglądu na żywo bez
# duplikowania pętli detekcji.
FrameCallback = Callable[[str, np.ndarray, "vision.DetectionResult", str], None]


@dataclass
class ScheduleWindow:
    """
    Jedno okno czasowe, w którym bot MOŻE pracować. Bot buduje sobie
    dowolny, spersonalizowany rytm dnia z kilku takich okien naraz -
    np. "9:00-12:00 w dni robocze", "14:30-18:00 codziennie",
    "20:00-22:30 pon/śr/pt" - zamiast jednego sztywnego cyklu
    praca/przerwa.

    days: zbiór dni tygodnia, w które to okno obowiązuje (0=poniedziałek
    ... 6=niedziela, zgodnie z datetime.weekday()). Pusty zbiór/None
    oznacza "każdy dzień".

    start / end: godzina:minuta w formacie "HH:MM" (24h). Okno
    przechodzące przez północ (np. "22:00"-"02:00") jest obsługiwane
    poprawnie.
    """
    start: str  # "HH:MM"
    end: str    # "HH:MM"
    days: tuple[int, ...] | None = None  # None = wszystkie dni

    def _parse(self, hhmm: str) -> tuple[int, int]:
        h, m = hhmm.split(":")
        return int(h), int(m)

    def contains(self, moment: dt.datetime) -> bool:
        if self.days is not None and moment.weekday() not in self.days:
            return False

        start_h, start_m = self._parse(self.start)
        end_h, end_m = self._parse(self.end)
        start_minutes = start_h * 60 + start_m
        end_minutes = end_h * 60 + end_m
        now_minutes = moment.hour * 60 + moment.minute

        if start_minutes <= end_minutes:
            # zwykłe okno w obrębie jednego dnia, np. 09:00-18:00
            return start_minutes <= now_minutes < end_minutes
        else:
            # okno przechodzące przez północ, np. 22:00-02:00
            return now_minutes >= start_minutes or now_minutes < end_minutes


@dataclass
class RandomRange:
    """
    Zakres losowania (min, max) dla dowolnego parametru czasowego -
    zamiast stałej wartości, bot losuje z tego przedziału za każdym
    razem, żeby unikać w pełni mechanicznego, identycznego rytmu
    klikania/przeciągania.

    Jeśli min == max, zachowuje się jak stała wartość (brak losowości) -
    wygodne do wyłączenia losowości dla konkretnego parametru bez
    zmiany reszty kodu.
    """
    min: float
    max: float

    def sample(self) -> float:
        if self.max <= self.min:
            return self.min
        return random.uniform(self.min, self.max)

    @staticmethod
    def from_value(value) -> "RandomRange":
        """
        Buduje RandomRange z wartości configu, obsługując trzy formaty
        dla wygody i wstecznej kompatybilności:
          - liczba (int/float): stała wartość, min == max == value
          - [min, max] / (min, max): zakres
          - {"min": ..., "max": ...}: zakres jako słownik (jak w JSON)
        """
        if isinstance(value, RandomRange):
            return value
        if isinstance(value, dict):
            return RandomRange(float(value["min"]), float(value["max"]))
        if isinstance(value, (list, tuple)):
            return RandomRange(float(value[0]), float(value[1]))
        return RandomRange(float(value), float(value))

    def to_dict(self) -> dict:
        return {"min": self.min, "max": self.max}


def schedule_from_list(raw: list | None) -> list[ScheduleWindow]:
    """Buduje listę ScheduleWindow z surowych danych JSON (lista słowników
    {"start": "HH:MM", "end": "HH:MM", "days": [0,1,2,...] | null})."""
    if not raw:
        return []
    windows = []
    for item in raw:
        days = item.get("days")
        windows.append(ScheduleWindow(
            start=item["start"],
            end=item["end"],
            days=tuple(days) if days is not None else None,
        ))
    return windows


def schedule_to_list(schedule: list[ScheduleWindow]) -> list[dict]:
    """Odwrotność schedule_from_list - do zapisu w JSON."""
    return [
        {
            "start": w.start,
            "end": w.end,
            "days": list(w.days) if w.days is not None else None,
        }
        for w in schedule
    ]


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

    # Maksymalny czas (s) oczekiwania na zniknięcie przeciągniętej kulki
    # z planszy, zanim bot pójdzie dalej. Bot sprawdza planszę co 0.15 s
    # i nie czeka dłużej, niż trzeba. Losowany zakres (s).
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
    # Przykład - dwa okna pracy w dni robocze + krótsza sesja weekendowa:
    #   schedule = [
    #       ScheduleWindow("09:00", "12:30", days=(0,1,2,3,4)),
    #       ScheduleWindow("14:00", "19:00", days=(0,1,2,3,4)),
    #       ScheduleWindow("11:00", "16:00", days=(5,6)),
    #   ]
    schedule: list[ScheduleWindow] = field(default_factory=list)

    # Co ile sekund sprawdzać, czy nadal jesteśmy w oknie harmonogramu
    # (podczas oczekiwania poza godzinami pracy - nie ma sensu sprawdzać
    # częściej niż raz na kilkanaście sekund).
    schedule_poll_interval: float = 15.0

    # Czy zamykać grę (force-stop) na czas przerwy między oknami
    # harmonogramu, i uruchamiać ją ponownie na starcie kolejnego
    # okna pracy. Wymaga ustawionego game_package - bez niego ta
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


@dataclass
class BotStats:
    cycles: int = 0
    balls_collected: int = 0
    errors: int = 0
    restarts: int = 0


class DustMineBot:
    def __init__(self, adb: ADBController, config: BotConfig,
                 on_frame: Optional[FrameCallback] = None):
        self.adb = adb
        self.config = config
        self.stats = BotStats()
        self._stop = False
        self._paused = False
        self._on_frame = on_frame

        # Watchdog czarnego ekranu: znacznik czasu (time.monotonic()),
        # od kiedy nieprzerwanie widzimy czarny ekran. None = ekran
        # aktualnie nie jest czarny (licznik zresetowany).
        self._black_screen_since: float | None = None
        self._watchdog_disabled_logged = False
        self._manual_restart_requested = False
        self._last_foreground_check: float = 0.0
        self._last_schedule_log: float = 0.0

    def stop(self) -> None:
        self._stop = True

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
        Działa niezależnie od tego, czy watchdog (game_package) jest
        skonfigurowany.
        """
        self._manual_restart_requested = True

    # ------------------------------------------------------------------ #
    def _random_point_near(self, center: tuple[int, int], radius: float) -> tuple[int, int]:
        """
        Losuje punkt w kole o danym promieniu wokół `center`, z
        rozkładem normalnym (gaussowskim) - punkty bliżej środka są
        znacznie bardziej prawdopodobne niż punkty przy samej krawędzi,
        co przypomina naturalną niedokładność ludzkiego dotyku (rzadko
        celuje się dokładnie w brzeg obszaru, który się widzi).

        Odległość od środka losowana z |N(0, radius/2.5)|, przycięta do
        `radius` - odchylenie radius/2.5 dobrane tak, żeby ~99% próbek
        mieściło się w promieniu (reguła 3-sigma), a jednocześnie
        rozkład wyraźnie faworyzował okolice środka.
        Kąt losowany równomiernie z [0, 2*pi).
        """
        if radius <= 0:
            return center

        sigma = radius / 2.5
        distance = min(abs(random.gauss(0.0, sigma)), radius)
        angle = random.uniform(0.0, 2 * 3.141592653589793)

        dx = distance * np.cos(angle)
        dy = distance * np.sin(angle)
        # Zaokrąglamy dopiero na samym końcu (raz), więc błąd
        # zaokrąglenia to maksymalnie ~0.71px (przekątna pół-piksela) -
        # nieistotne dla klikania na ekranie telefonu, celowo nie
        # kompensujemy tego dodatkowym przeskalowaniem, bo ono z kolei
        # wprowadzałoby własny błąd zaokrąglenia.
        return (int(round(center[0] + dx)), int(round(center[1] + dy)))

    # ------------------------------------------------------------------ #
    def _is_within_schedule(self, now: dt.datetime | None = None) -> bool:
        """
        Czy w tej chwili bot POWINIEN pracować wg `config.schedule`.
        Brak zdefiniowanego harmonogramu (pusta lista/None) oznacza
        "zawsze" - pełna wsteczna kompatybilność.
        """
        cfg = self.config
        if not cfg.schedule:
            return True
        if now is None:
            now = dt.datetime.now()
        return any(window.contains(now) for window in cfg.schedule)

    def _wait_for_schedule(self) -> None:
        """
        Blokuje (w pętli, sprawdzając self._stop) dopóki nie wejdziemy
        w okno harmonogramu. Loguje raz na wejście w stan oczekiwania
        i potem okresowo, żeby było widać że bot żyje, tylko czeka na
        swoją porę - a nie że się zawiesił.

        Jeśli `close_game_during_break` jest włączone i mamy
        skonfigurowany game_package, gra jest zamykana (force-stop) na
        czas przerwy i ponownie uruchamiana tuż przed powrotem do
        normalnej pracy.
        """
        cfg = self.config
        if self._is_within_schedule():
            return

        logger.info("[%s] poza godzinami harmonogramu - czekam na najbliższe okno pracy", cfg.name)

        game_was_closed = False
        if cfg.close_game_during_break and cfg.game_package:
            try:
                self.adb.stop_app(cfg.game_package)
                game_was_closed = True
                logger.info("[%s] zamknięto grę na czas przerwy", cfg.name)
            except ADBError as e:
                logger.warning("[%s] nie udało się zamknąć gry na przerwę: %s", cfg.name, e)

        while not self._stop and not self._is_within_schedule():
            now = time.monotonic()
            if now - self._last_schedule_log > 300:  # log co 5 minut, żeby nie zaśmiecać
                logger.info("[%s] nadal poza harmonogramem, czekam...", cfg.name)
                self._last_schedule_log = now
            time.sleep(cfg.schedule_poll_interval)

        if self._stop:
            return

        logger.info("[%s] wejście w okno harmonogramu - wznawiam pracę", cfg.name)
        if game_was_closed:
            try:
                self.adb.start_app(cfg.game_package, cfg.game_activity)
                logger.info("[%s] uruchomiono grę na start okna pracy", cfg.name)
                time.sleep(cfg.restart_settle_delay)
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
                self._idle_loop()
                if self._stop:
                    break
                self._collect_loop()
                self.stats.cycles += 1
            except ADBError as e:
                logger.error("[%s] błąd ADB: %s", self.config.name, e)
                self.stats.errors += 1
                time.sleep(2.0)
            except Exception:
                logger.exception("[%s] nieoczekiwany błąd", self.config.name)
                self.stats.errors += 1
                time.sleep(2.0)

    # ------------------------------------------------------------------ #
    def _do_restart_app(self, reason: str) -> bool:
        """
        Wykonuje faktyczny restart gry przez ADB (force-stop + start) i
        czyści stan watchdoga. Współdzielone przez auto-watchdog i
        ręczne żądanie restartu z GUI. Zwraca True przy powodzeniu.
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
        self.stats.restarts += 1
        time.sleep(cfg.restart_settle_delay)
        return True

    # ------------------------------------------------------------------ #
    def _check_foreign_app_watchdog(self) -> bool:
        """
        Sprawdza (co `foreground_check_interval` sekund), czy na
        pierwszym planie jest inna aplikacja niż gra - np. Google Play
        otwarty przez przypadkowe kliknięcie w reklamę. Jeśli tak,
        restartuje grę NATYCHMIAST, bez odliczania (w odróżnieniu od
        watchdoga czarnego ekranu) - tu nie ma niepewności, sygnał jest
        jednoznaczny.

        Zwraca True, jeśli wykonano restart.
        """
        cfg = self.config
        if not cfg.game_package or not cfg.foreign_app_packages:
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
            return False  # wszystko w porządku, gra jest na pierwszym planie

        if foreground in cfg.foreign_app_packages:
            logger.warning(
                "[%s] wykryto obcą aplikację na pierwszym planie (%s) zamiast gry - "
                "prawdopodobnie kliknięcie w reklamę wyrzuciło do sklepu/przeglądarki",
                cfg.name, foreground,
            )
            return self._do_restart_app(f"wykryto {foreground} zamiast gry")

        # inny pakiet, spoza znanej listy (np. launcher Androida w tle
        # przez ułamek sekundy) - nie traktujemy tego jako problem,
        # żeby uniknąć fałszywych restartów na nieznanych, neutralnych
        # ekranach przejściowych
        return False

    # ------------------------------------------------------------------ #
    def _check_black_screen_watchdog(self, frame: np.ndarray) -> bool:
        """
        Aktualizuje licznik czarnego ekranu na podstawie bieżącej klatki
        i - jeśli przekroczono `black_screen_timeout` - restartuje grę
        (force-stop + start) przez ADB.

        Zwraca True, jeśli wykonano restart (żeby wywołujący mógł
        przerwać bieżącą pętlę i zacząć od nowa od _idle_loop).
        """
        cfg = self.config

        # Ręczne żądanie restartu z GUI ma priorytet i działa
        # niezależnie od tego, czy watchdog czarnego ekranu jest
        # skonfigurowany (game_package) - liczy się jedynie fakt,
        # że game_package w ogóle istnieje, żeby było co restartować.
        if self._manual_restart_requested:
            self._manual_restart_requested = False
            return self._do_restart_app("ręczne żądanie z GUI")

        if not cfg.game_package:
            if not self._watchdog_disabled_logged:
                logger.warning(
                    "[%s] watchdog czarnego ekranu WYŁĄCZONY - brak 'game_package' w konfiguracji "
                    "tej instancji. Ustaw pole game_package (np. w GUI albo w config.json), "
                    "żeby auto-restart działał.",
                    cfg.name,
                )
                self._watchdog_disabled_logged = True
            return False

        # Sprawdzamy jasność TYLKO w obrębie board_region, jeśli jest
        # ustawiony - a nie całej klatki. Cała klatka zwykle zawiera też
        # pasek statusu / nawigację systemową Androida, które mogą być
        # jasne nawet gdy sama plansza gry jest czarna (zawieszona),
        # co zaniżałoby wykrywalność problemu.
        if cfg.board_region is not None:
            x1, y1, x2, y2 = cfg.board_region
            region_frame = frame[y1:y2, x1:x2]
        else:
            region_frame = frame

        if not vision.is_black_screen(region_frame, cfg.black_screen_brightness_threshold):
            self._black_screen_since = None
            return False

        now = time.monotonic()
        if self._black_screen_since is None:
            self._black_screen_since = now
            logger.info("[%s] wykryto czarny ekran, start odliczania watchdoga (limit %.0fs)",
                        cfg.name, cfg.black_screen_timeout)
            return False

        elapsed = now - self._black_screen_since
        if elapsed < cfg.black_screen_timeout:
            # log co ~30s, żeby było widać że odliczanie faktycznie trwa
            if int(elapsed) % 30 < 1:
                logger.info("[%s] czarny ekran trwa %.0fs / %.0fs", cfg.name, elapsed, cfg.black_screen_timeout)
            return False

        return self._do_restart_app(f"czarny ekran przez {elapsed:.0f}s (limit {cfg.black_screen_timeout:.0f}s)")

    # ------------------------------------------------------------------ #
    def _idle_loop(self) -> None:
        """Klika idle_point dopóki nie pojawi się ekran z kulkami."""
        cfg = self.config
        last_click = 0.0
        next_click_delay = cfg.idle_click_interval.sample()
        while not self._stop:
            frame = self.adb.screenshot()

            if self._check_foreign_app_watchdog():
                last_click = 0.0
                continue

            if self._check_black_screen_watchdog(frame):
                # gra została zrestartowana - zacznij tę pętlę od nowa
                # ze świeżym screenshotem zamiast działać na starej klatce
                last_click = 0.0
                continue

            det = vision.detect(frame, region=cfg.board_region)

            if self._on_frame is not None:
                self._on_frame(cfg.name, frame, det, "idle")

            if vision.has_game_screen(det):
                logger.info("[%s] wykryto ekran gry: DROP=%s, kulki=%d",
                            cfg.name, det.drop.center if det.drop else None, len(det.balls))
                return

            now = time.monotonic()
            if not self._paused and now - last_click >= next_click_delay:
                click_point = self._random_point_near(cfg.idle_point, cfg.idle_point_radius)
                self.adb.tap(*click_point)
                last_click = now
                next_click_delay = cfg.idle_click_interval.sample()

            time.sleep(cfg.idle_poll_interval)

    # ------------------------------------------------------------------ #
    def _collect_loop(self) -> None:
        """Przeciąga kolejno wszystkie kulki do (aktualnej) pozycji DROP."""
        cfg = self.config
        start_time = time.monotonic()
        empty_count = 0

        while not self._stop:
            if time.monotonic() - start_time > cfg.max_seconds_per_game_screen:
                logger.warning("[%s] przekroczono max czas ekranu gry - wracam do idle", cfg.name)
                return

            frame = self.adb.screenshot()

            if self._check_foreign_app_watchdog():
                logger.info("[%s] restart gry przerwał zbieranie - wracam do idle", cfg.name)
                return

            if self._check_black_screen_watchdog(frame):
                # gra została zrestartowana w trakcie zbierania kulek -
                # wracamy do idle, żeby od nowa poczekać na ekran gry
                logger.info("[%s] restart gry przerwał zbieranie - wracam do idle", cfg.name)
                return

            det = vision.detect(frame, region=cfg.board_region)

            if self._on_frame is not None:
                self._on_frame(cfg.name, frame, det, "collecting")

            if det.drop is None or len(det.balls) == 0:
                empty_count += 1
                if empty_count >= cfg.empty_detections_to_exit:
                    logger.info("[%s] ekran gry zakończony (kulki zebrane)", cfg.name)
                    return
                time.sleep(0.3)
                continue

            empty_count = 0

            if self._paused:
                time.sleep(0.3)
                continue

            # Bierzemy pierwszą wykrytą kulkę (kolejność nieistotna
            # dla wyniku, gra i tak liczy każdą tak samo).
            ball = det.balls[0]
            drop = det.drop

            logger.debug("[%s] przeciągam kulkę (%d,%d) -> DROP (%d,%d)",
                         cfg.name, ball.x, ball.y, drop.x, drop.y)

            self.adb.swipe(ball.x, ball.y, drop.x, drop.y, duration_ms=int(cfg.drag_duration_ms.sample()))
            self.stats.balls_collected += 1

            self._wait_ball_gone(ball, timeout=cfg.between_drags_delay.sample())

    # ------------------------------------------------------------------ #
    def _wait_ball_gone(self, ball: vision.Circle, timeout: float) -> None:
        """
        Czeka, aż kulka przeciągnięta przez bota zniknie z planszy, zamiast
        stałego sleepa. Dzięki temu nie przeciągamy tej samej kulki drugi raz
        i nie tracimy czasu, gdy gra reaguje szybciej. Po `timeout` wraca
        bez względu na wynik - pętla zbierania sama zdecyduje, co dalej.
        """
        cfg = self.config
        deadline = time.monotonic() + timeout
        while not self._stop and time.monotonic() < deadline:
            time.sleep(0.15)
            frame = self.adb.screenshot()
            det = vision.detect(frame, region=cfg.board_region)
            if self._on_frame is not None:
                self._on_frame(cfg.name, frame, det, "collecting")
            if not any(abs(b.x - ball.x) <= ball.r and abs(b.y - ball.y) <= ball.r for b in det.balls):
                return
