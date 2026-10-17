"""
bot.py
======
Logika jednej instancji bota działającej na jednej instancji LDPlayer.

Cykl działania (szczegóły: docs/minigry.md):
  1. Rozpoznaj ekran szablonami (dustbot2/screens.py) i obsłuż go:
     Profile -> JUST LOOT, koniec walki -> PROCEED, "?" -> odsłonięcie,
     popup przedmiotu -> Keep Current, Fishing Hole, Depth Collapse.
  2. Na planszy Dust Mine: co pętlę na nowo wykrywaj pozycję DROP (bo się
     przemieszcza) i listę pozostałych kulek, przeciągaj JEDNĄ kulkę do
     DROP, poczekaj, aż zniknie, powtórz aż kulek nie będzie.
  3. Wróć do kroku 1.

Cała logika jest niezależna od danej instancji ADB - dostaje
skonfigurowany ADBController i działa na nim w pętli `run()`,
co pozwala odpalać wiele instancji równolegle (patrz manager.py).
"""

from __future__ import annotations

import time
import math
import random
import logging
import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from adb_controller import ADBController, ADBError
from recorder import MinigameRecorder, UnknownScreenSaver
from dustbot2.touch.humanize import DragProfile, plan_drag, sample_duration, to_commands
from dustbot2.touch.minitouch import MinitouchSession, TouchError
from dustbot2.minigames import fishing as fishing_game
from dustbot2.minigames.depth_collapse import find_tiles
from dustbot2.schedule import DailyPlayTarget, PlayLedger, PlayScheduler
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

    # Ile razy z rzędu detekcja może nie znaleźć kulek/DROP zanim bot
    # uzna, że ekran gry się zakończył i wraca do obsługi innych ekranów.
    empty_detections_to_exit: int = 2

    # Maksymalny czas (s) na "wyczyszczenie" jednego ekranu gry, zanim
    # bot uzna to za błąd i mimo wszystko wróci do trybu idle
    # (zabezpieczenie przed zawieszeniem w pętli).
    max_seconds_per_game_screen: float = 60.0

    # Interwał odświeżania screenshotu w trybie idle (osobny od odstępu
    # tapów JUST LOOT, który bot losuje sam - patrz _just_loot_delay).
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

    # Dzienny plan gry: okno + liczba godzin; bot sam układa sesje i
    # nieregularne przerwy (dustbot2/schedule.py). Jeśli ustawiony, ma
    # pierwszeństwo przed listą okien `schedule`.
    daily_play: DailyPlayTarget | None = None

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

    # --- Zbieranie materiału (nowe minigry) --- #
    # Zapisuj zrzuty ekranów, których bot nie rozpoznaje (np. nowa minigra),
    # do <capture_dir>/Unknown/ - bez duplikatów, najwyżej co 2 s.
    capture_unknown_screens: bool = True
    # Gdy pojawi się nierozpoznany ekran, nagraj wideo + dotyk + klatki przez
    # tyle sekund, bez wykonywania żadnych akcji (0 = wyłączone).
    record_unknown_seconds: float = 0.0
    # Minimalny odstęp (s) między automatycznymi nagraniami.
    record_cooldown: float = 300.0
    # Folder na zrzuty i nagrania (względem katalogu uruchomienia).
    capture_dir: str = "captures"

    # --- Wejście dotykowe --- #
    # "adb"       - `adb shell input` (prosta linia, stała prędkość)
    # "minitouch" - zróżnicowane gesty z dustbot2/touch/humanize.py (łuki, S,
    #               haczyki, "L", rzuty); wymaga roota na emulatorze. Jeśli
    #               minitouch się nie uruchomi albo przestanie działać, bot
    #               sam wraca do "adb".
    touch_backend: str = "adb"

    # --- Minigry --- #
    # Początkowe opóźnienie (ms) od zrzutu ekranu do chwili, w której gra
    # przyjmuje tap. Bot poprawia je po każdym rzucie w Fishing Hole na
    # podstawie miejsca, w którym zatrzymał się wskaźnik.
    # None = 60 ms z minitouch, 150 ms z adb input.
    fishing_latency_ms: float | None = None
    # Rozrzut celowania w strefie (ułamek szerokości strefy; 0 = zawsze środek).
    fishing_aim_spread: float = 0.18


@dataclass
class BotStats:
    cycles: int = 0
    balls_collected: int = 0
    errors: int = 0
    restarts: int = 0
    fishing_casts: int = 0
    fishing_hits: int = 0


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

        # Rozpoznawanie ekranów (szablony w dustbot2/templates) - bez nich
        # bot nie wie, co jest na ekranie, więc błąd wczytania przerywa start.
        from dustbot2.screens import ScreenClassifier
        self._classifier = ScreenClassifier.load()
        self._unknown_saver = UnknownScreenSaver(Path(config.capture_dir) / "Unknown", config.name)
        self._record_request: tuple[str, float] | None = None
        self._last_auto_record: float = float("-inf")

        self._scheduler: PlayScheduler | None = None
        if config.daily_play is not None:
            ledger = PlayLedger(Path(config.capture_dir) / "play_time.json", config.name)
            self._scheduler = PlayScheduler(config.daily_play, config.name, ledger)
        self._last_play_tick: float | None = None
        self._last_ledger_save = 0.0

        self._fishing_latency: fishing_game.LatencyCalibrator | None = None
        self.last_fishing: dict | None = None  # wynik ostatniego rzutu (log / testy)
        self._region: tuple[int, int, int, int] | None = None

        self._touch_session: MinitouchSession | None = None
        self._touch = None  # MinitouchClient, gdy działa minitouch
        self._drag_rng = random.Random()

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

    def request_recording(self, seconds: float, label: str = "manual") -> None:
        """
        Zgłasza nagranie (wideo + dotyk + klatki) na `seconds` sekund. Wykona
        się w wątku bota przy najbliższym przejściu pętli; w trakcie bot nie
        klika ani nie przeciąga, więc można grać ręcznie.
        """
        self._record_request = (label, seconds)

    def _record(self, label: str, seconds: float) -> None:
        cfg = self.config
        recorder = MinigameRecorder(self.adb, Path(cfg.capture_dir) / "recordings", cfg.name)
        empty = vision.DetectionResult(drop=None, balls=[])

        def preview(frame: np.ndarray) -> None:
            if self._on_frame is not None:
                self._on_frame(cfg.name, frame, empty, "recording")

        recorder.record(label, seconds, should_stop=lambda: self._stop, on_frame=preview)

    def _handle_capture(self, frame: np.ndarray, state: str | None = None) -> bool:
        """
        Obsługuje nagrania: zgłoszone ręcznie oraz automatyczne przy
        nierozpoznanym ekranie. Zwraca True, jeśli coś nagrano (wtedy
        bieżąca klatka jest nieaktualna). `state` - już rozpoznany ekran,
        żeby nie klasyfikować klatki drugi raz.
        """
        cfg = self.config
        if self._record_request is not None:
            label, seconds = self._record_request
            self._record_request = None
            self._record(label, seconds)
            return True

        if not (cfg.capture_unknown_screens or cfg.record_unknown_seconds > 0):
            return False
        from dustbot2.screens import UNKNOWN
        if state is None:
            state = self._classifier.classify(frame).state
        if state != UNKNOWN:
            return False

        if cfg.capture_unknown_screens:
            saved = self._unknown_saver.offer(frame)
            if saved is not None:
                logger.info("[%s] nieznany ekran zapisany: %s", cfg.name, saved)
        now = time.monotonic()
        if cfg.record_unknown_seconds > 0 and now - self._last_auto_record >= cfg.record_cooldown:
            self._last_auto_record = now
            logger.info("[%s] nieznany ekran - nagrywam %.0f s bez wykonywania akcji",
                        cfg.name, cfg.record_unknown_seconds)
            self._record("unknown", cfg.record_unknown_seconds)
            return True
        return False

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
        if now is None:
            now = dt.datetime.now()
        if self._scheduler is not None:
            return self._scheduler.is_play_time(now)
        if not cfg.schedule:
            return True
        return any(window.contains(now) for window in cfg.schedule)

    def _tick_play_time(self) -> None:
        """Dolicza czas gry do dziennego licznika (gdy działa plan dzienny)."""
        if self._scheduler is None:
            return
        now = time.monotonic()
        if self._last_play_tick is not None and not self._paused:
            self._scheduler.record_play(min(now - self._last_play_tick, 60.0))
        self._last_play_tick = now
        if now - self._last_ledger_save > 60:
            self._scheduler.ledger.save()
            self._last_ledger_save = now

    def _break_message(self) -> str:
        if self._scheduler is None:
            return "poza godzinami harmonogramu - czekam na najbliższe okno pracy"
        now = dt.datetime.now()
        upcoming = self._scheduler.next_session(now)
        sessions = self._scheduler.sessions(now)
        planned = sum(x.minutes for x in sessions) / 60
        if upcoming is None:
            return "dzienny limit gry osiągnięty - czekam na następne okno"
        return (f"przerwa do {upcoming.start:%H:%M}, następna sesja {upcoming.minutes:.0f} min "
                f"(plan okna: {len(sessions)} sesji, {planned:.1f} h)")

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

        logger.info("[%s] %s", cfg.name, self._break_message())
        self._tick_play_time()
        self._last_play_tick = None  # przerwa nie liczy się do czasu gry
        if self._scheduler is not None:
            self._scheduler.ledger.save()

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
                logger.info("[%s] %s", cfg.name, self._break_message())
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
    # ------------------------------------------------------------------ #
    # Wejście dotykowe: minitouch z powrotem do ADB
    # ------------------------------------------------------------------ #
    def _start_touch(self) -> None:
        cfg = self.config
        if cfg.touch_backend != "minitouch":
            return
        session = MinitouchSession(self.adb)
        try:
            self._touch = session.start()
            self._touch_session = session
            logger.info("[%s] wejście: minitouch (zróżnicowane gesty)", cfg.name)
        except (TouchError, ADBError) as e:
            logger.warning("[%s] minitouch niedostępny, używam adb input: %s", cfg.name, e)
            session.stop()

    def _stop_touch(self) -> None:
        if self._touch_session is not None:
            self._touch_session.stop()
        self._touch_session = None
        self._touch = None

    def _touch_failed(self, error: Exception) -> None:
        logger.warning("[%s] minitouch przestał działać, przełączam na adb input: %s",
                       self.config.name, error)
        self._stop_touch()

    def _tap(self, x: int, y: int) -> None:
        if self._touch is not None:
            hold = random.uniform(45, 120)
            try:
                self._touch.tap(x, y, hold_ms=hold, pressure=random.randint(40, 70))
                time.sleep(hold / 1000)
                return
            except TouchError as e:
                self._touch_failed(e)
        self.adb.tap(x, y)

    def _drag(self, start: tuple[int, int], end: tuple[int, int]) -> None:
        cfg = self.config
        if self._touch is not None:
            plan = plan_drag(start, end, rng=self._drag_rng, bounds=self._region)
            try:
                self._touch.send(to_commands(plan.steps, self._touch))
                # pauzy wykonuje minitouch na urządzeniu - czekamy, aż gest się skończy
                time.sleep(plan.duration_ms / 1000)
                logger.debug("[%s] gest %s: %.0f ms, %d punktów",
                             cfg.name, plan.shape, plan.duration_ms, len(plan.steps))
                return
            except TouchError as e:
                self._touch_failed(e)
        # adb input swipe rysuje prostą linię - czas bierzemy z tego samego
        # rozkładu co gesty minitouch
        duration = sample_duration(DragProfile(), self._drag_rng)
        self.adb.swipe(start[0], start[1], end[0], end[1], duration_ms=int(duration))

    def run(self) -> None:
        logger.info("[%s] start bota", self.config.name)
        self._start_touch()
        try:
            self._run_loop()
        finally:
            self._stop_touch()
            self._tick_play_time()
            if self._scheduler is not None:
                self._scheduler.ledger.save()

    def _run_loop(self) -> None:
        while not self._stop:
            try:
                self._wait_for_schedule()
                if self._stop:
                    break
                if not self._idle_loop() or self._stop:
                    continue
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

        # Sprawdzamy jasność TYLKO w obszarze planszy, a nie całej
        # klatki. Cała klatka zwykle zawiera też
        # pasek statusu / nawigację systemową Androida, które mogą być
        # jasne nawet gdy sama plansza gry jest czarna (zawieszona),
        # co zaniżałoby wykrywalność problemu.
        x1, y1, x2, y2 = self._board_region(frame)
        region_frame = frame[y1:y2, x1:x2]

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
    def _idle_loop(self) -> bool:
        """
        Czeka na planszę Dust Mine, po drodze obsługując rozpoznane ekrany:
        Profile -> JUST LOOT, koniec walki -> PROCEED, "?" -> odsłonięcie,
        popup przedmiotu -> Keep Current, Fishing Hole i Depth Collapse.
        Na nierozpoznanych ekranach (walka, ładowanie) nic nie klika.
        """
        cfg = self.config
        last_click = 0.0
        next_click_delay = self._just_loot_delay()
        while not self._stop:
            self._tick_play_time()
            if not self._is_within_schedule():
                return False  # pora na przerwę - _run_loop przejdzie do oczekiwania

            frame = self.adb.screenshot()

            if self._check_foreign_app_watchdog():
                last_click = 0.0
                continue

            if self._check_black_screen_watchdog(frame):
                # gra została zrestartowana - zacznij tę pętlę od nowa
                # ze świeżym screenshotem zamiast działać na starej klatce
                last_click = 0.0
                continue

            state = self._classifier.classify(frame).state

            if self._handle_capture(frame, state):
                last_click = 0.0
                continue

            if state == "dust_mine":
                det = self._detect_board(frame)
            else:
                det = vision.DetectionResult(drop=None, balls=[])
            if self._on_frame is not None:
                self._on_frame(cfg.name, frame, det, "idle")

            if state == "dust_mine":
                logger.info("[%s] wykryto ekran gry: DROP=%s, kulki=%d",
                            cfg.name, det.drop.center if det.drop else None, len(det.balls))
                return True

            if not self._paused and self._handle_screen(frame, state):
                last_click = 0.0
                continue

            now = time.monotonic()
            if not self._paused and state == "home" and now - last_click >= next_click_delay:
                point = self._just_loot_point(frame)
                if point is not None:
                    self._tap(*point)
                    last_click = now
                    next_click_delay = self._just_loot_delay()

            time.sleep(cfg.idle_poll_interval)
        return False

    def _just_loot_delay(self) -> float:
        """
        Odstęp (s) do następnego tapu JUST LOOT: zwykle ok. 0.6-2 s
        (log-normalnie), a co jakiś czas dłuższa pauza 2.5-7 s - jak u
        człowieka, który na chwilę odwraca uwagę.
        """
        if self._drag_rng.random() < 0.08:
            return self._drag_rng.uniform(2.5, 7.0)
        # losujemy ponownie zamiast przycinać - przycinanie zbierałoby część
        # tapów dokładnie na granicach, co byłoby łatwym do wykrycia wzorcem
        while True:
            delay = self._drag_rng.lognormvariate(math.log(1.05), 0.3)
            if 0.6 <= delay <= 2.0:
                return delay

    def _just_loot_point(self, frame: np.ndarray) -> tuple[int, int] | None:
        """Losowy punkt wokół środka przycisku JUST LOOT albo None, gdy go nie widać."""
        match = self._classifier.locate(frame, "just_loot")
        if match is None:
            return None
        return self._random_point_near((match.x, match.y), 0.03 * frame.shape[1])

    # ------------------------------------------------------------------ #
    # Ekrany między walkami
    # ------------------------------------------------------------------ #
    # Przyciski szukane szablonem; pierwszy znaleziony wygrywa.
    BUTTONS = {
        "combat_done": ("proceed",),
        "item_popup": ("keep_current_v2", "keep_current"),
    }

    def _handle_screen(self, frame: np.ndarray, state: str) -> bool:
        """Akcja dla rozpoznanego ekranu. True, jeśli coś zrobiono."""
        cfg = self.config
        width, height = frame.shape[1], frame.shape[0]
        if state == "fishing":
            self._play_fishing()
            return True
        if state == "depth_collapse":
            self._play_depth_collapse()
            return True
        if state == "reveal":
            # "?" - TAP ANYWHERE TO REVEAL: tap w kartę na środku
            self._tap(*self._random_point_near((width // 2, int(0.47 * height)), 0.06 * width))
            logger.info("[%s] odsłaniam nagrodę", cfg.name)
            time.sleep(random.uniform(0.4, 0.9))
            return True
        for name in self.BUTTONS.get(state, ()):
            match = self._classifier.locate(frame, name)
            if match is not None:
                self._tap(*self._random_point_near((match.x, match.y), min(match.w, match.h) / 3))
                logger.info("[%s] %s -> %s", cfg.name, state, name)
                time.sleep(random.uniform(0.4, 0.9))
                return True
        return False

    def _play_depth_collapse(self) -> None:
        """Klika wszystkie kafelki, aż znikną (timer gry ok. 5.5 s)."""
        cfg = self.config
        deadline = time.monotonic() + 10.0
        taps = 0
        while not self._stop and time.monotonic() < deadline:
            frame = self.adb.screenshot()
            if self._classifier.classify(frame).state != "depth_collapse":
                break
            if self._on_frame is not None:
                self._on_frame(cfg.name, frame, vision.DetectionResult(drop=None, balls=[]), "minigame")
            tiles = find_tiles(frame)
            if self._paused or not tiles:
                time.sleep(0.15)
                continue
            for tile in tiles:
                self._tap(*self._random_point_near((tile.x, tile.y), 0.012 * frame.shape[1]))
                taps += 1
                time.sleep(random.uniform(0.05, 0.14))
        logger.info("[%s] Depth Collapse: %d tapów", cfg.name, taps)

    def _play_fishing(self) -> fishing_game.CastPlan | None:
        """
        Fishing Hole: obserwuje wskaźnik, oblicza moment, w którym będzie w
        strefie, i naciska CAST z wyprzedzeniem równym opóźnieniu. Po rzucie
        mierzy, gdzie wskaźnik stanął, i poprawia opóźnienie.
        """
        cfg = self.config
        if self._fishing_latency is None:
            initial = cfg.fishing_latency_ms
            if initial is None:
                initial = 60.0 if self._touch is not None else 150.0
            self._fishing_latency = fishing_game.LatencyCalibrator(latency_ms=initial)
        calibrator = self._fishing_latency
        tracker = fishing_game.FishingTracker()
        aim = fishing_game.AimProfile(spread=cfg.fishing_aim_spread)
        empty = vision.DetectionResult(drop=None, balls=[])
        cast_point: tuple[int, int] | None = None
        plan: fishing_game.CastPlan | None = None
        after_cast: list[tuple[float, float]] = []
        stopped_x: float | None = None
        frame_ms = 200.0  # średni czas zrzutu, aktualizowany na bieżąco
        deadline = time.monotonic() + 12.0

        while not self._stop and time.monotonic() < deadline:
            before = time.monotonic() * 1000
            frame = self.adb.screenshot()
            after = time.monotonic() * 1000
            frame_ms = 0.7 * frame_ms + 0.3 * (after - before)
            t_frame = (before + after) / 2  # obraz powstał gdzieś w trakcie zrzutu
            det = fishing_game.detect(frame)
            if self._on_frame is not None:
                self._on_frame(cfg.name, frame, empty, "fishing")
            if det is None:
                break  # ekran się zmienił (np. po rzucie)

            if plan is not None:
                after_cast.append((t_frame, det.indicator_x))
                if det.stopped:
                    stopped_x = det.indicator_x
                else:
                    stopped_x = fishing_game.stopped_position(after_cast)
                if stopped_x is not None or len(after_cast) >= 6:
                    break
                continue

            if cast_point is None:
                match = self._classifier.locate(frame, "cast_button")
                cast_point = ((match.x, match.y) if match is not None
                              else (frame.shape[1] // 2, int(det.indicator_y + 0.23 * frame.shape[1])))
            tracker.frame_width = frame.shape[1]
            tracker.update(t_frame, det)
            if self._paused:
                continue

            candidate = fishing_game.plan_cast(tracker, after, calibrator.latency_ms, aim, self._drag_rng)
            if candidate is None:
                continue
            wait = candidate.tap_at_ms - time.monotonic() * 1000
            if wait > 1.5 * frame_ms:
                continue  # zdążymy zrobić jeszcze zrzut i doprecyzować plan
            if wait > 0:
                time.sleep(wait / 1000)
            self._tap(*self._random_point_near(cast_point, 0.012 * frame.shape[1]))
            plan = candidate

        if plan is None:
            logger.warning("[%s] fishing: nie udało się zaplanować rzutu", cfg.name)
            return None

        self.stats.fishing_casts += 1
        result = {"plan": plan, "stopped_x": stopped_x, "zone": tracker.zone,
                  "speed": tracker.speed, "latency_before": calibrator.latency_ms}
        if stopped_x is not None and tracker.speed:
            zone = tracker.zone
            hit = zone is not None and zone[0] <= stopped_x <= zone[1]
            self.stats.fishing_hits += int(hit)
            error = calibrator.update(plan, stopped_x, tracker.speed)
            result.update(hit=hit, error_ms=error)
            logger.info("[%s] fishing: %s - cel %.0f px, stanął %.0f px, strefa %s, błąd %+.0f ms, "
                        "opóźnienie teraz %.0f ms (trafienia %d/%d)",
                        cfg.name, "TRAFIONY" if hit else "pudło", plan.target_x, stopped_x,
                        tuple(round(v) for v in zone) if zone else None, error,
                        calibrator.latency_ms, self.stats.fishing_hits, self.stats.fishing_casts)
        else:
            logger.info("[%s] fishing: rzut wysłany, nie udało się zmierzyć, gdzie stanął wskaźnik", cfg.name)
        result["latency_after"] = calibrator.latency_ms
        self.last_fishing = result
        return plan

    # ------------------------------------------------------------------ #
    # Plansza Dust Mine
    # ------------------------------------------------------------------ #
    AUTO_BOARD = (0.02, 0.18, 0.98, 0.98)  # ułamki ekranu
    BALL_WAIT_SECONDS = (1.0, 1.6)  # maks. czekanie na zniknięcie przeciągniętej kulki

    def _board_region(self, frame: np.ndarray) -> tuple[int, int, int, int]:
        """Obszar planszy liczony z rozmiaru ekranu - działa w każdej rozdzielczości."""
        height, width = frame.shape[:2]
        fx1, fy1, fx2, fy2 = self.AUTO_BOARD
        return int(fx1 * width), int(fy1 * height), int(fx2 * width), int(fy2 * height)

    def _detect_board(self, frame: np.ndarray) -> vision.DetectionResult:
        """
        Kulki z vision.detect, DROP z szablonu (działa w każdej
        rozdzielczości; stara heurystyka DROP zawodzi przy wyższym DPI).
        """
        self._region = self._board_region(frame)
        det = vision.detect(frame, region=self._region)
        drop = None
        for name in ("drop_text_v2", "drop_text"):
            match = self._classifier.locate(frame, name)
            if match is not None and (drop is None or match.score > drop.score):
                drop = match
        if drop is None:
            return det
        radius = int(round(0.065 * frame.shape[1]))  # otwór DROP: ok. 70 px przy 1080
        balls = [b for b in det.balls if np.hypot(b.x - drop.x, b.y - drop.y) > radius]
        return vision.DetectionResult(drop=vision.Circle(drop.x, drop.y, radius), balls=balls)

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

            if self._record_request is not None:
                label, seconds = self._record_request
                self._record_request = None
                self._record(label, seconds)

            self._tick_play_time()
            frame = self.adb.screenshot()

            if self._check_foreign_app_watchdog():
                logger.info("[%s] restart gry przerwał zbieranie - wracam do idle", cfg.name)
                return

            if self._check_black_screen_watchdog(frame):
                # gra została zrestartowana w trakcie zbierania kulek -
                # wracamy do idle, żeby od nowa poczekać na ekran gry
                logger.info("[%s] restart gry przerwał zbieranie - wracam do idle", cfg.name)
                return

            det = self._detect_board(frame)

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

            self._drag((ball.x, ball.y), (drop.x, drop.y))
            self.stats.balls_collected += 1

            self._wait_ball_gone(ball, timeout=random.uniform(*self.BALL_WAIT_SECONDS))

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
            det = self._detect_board(frame)
            if self._on_frame is not None:
                self._on_frame(cfg.name, frame, det, "collecting")
            if not any(abs(b.x - ball.x) <= ball.r and abs(b.y - ball.y) <= ball.r for b in det.balls):
                return
