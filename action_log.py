"""
Log akcji bota do CSV - każde tapnięcie, swipe, przeciągnięcie i
przytrzymanie z czasem, ekranem i torem. Służy do zestawienia tego, co bot
faktycznie zrobił, z danymi z innych źródeł (np. telemetrii gry).

Jeden plik na instancję i dzień: action_logs/RRRR-MM-DD_<instancja>.csv
(folder obok programu). Każdy wiersz jest od razu zapisywany na dysk.
"""
from __future__ import annotations

import csv
import datetime as dt
import logging
import re
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

FIELDS = [
    "time",          # czas lokalny ISO z milisekundami
    "t_ms",          # czas monotoniczny (ms) - do liczenia odstępów
    "instance",
    "state",         # idle | collecting | debris | fishing | menu | ...
    "activity",      # opis bieżącej czynności bota (jak w GUI)
    "action",        # tap | swipe | drag | hold
    "x", "y",        # początek (piksele urządzenia)
    "end_x", "end_y",
    "points",        # liczba punktów toru
    "path",          # tor "x:y|x:y|..." (piksele urządzenia)
    "duration_ms",
    "hold_ms",       # przytrzymanie (tap / koniec przeciągnięcia)
    "turbo",
    "drag_style",
]


def action_logs_dir() -> Path:
    """Folder na logi akcji - obok programu (obok .exe w wersji zbudowanej)."""
    base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    return base / "action_logs"


def _safe(name: str) -> str:
    return re.sub(r"[^\w.-]+", "_", name).strip("_") or "instancja"


def log_path(instance: str, day: dt.date | None = None, folder: Path | None = None) -> Path:
    day = day or dt.date.today()
    return (folder or action_logs_dir()) / f"{day.isoformat()}_{_safe(instance)}.csv"


class ActionLog:
    """Zapis wierszy CSV dla jednej instancji; plik zmienia się o północy."""

    def __init__(self, instance: str, folder: Path | None = None):
        self.instance = instance
        self.folder = folder or action_logs_dir()
        self._lock = threading.Lock()
        self._day: dt.date | None = None
        self._file = None
        self._writer = None
        self.rows = 0
        self.failed = False

    def _open(self, day: dt.date) -> None:
        self.close()
        self.folder.mkdir(parents=True, exist_ok=True)
        path = log_path(self.instance, day, self.folder)
        new = not path.exists() or path.stat().st_size == 0
        self._file = open(path, "a", newline="", encoding="utf-8")
        self._writer = csv.DictWriter(self._file, fieldnames=FIELDS, extrasaction="ignore")
        if new:
            self._writer.writeheader()
        self._day = day

    def write(self, row: dict) -> None:
        if self.failed:
            return
        now = dt.datetime.now()
        row = dict(row)
        row.setdefault("time", now.isoformat(timespec="milliseconds"))
        row.setdefault("t_ms", int(time.monotonic() * 1000))
        row.setdefault("instance", self.instance)
        with self._lock:
            try:
                if self._day != now.date():
                    self._open(now.date())
                self._writer.writerow(row)
                self._file.flush()
                self.rows += 1
            except OSError as e:
                # dysk pełny / brak uprawnień - nie przerywaj bota, tylko przestań logować
                self.failed = True
                logger.warning("[%s] nie udało się zapisać logu akcji (%s) - log wyłączony", self.instance, e)

    def close(self) -> None:
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
        self._file = None
        self._writer = None


def format_path(points, scale: float = 1.0) -> str:
    return "|".join(f"{int(round(x * scale))}:{int(round(y * scale))}" for x, y in points)


def read_tail(path: Path, limit: int = 300) -> list[dict]:
    """Ostatnie `limit` wierszy pliku (do podglądu w GUI)."""
    if not path.exists():
        return []
    try:
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except (OSError, csv.Error):
        return []
    return rows[-limit:]
