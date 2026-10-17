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
    `status_changed`.

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
import time

import numpy as np
from PySide6.QtCore import QThread, Signal

from adb_controller import ADBController, ADBError
from bot import DustMineBot, BotConfig
import vision

logger = logging.getLogger("gui_worker")


class QtLogHandler(logging.Handler):
    """Handler logowania, który przekierowuje rekordy do sygnału Qt danego workera."""

    def __init__(self, emit_fn):
        super().__init__()
        self._emit_fn = emit_fn
        self.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                                             datefmt="%H:%M:%S"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self._emit_fn(msg)
        except Exception:
            pass


class BotWorker(QThread):
    # (instance_name, frame_bgr, DetectionResult, mode)
    frame_ready = Signal(str, np.ndarray, object, str)
    log_message = Signal(str, str)          # (instance_name, formatted_line)
    status_changed = Signal(str, str)       # (instance_name, status)  status: connecting|idle|collecting|paused|stopped|error
    stats_updated = Signal(str, int, int, int)  # (instance_name, cycles, balls_collected, errors)
    connection_failed = Signal(str, str)    # (instance_name, error_message)

    def __init__(self, config: BotConfig, serial: str, parent=None):
        super().__init__(parent)
        self.config = config
        self.serial = serial
        self._bot: DustMineBot | None = None
        self._adb: ADBController | None = None
        self._log_handler: QtLogHandler | None = None

    # ------------------------------------------------------------------ #
    def run(self) -> None:
        name = self.config.name

        self._log_handler = QtLogHandler(lambda line: self.log_message.emit(name, line))
        bot_logger = logging.getLogger("bot")
        bot_logger.addHandler(self._log_handler)
        bot_logger.setLevel(logging.INFO)

        self.status_changed.emit(name, "connecting")
        try:
            self._adb = ADBController(serial=self.serial)
        except ADBError as e:
            self.connection_failed.emit(name, str(e))
            self.status_changed.emit(name, "error")
            bot_logger.removeHandler(self._log_handler)
            return

        self._bot = DustMineBot(self._adb, self.config, on_frame=self._handle_frame)

        last_stats_emit = 0.0
        self._stats_ticker_running = True

        try:
            self._bot.run()
        except Exception:
            logger.exception("[%s] worker crashed", name)
            self.status_changed.emit(name, "error")
        finally:
            bot_logger.removeHandler(self._log_handler)
            self.status_changed.emit(name, "stopped")

    # ------------------------------------------------------------------ #
    def _handle_frame(self, name: str, frame: np.ndarray,
                       det: "vision.DetectionResult", mode: str) -> None:
        if self._bot is None:
            return

        status = "paused" if self._bot.is_paused else mode
        self.status_changed.emit(name, status)
        self.frame_ready.emit(name, frame, det, mode)

        s = self._bot.stats
        self.stats_updated.emit(name, s.cycles, s.balls_collected, s.errors)

    # ------------------------------------------------------------------ #
    def request_stop(self) -> None:
        if self._bot is not None:
            self._bot.stop()

    def request_pause(self) -> None:
        if self._bot is not None:
            self._bot.pause()
            self.status_changed.emit(self.config.name, "paused")

    def request_resume(self) -> None:
        if self._bot is not None:
            self._bot.resume()

    def request_recording(self, seconds: float, label: str = "manual") -> None:
        if self._bot is not None:
            self._bot.request_recording(seconds, label)

    def request_restart_app(self) -> None:
        """Zgłasza żądanie ręcznego restartu gry (force-stop + start).
        Wykonywane bezpiecznie wewnątrz wątku bota, nie tutaj."""
        if self._bot is not None:
            self._bot.request_restart_app()

    def update_config(self, idle_point: tuple[int, int] | None = None,
                       board_region: tuple[int, int, int, int] | None = None) -> None:
        """Pozwala GUI na żywo zaktualizować idle_point/board_region
        (np. po kliknięciu na podglądzie), bez restartu wątku."""
        if idle_point is not None:
            self.config.idle_point = idle_point
        if board_region is not None:
            self.config.board_region = board_region
