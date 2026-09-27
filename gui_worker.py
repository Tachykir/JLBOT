"""
gui_worker.py
=============
Most między logiką bota (bot.py, działającą synchronicznie w pętli
`run()`) a GUI Qt. Każda instancja LDPlayer dostaje własny `BotWorker`
(QThread), który:

  - tworzy ADBController i DustMineBot dla swojego serial/configu,
  - uruchamia bota w tle (osobny wątek, żeby nie blokować GUI),
  - przekazuje każdą sklatkowaną detekcję (screenshot + wynik OpenCV)
    do GUI przez sygnał `frame_ready` - GUI używa tego do podglądu
    na żywo i nakładki z wykrytymi kulkami/DROP,
  - przekazuje logi przez sygnał `log_message`,
  - przekazuje zmiany stanu (idle/collecting/stopped/error) przez
    `status_changed` (tylko przy faktycznej zmianie).

Wątki (QThread) zamiast osobnych procesów (jak w manager.py) są tu
celowe - dzięki temu GUI może bezpośrednio, bez IPC, rysować podgląd
z każdej instancji i reagować na klik myszką (ustawianie idle_point /
board_region) w czasie rzeczywistym. Cena: cały bot dzieli jeden
proces Python z GUI, ale ponieważ dominujący koszt (OpenCV, ADB I/O)
zwalnia GIL w trakcie wywołań C/subprocess, wielo-instancyjność nadal
skaluje się nieźle. Jeśli w praktyce GIL zacznie ograniczać przy 6-8+
instancjach, worker można łatwo przenieść do QProcess bez zmiany API
sygnałów.
"""

from __future__ import annotations

import logging
import threading

import numpy as np
from PySide6.QtCore import QThread, Signal

from adb_controller import ADBController, ADBError
from bot import DustMineBot, BotConfig, Decision
import vision

logger = logging.getLogger("gui_worker")


class QtLogHandler(logging.Handler):
    """
    Handler logowania, który przekierowuje rekordy do sygnału Qt danego
    workera - ale TYLKO rekordy zalogowane z wątku tego workera.

    Wszystkie instancje bota logują przez te same loggery ("bot", "adb"),
    więc bez filtra po wątku każdy worker łapałby logi wszystkich
    instancji i każda linia pojawiałaby się w GUI N razy, przypisana
    do różnych instancji.
    """

    def __init__(self, emit_fn, thread_id: int):
        super().__init__()
        self._emit_fn = emit_fn
        self._thread_id = thread_id
        self.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                             datefmt="%H:%M:%S"))

    def filter(self, record: logging.LogRecord) -> bool:
        return record.thread == self._thread_id and super().filter(record)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._emit_fn(self.format(record))
        except Exception:
            pass


# loggery, z których logi trafiają do panelu logów GUI
_FORWARDED_LOGGERS = ("bot", "adb")


class BotWorker(QThread):
    # (instance_name, frame_bgr, DetectionResult, mode)
    frame_ready = Signal(str, np.ndarray, object, str)
    log_message = Signal(str, str)          # (instance_name, formatted_line)
    status_changed = Signal(str, str)       # (instance_name, status)  status: connecting|idle|collecting|debris|fishing|paused|break|offline|stopped|error
    schedule_changed = Signal(str, str)     # (instance_name, opis stanu harmonogramu, np. "przerwa do 10:19")
    stats_updated = Signal(str, object)     # (instance_name, BotStats)
    activity_changed = Signal(str, str)     # (instance_name, dokładny opis bieżącej czynności)
    connection_failed = Signal(str, str)    # (instance_name, error_message)

    def __init__(self, config: BotConfig, serial: str, parent=None):
        super().__init__(parent)
        self.config = config
        self.serial = serial
        # nazwa zapamiętana przy starcie - GUI nie pozwala zmienić nazwy
        # działającej instancji, a sygnały muszą trafiać pod stały klucz
        self.name = config.name
        self.scale = 1.0  # piksele urządzenia na piksel klatki (pomniejszanie zrzutów)
        self._bot: DustMineBot | None = None
        self._stop_requested = threading.Event()
        self._last_status: str | None = None

    # ------------------------------------------------------------------ #
    def _set_status(self, status: str) -> None:
        if status != self._last_status:
            self._last_status = status
            self.status_changed.emit(self.name, status)

    def run(self) -> None:
        name = self.name

        handler = QtLogHandler(lambda line: self.log_message.emit(name, line), threading.get_ident())
        loggers = [logging.getLogger(n) for n in _FORWARDED_LOGGERS]
        for lg in loggers:
            lg.addHandler(handler)
            if lg.level == logging.NOTSET or lg.level > logging.INFO:
                lg.setLevel(logging.INFO)

        try:
            self._set_status("connecting")
            try:
                adb = ADBController(serial=self.serial)
            except ADBError as e:
                self.connection_failed.emit(name, str(e))
                self._set_status("error")
                return

            bot = DustMineBot(adb, self.config, on_frame=self._handle_frame,
                              on_schedule=self._handle_schedule,
                              on_activity=lambda _n, text: self.activity_changed.emit(self.name, text))
            self._bot = bot
            # Stop mógł zostać kliknięty w trakcie łączenia (zanim bot istniał).
            if self._stop_requested.is_set():
                bot.stop()

            try:
                bot.run()
            except Exception:
                logging.getLogger("bot").exception("[%s] worker crashed", name)
                self._set_status("error")
                return
            self._set_status("stopped")
        finally:
            for lg in loggers:
                lg.removeHandler(handler)

    # ------------------------------------------------------------------ #
    def _handle_frame(self, name: str, frame: np.ndarray,
                       det: "vision.DetectionResult", mode: str) -> None:
        if self._bot is None:
            return

        self._set_status("paused" if self._bot.is_paused else mode)
        self.scale = getattr(self._bot.adb, "scale", 1.0)
        self.frame_ready.emit(self.name, frame, det, mode)
        self.stats_updated.emit(self.name, self._bot.stats)

    def _handle_schedule(self, name: str, decision: Decision) -> None:
        if decision.play:
            text = decision.describe() if decision.reason == "session" else ""
        else:
            self._set_status("break" if decision.reason == "break" else "offline")
            text = decision.describe()
        self.schedule_changed.emit(self.name, text)

    # ------------------------------------------------------------------ #
    def request_stop(self) -> None:
        self._stop_requested.set()
        if self._bot is not None:
            self._bot.stop()

    def request_pause(self) -> None:
        if self._bot is not None:
            self._bot.pause()
            self._set_status("paused")

    def request_resume(self) -> None:
        if self._bot is not None:
            self._bot.resume()

    def request_salvage(self, count: int | None = None) -> None:
        """Zgłasza wymuszone rozebranie przedmiotów (wykonywane w wątku bota)."""
        if self._bot is not None:
            self._bot.request_salvage(count)

    def request_packs(self, count: int) -> None:
        """Zgłasza otwarcie `count` paczek (wykonywane w wątku bota)."""
        if self._bot is not None:
            self._bot.request_packs(count)

    def request_restart_app(self) -> None:
        """Zgłasza żądanie ręcznego restartu gry (force-stop + start).
        Wykonywane bezpiecznie wewnątrz wątku bota, nie tutaj."""
        if self._bot is not None:
            self._bot.request_restart_app()


class SnapshotWorker(QThread):
    """
    Jednorazowy zrzut ekranu + detekcja w tle (przycisk "Odśwież teraz"),
    żeby połączenie ADB i screenshot (do kilku-kilkunastu sekund przy
    problemach) nie zamrażały okna GUI.
    """
    done = Signal(str, np.ndarray, object)   # (instance_name, frame, DetectionResult)
    failed = Signal(str, str)                # (instance_name, error_message)

    def __init__(self, name: str, serial: str,
                 board_region: tuple[int, int, int, int] | None, parent=None):
        super().__init__(parent)
        self.name = name
        self.serial = serial
        self.board_region = board_region

    def run(self) -> None:
        try:
            adb = ADBController(serial=self.serial)
            frame = adb.screenshot()
            det = vision.detect_all(frame, region=self.board_region)
        except Exception as e:
            self.failed.emit(self.name, str(e))
            return
        self.done.emit(self.name, frame, det)
