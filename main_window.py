"""
main_window.py
==============
Główne okno aplikacji Dust Mine Bot GUI.

Layout:
  +------------------+---------------------------+
  | Lista instancji   |  Podgląd wybranej         |
  | (status, Start/   |  instancji (żywy zrzut +  |
  |  Stop/Pauza)      |  detekcja + edycja klik.)  |
  |                   +---------------------------+
  |                   |  Panel configu instancji   |
  |                   |  (idle_point, region, itd.)|
  +-------------------+---------------------------+
  |              Logi (wszystkie instancje)         |
  +--------------------------------------------------+
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import random
import sys
import time
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt, Slot, QTime, QTimer, QRectF
from PySide6.QtCore import QUrl
import theme
import action_log
from version import APP_VERSION
from PySide6.QtGui import QAction, QColor, QDesktopServices, QKeySequence, QPainter, QPalette
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QFormLayout, QGridLayout,
    QListWidget, QListWidgetItem, QPushButton, QLabel, QLineEdit, QSpinBox,
    QDoubleSpinBox, QPlainTextEdit, QSplitter, QGroupBox, QMessageBox, QFileDialog,
    QTimeEdit, QCheckBox, QDialog, QDialogButtonBox, QScrollArea, QFrame, QTabWidget, QComboBox,
    QInputDialog, QStackedWidget, QSlider, QTableWidget, QTableWidgetItem, QHeaderView, QAbstractItemView,
    QToolTip,
)

from bot import (BotConfig, BotStats, RandomRange, ScheduleWindow, POINT_DEFAULTS, DROP_CHOICES, DEPTH_MODES,
                 SPEED_MIN_PCT, SPEED_MAX_PCT, unknown_screens_dir)
from scheduler import day_timeline, PLAY, BREAK, OFFLINE, LIMIT
import i18n
from i18n import tr
from gui_worker import BotWorker, SnapshotWorker
from preview_widget import PreviewWidget, EditMode
from dashboard import DashboardWidget
from adb_controller import list_ldplayer_devices
import vision

logger = logging.getLogger("gui")


def default_config_path() -> Path:
    """config.json obok aplikacji: obok .exe w wersji zbudowanej
    (PyInstaller), obok main_window.py przy uruchamianiu ze źródeł."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "config.json"
    return Path(__file__).resolve().parent / "config.json"

# kolory czytelne na jasnym i ciemnym tle
STATUS_COLORS = {
    "connecting": "#d9962f",
    "idle": "#3a9fd0",
    "collecting": "#36ad72",
    "debris": "#d9962f",
    "fishing": "#2aa9bf",
    "menu": "#4fa85f",
    "paused": "#8b97a3",
    "break": "#9c7bd0",
    "offline": "#8a7f6a",
    "stopped": "#7d8792",
    "error": "#e0604f",
}

# kolumny tabeli na stronie "Log akcji": (pole CSV, nagłówek)
_ACTION_COLUMNS = [
    ("time", "Czas"), ("state", "Tryb"), ("action", "Akcja"), ("x", "X"), ("y", "Y"),
    ("end_x", "X końca"), ("end_y", "Y końca"), ("points", "Punkty"), ("duration_ms", "Czas [ms]"),
    ("hold_ms", "Przytrz. [ms]"), ("activity", "Czynność"),
]

# Punkty do klikania w zakładce "Punkty": (klucz BotConfig, etykieta, kolor BGR na podglądzie)
_POINTS = [
    ("nav_profile", "Zakładka PROFILE", (255, 200, 0)),
    ("nav_craft", "Zakładka CRAFT", (0, 200, 255)),
    ("nav_inventory", "Zakładka INVENTORY", (255, 255, 0)),
    ("craft_loot_packs_tab", "Zakładka LOOT PACKS (w Craft)", (120, 255, 120)),
    ("card_tap_anywhere", "Karta bez przycisku („tap anywhere”)", (255, 120, 255)),
]

# (pole BotStats, etykieta) - panel statystyk
_STAT_FIELDS = [
    ("cycles", "Plansze"), ("balls_collected", "Kulki"), ("drags", "Przeciągnięcia"),
    ("debris_games", "Depth Collapse"), ("fishing_hits", "Fishing trafione"), ("fishing_casts", "Fishing rzuty"),
    ("fishing_skipped", "Fishing pominięte"), ("defeats", "Porażki"),
    ("pack_rounds", "Rundy paczek"), ("packs_opened", "Otwarte paczki"), ("drops", "Dropy po walce"), ("salvaged", "Rozebrane"),
    ("restarts", "Restarty gry"), ("errors", "Błędy"),
]

STATUS_LABELS_PL = {
    "connecting": "łączenie...",
    "idle": "czekanie",
    "collecting": "zbieranie",
    "debris": "Depth Collapse",
    "fishing": "Fishing Hole",
    "menu": "menu gry",
    "paused": "wstrzymany",
    "break": "przerwa",
    "offline": "offline",
    "stopped": "zatrzymany",
    "error": "błąd",
}


class InstanceEntry:
    """Stan jednej instancji utrzymywany po stronie GUI."""

    def __init__(self, config: BotConfig, serial: str):
        self.config = config
        self.serial = serial
        self.worker: BotWorker | None = None
        self.status: str = "stopped"
        self.last_frame: np.ndarray | None = None
        self.last_det: vision.DetectionResult | None = None
        self.last_stats: BotStats | None = None
        self.last_thumb: float = 0.0
        self.last_preview: float = 0.0
        self.last_scale: float = 1.0   # piksele urządzenia na piksel last_frame
        self.schedule_text: str = ""
        self.activity: str = ""          # dokładny opis tego, co bot robi teraz
        self.list_item: QListWidgetItem | None = None
        self.snapshot_worker: SnapshotWorker | None = None


_DAY_LABELS = ["Pon", "Wt", "Śr", "Czw", "Pt", "Sob", "Nd"]


def _hm(seconds: float) -> str:
    """Czas w formie „6 h 20 min” (albo „40 min”)."""
    minutes = round(seconds / 60)
    h, m = divmod(minutes, 60)
    return f"{h} {tr('h')} {m:02d} {tr('min')}" if h else f"{m} {tr('min')}"


class DayTimelineWidget(QWidget):
    """
    Pasek doby dnia: sesje gry (zielony), przerwy (żółty), poza
    harmonogramem (szary) i wyczerpany limit dzienny (czerwony). Najechanie
    myszą pokazuje godziny i rodzaj odcinka; czerwona kreska to „teraz”.
    """

    COLORS = {PLAY: "#3fa34d", BREAK: "#e0a526", OFFLINE: "#6b6b6b", LIMIT: "#c0504d"}
    LABELS = {PLAY: "gra", BREAK: "przerwa", OFFLINE: "poza harmonogramem", LIMIT: "limit dzienny wyczerpany"}
    MARGIN = 10
    BAR_TOP = 4
    BAR_H = 28

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(62)
        self.setMouseTracking(True)
        self._day: dt.date | None = None
        self._segments: list[tuple[dt.datetime, dt.datetime, str]] = []

    def set_segments(self, day: dt.date, segments) -> None:
        self._day = day
        self._segments = segments
        self.update()

    def _x_at(self, t: dt.datetime) -> float:
        width = self.width() - 2 * self.MARGIN
        minutes = (t - dt.datetime.combine(self._day, dt.time())).total_seconds() / 60
        return self.MARGIN + width * minutes / 1440

    def _segment_at(self, x: float):
        if self._day is None or not self._segments:
            return None
        width = self.width() - 2 * self.MARGIN
        minute = (x - self.MARGIN) / width * 1440
        for s, e, kind in self._segments:
            if s.hour * 60 + s.minute <= minute < (e - dt.datetime.combine(self._day, dt.time())).total_seconds() / 60:
                return s, e, kind
        return None

    def paintEvent(self, event) -> None:
        if self._day is None:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        pal = self.palette()
        p.setPen(Qt.NoPen)
        p.setBrush(pal.color(QPalette.AlternateBase))
        p.drawRoundedRect(QRectF(self.MARGIN, self.BAR_TOP, self.width() - 2 * self.MARGIN, self.BAR_H), 4, 4)
        for s, e, kind in self._segments:
            x1, x2 = self._x_at(s), self._x_at(e)
            p.setBrush(QColor(self.COLORS[kind]))
            p.drawRect(QRectF(x1, self.BAR_TOP, max(1.0, x2 - x1), self.BAR_H))
        if self._day == dt.date.today():
            x = self._x_at(dt.datetime.now())
            p.setBrush(pal.color(QPalette.Text))
            p.drawRect(QRectF(x - 1, self.BAR_TOP - 3, 2, self.BAR_H + 6))
        p.setPen(pal.color(QPalette.Text))
        font = p.font()
        font.setPointSize(max(7, font.pointSize() - 1))
        p.setFont(font)
        for hour in range(0, 25, 3):
            x = self._x_at(dt.datetime.combine(self._day, dt.time()) + dt.timedelta(hours=hour))
            p.drawText(QRectF(x - 15, self.BAR_TOP + self.BAR_H + 4, 30, 16), Qt.AlignCenter, f"{hour:02d}")
        p.end()

    def mouseMoveEvent(self, event) -> None:
        hit = self._segment_at(event.position().x())
        if hit is None:
            QToolTip.hideText()
            return
        s, e, kind = hit
        QToolTip.showText(event.globalPosition().toPoint(),
                          f"{s:%H:%M}–{e:%H:%M}: {tr(self.LABELS[kind])}", self)


class ScheduleEditorDialog(QDialog):
    """
    Edytor harmonogramu pracy instancji - lista okien czasowych, w
    których bot może grać. Każde okno ma własne ustawienia sesji: bot
    gra przez losowy czas, robi losową przerwę i tak do końca okna, a
    granice okna są losowo rozmyte. Do tego opcjonalny limit dzienny.
    Pusta lista okien = bot gra bez ograniczeń godzinowych.
    """

    def __init__(self, windows: list[ScheduleWindow], close_game_during_break: bool = True,
                 daily_limit_hours: RandomRange | None = None, schedule_seed: int = 1, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Harmonogram pracy")
        self.resize(680, 760)
        # kopia robocza - oryginał podmieniamy dopiero po "OK"
        self._windows: list[ScheduleWindow] = list(windows)

        root = QVBoxLayout(self)

        info = QLabel(
            "Bot gra TYLKO w oknach zdefiniowanych poniżej. W każdym oknie sam układa\n"
            "sesje losowej długości z losowymi przerwami.\n"
            "Brak okien (pusta lista) = bot gra bez ograniczeń godzinowych."
        )
        info.setStyleSheet("color: #888; font-size: 11px;")
        root.addWidget(info)

        body = QHBoxLayout()
        root.addLayout(body, stretch=1)

        # --- lewa strona: lista okien --- #
        left = QVBoxLayout()
        body.addLayout(left, stretch=1)

        self.list_widget = QListWidget()
        self.list_widget.setWordWrap(True)
        self.list_widget.currentRowChanged.connect(self._on_row_changed)
        left.addWidget(self.list_widget)

        list_btn_row = QHBoxLayout()
        self.btn_add = QPushButton("+ Dodaj okno")
        self.btn_add.clicked.connect(self._add_window)
        self.btn_remove = QPushButton("- Usuń")
        self.btn_remove.clicked.connect(self._remove_window)
        list_btn_row.addWidget(self.btn_add)
        list_btn_row.addWidget(self.btn_remove)
        left.addLayout(list_btn_row)

        # --- prawa strona: edycja wybranego okna --- #
        right = QVBoxLayout()
        body.addLayout(right, stretch=1)

        form = QFormLayout()
        right.addLayout(form)

        self.field_start = QTimeEdit()
        self.field_start.setDisplayFormat("HH:mm")
        self.field_start.timeChanged.connect(self._apply_current_window)
        form.addRow("Początek:", self.field_start)

        self.field_end = QTimeEdit()
        self.field_end.setDisplayFormat("HH:mm")
        self.field_end.timeChanged.connect(self._apply_current_window)
        form.addRow("Koniec:", self.field_end)

        hint = QLabel("Okno przez północ (np. 22:00 -> 02:00) jest obsługiwane.\n"
                      "Dni tygodnia dotyczą dnia, w którym okno się zaczyna.")
        hint.setStyleSheet("color: #888; font-size: 11px;")
        form.addRow(hint)

        right.addWidget(QLabel("Dni tygodnia (brak zaznaczenia = wszystkie):"))
        days_row = QHBoxLayout()
        self.day_checks: list[QCheckBox] = []
        for label in _DAY_LABELS:
            cb = QCheckBox(label)
            cb.stateChanged.connect(self._apply_current_window)
            days_row.addWidget(cb)
            self.day_checks.append(cb)
        right.addLayout(days_row)

        self.check_sessions = QCheckBox("Losowe sesje i przerwy w tym oknie")
        self.check_sessions.setToolTip("Odznaczone = bot gra przez całe okno bez przerw.")
        self.check_sessions.stateChanged.connect(self._apply_current_window)
        right.addWidget(self.check_sessions)

        session_form = QFormLayout()
        right.addLayout(session_form)
        self.field_session_min, self.field_session_max, session_row = self._minmax_row(5, 720, 5, 0)
        session_form.addRow("Długość sesji:", session_row)
        self.field_break_min, self.field_break_max, break_row = self._minmax_row(1, 720, 5, 0)
        session_form.addRow("Długość przerwy:", break_row)

        self.field_jitter = QSpinBox()
        self.field_jitter.setRange(0, 120)
        self.field_jitter.setSuffix(" min")
        self.field_jitter.setToolTip("Start okna losowo przesuwany o 0..tyle minut później,\n"
                                     "koniec o 0..tyle minut wcześniej - osobno każdego dnia.")
        self.field_jitter.valueChanged.connect(self._apply_current_window)
        session_form.addRow("Rozmycie granic:", self.field_jitter)

        right.addStretch(1)

        self._set_editor_enabled(False)

        # --- ustawienia wspólne dla instancji --- #
        limit_row = QHBoxLayout()
        self.check_daily_limit = QCheckBox("Limit gry dziennie:")
        self.check_daily_limit.setToolTip("Losowany raz na dzień z zakresu. Po wyczerpaniu\n"
                                          "bot jest offline do północy.")
        self.field_limit_min = QDoubleSpinBox(); self.field_limit_min.setRange(0.5, 24.0); self.field_limit_min.setSingleStep(0.5)
        self.field_limit_max = QDoubleSpinBox(); self.field_limit_max.setRange(0.5, 24.0); self.field_limit_max.setSingleStep(0.5)
        limit = daily_limit_hours or RandomRange(5.0, 6.0)
        self.field_limit_min.setValue(limit.min)
        self.field_limit_max.setValue(limit.max)
        self.check_daily_limit.setChecked(daily_limit_hours is not None)
        self.check_daily_limit.toggled.connect(self._update_limit_enabled)
        limit_row.addWidget(self.check_daily_limit)
        self.field_limit_min.setSuffix(" h"); self.field_limit_max.setSuffix(" h")
        limit_row.addWidget(QLabel("od")); limit_row.addWidget(self.field_limit_min)
        limit_row.addWidget(QLabel("do")); limit_row.addWidget(self.field_limit_max)
        limit_row.addStretch(1)
        root.addLayout(limit_row)
        self._update_limit_enabled()

        seed_row = QHBoxLayout()
        self.field_seed = QSpinBox()
        self.field_seed.setRange(0, 999_999)
        self.field_seed.setValue(schedule_seed)
        self.field_seed.setToolTip("Ziarno losowania planu. Ten sam numer = ten sam plan (sesje, przerwy,\n"
                                   "rozmycie granic, limit dzienny). Zmień go, żeby dostać inny układ.")
        self.btn_reroll = QPushButton("🎲 Losuj ponownie")
        self.btn_reroll.setToolTip("Losuje nowe ziarno - nowe sesje i przerwy w tych samych oknach.")
        self.btn_reroll.clicked.connect(self._reroll_seed)
        seed_row.addWidget(QLabel("Ziarno planu:"))
        seed_row.addWidget(self.field_seed)
        seed_row.addWidget(self.btn_reroll)
        seed_row.addStretch(1)
        root.addLayout(seed_row)

        self.check_close_game = QCheckBox("Zamykaj grę (force-stop) na przerwach, poza oknami i po limicie dziennym")
        self.check_close_game.setChecked(close_game_during_break)
        self.check_close_game.setToolTip(
            "Gdy zaznaczone: gra jest zamykana na początku przerwy i\n"
            "uruchamiana ponownie na start kolejnej sesji.\n"
            "Wymaga ustawionego game_package."
        )
        root.addWidget(self.check_close_game)

        root.addWidget(self._heading("Podgląd dnia"))
        day_row = QHBoxLayout()
        self.combo_day = QComboBox()
        today = dt.date.today()
        for i in range(7):
            day = today + dt.timedelta(days=i)
            name = "dziś" if i == 0 else "jutro" if i == 1 else _DAY_LABELS[day.weekday()]
            self.combo_day.addItem(f"{tr(name)}, {day:%d.%m}")
        day_row.addWidget(self.combo_day)
        day_row.addStretch(1)
        root.addLayout(day_row)

        self.timeline = DayTimelineWidget()
        root.addWidget(self.timeline)
        legend = "  ".join(f'<span style="color:{DayTimelineWidget.COLORS[k]}">■</span> {DayTimelineWidget.LABELS[k]}'
                           for k in (PLAY, BREAK, OFFLINE, LIMIT))
        root.addWidget(QLabel(legend))
        self.timeline_stats = QLabel()
        self.timeline_stats.setWordWrap(True)
        root.addWidget(self.timeline_stats)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._refresh_list()
        self._refresh_timeline()
        # sygnały podglądu podpinamy dopiero tu, gdy wszystkie widgety istnieją
        self.field_seed.valueChanged.connect(self._refresh_timeline)
        self.combo_day.currentIndexChanged.connect(self._refresh_timeline)
        self.field_limit_min.valueChanged.connect(self._refresh_timeline)
        self.field_limit_max.valueChanged.connect(self._refresh_timeline)
        self.check_daily_limit.toggled.connect(self._refresh_timeline)
        i18n.translate_tree(self)

    # ------------------------------------------------------------------ #
    def _minmax_row(self, minimum, maximum, step, decimals):
        spin_min = QDoubleSpinBox(); spin_min.setRange(minimum, maximum); spin_min.setSingleStep(step)
        spin_max = QDoubleSpinBox(); spin_max.setRange(minimum, maximum); spin_max.setSingleStep(step)
        spin_min.setDecimals(decimals); spin_max.setDecimals(decimals)
        spin_min.setSuffix(" min"); spin_max.setSuffix(" min")
        spin_min.valueChanged.connect(self._apply_current_window)
        spin_max.valueChanged.connect(self._apply_current_window)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(QLabel("od")); row.addWidget(spin_min, 1)
        row.addWidget(QLabel("do")); row.addWidget(spin_max, 1)
        container = QWidget(); container.setLayout(row)
        return spin_min, spin_max, container

    def _editor_widgets(self):
        return (self.field_start, self.field_end, *self.day_checks, self.check_sessions,
                self.field_session_min, self.field_session_max,
                self.field_break_min, self.field_break_max, self.field_jitter)

    def _set_editor_enabled(self, enabled: bool) -> None:
        for w in self._editor_widgets():
            w.setEnabled(enabled)
        if enabled:
            self._update_session_enabled()

    def _update_session_enabled(self) -> None:
        on = self.check_sessions.isChecked() and self.check_sessions.isEnabled()
        for w in (self.field_session_min, self.field_session_max,
                  self.field_break_min, self.field_break_max):
            w.setEnabled(on)

    def _update_limit_enabled(self) -> None:
        on = self.check_daily_limit.isChecked()
        self.field_limit_min.setEnabled(on)
        self.field_limit_max.setEnabled(on)

    @staticmethod
    def window_label(w: ScheduleWindow) -> str:
        if w.days is None:
            days_str = "codziennie"
        else:
            days_str = ",".join(_DAY_LABELS[d] for d in sorted(w.days))
        label = f"{w.start} - {w.end}  ({days_str})"
        if w.session_minutes is not None:
            s = w.session_minutes
            b = w.break_minutes or RandomRange(0, 0)
            label += f"\n   sesje {s.min:.0f}-{s.max:.0f} min, przerwy {b.min:.0f}-{b.max:.0f} min"
        else:
            label += "\n   całe okno bez przerw"
        if w.jitter_minutes:
            label += f", rozmycie ±{w.jitter_minutes:.0f} min"
        return label

    @staticmethod
    def _heading(text: str) -> QLabel:
        label = QLabel(f"<b>{text}</b>")
        label.setStyleSheet("margin-top: 6px;")
        return label

    def _reroll_seed(self) -> None:
        self.field_seed.setValue((self.field_seed.value() + random.randint(1, 999_998)) % 1_000_000)

    def _refresh_timeline(self, *_) -> None:
        day = dt.date.today() + dt.timedelta(days=self.combo_day.currentIndex())
        segments = day_timeline(self._windows, self.result_daily_limit_hours(), self.field_seed.value(), day)
        self.timeline.set_segments(day, segments)
        self.timeline_stats.setText(self._timeline_stats(segments))

    def _timeline_stats(self, segments) -> str:
        def total(kind: str) -> float:
            return sum((e - s).total_seconds() for s, e, k in segments if k == kind)

        sessions = sum(1 for _s, _e, k in segments if k == PLAY)
        parts = [f"{tr('Sesje')}: {sessions}", f"{tr('gra')} {_hm(total(PLAY))}",
                 f"{tr('przerwy')} {_hm(total(BREAK))}"]
        if total(OFFLINE):
            parts.append(f"{tr('poza oknami')} {_hm(total(OFFLINE))}")
        limit_start = next((s for s, _e, k in segments if k == LIMIT), None)
        if limit_start is not None:
            parts.append(f"{tr('limit dzienny wyczerpany o')} {limit_start:%H:%M}")
        if not self._windows:
            parts.insert(0, tr("Brak okien – bot gra bez ograniczeń godzinowych."))
        return " · ".join(parts)

    def _refresh_list(self, select_row: int | None = None) -> None:
        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        for w in self._windows:
            self.list_widget.addItem(self.window_label(w))
        self.list_widget.blockSignals(False)
        if select_row is not None and 0 <= select_row < self.list_widget.count():
            self.list_widget.setCurrentRow(select_row)
        elif self._windows:
            self.list_widget.setCurrentRow(0)
        else:
            self._on_row_changed(-1)

    def _add_window(self) -> None:
        # domyślne nowe okno: 09:00-13:00, wszystkie dni, z losowymi sesjami
        self._windows.append(ScheduleWindow(
            start="09:00", end="13:00", days=None,
            session_minutes=RandomRange(40, 90), break_minutes=RandomRange(10, 30),
            jitter_minutes=15,
        ))
        self._refresh_list(select_row=len(self._windows) - 1)
        self._refresh_timeline()

    def _remove_window(self) -> None:
        row = self.list_widget.currentRow()
        if row < 0 or row >= len(self._windows):
            return
        del self._windows[row]
        self._refresh_list(select_row=min(row, len(self._windows) - 1))
        self._refresh_timeline()

    def _on_row_changed(self, row: int) -> None:
        if row < 0 or row >= len(self._windows):
            self._set_editor_enabled(False)
            return
        w = self._windows[row]

        widgets = self._editor_widgets()
        for widget in widgets:
            widget.blockSignals(True)
        try:
            sh, sm = (int(x) for x in w.start.split(":"))
            eh, em = (int(x) for x in w.end.split(":"))
            self.field_start.setTime(QTime(sh, sm))
            self.field_end.setTime(QTime(eh, em))
            active_days = set(w.days) if w.days is not None else set()
            for i, cb in enumerate(self.day_checks):
                cb.setChecked(i in active_days)
            session = w.session_minutes or RandomRange(40, 90)
            brk = w.break_minutes or RandomRange(10, 30)
            self.check_sessions.setChecked(w.session_minutes is not None)
            self.field_session_min.setValue(session.min)
            self.field_session_max.setValue(session.max)
            self.field_break_min.setValue(brk.min)
            self.field_break_max.setValue(brk.max)
            self.field_jitter.setValue(int(w.jitter_minutes))
        finally:
            for widget in widgets:
                widget.blockSignals(False)
        self._set_editor_enabled(True)

    def _apply_current_window(self) -> None:
        self._update_session_enabled()
        row = self.list_widget.currentRow()
        if row < 0 or row >= len(self._windows):
            return
        checked_days = [i for i, cb in enumerate(self.day_checks) if cb.isChecked()]
        sessions = self.check_sessions.isChecked()
        s_min = self.field_session_min.value()
        b_min = self.field_break_min.value()
        self._windows[row] = ScheduleWindow(
            start=self.field_start.time().toString("HH:mm"),
            end=self.field_end.time().toString("HH:mm"),
            days=tuple(checked_days) if checked_days else None,
            # max < min przycinamy do min zamiast tworzyć nonsensowny zakres
            session_minutes=RandomRange(s_min, max(s_min, self.field_session_max.value())) if sessions else None,
            break_minutes=RandomRange(b_min, max(b_min, self.field_break_max.value())) if sessions else None,
            jitter_minutes=float(self.field_jitter.value()),
        )
        # odśwież etykietę w liście bez utraty zaznaczenia
        self.list_widget.blockSignals(True)
        self.list_widget.item(row).setText(self.window_label(self._windows[row]))
        self.list_widget.blockSignals(False)
        self._refresh_timeline()

    def result_windows(self) -> list[ScheduleWindow]:
        return self._windows

    def result_schedule_seed(self) -> int:
        return self.field_seed.value()

    def result_close_game_during_break(self) -> bool:
        return self.check_close_game.isChecked()

    def result_daily_limit_hours(self) -> RandomRange | None:
        if not self.check_daily_limit.isChecked():
            return None
        lo = self.field_limit_min.value()
        return RandomRange(lo, max(lo, self.field_limit_max.value()))


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.resize(1280, 820)
        self.setMinimumSize(1000, 640)

        self.instances: dict[str, InstanceEntry] = {}
        self.selected_name: str | None = None
        self._config_path: Path | None = None
        self._dirty = False
        # ustawienia wspólne - każda instancja dostaje ich kopię, zmiana trafia do wszystkich
        self.shared_config = BotConfig(name="wspólne")

        self._build_ui()
        self._update_title()

        path = default_config_path()
        if path.exists():
            self._load_config_file(path)

    # ------------------------------------------------------------------ #
    # Budowa interfejsu
    # ------------------------------------------------------------------ #
    @staticmethod
    def _hint(text: str) -> QLabel:
        """Szary, zawijany opis pod polami - tłumaczy, co robi ustawienie."""
        label = QLabel(text)
        label.setWordWrap(True)
        label.setObjectName("hint")
        return label

    @staticmethod
    def _form() -> QFormLayout:
        form = QFormLayout()
        # długie etykiety przechodzą nad pole zamiast się ucinać
        form.setRowWrapPolicy(QFormLayout.WrapLongRows)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        form.setLabelAlignment(Qt.AlignLeft)
        return form

    def _scroll_tab(self, content: QWidget) -> QScrollArea:
        area = QScrollArea()
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        area.setWidget(content)
        return area

    def _percent_spin(self, tooltip: str) -> QDoubleSpinBox:
        """Pole procentowe 0-100 % (w configu to ułamek 0-1)."""
        spin = QDoubleSpinBox()
        spin.setRange(0.0, 100.0)
        spin.setSingleStep(5.0)
        spin.setDecimals(0)
        spin.setSuffix(" %")
        spin.setToolTip(tooltip)
        spin.valueChanged.connect(self._apply_config_fields)
        return spin

    def _range_row(self, minimum, maximum, step, suffix, decimals=2):
        """Pole zakresu "od ... do ..." - bot losuje wartość z przedziału."""
        spin_min = QDoubleSpinBox(); spin_max = QDoubleSpinBox()
        for spin in (spin_min, spin_max):
            spin.setRange(minimum, maximum)
            spin.setSingleStep(step)
            spin.setDecimals(decimals)
            spin.setSuffix(suffix)
            spin.valueChanged.connect(self._apply_config_fields)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(QLabel("od")); row.addWidget(spin_min, 1)
        row.addWidget(QLabel("do")); row.addWidget(spin_max, 1)
        container = QWidget(); container.setLayout(row)
        return spin_min, spin_max, container

    @staticmethod
    def speed_text(pct: int) -> str:
        if pct == 100:
            return "100 % · bez zmian"
        return f"{pct} % · " + (f"{pct / 100:.2g}× szybciej" if pct > 100 else f"{100 / pct:.2g}× wolniej").replace(".", ",")

    def _speed_row(self):
        """Suwak tempa w procentach (100 % = czasy jak w ustawieniach)."""
        slider = QSlider(Qt.Horizontal)
        slider.setRange(SPEED_MIN_PCT, SPEED_MAX_PCT)
        slider.setSingleStep(5); slider.setPageStep(25)
        slider.setValue(100)
        label = QLabel(self.speed_text(100)); label.setMinimumWidth(130)
        slider.speed_label = label  # napis odświeżany też przy wczytaniu (sygnały są wtedy zablokowane)
        slider.valueChanged.connect(lambda v: label.setText(self.speed_text(v)))
        slider.valueChanged.connect(self._apply_config_fields)
        reset = QPushButton("100 %"); reset.setToolTip("Przywróć tempo z ustawień")
        reset.clicked.connect(lambda: slider.setValue(100))
        row = QHBoxLayout(); row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(QLabel("wolniej")); row.addWidget(slider, 1); row.addWidget(QLabel("szybciej"))
        row.addWidget(label); row.addWidget(reset)
        container = QWidget(); container.setLayout(row)
        return slider, container

    def _build_ui(self) -> None:
        self._build_toolbar()

        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # --- nawigacja po lewej ------------------------------------------ #
        rail = QFrame()
        rail.setObjectName("navRail")
        rail.setFixedWidth(196)
        rail_lay = QVBoxLayout(rail)
        rail_lay.setContentsMargins(0, 16, 0, 12)
        rail_lay.setSpacing(2)
        name = QLabel("Dust Mine Bot")
        name.setObjectName("appName")
        name.setContentsMargins(20, 0, 12, 0)
        ver = QLabel(f"wersja {APP_VERSION}")
        ver.setObjectName("appVersion")
        ver.setContentsMargins(20, 0, 12, 12)
        rail_lay.addWidget(name)
        rail_lay.addWidget(ver)
        self.nav = QListWidget()
        self.nav.setObjectName("nav")
        self.nav.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        for text, tip in (("📋  Przegląd", "Wszystkie instancje: podgląd, liczby, akcje dla wszystkich"),
                          ("⚙  Instancje", "Podgląd na żywo i ustawienia wybranej instancji"),
                          ("🧾  Log akcji", "Zapis każdej akcji bota do CSV i jego podgląd")):
            item = QListWidgetItem(tr(text))
            item.setToolTip(tr(tip))
            self.nav.addItem(item)
        rail_lay.addWidget(self.nav, 1)
        self.nav_running = QLabel("")
        self.nav_running.setObjectName("appVersion")
        self.nav_running.setContentsMargins(20, 0, 12, 0)
        rail_lay.addWidget(self.nav_running)
        root.addWidget(rail)

        # --- strony ------------------------------------------------------- #
        vsplit = QSplitter(Qt.Vertical)
        root.addWidget(vsplit, 1)

        top = QSplitter(Qt.Horizontal)
        top.setContentsMargins(8, 8, 8, 0)
        top.addWidget(self._build_instances_panel())
        top.addWidget(self._build_preview_panel())
        top.addWidget(self._build_settings_panel())
        top.setStretchFactor(0, 0)
        top.setStretchFactor(1, 1)
        top.setStretchFactor(2, 0)
        top.setSizes([230, 500, 540])

        self.dashboard = DashboardWidget()
        self.dashboard.card_action.connect(self._on_card_action)
        self.dashboard.global_action.connect(self._on_global_action)
        self.dashboard.depth_all.connect(self._on_depth_all)
        # strony w tej samej kolejności co nawigacja: 0 przegląd, 1 instancje, 2 log akcji
        self.main_tabs = QStackedWidget()
        self.main_tabs.addWidget(self.dashboard)
        self.main_tabs.addWidget(top)
        self.main_tabs.addWidget(self._build_action_log_page())
        self.nav.currentRowChanged.connect(self.main_tabs.setCurrentIndex)
        self.main_tabs.currentChanged.connect(self._on_page_changed)
        self.nav.setCurrentRow(0)
        vsplit.addWidget(self.main_tabs)
        vsplit.addWidget(self._build_log_panel())
        vsplit.setStretchFactor(0, 4)
        vsplit.setStretchFactor(1, 1)
        vsplit.setSizes([640, 150])

        self.statusBar().showMessage(tr("Wybierz instancję z listy albo wykryj emulatory przyciskiem na pasku narzędzi."))
        self._set_config_form_enabled(False)
        i18n.translate_tree(self)

    def _on_page_changed(self, index: int) -> None:
        if self.nav.currentRow() != index:
            self.nav.blockSignals(True)
            self.nav.setCurrentRow(index)
            self.nav.blockSignals(False)
        if index == 0:
            self._rebuild_dashboard()  # miniatury od razu po przejściu na przegląd
        elif index == 2:
            self._refresh_action_log()

    def _build_toolbar(self) -> None:
        tb = self.addToolBar(tr("Główne"))
        tb.setMovable(False)
        tb.setFloatable(False)
        tb.setContextMenuPolicy(Qt.PreventContextMenu)
        tb.setToolButtonStyle(Qt.ToolButtonTextOnly)

        def action(text, slot, tip, shortcut=None):
            act = QAction(text, self)
            act.setToolTip(tip)
            act.setStatusTip(tip)
            act.triggered.connect(slot)
            if shortcut:
                act.setShortcut(QKeySequence(shortcut))
            tb.addAction(act)
            return act

        action("📂 Wczytaj", self._load_config_dialog, "Wczytaj konfigurację z pliku JSON", "Ctrl+O")
        action("💾 Zapisz", self._save_config, "Zapisz konfigurację (Ctrl+S)", "Ctrl+S")
        action("Zapisz jako…", self._save_config_dialog, "Zapisz konfigurację do wybranego pliku")
        tb.addSeparator()
        action("🔍 Wykryj emulatory", self._autodetect_instances,
               "Dodaj instancje dla wszystkich urządzeń widocznych w `adb devices`")
        action("➕ Dodaj", self._add_instance_dialog, "Dodaj pustą instancję (ADB serial ustawisz w zakładce Instancja)")
        action("🗑 Usuń", self._remove_selected_instance, "Usuń zaznaczoną instancję")
        tb.addSeparator()
        action("▶▶ Start wszystkich", self._start_all, "Uruchom bota na wszystkich instancjach")
        action("■■ Stop wszystkich", self._stop_all, "Zatrzymaj bota na wszystkich instancjach")
        tb.addSeparator()
        tb.addWidget(QLabel(" 🌓 Motyw: "))  # tekst przetłumaczy i18n.translate_tree
        self.theme_combo = QComboBox()
        for key, label in theme.THEMES.items():
            self.theme_combo.addItem(label, key)
        self.theme_combo.setCurrentIndex(max(0, self.theme_combo.findData(theme.saved_theme())))
        self.theme_combo.setToolTip("Tryb jasny / ciemny okna programu (zapamiętywany)")
        self.theme_combo.currentIndexChanged.connect(
            lambda _i: theme.apply_theme(self.theme_combo.currentData()))
        tb.addWidget(self.theme_combo)

        tb.addWidget(QLabel(" 🌐 Language: "))
        self.language_combo = QComboBox()
        for key, label in i18n.LANGUAGES.items():
            self.language_combo.addItem(label, key)
        self.language_combo.setCurrentIndex(max(0, self.language_combo.findData(i18n.current_language())))
        self.language_combo.setToolTip("Interface language (applies after restarting the program)")
        self.language_combo.currentIndexChanged.connect(self._on_language_changed)
        tb.addWidget(self.language_combo)

    def _on_language_changed(self, _index: int) -> None:
        lang = self.language_combo.currentData()
        if lang == i18n.current_language():
            return
        i18n.set_language(lang)
        QMessageBox.information(self, "Dust Mine Bot",
                                "Restart the program to switch the interface language.\n"
                                "Zmiana języka wymaga ponownego uruchomienia programu.")

    def _build_instances_panel(self) -> QWidget:
        panel = QWidget()
        panel.setMinimumWidth(200)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(4, 4, 4, 4)

        title = QLabel("Instancje")
        title.setObjectName("pageTitle")
        layout.addWidget(title)
        self.instance_list = QListWidget()
        self.instance_list.setWordWrap(True)
        self.instance_list.setSpacing(2)
        self.instance_list.currentItemChanged.connect(self._on_selection_changed)
        layout.addWidget(self.instance_list, stretch=1)

        box = QGroupBox("Wybrana instancja")
        grid = QGridLayout(box)
        self.btn_start = QPushButton("▶ Start")
        self.btn_start.setToolTip("Uruchom bota na tej instancji")
        self.btn_start.clicked.connect(self._start_selected)
        self.btn_pause = QPushButton("⏸ Pauza")
        self.btn_pause.setToolTip("Wstrzymaj/wznów klikanie (podgląd dalej się odświeża)")
        self.btn_pause.clicked.connect(self._toggle_pause_selected)
        self.btn_stop = QPushButton("■ Stop")
        self.btn_stop.setToolTip("Zatrzymaj bota na tej instancji")
        self.btn_stop.clicked.connect(self._stop_selected)
        self.btn_start.setProperty("accent", True)
        self.btn_stop.setProperty("danger", True)
        self.btn_restart_app = QPushButton("🔄 Restart")
        self.btn_restart_app.setToolTip("Restart gry: zamknij i uruchom grę ponownie (wymaga pakietu gry)")
        self.btn_restart_app.clicked.connect(self._restart_app_selected)
        self.btn_salvage_now = QPushButton("🧹 Rozbierz przedmioty")
        self.btn_salvage_now.setToolTip("Rozbierz przedmioty z góry listy Inventory (zapyta o liczbę)")
        self.btn_salvage_now.clicked.connect(lambda: self._salvage_entries([self._current_entry()]))
        grid.setSpacing(6)
        for b, row, col, span in ((self.btn_start, 0, 0, 1), (self.btn_pause, 0, 1, 1),
                                  (self.btn_stop, 1, 0, 1), (self.btn_restart_app, 1, 1, 1),
                                  (self.btn_salvage_now, 2, 0, 2)):
            b.setMinimumHeight(30)
            grid.addWidget(b, row, col, 1, span)
        layout.addWidget(box)
        return panel

    def _build_preview_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(4, 4, 4, 4)

        self.preview_title = QLabel(tr("<b>Podgląd</b> – wybierz instancję"))
        self.preview_title.setStyleSheet("font-size: 15px;")
        layout.addWidget(self.preview_title)
        legend = QLabel(
            "<span style='color:#e33'>●</span> DROP &nbsp; "
            "<span style='color:#3c3'>●</span> kulki &nbsp; "
            "<span style='color:#f90'>●</span> kafelki &nbsp; "
            "<span style='color:#0cf'>●</span> CAST &nbsp; "
            "<span style='color:#fc0'>▢</span> klikany element &nbsp; "
            "<span style='color:#f80'>○</span> kliknięcie &nbsp; "
            "<span style='color:#0cf'>➝</span> przeciągnięcie"
        )
        legend.setWordWrap(True)
        legend.setObjectName("hint")
        layout.addWidget(legend)

        self.preview = PreviewWidget()
        self.preview.idle_point_picked.connect(self._on_idle_point_picked)
        self.preview.board_region_picked.connect(self._on_board_region_picked)
        self.preview.named_point_picked.connect(self._on_named_point_picked)
        layout.addWidget(self.preview, stretch=1)

        grid = QGridLayout()
        self.btn_refresh_preview = QPushButton("⟳ Odśwież podgląd")
        self.btn_refresh_preview.setToolTip("Pobierz jeden zrzut ekranu (gdy bot jest zatrzymany)")
        self.btn_refresh_preview.clicked.connect(self._refresh_preview_once)
        grid.addWidget(self.btn_refresh_preview, 0, 0)
        layout.addLayout(grid)
        return panel

    def _build_settings_panel(self) -> QWidget:
        panel = QWidget()
        panel.setMinimumWidth(500)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(4, 4, 4, 4)
        settings_title = QLabel("Ustawienia")
        settings_title.setObjectName("pageTitle")
        layout.addWidget(settings_title)

        self.btn_shared = QPushButton("⚙ Ustawienia wspólne (dla wszystkich instancji)…")
        self.btn_shared.setMinimumHeight(30)
        self.btn_shared.setToolTip("Punkty, tempo, minigry, paczki, ochrona i reszta ustawień - "
                                   "zmiana działa na wszystkich instancjach, także nowo dodanych.")
        self.btn_shared.clicked.connect(self._open_shared_settings)
        layout.addWidget(self.btn_shared)
        layout.addWidget(self._hint("Ustawienia tej instancji: nazwa, urządzenie ADB, pakiet gry, "
                                    "głębokość farmy i harmonogram."))

        self.tabs = QTabWidget()
        self.tabs.setUsesScrollButtons(False)  # wszystkie zakładki zawsze widoczne
        self.tabs.addTab(self._scroll_tab(self._tab_instance()), "Instancja")
        self.tabs.addTab(self._scroll_tab(self._tab_schedule()), "Plan")
        self.tabs.setTabToolTip(1, "Harmonogram (osobny dla każdej instancji)")
        layout.addWidget(self.tabs, stretch=1)

        # okno ustawień wspólnych - działa też bez zaznaczonej instancji
        self.shared_dialog = QDialog(self)
        self.shared_dialog.setWindowTitle("Ustawienia wspólne")
        self.shared_dialog.resize(760, 820)
        dlg_layout = QVBoxLayout(self.shared_dialog)
        dlg_layout.addWidget(self._hint("Te ustawienia obowiązują na wszystkich instancjach, także na "
                                        "nowo dodanych. Zmiany działają od razu."))
        self.shared_tabs = QTabWidget()
        self.shared_tabs.setUsesScrollButtons(False)
        for title, builder in (("Ogólne", self._tab_general), ("Punkty", self._tab_points),
                               ("Tempo", self._tab_tempo), ("Minigry", self._tab_minigames),
                               ("Głębokość i paczki", self._tab_packs), ("Ochrona", self._tab_protection)):
            self.shared_tabs.addTab(self._scroll_tab(builder()), title)
        self.shared_tabs.currentChanged.connect(lambda _i: self._update_preview_points())
        dlg_layout.addWidget(self.shared_tabs, stretch=1)
        close_buttons = QDialogButtonBox(QDialogButtonBox.Close)
        close_buttons.rejected.connect(self.shared_dialog.reject)
        dlg_layout.addWidget(close_buttons)
        self.shared_dialog.finished.connect(lambda _r: self._update_preview_points())

        stats_box = QGroupBox("Statystyki")
        grid = QGridLayout(stats_box)
        self.stat_labels: dict[str, QLabel] = {}
        for i, (key, title) in enumerate(_STAT_FIELDS):
            name = QLabel(title)
            name.setObjectName("metricName")
            value = QLabel("0")
            value.setObjectName("metricValue")
            grid.addWidget(name, i // 3, (i % 3) * 2)
            grid.addWidget(value, i // 3, (i % 3) * 2 + 1)
            self.stat_labels[key] = value
        # wydajność: gdzie bot traci czas (zrzut ekranu / akcja)
        self.perf_label = QLabel("")
        self.perf_label.setObjectName("hint")
        self.perf_label.setWordWrap(True)
        grid.addWidget(self.perf_label, len(_STAT_FIELDS) // 3 + 1, 0, 1, 6)
        layout.addWidget(stats_box)
        return panel

    @staticmethod
    def perf_text(stats, long: bool = False) -> str:
        """Opis wydajności z pomiarów bota (pusty, gdy jeszcze ich nie ma)."""
        if stats is None or not getattr(stats, "capture_ms", 0):
            return ""
        text = (f"Zrzut ekranu {stats.capture_ms:.0f} ms · akcja {stats.input_ms:.0f} ms · "
                f"{stats.fps:.1f} zrzutu/s".replace(".", ","))
        method = getattr(stats, "capture_method", "")
        if method:
            text += f" · {method}"
        if long:
            if stats.device_width:
                text += f" · ekran emulatora {stats.device_width} px szerokości"
            if stats.device_width > 720 and method != "scrcpy":
                text += (". Wskazówka: ustaw w LDPlayerze rozdzielczość 540×960 - bot i tak pracuje na "
                         "540 px, a zrzut będzie kilka razy mniejszy i szybszy.")
        return text

    def _tab_instance(self) -> QWidget:
        """Ustawienia tylko tej instancji (reszta jest we wspólnym oknie)."""
        w = QWidget()
        form = self._form()
        w.setLayout(form)
        form.addRow(self._hint("Tylko ta instancja. Pozostałe ustawienia są we wspólnym oknie "
                               "„Ustawienia wspólne”."))
        self.field_name = QLineEdit()
        self.field_name.setToolTip("Nazwa widoczna na liście i w logach")
        self.field_name.editingFinished.connect(self._apply_config_fields)
        form.addRow("Nazwa:", self.field_name)

        self.field_serial = QLineEdit()
        self.field_serial.setPlaceholderText("np. emulator-5554 albo 127.0.0.1:5555")
        self.field_serial.setToolTip("Identyfikator urządzenia z `adb devices`")
        self.field_serial.editingFinished.connect(self._apply_config_fields)
        form.addRow("Urządzenie (ADB):", self.field_serial)

        self.field_game_package = QLineEdit()
        self.field_game_package.setPlaceholderText("com.timhilgenstoehlerexpo.justlootapp")
        self.field_game_package.setToolTip("Nazwa pakietu gry - potrzebna do restartu gry i ochrony (zakładka Ochrona)")
        self.field_game_package.editingFinished.connect(self._apply_config_fields)
        form.addRow("Pakiet gry:", self.field_game_package)

        self.field_game_activity = QLineEdit()
        self.field_game_activity.setPlaceholderText("zwykle puste")
        self.field_game_activity.setToolTip("Opcjonalna activity do uruchamiania gry, np. pakiet/.MainActivity")
        self.field_game_activity.editingFinished.connect(self._apply_config_fields)
        form.addRow("Activity (opcjonalnie):", self.field_game_activity)

        form.addRow(self._section("Farm na głębokości"))
        self.field_depth_mode = QComboBox()
        for key, label in DEPTH_MODES.items():
            self.field_depth_mode.addItem(label, key)
        self.field_depth_mode.setToolTip(
            "Maksymalna odblokowana: bot klika „›”, aż przestanie się dać.\n"
            "Bez bonusu wrogów: najwyższa głębokość, na której gra pisze „enemies at normal "
            "strength” (gear wystarcza) - rośnie sama, gdy gear się poprawi.\n"
            "Maksymalna minus X%: np. maks. 362 i 10% -> 325.")
        self.field_depth_mode.currentIndexChanged.connect(self._apply_config_fields)
        form.addRow("Tryb:", self.field_depth_mode)
        self.field_farm_depth = QSpinBox(); self.field_farm_depth.setRange(0, 9999)
        self.field_farm_depth.setSpecialValueText("nie zmieniaj")
        self.field_farm_depth.setToolTip("0 = bot nie rusza wyboru głębokości")
        self.field_farm_depth.valueChanged.connect(self._apply_config_fields)
        form.addRow("Farmij na głębokości:", self.field_farm_depth)
        self.field_depth_pct = QSpinBox(); self.field_depth_pct.setRange(1, 90)
        self.field_depth_pct.setSuffix(" % poniżej maks.")
        self.field_depth_pct.valueChanged.connect(self._apply_config_fields)
        form.addRow("Maks. minus:", self.field_depth_pct)
        return w

    def _tab_general(self) -> QWidget:
        w = QWidget()
        form = self._form()
        w.setLayout(form)

        form.addRow(self._section("Klikanie w trybie czekania"))
        self.field_idle_just_loot = QCheckBox("Klikaj JUST LOOT (bot sam go znajduje)")
        self.field_idle_just_loot.setToolTip(
            "Bot na każdej klatce szuka przycisku JUST LOOT, a po walce PROCEED.\n"
            "Gdy nie rozpozna ekranu, nic nie klika (chyba że włączysz punkt awaryjny w zakładce Punkty).")
        self.field_idle_just_loot.toggled.connect(self._apply_config_fields)
        form.addRow(self.field_idle_just_loot)
        self.field_just_loot_radius = self._percent_spin(
            "Jak daleko od środka przycisku bot może kliknąć: 0 % = zawsze w środku,\n"
            "100 % = losowo aż do brzegu przycisku JUST LOOT.")
        form.addRow("Obszar kliknięcia JUST LOOT:", self.field_just_loot_radius)

        self.field_drop_choice = QComboBox()
        for key, (label, _row) in DROP_CHOICES.items():
            self.field_drop_choice.addItem(label, key)
        self.field_drop_choice.currentIndexChanged.connect(self._apply_config_fields)
        form.addRow("Drop po wygranej walce:", self.field_drop_choice)
        form.addRow(self._hint("Na ekranie porównania przedmiotów bot klika wybraną opcję. Zakrytą "
                               "kartę („tap anywhere to reveal”) odkrywa sam."))

        self.btn_advanced = QPushButton("▸ Zaawansowane")
        self.btn_advanced.setCheckable(True)
        self.btn_advanced.setFlat(True)
        self.btn_advanced.setStyleSheet("text-align: left; font-weight: bold;")
        form.addRow(self.btn_advanced)
        self.advanced_box = QWidget()
        adv = QVBoxLayout(self.advanced_box)
        adv.setContentsMargins(0, 0, 0, 0)
        adv.addWidget(QLabel("<b>Obszar planszy</b>"))
        adv.addWidget(self._hint("Zwykle niepotrzebny - wykrywanie działa na całym ekranie. Ogranicza "
                                 "szukanie kulek do prostokąta (np. na bardzo słabym komputerze). "
                                 "Wszystkie zera = cały ekran."))
        self.field_region_x1 = QSpinBox(); self.field_region_y1 = QSpinBox()
        self.field_region_x2 = QSpinBox(); self.field_region_y2 = QSpinBox()
        grid = QGridLayout(); grid.setContentsMargins(0, 0, 0, 0)
        for i, (label, f) in enumerate((("lewo", self.field_region_x1), ("góra", self.field_region_y1),
                                         ("prawo", self.field_region_x2), ("dół", self.field_region_y2))):
            f.setRange(0, 9999)
            f.valueChanged.connect(self._apply_config_fields)
            grid.addWidget(QLabel(label), i // 2, (i % 2) * 2)
            grid.addWidget(f, i // 2, (i % 2) * 2 + 1)
        adv.addLayout(grid)
        self.btn_edit_region = QPushButton("▭ Zaznacz na podglądzie")
        self.btn_edit_region.setToolTip("Przeciągnij potem prostokąt na podglądzie")
        self.btn_edit_region.setCheckable(True)
        self.btn_edit_region.clicked.connect(self._toggle_edit_region)
        self.btn_clear_region = QPushButton("⤢ Cały ekran")
        self.btn_clear_region.clicked.connect(self._clear_board_region)
        row = QHBoxLayout(); row.addWidget(self.btn_edit_region); row.addWidget(self.btn_clear_region)
        adv.addLayout(row)
        self.advanced_box.setVisible(False)
        self.btn_advanced.toggled.connect(self._toggle_advanced)
        form.addRow(self.advanced_box)
        return w

    def _open_shared_settings(self) -> None:
        self._load_fields_from_entry(self._current_entry())
        self.shared_dialog.exec()

    def _toggle_advanced(self, shown: bool) -> None:
        self.advanced_box.setVisible(shown)
        self.btn_advanced.setText(("▾" if shown else "▸") + " Zaawansowane")

    def _tab_points(self) -> QWidget:
        w = QWidget()
        form = self._form()
        w.setLayout(form)
        form.addRow(self._hint("Każdy punkt to obszar - bot klika w losowe miejsce wewnątrz koła. "
                               "„Wskaż” i kliknij na podglądzie (podgląd musi mieć obraz - uruchom "
                               "bota albo „Odśwież podgląd”). Obszary są narysowane na podglądzie, "
                               "gdy ta zakładka jest otwarta."))

        form.addRow(self._section("Punkt awaryjny"))
        self.field_idle_fallback = QCheckBox("Klikaj punkt awaryjny, gdy bot nic nie rozpozna")
        self.field_idle_fallback.setToolTip(
            "Domyślnie wyłączone: na nieznanym ekranie bot nic nie klika (klikanie na ślepo może\n"
            "trafić w coś przypadkowego), tylko po chwili zapisuje zrzut ekranu (zakładka Ochrona).")
        self.field_idle_fallback.toggled.connect(self._apply_config_fields)
        form.addRow(self.field_idle_fallback)
        self.field_idle_x = QSpinBox(); self.field_idle_x.setRange(0, 9999); self.field_idle_x.setPrefix("x ")
        self.field_idle_y = QSpinBox(); self.field_idle_y.setRange(0, 9999); self.field_idle_y.setPrefix("y ")
        for f in (self.field_idle_x, self.field_idle_y):
            f.valueChanged.connect(self._apply_config_fields)
        self.field_idle_radius = QDoubleSpinBox()
        self.field_idle_radius.setRange(0.0, 300.0); self.field_idle_radius.setSingleStep(5.0)
        self.field_idle_radius.setSuffix(" px"); self.field_idle_radius.setPrefix("promień ")
        self.field_idle_radius.valueChanged.connect(self._apply_config_fields)
        self.btn_edit_idle = QPushButton("🎯 Wskaż")
        self.btn_edit_idle.setCheckable(True)
        self.btn_edit_idle.clicked.connect(self._toggle_edit_idle)
        row = QGridLayout(); row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.field_idle_x, 0, 0); row.addWidget(self.field_idle_y, 0, 1)
        row.addWidget(self.field_idle_radius, 1, 0); row.addWidget(self.btn_edit_idle, 1, 1)
        self.idle_point_box = QWidget(); self.idle_point_box.setLayout(row)
        form.addRow(self.idle_point_box)

        form.addRow(self._section("Nawigacja i paczki"))
        self.point_widgets: dict[str, tuple[QLabel, QDoubleSpinBox, QPushButton, QPushButton]] = {}
        for key, label, _color in _POINTS:
            value = QLabel("—")
            value.setStyleSheet("color: #888;")
            radius = QDoubleSpinBox(); radius.setRange(0.5, 25.0); radius.setSingleStep(0.5); radius.setDecimals(1)
            radius.setPrefix("promień "); radius.setSuffix(" % szer.")
            radius.valueChanged.connect(self._apply_config_fields)
            pick = QPushButton("🎯 Wskaż")
            pick.clicked.connect(lambda _=False, k=key: self._pick_point(k))
            reset = QPushButton("↺ Domyślny")
            reset.setToolTip("Przywróć wartość zmierzoną na LDPlayer")
            reset.clicked.connect(lambda _=False, k=key: self._reset_point(k))
            grid = QGridLayout(); grid.setContentsMargins(0, 0, 0, 6)
            title = QLabel(f"<b>{label}</b>"); title.setWordWrap(True)
            grid.addWidget(title, 0, 0, 1, 2)
            grid.addWidget(value, 1, 0, 1, 2)
            grid.addWidget(radius, 2, 0, 1, 2)
            grid.addWidget(pick, 3, 0); grid.addWidget(reset, 3, 1)
            box = QWidget(); box.setLayout(grid)
            form.addRow(box)
            self.point_widgets[key] = (value, radius, pick, reset)
        return w

    def _tab_tempo(self) -> QWidget:
        w = QWidget()
        form = self._form()
        w.setLayout(form)
        form.addRow(self._hint("Każda wartość jest losowana z zakresu \"od–do\", żeby rytm nie był "
                               "mechaniczny. Takie same \"od\" i \"do\" = stała wartość."))
        self.field_turbo = QCheckBox("⚡ Tryb turbo – maksymalna szybkość (ignoruje poniższe ustawienia)")
        self.field_turbo.setToolTip("Minimalne przerwy i czas reakcji, krótkie przeciągnięcia (ok. 0.2–0.3 s), "
                                    "bez celowych pomyłek, pozostałe stałe przerwy ~3× krótsze. Ustawienia "
                                    "poniżej zostają zapisane i wracają po wyłączeniu turbo.")
        self.field_turbo.toggled.connect(self._apply_config_fields)
        form.addRow(self.field_turbo)

        form.addRow(self._section("Szybkość"))
        self.field_speed_board, box = self._speed_row()
        box.setToolTip("Przeciąganie kulek, przytrzymanie na DROP i przerwy między przeciągnięciami")
        form.addRow("Plansza (kulki):", box)
        self.field_speed_react, box = self._speed_row()
        box.setToolTip("Czas reakcji na nowy ekran, kafelki minigier i pozostałe przerwy między akcjami")
        form.addRow("Reakcja i przejścia:", box)
        self.field_speed_tap, box = self._speed_row()
        box.setToolTip("Odstęp między tapnięciami JUST LOOT w walce i długość tapnięcia")
        form.addRow("Tapanie w walce:", box)
        form.addRow(self._hint("Suwaki mnożą czasy poniżej (i w turbo): 200 % = czasy o połowę krótsze, "
                               "50 % = dwa razy dłuższe. Przeciągnięcie i przytrzymanie na DROP nie schodzą "
                               "poniżej minimum, przy którym kulka jeszcze trafia. Czasy gry (walka, "
                               "animacje, ładowanie) się nie zmieniają."))

        self.field_idle_interval_min, self.field_idle_interval_max, box = self._range_row(1, 30000, 10, " ms", decimals=0)
        form.addRow("Odstęp między kliknięciami:", box)
        self.field_drag_ms_min, self.field_drag_ms_max, box = self._range_row(50, 5000, 50, " ms", decimals=0)
        form.addRow("Czas przeciągania kulki:", box)
        self.field_hold_end_min, self.field_hold_end_max, box = self._range_row(0, 2000, 50, " ms", decimals=0)
        box.setToolTip("Ile palec stoi na DROP przed puszczeniem - gra przesuwa kulkę za palcem z "
                       "opóźnieniem; za krótko = kulka ląduje przed środkiem DROP")
        form.addRow("Przytrzymanie na DROP:", box)
        self.field_between_drags_min, self.field_between_drags_max, box = self._range_row(0.0, 10.0, 0.1, " s")
        form.addRow("Przerwa między przeciągnięciami:", box)

        form.addRow(self._section("Zachowanie jak człowiek"))
        self.field_reaction_min, self.field_reaction_max, box = self._range_row(0.0, 10.0, 0.1, " s")
        form.addRow("Czas reakcji na planszę / minigrę:", box)
        self.field_hold_min, self.field_hold_max, box = self._range_row(0, 400, 10, " ms", decimals=0)
        form.addRow("Przytrzymanie tapnięcia:", box)
        self.field_mistake = QDoubleSpinBox(); self.field_mistake.setRange(0.0, 20.0)
        self.field_mistake.setSingleStep(0.5); self.field_mistake.setDecimals(1); self.field_mistake.setSuffix(" %")
        self.field_mistake.setSpecialValueText("wyłączone")
        self.field_mistake.valueChanged.connect(self._apply_config_fields)
        form.addRow("Szansa na drobną pomyłkę:", self.field_mistake)
        form.addRow(self._hint("Pomyłka = puszczenie kulki tuż obok DROP albo kliknięcie obok kafelka. "
                               "Bot od razu to poprawia, więc kosztuje tylko chwilę, a 100% skuteczności "
                               "wygląda nieludzko. Czasy są losowane częściej bliżej dolnej granicy, "
                               "czasem dłużej - jak u człowieka."))

        form.addRow(self._section("Styl ruchu"))
        self.field_human_drag = QCheckBox("Ludzki ruch przy przeciąganiu")
        self.field_human_drag.toggled.connect(self._apply_config_fields)
        form.addRow(self.field_human_drag)
        self.field_grab_radius = self._percent_spin(
            "Gdzie na kulce bot łapie palcem: 0 % = zawsze w środku kulki,\n"
            "100 % = losowo aż do jej brzegu.")
        form.addRow("Punkt chwytu kulki:", self.field_grab_radius)
        self.field_drag_points_custom = QCheckBox("Ustaw liczbę punktów toru ręcznie")
        self.field_drag_points_custom.setToolTip("Wyłączone = liczba punktów zależy od długości drogi kulki.")
        self.field_drag_points_custom.toggled.connect(self._apply_config_fields)
        form.addRow(self.field_drag_points_custom)
        self.field_drag_points_min, self.field_drag_points_max, box = self._range_row(1, 60, 1, " pkt", decimals=0)
        box.setToolTip("Ile punktów pośrednich ma tor jednego przeciągnięcia - każdy to osobny ruch palca.\n"
                       "Mało punktów = bardziej prosty, „skokowy” ruch; dużo = płynniejszy, dłuższy gest.")
        form.addRow("Punkty na torze kulki:", box)
        self.field_release_radius = self._percent_spin(
            "Gdzie nad DROP bot puszcza kulkę: 0 % = zawsze w środku DROP,\n"
            "100 % = losowo aż do jego brzegu.")
        form.addRow("Punkt puszczenia na DROP:", self.field_release_radius)
        self.field_drag_jitter = QDoubleSpinBox()
        self.field_drag_jitter.setRange(0.0, 10.0)
        self.field_drag_jitter.setSingleStep(0.1)
        self.field_drag_jitter.setDecimals(1)
        self.field_drag_jitter.setSuffix(" px")
        self.field_drag_jitter.setSpecialValueText("wyłączone")
        self.field_drag_jitter.setToolTip("Drżenie dłoni: losowe odchylenie punktów toru od idealnej linii. "
                                          "0 = palec idzie gładko.")
        self.field_drag_jitter.valueChanged.connect(self._apply_config_fields)
        form.addRow("Drżenie dłoni na torze:", self.field_drag_jitter)
        form.addRow(self._hint("Palec jedzie po losowej krzywej, przyspiesza i zwalnia, łapie kulkę w "
                               "losowym miejscu, a kulki zbiera w losowej kolejności. Wyłączone: "
                               "prosta linia środek–środek, kulki po kolei."))

        form.addRow(self._section("Wydajność"))
        self.field_proc_width = QSpinBox(); self.field_proc_width.setRange(0, 2160)
        self.field_proc_width.setSingleStep(60)
        self.field_proc_width.setSuffix(" px szerokości")
        self.field_proc_width.setSpecialValueText("pełna rozdzielczość")
        self.field_proc_width.valueChanged.connect(self._apply_config_fields)
        form.addRow("Analizuj zrzut w:", self.field_proc_width)

        self.field_capture = QComboBox()
        self.field_capture.addItem("scrcpy - strumień wideo (szybko)", "scrcpy")
        self.field_capture.addItem("screencap - zrzut przez ADB (wolno)", "screencap")
        self.field_capture.setToolTip(
            "scrcpy: emulator wysyła obraz jako wideo już zmniejszone do szerokości analizy - klatka "
            "jest gotowa od razu. Gra działa w normalnej rozdzielczości. Przy błędzie strumienia bot "
            "sam przechodzi na chwilę na screencap.")
        self.field_capture.currentIndexChanged.connect(self._apply_config_fields)
        form.addRow("Obraz z emulatora:", self.field_capture)
        form.addRow(self._hint("Zrzut ekranu jest przed analizą pomniejszany do tej szerokości - przy "
                               "1080×1920 i 540 px bot zużywa ok. 3× mniej CPU, a rozpoznawanie działa "
                               "tak samo. Kliknięcia trafiają w pełnej rozdzielczości. Jeszcze mniej "
                               "obciążenia: niższa rozdzielczość (np. 540×960) i FPS (np. 30) w "
                               "ustawieniach LDPlayera."))
        return w

    def _tab_minigames(self) -> QWidget:
        w = QWidget()
        form = self._form()
        w.setLayout(form)

        self.field_play_debris = QCheckBox("Depth Collapse – klikaj wszystkie kafelki")
        self.field_play_debris.toggled.connect(self._apply_config_fields)
        form.addRow(self.field_play_debris)
        form.addRow(self._hint("Brązowe kafelki „TAP” - bot klika wszystkie przed końcem czasu."))
        self.field_tile_min, self.field_tile_max, box = self._range_row(1, 5000, 10, " ms", decimals=0)
        box.setToolTip("Odstęp między kolejnymi kliknięciami kafelków Depth Collapse. Losowany z zakresu.")
        form.addRow("Odstęp między kliknięciami kafelków:", box)
        self.field_tile_by_distance = QCheckBox("Odstęp zależy od odległości między kafelkami")
        self.field_tile_by_distance.setToolTip("Dalszy kafelek = dłuższy odstęp. Sąsiednie kafelki losują "
                                               "z podanego zakresu, dalsze - z zakresu pomnożonego przez "
                                               "odległość (0,5–3×).")
        self.field_tile_by_distance.toggled.connect(self._apply_config_fields)
        form.addRow(self.field_tile_by_distance)
        self.field_skip_fishing = QCheckBox("Fishing Hole – obsługuj minigrę")
        self.field_skip_fishing.toggled.connect(self._apply_config_fields)
        form.addRow(self.field_skip_fishing)
        self.field_fishing_aim = QComboBox()
        self.field_fishing_aim.addItem("celuj w strefę", True)
        self.field_fishing_aim.addItem("od razu CAST (pomiń)", False)
        self.field_fishing_aim.currentIndexChanged.connect(self._apply_config_fields)
        form.addRow("Rzut:", self.field_fishing_aim)
        form.addRow(self._hint("Celowanie: bot śledzi wskaźnik przez kilka klatek, wylicza jego ruch i "
                               "naciska CAST tak, żeby stanął na środku jasnej strefy. Po każdym rzucie "
                               "sprawdza, gdzie wskaźnik stanął, i sam poprawia opóźnienie - pierwsze "
                               "rzuty mogą być niecelne, potem trafia. Najlepiej działa z obrazem scrcpy."))
        return w

    def _tab_packs(self) -> QWidget:
        w = QWidget()
        form = self._form()
        w.setLayout(form)
        form.addRow(self._section("Sprawdzanie głębokości"))
        self.field_depth_interval = QSpinBox(); self.field_depth_interval.setRange(1, 1440)
        self.field_depth_interval.setSuffix(" min")
        self.field_depth_interval.valueChanged.connect(self._apply_config_fields)
        form.addRow("Sprawdzaj głębokość co:", self.field_depth_interval)
        self.field_depth_repeat = QSpinBox(); self.field_depth_repeat.setRange(2, 50)
        self.field_depth_repeat.setSuffix(" walk")
        self.field_depth_repeat.setToolTip("Tryby maksymalne: gdy tyle walk z rzędu jest na tej samej głębokości "
                                           "(odczyt z nagłówka walki), bot od razu sprawdza / podkręca głębokość")
        self.field_depth_repeat.valueChanged.connect(self._apply_config_fields)
        form.addRow("Podkręć po tylu walkach na tej samej:", self.field_depth_repeat)
        self.field_depth_push = QSpinBox(); self.field_depth_push.setRange(0, 50)
        self.field_depth_push.setSpecialValueText("wyłączone")
        self.field_depth_push.setSuffix("× „›”")
        self.field_depth_push.setToolTip("Po każdej walce, przed JUST LOOT: tyle szybkich kliknięć strzałki „›” "
                                         "(wyższa głębokość). Na maks. odblokowanej strzałka jest przygaszona i "
                                         "bot jej nie klika. Nie działa z trybem stałej głębokości ani maks. − X%.")
        self.field_depth_push.valueChanged.connect(self._apply_config_fields)
        form.addRow("Po każdej walce zwiększ głębokość:", self.field_depth_push)
        self.field_loadout = QLineEdit()
        self.field_loadout.setPlaceholderText("puste = nie sprawdzaj")
        self.field_loadout.setToolTip("Gdy wybrana głębokość spadnie do 1 (np. gra przełączyła loadout na "
                                      "HUNT), bot w Profile wybierze loadout o tej nazwie")
        self.field_loadout.editingFinished.connect(self._apply_config_fields)
        form.addRow("Loadout przy głębokości 1:", self.field_loadout)
        form.addRow(self._hint("Bot wchodzi w Profile → CHOOSE DEPTH i ustawia głębokość: klika kafelek "
                               "z numerem (do 79) albo strzałki ‹ › (80+). Sprawdza ją od razu po zmianie "
                               "i potem co podany czas - w trybach automatycznych wtedy też podnosi ją, gdy "
                               "odblokujesz więcej albo gear się poprawi. Gdy wpisana głębokość nie jest "
                               "odblokowana, zostaje najbliższa możliwa. Ustawienie osobne dla każdej "
                               "instancji."))

        form.addRow(self._section("Paczki"))
        self.field_auto_packs = QCheckBox("Otwieraj paczki, gdy gear jest za słaby")
        self.field_auto_packs.toggled.connect(self._apply_config_fields)
        form.addRow(self.field_auto_packs)
        form.addRow(self._hint("Bot sprawdza w Profile, czy wrogowie są „stronger” (gear za słaby na "
                               "głębokość), albo reaguje na serię porażek - wtedy w Craft otwiera "
                               "maksymalną liczbę paczek, aż gear wystarczy albo skończy się pył."))
        self.field_packs_interval = QSpinBox(); self.field_packs_interval.setRange(1, 1440)
        self.field_packs_interval.setSuffix(" min")
        self.field_packs_interval.valueChanged.connect(self._apply_config_fields)
        form.addRow("Sprawdzaj Profile co:", self.field_packs_interval)
        self.field_packs_defeats = QSpinBox(); self.field_packs_defeats.setRange(1, 50)
        self.field_packs_defeats.valueChanged.connect(self._apply_config_fields)
        form.addRow("Po tylu porażkach z rzędu:", self.field_packs_defeats)
        self.field_packs_rounds = QSpinBox(); self.field_packs_rounds.setRange(1, 100)
        self.field_packs_rounds.setToolTip("Jedna runda = maksymalna liczba paczek + przejrzenie kart")
        self.field_packs_rounds.valueChanged.connect(self._apply_config_fields)
        form.addRow("Maks. rund za jednym razem:", self.field_packs_rounds)
        self.field_packs_per_run = QSpinBox(); self.field_packs_per_run.setRange(0, 1000)
        self.field_packs_per_run.setSpecialValueText("aż gear wystarczy")
        self.field_packs_per_run.setSuffix(" paczek")
        self.field_packs_per_run.setToolTip("Ile paczek otworzyć za jednym razem. 0 = rundy po maks. 20 paczek, "
                                            "aż wrogowie wrócą do normalnej siły / skończy się pył")
        self.field_packs_per_run.valueChanged.connect(self._apply_config_fields)
        form.addRow("Otwieraj za jednym razem:", self.field_packs_per_run)
        self.field_gear_margin = QSpinBox(); self.field_gear_margin.setRange(0, 1000)
        self.field_gear_margin.setSpecialValueText("nie sprawdzaj")
        self.field_gear_margin.setToolTip("Paczki, gdy głębokość walki (nagłówek walki) >= Gear Level (Profile) + tyle. "
                                          "55 = granica, od której wrogowie mają bonus")
        self.field_gear_margin.valueChanged.connect(self._apply_config_fields)
        form.addRow("Paczki, gdy głębokość ≥ Gear Level +", self.field_gear_margin)

        form.addRow(self._section("Pełny ekwipunek"))
        self.field_salvage_full = QCheckBox("Gdy ekwipunek pełny – rozbieraj przedmioty")
        self.field_salvage_full.toggled.connect(self._apply_config_fields)
        form.addRow(self.field_salvage_full)
        form.addRow(self._hint("Po „Inventory Full”: OK, przytrzymanie Salvage na ekranie dropu, potem w "
                               "Inventory przytrzymanie „+x DUST” na przedmiotach Z GÓRY LISTY - ustaw w "
                               "grze sortowanie tak, żeby na górze były najsłabsze (np. Item Level rosnąco). "
                               "Rozebrania nie da się cofnąć."))
        self.field_salvage_items = QSpinBox(); self.field_salvage_items.setRange(1, 1000)
        self.field_salvage_items.setSuffix(" przedmiotów")
        self.field_salvage_items.valueChanged.connect(self._apply_config_fields)
        form.addRow("Rozbieraj za jednym razem:", self.field_salvage_items)
        self.field_salvage_hold_min, self.field_salvage_hold_max, box = self._range_row(300, 5000, 100, " ms", decimals=0)
        form.addRow("Przytrzymanie przycisku:", box)
        return w

    def _tab_schedule(self) -> QWidget:
        w = QWidget()
        layout = QVBoxLayout(w)
        layout.addWidget(self._hint("Okna czasowe, w których bot gra, losowe sesje i przerwy w oknach "
                                    "oraz dzienny limit czasu gry. Harmonogram jest osobny dla każdej "
                                    "instancji."))
        self.schedule_summary_label = QLabel("brak okien (działa zawsze)")
        self.schedule_summary_label.setWordWrap(True)
        layout.addWidget(self.schedule_summary_label)
        self.btn_edit_schedule = QPushButton("📅 Edytuj harmonogram…")
        self.btn_edit_schedule.setMinimumHeight(30)
        self.btn_edit_schedule.clicked.connect(self._open_schedule_editor)
        layout.addWidget(self.btn_edit_schedule)
        layout.addStretch(1)
        return w

    def _tab_protection(self) -> QWidget:
        w = QWidget()
        form = self._form()
        w.setLayout(form)
        form.addRow(self._hint("Automatyczny restart gry, gdy coś pójdzie nie tak. Wymaga pakietu gry "
                               "(zakładka Instancja)."))
        self.field_black_timeout = QDoubleSpinBox(); self.field_black_timeout.setRange(5, 600)
        self.field_black_timeout.setSuffix(" s"); self.field_black_timeout.setDecimals(0)
        self.field_black_timeout.valueChanged.connect(self._apply_config_fields)
        form.addRow("Restart po czarnym ekranie trwającym:", self.field_black_timeout)
        self.field_fg_lost = QDoubleSpinBox(); self.field_fg_lost.setRange(0, 600)
        self.field_fg_lost.setSuffix(" s"); self.field_fg_lost.setDecimals(0)
        self.field_fg_lost.setSpecialValueText("wyłączone")
        self.field_fg_lost.valueChanged.connect(self._apply_config_fields)
        form.addRow("Restart, gdy gry nie ma na ekranie dłużej niż:", self.field_fg_lost)
        form.addRow(self._hint("Np. gra wysypała się do pulpitu Androida. Sklep Google Play i przeglądarka "
                               "(kliknięta reklama) powodują restart od razu."))
        self.field_settle = QDoubleSpinBox(); self.field_settle.setRange(0, 120)
        self.field_settle.setSuffix(" s"); self.field_settle.setDecimals(0)
        self.field_settle.valueChanged.connect(self._apply_config_fields)
        form.addRow("Czas na załadowanie gry po restarcie:", self.field_settle)

        form.addRow(self._section("Nieznane ekrany"))
        self.field_save_unknown = QCheckBox("Zapisuj zrzut ekranu, którego bot nie rozpoznaje")
        self.field_save_unknown.toggled.connect(self._apply_config_fields)
        form.addRow(self.field_save_unknown)
        self.field_unknown_after = QDoubleSpinBox(); self.field_unknown_after.setRange(5, 600)
        self.field_unknown_after.setSuffix(" s"); self.field_unknown_after.setDecimals(0)
        self.field_unknown_after.valueChanged.connect(self._apply_config_fields)
        form.addRow("Po jakim czasie:", self.field_unknown_after)
        self.field_restart_unknown = QCheckBox("Potem zrestartuj grę")
        self.field_restart_unknown.setToolTip("Po tym czasie na nierozpoznanym ekranie bot zapisuje zrzut "
                                              "(jeśli włączone) i restartuje grę, zamiast czekać")
        self.field_restart_unknown.toggled.connect(self._apply_config_fields)
        form.addRow(self.field_restart_unknown)
        form.addRow(self._hint("Zrzut trafia do folderu unknown_screens obok programu, a w logu pojawia "
                               "się ostrzeżenie. Prześlij go, żeby dodać obsługę tego ekranu."))
        open_dir = QPushButton("📁 Otwórz folder unknown_screens")
        open_dir.clicked.connect(self._open_unknown_dir)
        form.addRow(open_dir)
        return w

    def _open_unknown_dir(self) -> None:
        folder = unknown_screens_dir()
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    @staticmethod
    def _section(title: str) -> QLabel:
        label = QLabel(title.upper())
        label.setObjectName("section")
        return label

    def _build_log_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(12, 4, 12, 6)
        header = QHBoxLayout()
        header.addWidget(self._section("Logi"))
        header.addStretch(1)
        self.log_autoscroll = QCheckBox("Przewijaj automatycznie")
        self.log_autoscroll.setChecked(True)
        header.addWidget(self.log_autoscroll)
        clear = QPushButton("Wyczyść")
        header.addWidget(clear)
        layout.addLayout(header)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(5000)
        self.log_view.setStyleSheet("background-color: #0e1216; color: #d6dde3; border-radius: 8px; "
                                    "font-family: Consolas, 'DejaVu Sans Mono', monospace; font-size: 11px;")
        clear.clicked.connect(self.log_view.clear)
        layout.addWidget(self.log_view)
        return panel

    # ------------------------------------------------------------------ #
    # Strona "Log akcji"
    # ------------------------------------------------------------------ #
    def _build_action_log_page(self) -> QWidget:
        page = QWidget()
        root = QVBoxLayout(page)
        root.setContentsMargins(16, 14, 16, 8)
        root.setSpacing(10)

        title = QLabel("Log akcji")
        title.setObjectName("pageTitle")
        root.addWidget(title)
        root.addWidget(self._hint(
            "Każde tapnięcie, przeciągnięcie, swipe i przytrzymanie bota trafia do pliku CSV: czas, tryb, "
            "czynność, współrzędne (piksele urządzenia), cały tor przeciągnięcia, czas trwania i przytrzymanie. "
            "Jeden plik na instancję i dzień w folderze action_logs obok programu."))

        bar_box = QFrame()
        bar_box.setObjectName("panel")
        bar = QHBoxLayout(bar_box)
        bar.setContentsMargins(12, 8, 12, 8)
        self.field_action_log = QCheckBox("Zapisuj log akcji (wszystkie instancje)")
        self.field_action_log.toggled.connect(self._on_action_log_toggled)
        bar.addWidget(self.field_action_log)
        bar.addSpacing(16)
        bar.addWidget(QLabel("Instancja"))
        self.action_log_instance = QComboBox()
        self.action_log_instance.setMinimumWidth(160)
        self.action_log_instance.currentIndexChanged.connect(lambda _i: self._refresh_action_log())
        bar.addWidget(self.action_log_instance)
        refresh = QPushButton("⟳ Odśwież")
        refresh.clicked.connect(self._refresh_action_log)
        bar.addWidget(refresh)
        open_dir = QPushButton("📁 Otwórz folder")
        open_dir.clicked.connect(self._open_action_log_dir)
        bar.addWidget(open_dir)
        bar.addStretch(1)
        root.addWidget(bar_box)

        self.action_log_summary = QLabel("")
        self.action_log_summary.setObjectName("hint")
        root.addWidget(self.action_log_summary)

        self.action_log_table = QTableWidget(0, len(_ACTION_COLUMNS))
        self.action_log_table.setHorizontalHeaderLabels([t for _k, t in _ACTION_COLUMNS])
        self.action_log_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.action_log_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.action_log_table.setAlternatingRowColors(True)
        self.action_log_table.verticalHeader().setVisible(False)
        header = self.action_log_table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeToContents)
        header.setStretchLastSection(True)
        root.addWidget(self.action_log_table, 1)

        # odświeżanie, gdy strona jest widoczna
        self._action_log_timer = QTimer(self)
        self._action_log_timer.setInterval(2000)
        self._action_log_timer.timeout.connect(
            lambda: self.main_tabs.currentIndex() == 2 and self._refresh_action_log())
        self._action_log_timer.start()
        return page

    def _sync_action_log_controls(self) -> None:
        """Przełącznik i lista instancji zgodne z wczytanym configiem."""
        enabled = any(e.config.action_log for e in self.instances.values())
        self.field_action_log.blockSignals(True)
        self.field_action_log.setChecked(enabled)
        self.field_action_log.blockSignals(False)
        current = self.action_log_instance.currentText()
        self.action_log_instance.blockSignals(True)
        self.action_log_instance.clear()
        self.action_log_instance.addItems(list(self.instances))
        if current in self.instances:
            self.action_log_instance.setCurrentText(current)
        self.action_log_instance.blockSignals(False)

    def _on_action_log_toggled(self, on: bool) -> None:
        for e in self.instances.values():
            e.config.action_log = on  # działające boty biorą to od razu
        self._mark_dirty()
        self._log_local(f"Log akcji {'włączony' if on else 'wyłączony'} na {len(self.instances)} instancjach")
        self._refresh_action_log()

    def _open_action_log_dir(self) -> None:
        folder = action_log.action_logs_dir()
        folder.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _refresh_action_log(self) -> None:
        if self.action_log_instance.count() != len(self.instances):
            self._sync_action_log_controls()
        name = self.action_log_instance.currentText()
        table = self.action_log_table
        if not name:
            table.setRowCount(0)
            self.action_log_summary.setText("Brak instancji.")
            return
        path = action_log.log_path(name)
        rows = action_log.read_tail(path, 300)
        rows.reverse()  # najnowsze na górze
        table.setUpdatesEnabled(False)
        table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, (key, _title) in enumerate(_ACTION_COLUMNS):
                value = row.get(key, "")
                if key == "time":
                    value = value[11:23]  # tylko godzina z milisekundami
                table.setItem(r, c, QTableWidgetItem(value))
        table.setUpdatesEnabled(True)
        if rows:
            counts: dict[str, int] = {}
            for row in rows:
                counts[row.get("action", "?")] = counts.get(row.get("action", "?"), 0) + 1
            parts = ", ".join(f"{k} {v}" for k, v in sorted(counts.items()))
            self.action_log_summary.setText(f"{path.name} · ostatnie {len(rows)} wpisów: {parts}")
        else:
            state = "włączony" if self.field_action_log.isChecked() else "wyłączony"
            self.action_log_summary.setText(f"Brak wpisów z dzisiaj dla „{name}” (log {state}).")

    # ------------------------------------------------------------------ #
    # Zarządzanie instancjami
    # ------------------------------------------------------------------ #
    def _add_instance_dialog(self) -> None:
        idx = len(self.instances) + 1
        name = f"instance-{idx}"
        while name in self.instances:
            idx += 1
            name = f"instance-{idx}"

        config = BotConfig(name=name)
        config.copy_shared_from(self.shared_config)
        entry = InstanceEntry(config=config, serial="")
        self._register_instance(entry)
        self._mark_dirty()
        self.tabs.setCurrentIndex(0)
        self.field_serial.setFocus()

    def _autodetect_instances(self) -> None:
        try:
            serials = list_ldplayer_devices()
        except Exception as e:
            QMessageBox.critical(self, "Błąd", f"Nie udało się uruchomić adb devices:\n{e}")
            return

        if not serials:
            QMessageBox.information(self, "Brak urządzeń",
                                     "Nie znaleziono żadnych urządzeń w `adb devices`.\n"
                                     "Upewnij się, że ADB debugging jest włączone w LDPlayer.")
            return

        existing_serials = {e.serial for e in self.instances.values()}
        added = 0
        for serial in serials:
            if serial in existing_serials:
                continue
            idx = len(self.instances) + 1
            name = f"instance-{idx}"
            while name in self.instances:
                idx += 1
                name = f"instance-{idx}"
            config = BotConfig(name=name)
            config.copy_shared_from(self.shared_config)
            entry = InstanceEntry(config=config, serial=serial)
            self._register_instance(entry)
            added += 1
        if added:
            self._mark_dirty()

        self._log_local(f"Auto-wykrywanie: znaleziono {len(serials)} urządzeń, dodano {added} nowych.")

    def _register_instance(self, entry: InstanceEntry) -> None:
        item = QListWidgetItem()
        item.setData(Qt.UserRole, entry.config.name)
        self.instances[entry.config.name] = entry
        entry.list_item = item
        self.instance_list.addItem(item)
        self._rebuild_dashboard()
        self._refresh_list_item(entry)
        self.instance_list.setCurrentItem(item)

    def _format_list_label(self, entry: InstanceEntry) -> str:
        status_pl = tr(STATUS_LABELS_PL.get(entry.status, entry.status))
        if entry.status in ("idle", "collecting", "debris", "fishing") and entry.activity:
            status_pl = entry.activity  # dokładnie, co bot robi w tej chwili
        if entry.status in ("break", "offline") and entry.schedule_text:
            status_pl = entry.schedule_text
        elif entry.status in ("idle", "collecting", "debris", "fishing", "paused") and entry.schedule_text:
            status_pl += f" · {entry.schedule_text}"
        return f"● {entry.config.name}\n    {status_pl}"

    def _refresh_list_item(self, entry: InstanceEntry) -> None:
        if entry.list_item is None:
            return
        entry.list_item.setText(self._format_list_label(entry))
        entry.list_item.setForeground(QColor(STATUS_COLORS.get(entry.status, "#ddd")))
        entry.list_item.setToolTip(f"{entry.config.name} ({entry.serial})" + (f"\n{entry.activity}" if entry.activity else ""))
        if entry.config.name == self.selected_name:
            self.btn_pause.setText("▶ Wznów" if entry.status == "paused" else "⏸ Pauza")
        self._update_dashboard(entry)

    def _remove_selected_instance(self) -> None:
        entry = self._current_entry()
        if entry is None:
            return
        if entry.worker is not None and entry.worker.isRunning():
            QMessageBox.warning(self, "Instancja aktywna",
                                 "Zatrzymaj instancję przed usunięciem.")
            return
        row = self.instance_list.row(entry.list_item)
        self.instance_list.takeItem(row)
        del self.instances[entry.config.name]
        self._rebuild_dashboard()
        self._mark_dirty()
        if not self.instances:
            self.selected_name = None
            self._set_config_form_enabled(False)

    def _current_entry(self) -> InstanceEntry | None:
        if self.selected_name is None:
            return None
        return self.instances.get(self.selected_name)

    # ------------------------------------------------------------------ #
    @Slot(QListWidgetItem, QListWidgetItem)
    def _on_selection_changed(self, current: QListWidgetItem, previous: QListWidgetItem) -> None:
        if current is None:
            self.selected_name = None
            self._set_config_form_enabled(False)
            return
        name = current.data(Qt.UserRole)
        self.selected_name = name
        entry = self.instances[name]

        self.preview_title.setText(f"<b>{tr('Podgląd')}</b> - {name}")
        self._load_fields_from_entry(entry)
        self._set_config_form_enabled(True)

        if entry.last_frame is not None:
            self.preview.update_frame(entry.last_frame, entry.last_det, entry.last_scale)
        self.preview.set_known_config(self._shown_idle_point(entry.config), entry.config.board_region,
                                      entry.config.idle_point_radius)
        self._update_preview_points()

    def _config_widgets(self) -> tuple:
        return (self.field_name, self.field_serial, self.field_game_package, self.field_game_activity,
                self.field_idle_just_loot, self.field_just_loot_radius, self.field_drop_choice,
                self.field_idle_x, self.field_idle_y, self.field_idle_radius,
                self.field_region_x1, self.field_region_y1, self.field_region_x2, self.field_region_y2,
                self.field_idle_interval_min, self.field_idle_interval_max,
                self.field_drag_ms_min, self.field_drag_ms_max, self.field_hold_end_min, self.field_hold_end_max,
                self.field_between_drags_min, self.field_between_drags_max, self.field_human_drag,
                self.field_grab_radius, self.field_release_radius, self.field_drag_jitter,
                self.field_drag_points_custom, self.field_drag_points_min, self.field_drag_points_max,
                self.field_reaction_min, self.field_reaction_max, self.field_hold_min, self.field_hold_max,
                self.field_mistake, self.field_proc_width, self.field_capture, self.field_turbo,
                self.field_play_debris, self.field_tile_min, self.field_tile_max, self.field_tile_by_distance,
                self.field_skip_fishing, self.field_fishing_aim,
                self.field_depth_mode, self.field_farm_depth, self.field_depth_pct, self.field_depth_interval,
                self.field_loadout, self.field_depth_repeat, self.field_depth_push,
                self.field_auto_packs, self.field_packs_interval, self.field_packs_defeats, self.field_packs_rounds,
                self.field_packs_per_run, self.field_gear_margin,
                self.field_salvage_full, self.field_salvage_items, self.field_salvage_hold_min,
                self.field_salvage_hold_max,
                self.field_black_timeout, self.field_fg_lost, self.field_settle,
                self.field_idle_fallback, self.field_save_unknown, self.field_unknown_after,
                self.field_restart_unknown, self.field_speed_board, self.field_speed_react, self.field_speed_tap,
                *[pw[1] for pw in self.point_widgets.values()])

    def _load_fields_from_entry(self, entry: InstanceEntry | None) -> None:
        cfg = entry.config if entry is not None else self.shared_config
        # Blokujemy sygnały na czas wypełniania pól - inaczej samo ustawienie
        # wartości wywołuje _apply_config_fields i nadpisuje config wartościami
        # z formularza, zanim zdążymy wczytać prawdziwe dane.
        fields = self._config_widgets()
        for w in fields:
            w.blockSignals(True)
        try:
            if entry is not None:
                self.field_name.setText(cfg.name)
                self.field_serial.setText(entry.serial)
                self.field_game_package.setText(cfg.game_package or "")
                self.field_game_package.setCursorPosition(0)
                self.field_game_activity.setText(cfg.game_activity or "")
                self.field_depth_mode.setCurrentIndex(max(0, self.field_depth_mode.findData(cfg.depth_mode)))
                self.field_farm_depth.setValue(max(0, cfg.farm_depth))
                self.field_depth_pct.setValue(int(round(cfg.depth_below_max_pct)))
            else:
                for field in (self.field_name, self.field_serial, self.field_game_package, self.field_game_activity):
                    field.clear()
            self.field_idle_just_loot.setChecked(cfg.idle_target == "just_loot")
            self.field_drop_choice.setCurrentIndex(max(0, self.field_drop_choice.findData(cfg.drop_choice)))
            self.field_idle_x.setValue(cfg.idle_point[0])
            self.field_idle_y.setValue(cfg.idle_point[1])
            self.field_idle_radius.setValue(cfg.idle_point_radius)
            region = cfg.board_region or (0, 0, 0, 0)
            for f, v in zip((self.field_region_x1, self.field_region_y1,
                             self.field_region_x2, self.field_region_y2), region):
                f.setValue(v)
            # konfig trzyma sekundy, GUI pokazuje milisekundy
            self.field_idle_interval_min.setValue(cfg.idle_click_interval.min * 1000)
            self.field_idle_interval_max.setValue(cfg.idle_click_interval.max * 1000)
            self.field_drag_ms_min.setValue(cfg.drag_duration_ms.min)
            self.field_drag_ms_max.setValue(cfg.drag_duration_ms.max)
            self.field_hold_end_min.setValue(cfg.drag_end_hold_ms.min)
            self.field_hold_end_max.setValue(cfg.drag_end_hold_ms.max)
            self.field_between_drags_min.setValue(cfg.between_drags_delay.min)
            self.field_between_drags_max.setValue(cfg.between_drags_delay.max)
            self.field_human_drag.setChecked(cfg.drag_style == "human")
            self.field_just_loot_radius.setValue(cfg.just_loot_click_radius * 100)
            self.field_grab_radius.setValue(cfg.drag_grab_radius * 100)
            self.field_release_radius.setValue(cfg.drag_release_radius * 100)
            self.field_drag_jitter.setValue(cfg.drag_jitter_px)
            self.field_drag_points_custom.setChecked(cfg.drag_points is not None)
            pts = cfg.drag_points or RandomRange(4, 7)
            self.field_drag_points_min.setValue(pts.min)
            self.field_drag_points_max.setValue(pts.max)
            self.field_reaction_min.setValue(cfg.reaction_delay.min)
            self.field_reaction_max.setValue(cfg.reaction_delay.max)
            self.field_hold_min.setValue(cfg.tap_hold_ms.min)
            self.field_hold_max.setValue(cfg.tap_hold_ms.max)
            self.field_mistake.setValue(cfg.mistake_chance * 100)
            self.field_proc_width.setValue(max(0, cfg.processing_width))
            self.field_capture.setCurrentIndex(max(0, self.field_capture.findData(cfg.capture_method)))
            self.field_play_debris.setChecked(cfg.play_debris)
            # konfig trzyma sekundy, GUI pokazuje milisekundy
            self.field_tile_min.setValue(cfg.tile_tap_delay.min * 1000)
            self.field_tile_max.setValue(cfg.tile_tap_delay.max * 1000)
            self.field_tile_by_distance.setChecked(cfg.tile_delay_by_distance)
            self.field_skip_fishing.setChecked(cfg.skip_fishing)
            self.field_fishing_aim.setCurrentIndex(0 if cfg.fishing_aim else 1)
            self.field_loadout.setText(cfg.loadout_name)
            self.field_depth_repeat.setValue(max(2, cfg.depth_repeat_fights))
            self.field_depth_push.setValue(max(0, cfg.depth_push_taps))
            self.field_depth_interval.setValue(max(1, int(cfg.depth_check_interval_min)))
            self.field_auto_packs.setChecked(cfg.auto_packs)
            self.field_turbo.setChecked(bool(cfg.turbo))
            self.field_packs_interval.setValue(int(cfg.packs_check_interval_min))
            self.field_packs_defeats.setValue(cfg.packs_defeat_streak)
            self.field_packs_rounds.setValue(cfg.packs_max_rounds)
            self.field_packs_per_run.setValue(max(0, cfg.packs_per_run))
            self.field_gear_margin.setValue(max(0, cfg.gear_depth_margin))
            self.field_salvage_full.setChecked(cfg.salvage_when_full)
            self.field_salvage_items.setValue(cfg.salvage_items)
            self.field_salvage_hold_min.setValue(cfg.salvage_hold_ms.min)
            self.field_salvage_hold_max.setValue(cfg.salvage_hold_ms.max)
            self.field_black_timeout.setValue(cfg.black_screen_timeout)
            self.field_fg_lost.setValue(cfg.foreground_lost_timeout)
            self.field_settle.setValue(cfg.restart_settle_delay)
            self.field_idle_fallback.setChecked(cfg.idle_fallback_enabled)
            self.field_save_unknown.setChecked(cfg.save_unknown_screens)
            self.field_unknown_after.setValue(cfg.unknown_screen_after_s)
            self.field_restart_unknown.setChecked(cfg.restart_on_unknown)
            self.field_speed_board.setValue(cfg.speed_board_pct)
            self.field_speed_react.setValue(cfg.speed_react_pct)
            self.field_speed_tap.setValue(cfg.speed_tap_pct)
            for slider in (self.field_speed_board, self.field_speed_react, self.field_speed_tap):
                slider.speed_label.setText(self.speed_text(slider.value()))
            for key, (value, radius, _pick, _reset) in self.point_widgets.items():
                x, y, r = getattr(cfg, key)
                value.setText(f"x {x * 100:.1f}% · y {y * 100:.1f}% {tr('ekranu')}"
                              + (f"  {tr('(domyślny)')}" if (x, y, r) == POINT_DEFAULTS[key] else ""))
                radius.setValue(r * 100)
        finally:
            for w in fields:
                w.blockSignals(False)
        self._update_dependent_enabled()
        if cfg.board_region is not None:
            self.btn_advanced.setChecked(True)  # pokaż, że obszar jest ustawiony
        if entry is not None:
            self.schedule_summary_label.setText(self._schedule_summary_text(cfg))
            self._show_stats(entry.last_stats)
        else:
            self.schedule_summary_label.setText("—")

    def _show_stats(self, stats: BotStats | None) -> None:
        for key, _title in _STAT_FIELDS:
            self.stat_labels[key].setText(str(getattr(stats, key, 0) if stats is not None else 0))
        self.perf_label.setText(self.perf_text(stats, long=True))

    def _update_dependent_enabled(self) -> None:
        """Wyszarza pola, które przy obecnych ustawieniach nic nie robią."""
        has_entry = self._current_entry() is not None
        packs = self.field_auto_packs.isChecked()
        for w in (self.field_packs_interval, self.field_packs_defeats, self.field_packs_rounds,
                  self.field_packs_per_run, self.field_gear_margin):
            w.setEnabled(packs)
        self.field_packs_rounds.setEnabled(packs and self.field_packs_per_run.value() == 0)
        fixed = self.field_depth_mode.currentData() in (None, "fixed")
        self.field_farm_depth.setEnabled(has_entry and fixed)
        self.field_depth_pct.setEnabled(has_entry and self.field_depth_mode.currentData() == "max_pct")
        self.field_fishing_aim.setEnabled(self.field_skip_fishing.isChecked())
        self.field_depth_repeat.setEnabled(self.field_depth_mode.currentData() in ("max", "max_normal"))
        mode = self.field_depth_mode.currentData()
        self.field_depth_push.setEnabled(mode != "max_pct"
                                         and not (mode in (None, "fixed") and self.field_farm_depth.value() > 0))
        self.field_depth_interval.setEnabled(not fixed or self.field_farm_depth.value() > 0
                                             or bool(self.field_loadout.text().strip()))
        fallback = self.field_idle_fallback.isChecked() or not self.field_idle_just_loot.isChecked()
        self.idle_point_box.setVisible(fallback)
        self.field_unknown_after.setEnabled(self.field_save_unknown.isChecked()
                                            or self.field_restart_unknown.isChecked())
        salvage = self.field_salvage_full.isChecked()
        for w in (self.field_salvage_items, self.field_salvage_hold_min, self.field_salvage_hold_max):
            w.setEnabled(salvage)
        protection = bool(self.field_game_package.text().strip())
        for w in (self.field_black_timeout, self.field_fg_lost, self.field_settle):
            w.setEnabled(protection)

    def _schedule_summary_text(self, cfg: BotConfig) -> str:
        schedule = cfg.schedule
        if not schedule:
            text = "Brak okien – bot gra bez ograniczeń godzinowych."
        else:
            text = "\n".join("• " + self._format_window(w) for w in schedule)
        if cfg.daily_limit_hours is not None:
            lim = cfg.daily_limit_hours
            text += f"\nLimit dzienny: {lim.min:g}–{lim.max:g} h"
        text += f"\nZiarno planu: {cfg.schedule_seed}"
        text += "\nZamykanie gry na przerwach: " + ("tak" if cfg.close_game_during_break else "nie")
        return text

    def _format_window(self, w: ScheduleWindow) -> str:
        text = f"{w.start}–{w.end}"
        text += " (" + (",".join(_DAY_LABELS[d] for d in sorted(w.days)) if w.days is not None else "codziennie") + ")"
        if w.session_minutes is not None:
            s = w.session_minutes
            text += f", sesje {s.min:.0f}–{s.max:.0f} min"
        return text

    def _open_schedule_editor(self) -> None:  # noqa: C901
        entry = self._current_entry()
        if entry is None:
            return
        dialog = ScheduleEditorDialog(
            entry.config.schedule,
            close_game_during_break=entry.config.close_game_during_break,
            daily_limit_hours=entry.config.daily_limit_hours,
            schedule_seed=entry.config.schedule_seed,
            parent=self,
        )
        if dialog.exec() == QDialog.Accepted:
            entry.config.schedule = dialog.result_windows()
            entry.config.schedule_seed = dialog.result_schedule_seed()
            entry.config.close_game_during_break = dialog.result_close_game_during_break()
            entry.config.daily_limit_hours = dialog.result_daily_limit_hours()
            self.schedule_summary_label.setText(self._schedule_summary_text(entry.config))
            self._mark_dirty()
            self._log_local(f"[{entry.config.name}] zaktualizowano harmonogram")

    def _entry_widgets(self) -> tuple:
        """Pola i przyciski, które działają tylko na wybranej instancji."""
        point_buttons = tuple(b for pw in self.point_widgets.values() for b in pw[2:])
        return (self.field_name, self.field_serial, self.field_game_package, self.field_game_activity,
                self.field_depth_mode, self.field_farm_depth, self.field_depth_pct,
                *point_buttons, self.btn_edit_schedule, self.btn_edit_idle, self.btn_edit_region,
                self.btn_clear_region, self.btn_refresh_preview, self.btn_start,
                self.btn_pause, self.btn_stop, self.btn_restart_app, self.btn_salvage_now)

    def _set_config_form_enabled(self, enabled: bool) -> None:
        for w in self._entry_widgets():
            w.setEnabled(enabled)
        if not enabled:
            self.schedule_summary_label.setText("—")
            self.preview_title.setText(tr("<b>Podgląd</b> – wybierz instancję"))
            self._show_stats(None)
        self._update_dependent_enabled()

    def _apply_config_fields(self) -> None:
        entry = self._current_entry()
        # bez zaznaczonej instancji zmiany idą do ustawień wspólnych
        cfg = entry.config if entry is not None else self.shared_config
        if entry is not None:
            new_name = self.field_name.text().strip()
            if new_name and new_name != cfg.name:
                self._rename_entry(entry, new_name)
            cfg.game_package = self.field_game_package.text().strip() or None
            cfg.game_activity = self.field_game_activity.text().strip() or None
            cfg.depth_mode = self.field_depth_mode.currentData() or "fixed"
            cfg.farm_depth = self.field_farm_depth.value()
            cfg.depth_below_max_pct = float(self.field_depth_pct.value())
        cfg.idle_target = "just_loot" if self.field_idle_just_loot.isChecked() else "fixed"
        cfg.drop_choice = self.field_drop_choice.currentData() or "keep"
        cfg.idle_point = (self.field_idle_x.value(), self.field_idle_y.value())
        cfg.idle_point_radius = self.field_idle_radius.value()
        region = (self.field_region_x1.value(), self.field_region_y1.value(),
                  self.field_region_x2.value(), self.field_region_y2.value())
        # niepełny / odwrócony prostokąt = cały ekran
        cfg.board_region = region if region[2] > region[0] and region[3] > region[1] else None
        # "do" mniejsze niż "od" przycinamy do "od"
        lo_ms = self.field_idle_interval_min.value()
        cfg.idle_click_interval = RandomRange(
            lo_ms / 1000, max(lo_ms, self.field_idle_interval_max.value()) / 1000)
        cfg.drag_duration_ms = RandomRange(
            self.field_drag_ms_min.value(), max(self.field_drag_ms_min.value(), self.field_drag_ms_max.value()))
        cfg.drag_end_hold_ms = RandomRange(
            self.field_hold_end_min.value(), max(self.field_hold_end_min.value(), self.field_hold_end_max.value()))
        cfg.between_drags_delay = RandomRange(
            self.field_between_drags_min.value(),
            max(self.field_between_drags_min.value(), self.field_between_drags_max.value()))
        cfg.drag_style = "human" if self.field_human_drag.isChecked() else "straight"
        cfg.just_loot_click_radius = self.field_just_loot_radius.value() / 100.0
        cfg.drag_grab_radius = self.field_grab_radius.value() / 100.0
        cfg.drag_release_radius = self.field_release_radius.value() / 100.0
        cfg.drag_jitter_px = self.field_drag_jitter.value()
        custom = self.field_drag_points_custom.isChecked()
        self.field_drag_points_min.setEnabled(custom)
        self.field_drag_points_max.setEnabled(custom)
        if custom:
            lo = self.field_drag_points_min.value()
            cfg.drag_points = RandomRange(lo, max(lo, self.field_drag_points_max.value()))
        else:
            cfg.drag_points = None
        cfg.reaction_delay = RandomRange(
            self.field_reaction_min.value(), max(self.field_reaction_min.value(), self.field_reaction_max.value()))
        cfg.tap_hold_ms = RandomRange(
            self.field_hold_min.value(), max(self.field_hold_min.value(), self.field_hold_max.value()))
        cfg.mistake_chance = self.field_mistake.value() / 100.0
        cfg.processing_width = self.field_proc_width.value()
        cfg.capture_method = self.field_capture.currentData() or "scrcpy"
        cfg.play_debris = self.field_play_debris.isChecked()
        tile_lo = self.field_tile_min.value()
        cfg.tile_tap_delay = RandomRange(tile_lo / 1000, max(tile_lo, self.field_tile_max.value()) / 1000)
        cfg.tile_delay_by_distance = self.field_tile_by_distance.isChecked()
        cfg.skip_fishing = self.field_skip_fishing.isChecked()
        cfg.fishing_aim = bool(self.field_fishing_aim.currentData())
        cfg.loadout_name = self.field_loadout.text().strip().upper()
        cfg.depth_repeat_fights = self.field_depth_repeat.value()
        cfg.depth_push_taps = self.field_depth_push.value()
        cfg.depth_check_interval_min = float(self.field_depth_interval.value())
        cfg.auto_packs = self.field_auto_packs.isChecked()
        cfg.turbo = self.field_turbo.isChecked()
        cfg.packs_check_interval_min = float(self.field_packs_interval.value())
        cfg.packs_defeat_streak = self.field_packs_defeats.value()
        cfg.packs_max_rounds = self.field_packs_rounds.value()
        cfg.packs_per_run = self.field_packs_per_run.value()
        cfg.gear_depth_margin = self.field_gear_margin.value()
        cfg.salvage_when_full = self.field_salvage_full.isChecked()
        cfg.salvage_items = self.field_salvage_items.value()
        cfg.salvage_hold_ms = RandomRange(self.field_salvage_hold_min.value(),
                                          max(self.field_salvage_hold_min.value(), self.field_salvage_hold_max.value()))
        cfg.black_screen_timeout = self.field_black_timeout.value()
        cfg.foreground_lost_timeout = self.field_fg_lost.value()
        cfg.restart_settle_delay = self.field_settle.value()
        cfg.idle_fallback_enabled = self.field_idle_fallback.isChecked()
        cfg.save_unknown_screens = self.field_save_unknown.isChecked()
        cfg.unknown_screen_after_s = self.field_unknown_after.value()
        cfg.restart_on_unknown = self.field_restart_unknown.isChecked()
        cfg.speed_board_pct = self.field_speed_board.value()
        cfg.speed_react_pct = self.field_speed_react.value()
        cfg.speed_tap_pct = self.field_speed_tap.value()
        for key, (_value, radius, _pick, _reset) in self.point_widgets.items():
            x, y, _r = getattr(cfg, key)
            setattr(cfg, key, (x, y, radius.value() / 100.0))
        if entry is not None:
            new_serial = self.field_serial.text().strip()
            if new_serial != entry.serial:
                entry.serial = new_serial
                self._rebuild_dashboard()
        self._sync_shared(cfg)

        self._update_dependent_enabled()
        self._mark_dirty()
        # Działający bot trzyma ten sam obiekt BotConfig, więc zmiany
        # działają od razu, bez restartu wątku.
        self.preview.set_known_config(self._shown_idle_point(cfg), cfg.board_region, cfg.idle_point_radius)
        self._update_preview_points()

    @staticmethod
    def _shown_idle_point(cfg: BotConfig):
        """Punkt awaryjny na podglądzie tylko wtedy, gdy bot go używa."""
        return cfg.idle_point if (cfg.idle_fallback_enabled or cfg.idle_target == "fixed") else None

    def _sync_shared(self, source: BotConfig) -> None:
        """Wspólne ustawienia (wszystko poza nazwą, urządzeniem, pakietem,
        harmonogramem i głębokością farmy) z `source` do ustawień wspólnych
        i na wszystkie instancje. Działające boty trzymają te same obiekty
        configu, więc zmiana działa od razu."""
        if source is not self.shared_config:
            self.shared_config.copy_shared_from(source)
        for entry in self.instances.values():
            if entry.config is not source:
                entry.config.copy_shared_from(source)

    def _update_preview_points(self) -> None:
        entry = self._current_entry()
        on_points_tab = (self.shared_dialog.isVisible()
                         and self.shared_tabs.tabText(self.shared_tabs.currentIndex()) == "Punkty")
        if entry is None or not on_points_tab:
            self.preview.set_named_points([])
            return
        pts = []
        for key, label, color in _POINTS:
            x, y, r = getattr(entry.config, key)
            pts.append((label.split(" (")[0], x, y, r, color))
        self.preview.set_named_points(pts)

    def _pick_point(self, key: str) -> None:
        entry = self._current_entry()
        if entry is None:
            return
        if entry.last_frame is None:
            QMessageBox.information(self, "Brak podglądu",
                                    "Podgląd nie ma jeszcze obrazu - uruchom bota albo kliknij "
                                    "„Odśwież podgląd”, a potem wskaż punkt.")
            return
        label = next(lbl for k, lbl, _c in _POINTS if k == key)
        self.preview.pick_named_point(key)
        self.statusBar().showMessage(f"Kliknij na podglądzie: {label}", 15000)

    def _on_named_point_picked(self, key: str, x: int, y: int, w: int, h: int) -> None:
        entry = self._current_entry()
        if entry is None or w <= 0 or h <= 0:
            return
        r = getattr(entry.config, key)[2]
        setattr(entry.config, key, (x / w, y / h, r))
        self._sync_shared(entry.config)
        self._mark_dirty()
        self._load_fields_from_entry(entry)
        self._update_preview_points()
        label = next(lbl for k, lbl, _c in _POINTS if k == key)
        self._log_local(f"Ustawiono punkt: {label} = ({x}, {y}) - na wszystkich instancjach")

    def _reset_point(self, key: str) -> None:
        entry = self._current_entry()
        if entry is None:
            return
        setattr(entry.config, key, POINT_DEFAULTS[key])
        self._sync_shared(entry.config)
        self._mark_dirty()
        self._load_fields_from_entry(entry)
        self._update_preview_points()

    def _clear_board_region(self) -> None:
        for f in (self.field_region_x1, self.field_region_y1, self.field_region_x2, self.field_region_y2):
            f.blockSignals(True); f.setValue(0); f.blockSignals(False)
        self._apply_config_fields()
        self._log_local("Obszar planszy: cały ekran")

    def _mark_dirty(self) -> None:
        if not self._dirty:
            self._dirty = True
            self._update_title()

    def _update_title(self) -> None:
        name = self._config_path.name if self._config_path else "nowa konfiguracja"
        self.setWindowTitle(f"Dust Mine Bot {APP_VERSION} – {name}{' *' if self._dirty else ''}")

    def _rename_entry(self, entry: InstanceEntry, new_name: str) -> None:
        old_name = entry.config.name
        if entry.worker is not None and entry.worker.isRunning():
            QMessageBox.warning(self, "Instancja aktywna",
                                 "Zatrzymaj instancję przed zmianą nazwy.")
            self.field_name.setText(old_name)
            return
        if new_name in self.instances:
            QMessageBox.warning(self, "Nazwa zajęta",
                                 f"Instancja o nazwie '{new_name}' już istnieje.")
            self.field_name.setText(old_name)
            return
        entry.config.name = new_name
        self.instances = {(new_name if k == old_name else k): v for k, v in self.instances.items()}
        self.selected_name = new_name
        entry.list_item.setData(Qt.UserRole, new_name)
        entry.list_item.setText(self._format_list_label(entry))
        self._rebuild_dashboard()
        self.preview_title.setText(f"<b>{tr('Podgląd')}</b> - {new_name}")
        self._log_local(f"Zmieniono nazwę instancji '{old_name}' -> '{new_name}'")

    # ------------------------------------------------------------------ #
    # Podgląd - edycja przez klik
    # ------------------------------------------------------------------ #
    def _toggle_edit_idle(self, checked: bool) -> None:
        if checked:
            self.btn_edit_region.setChecked(False)
            self.preview.set_edit_mode(EditMode.SET_IDLE_POINT)
        else:
            self.preview.set_edit_mode(EditMode.NONE)

    def _toggle_edit_region(self, checked: bool) -> None:
        if checked:
            self.btn_edit_idle.setChecked(False)
            self.preview.set_edit_mode(EditMode.SET_BOARD_REGION)
        else:
            self.preview.set_edit_mode(EditMode.NONE)

    def _on_idle_point_picked(self, x: int, y: int) -> None:
        self.field_idle_x.setValue(x)
        self.field_idle_y.setValue(y)
        self._apply_config_fields()
        self.btn_edit_idle.setChecked(False)
        self.preview.set_edit_mode(EditMode.NONE)
        self._log_local(f"Ustawiono idle_point = ({x}, {y})")

    def _on_board_region_picked(self, x1: int, y1: int, x2: int, y2: int) -> None:
        self.field_region_x1.setValue(x1)
        self.field_region_y1.setValue(y1)
        self.field_region_x2.setValue(x2)
        self.field_region_y2.setValue(y2)
        self._apply_config_fields()
        self.btn_edit_region.setChecked(False)
        self.preview.set_edit_mode(EditMode.NONE)
        self._log_local(f"Ustawiono board_region = ({x1}, {y1}, {x2}, {y2})")

    def _refresh_preview_once(self) -> None:
        """Pobiera jeden zrzut ekranu w tle (przydatne, gdy bot jest
        zatrzymany i nie generuje klatek automatycznie)."""
        entry = self._current_entry()
        if entry is None:
            return
        if entry.worker is not None and entry.worker.isRunning():
            self._log_local(f"[{entry.config.name}] bot działa - podgląd odświeża się automatycznie")
            return
        if entry.snapshot_worker is not None and entry.snapshot_worker.isRunning():
            return
        # parent=self: wątek żyje do końca, nawet gdy instancja zostanie
        # w międzyczasie usunięta z listy
        worker = SnapshotWorker(entry.config.name, entry.serial, entry.config.board_region, parent=self)
        worker.done.connect(self._on_snapshot_done)
        worker.failed.connect(self._on_snapshot_failed)
        entry.snapshot_worker = worker
        self.btn_refresh_preview.setEnabled(False)
        worker.finished.connect(lambda: self._on_snapshot_finished(entry, worker))
        worker.start()
        self._log_local(f"[{entry.config.name}] pobieram zrzut ekranu...")

    def _on_snapshot_finished(self, entry: InstanceEntry, worker: SnapshotWorker) -> None:
        self.btn_refresh_preview.setEnabled(True)
        if entry.snapshot_worker is worker:
            entry.snapshot_worker = None
        worker.deleteLater()

    @Slot(str, np.ndarray, object)
    def _on_snapshot_done(self, name: str, frame: np.ndarray, det: object) -> None:
        entry = self.instances.get(name)
        if entry is None:
            return
        entry.last_frame = frame
        entry.last_det = det
        entry.last_scale = 1.0  # ręczny podgląd jest w pełnej rozdzielczości
        if name == self.selected_name:
            self.preview.update_frame(frame, det, 1.0)
        self._log_local(f"[{name}] odświeżono podgląd ręcznie "
                         f"(DROP={'tak' if det.drop else 'nie'}, kulki={len(det.balls)})")

    @Slot(str, str)
    def _on_snapshot_failed(self, name: str, message: str) -> None:
        self._log_local(f"[{name}] błąd odświeżania podglądu: {message}")
        QMessageBox.critical(self, "Błąd ADB", message)

    # ------------------------------------------------------------------ #
    # Start / Stop / Pauza
    # ------------------------------------------------------------------ #
    def _start_selected(self) -> None:
        entry = self._current_entry()
        if entry is not None:
            self._start_entry(entry)

    def _start_entry(self, entry: InstanceEntry) -> None:
        if entry.worker is not None and entry.worker.isRunning():
            return
        worker = BotWorker(config=entry.config, serial=entry.serial)
        worker.frame_ready.connect(self._on_frame_ready)
        worker.log_message.connect(self._on_log_message)
        worker.status_changed.connect(self._on_status_changed)
        worker.stats_updated.connect(self._on_stats_updated)
        worker.connection_failed.connect(self._on_connection_failed)
        worker.schedule_changed.connect(self._on_schedule_changed)
        worker.activity_changed.connect(self._on_activity_changed)
        entry.activity = ""
        entry.schedule_text = ""
        entry.worker = worker
        worker.start()
        self._log_local(f"[{entry.config.name}] uruchamiam...")

    def _stop_selected(self) -> None:
        entry = self._current_entry()
        if entry is not None:
            self._stop_entry(entry)

    def _stop_entry(self, entry: InstanceEntry) -> None:
        if entry.worker is not None:
            entry.worker.request_stop()
            self._log_local(f"[{entry.config.name}] zatrzymywanie...")

    def _toggle_pause_selected(self) -> None:
        entry = self._current_entry()
        if entry is not None:
            self._set_paused(entry, entry.status != "paused")

    @staticmethod
    def _running(entry: InstanceEntry | None) -> bool:
        return entry is not None and entry.worker is not None and entry.worker.isRunning()

    def _set_paused(self, entry: InstanceEntry, paused: bool) -> None:
        if not self._running(entry):
            return
        if paused:
            entry.worker.request_pause()
            self._log_local(f"[{entry.config.name}] wstrzymano")
        else:
            entry.worker.request_resume()
            entry.status = "idle"  # do czasu następnej klatki z workera
            self._log_local(f"[{entry.config.name}] wznowiono")
        self._refresh_list_item(entry)

    def _salvage_entries(self, entries: list) -> None:
        """Wymuszone rozebranie przedmiotów na podanych (działających)
        instancjach - z pytaniem o liczbę i ostrzeżeniem."""
        entries = [e for e in entries if self._running(e)]
        if not entries:
            QMessageBox.information(self, "Bot nieaktywny",
                                    "Rozbieranie działa na uruchomionych instancjach - najpierw kliknij Start.")
            return
        default = entries[0].config.salvage_items
        count, ok = QInputDialog.getInt(
            self, "Rozbierz przedmioty",
            f"Ile przedmiotów rozebrać z GÓRY listy Inventory\n"
            f"(instancji: {len(entries)})?\n\n"
            f"Sortowanie w grze powinno stawiać najsłabsze przedmioty na górze.\n"
            f"Rozebrania nie da się cofnąć.",
            default, 1, 1000)
        if not ok:
            return
        for e in entries:
            e.worker.request_salvage(count)
            self._log_local(f"[{e.config.name}] zlecono rozebranie {count} przedmiotów - "
                            f"bot zrobi to przy najbliższym ekranie z paskiem nawigacji")

    def _on_card_action(self, name: str, action: str) -> None:
        entry = self.instances.get(name)
        if entry is None:
            return
        if action == "start":
            self._start_entry(entry)
        elif action == "stop":
            self._stop_entry(entry)
        elif action == "pause":
            self._set_paused(entry, entry.status != "paused")
        elif action == "salvage":
            self._salvage_entries([entry])
        elif action == "details":
            if entry.list_item is not None:
                self.instance_list.setCurrentItem(entry.list_item)
            self.main_tabs.setCurrentIndex(1)

    def _packs_entries(self, entries: list) -> None:
        """Ręczne otwarcie X paczek na działających instancjach."""
        running = [e for e in entries if self._running(e) and e.status != "paused"]
        if not running:
            QMessageBox.information(self, "Otwórz paczki", "Brak działających (niewstrzymanych) instancji.")
            return
        count, ok = QInputDialog.getInt(
            self, "Otwórz paczki",
            f"Ile paczek otworzyć na każdej z {len(running)} instancji?\n"
            "Bot zrobi to przy najbliższym ekranie z paskiem nawigacji (Craft → Loot Packs). "
            "Gdy zabraknie pyłu, otworzy tyle, ile się da.",
            10, 1, 1000, 1)
        if not ok:
            return
        for e in running:
            e.worker.request_packs(count)
        self._log_local(f"Zlecono otwarcie {count} paczek na {len(running)} instancjach")

    def _on_global_action(self, action: str) -> None:
        entries = list(self.instances.values())
        if action == "start_all":
            self._start_all()
        elif action == "stop_all":
            self._stop_all()
        elif action in ("pause_all", "resume_all"):
            pause = action == "pause_all"
            for e in entries:
                if (e.status == "paused") != pause:
                    self._set_paused(e, pause)
        elif action == "salvage_all":
            self._salvage_entries(entries)
        elif action == "packs_all":
            self._packs_entries(entries)
        elif action in ("turbo_on", "turbo_off"):
            on = action == "turbo_on"
            for e in entries:
                e.config.turbo = on  # działające boty biorą to od razu
            if self.selected_name in self.instances:
                self.field_turbo.blockSignals(True)
                self.field_turbo.setChecked(on)
                self.field_turbo.blockSignals(False)
            self._mark_dirty()
            self._log_local(f"Tryb turbo {'włączony' if on else 'wyłączony'} na {len(entries)} instancjach")
        elif action == "restart_all":
            running = [e for e in entries if self._running(e) and e.config.game_package]
            if not running:
                QMessageBox.information(self, "Restart gry",
                                        "Brak działających instancji z ustawionym pakietem gry.")
                return
            if QMessageBox.question(self, "Restart gry",
                                    f"Zrestartować grę na {len(running)} instancjach?") == QMessageBox.Yes:
                for e in running:
                    e.worker.request_restart_app()
                self._log_local(f"Zlecono restart gry na {len(running)} instancjach")

    def _on_depth_all(self, mode: str, value: int) -> None:
        """Ustawienie głębokości z panelu "Wszystkie instancje" - na każdej
        instancji (działające boty biorą je od razu przy najbliższym
        ekranie z paskiem nawigacji)."""
        for e in self.instances.values():
            e.config.depth_mode = mode
            if mode == "fixed":
                e.config.farm_depth = value
            elif mode == "max_pct":
                e.config.depth_below_max_pct = float(value)
        self._mark_dirty()
        entry = self._current_entry()
        if entry is not None:
            self._load_fields_from_entry(entry)
        desc = DEPTH_MODES.get(mode, mode)
        if mode == "fixed":
            desc = f"stała {value}" if value else "bez zmian (0)"
        elif mode == "max_pct":
            desc = f"maks. minus {value}%"
        self._log_local(f"Głębokość na wszystkich instancjach: {desc} (zapisz config, żeby zachować)")

    def _update_dashboard(self, entry: InstanceEntry) -> None:
        card = self.dashboard.card(entry.config.name)
        if card is not None:
            # plakietka: krótki stan; pod nią: dokładnie, co bot robi
            state = STATUS_LABELS_PL.get(entry.status, entry.status)
            detail = self._format_list_label(entry).split("\n", 1)[-1].strip()
            if detail == state:
                detail = ""
            card.set_status(f"{state} – {detail}" if detail else state, STATUS_COLORS.get(entry.status, "#7d8792"))
            card.set_stats(entry.last_stats)
            card.set_running(self._running(entry) and entry.status not in ("stopped", "error"),
                             entry.status == "paused")
        running = sum(1 for e in self.instances.values() if self._running(e))

        def total(attr: str) -> int:
            return sum(getattr(e.last_stats, attr, 0) for e in self.instances.values() if e.last_stats)
        self.dashboard.set_summary(
            f"{tr('Działa')}: <b>{running}/{len(self.instances)}</b> &nbsp;·&nbsp; {tr('łącznie')}: "
            f"{tr('plansze')} {total('cycles')}, {tr('kulki')} {total('balls_collected')}, "
            f"{tr('porażki')} {total('defeats')}, {tr('dropy')} {total('drops')}, "
            f"{tr('rozebrane')} {total('salvaged')}, {tr('błędy')} {total('errors')}")
        totals = {k: total(k) for k, _label in DashboardWidget.KPIS if k != "running"}
        totals["running"] = f"{running}/{len(self.instances)}"
        self.dashboard.set_totals(totals)
        self.nav_running.setText(f"{tr('działa')} {running} {tr('z')} {len(self.instances)}")

    def _rebuild_dashboard(self) -> None:
        self.dashboard.rebuild([(n, e.serial) for n, e in self.instances.items()])
        for e in self.instances.values():
            self._update_dashboard(e)
            if e.last_frame is not None:
                self.dashboard.card(e.config.name).set_frame(e.last_frame)
        if not self.instances:
            self.dashboard.set_totals({"running": "0/0"})
            self.nav_running.setText("")
        i18n.translate_tree(self.dashboard)


    def _restart_app_selected(self) -> None:
        """Ręczny restart gry (force-stop + start) na wybranej instancji,
        z potwierdzeniem - to akcja destrukcyjna dla bieżącego stanu gry."""
        entry = self._current_entry()
        if entry is None:
            return
        if entry.worker is None or not entry.worker.isRunning():
            QMessageBox.information(
                self, "Bot nieaktywny",
                f"Instancja '{entry.config.name}' nie jest uruchomiona - "
                f"uruchom bota, żeby móc zrestartować grę."
            )
            return
        if not entry.config.game_package:
            QMessageBox.warning(
                self, "Brak game_package",
                f"Instancja '{entry.config.name}' nie ma ustawionego pola "
                f"'game_package' - nie wiadomo, którą aplikację zrestartować.\n\n"
                f"Wpisz game_package w panelu konfiguracji instancji."
            )
            return

        reply = QMessageBox.question(
            self, "Restart gry",
            f"Zrestartować grę na instancji '{entry.config.name}'?\n\n"
            f"Pakiet: {entry.config.game_package}\n\n"
            f"To zamknie i ponownie uruchomi aplikację - bieżący stan "
            f"gry (np. otwarty ekran) zostanie utracony.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        )
        if reply != QMessageBox.Yes:
            return

        entry.worker.request_restart_app()
        self._log_local(f"[{entry.config.name}] zgłoszono ręczny restart gry...")

    def _start_all(self) -> None:
        for entry in self.instances.values():
            self._start_entry(entry)

    def _stop_all(self) -> None:
        for entry in self.instances.values():
            self._stop_entry(entry)

    # ------------------------------------------------------------------ #
    # Sygnały z workerów
    # ------------------------------------------------------------------ #
    @Slot(str, np.ndarray, object, str)
    def _on_frame_ready(self, name: str, frame: np.ndarray, det: object, mode: str) -> None:
        entry = self.instances.get(name)
        if entry is None:
            return
        entry.last_frame = frame
        entry.last_det = det
        entry.last_scale = getattr(entry.worker, "scale", 1.0) if entry.worker is not None else 1.0
        # podgląd tylko gdy widać kartę szczegółów i najwyżej ~5 razy na sekundę
        now = time.monotonic()
        if (name == self.selected_name and self.main_tabs.currentIndex() == 1
                and now - entry.last_preview >= 0.2):
            entry.last_preview = now
            self.preview.update_frame(frame, det, entry.last_scale,
                                      trace=getattr(entry.worker, "trace", None), activity=entry.activity)
        # miniatury w przeglądzie - najwyżej 2 razy na sekundę na instancję
        if now - entry.last_thumb >= 0.5 and self.main_tabs.currentIndex() == 0:
            entry.last_thumb = now
            card = self.dashboard.card(name)
            if card is not None:
                card.set_frame(frame)

    @Slot(str, str)
    def _on_log_message(self, name: str, line: str) -> None:
        self._append_log(f"[{name}] {line}")

    def _log_local(self, text: str) -> None:
        self._append_log(f"{time.strftime('%H:%M:%S')} [GUI] {text}")
        self.statusBar().showMessage(text, 8000)

    def _append_log(self, line: str) -> None:
        bar = self.log_view.verticalScrollBar()
        self.log_view.appendPlainText(line)
        if self.log_autoscroll.isChecked():
            bar.setValue(bar.maximum())

    @Slot(str, str)
    def _on_status_changed(self, name: str, status: str) -> None:
        entry = self.instances.get(name)
        if entry is None:
            return
        entry.status = status
        self._refresh_list_item(entry)

    @Slot(str, str)
    def _on_activity_changed(self, name: str, text: str) -> None:
        entry = self.instances.get(name)
        if entry is None:
            return
        entry.activity = text
        self._refresh_list_item(entry)

    @Slot(str, str)
    def _on_schedule_changed(self, name: str, text: str) -> None:
        entry = self.instances.get(name)
        if entry is None:
            return
        entry.schedule_text = text
        self._refresh_list_item(entry)

    @Slot(str, object)
    def _on_stats_updated(self, name: str, stats: object) -> None:
        entry = self.instances.get(name)
        if entry is None:
            return
        entry.last_stats = stats
        if name == self.selected_name:
            self._show_stats(stats)
        self._update_dashboard(entry)

    @Slot(str, str)
    def _on_connection_failed(self, name: str, message: str) -> None:
        self._log_local(f"[{name}] BŁĄD POŁĄCZENIA: {message}")
        QMessageBox.critical(self, "Błąd połączenia ADB",
                              f"Nie udało się połączyć z instancją '{name}':\n{message}")

    # ------------------------------------------------------------------ #
    # Config JSON
    # ------------------------------------------------------------------ #
    def _load_config_dialog(self) -> None:
        start_dir = str(self._config_path or default_config_path())
        path, _ = QFileDialog.getOpenFileName(self, "Wczytaj config", start_dir, "JSON (*.json)")
        if not path:
            return
        if any(e.worker is not None and e.worker.isRunning() for e in self.instances.values()):
            QMessageBox.warning(self, "Instancje aktywne",
                                 "Zatrzymaj wszystkie instancje przed wczytaniem innego configu.")
            return
        self._load_config_file(Path(path))

    def _load_config_file(self, path: Path) -> None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            loaded = [(BotConfig.from_dict(inst), inst["serial"]) for inst in data.get("instances", [])]
        except Exception as e:
            QMessageBox.critical(self, "Błąd wczytywania", f"{path}:\n{e}")
            return

        names = [cfg.name for cfg, _ in loaded]
        if len(names) != len(set(names)):
            QMessageBox.critical(self, "Błąd wczytywania",
                                 f"{path}:\nNazwy instancji w configu muszą być unikalne.")
            return

        self.instance_list.clear()
        self.instances.clear()
        self.selected_name = None
        for cfg, serial in loaded:
            self._register_instance(InstanceEntry(config=cfg, serial=serial))
        if self.instances:
            self._sync_shared(next(iter(self.instances.values())).config)

        self._config_path = path
        self._dirty = False
        self._update_title()
        self._log_local(f"Wczytano config: {path} ({len(self.instances)} instancji)")
        self._sync_action_log_controls()

    def _save_config(self) -> bool:
        """Zapis do bieżącego pliku (Ctrl+S); przy nowej konfiguracji pyta o plik."""
        if self._config_path is None:
            return self._save_config_dialog()
        return self._write_config(self._config_path)

    def _save_config_dialog(self) -> bool:
        default = str(self._config_path or default_config_path())
        path, _ = QFileDialog.getSaveFileName(self, "Zapisz config", default, "JSON (*.json)")
        if not path:
            return False
        return self._write_config(Path(path))

    def _write_config(self, path: Path) -> bool:
        data = {"instances": [
            {"serial": entry.serial, **entry.config.to_dict()}
            for entry in self.instances.values()
        ]}
        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            QMessageBox.critical(self, "Błąd zapisu", str(e))
            return False
        self._config_path = path
        self._dirty = False
        self._update_title()
        self._log_local(f"Zapisano config: {path}")
        return True

    # ------------------------------------------------------------------ #
    def closeEvent(self, event) -> None:
        if self._dirty:
            reply = QMessageBox.question(
                self, "Niezapisane zmiany", "Zapisać zmiany w konfiguracji przed zamknięciem?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Save)
            if reply == QMessageBox.Cancel or (reply == QMessageBox.Save and not self._save_config()):
                event.ignore()
                return
        threads = []
        for entry in self.instances.values():
            if entry.worker is not None and entry.worker.isRunning():
                entry.worker.request_stop()
                threads.append(entry.worker)
            if entry.snapshot_worker is not None and entry.snapshot_worker.isRunning():
                threads.append(entry.snapshot_worker)
        # Bot przerywa swoje pauzy natychmiast po stop(); najdłużej może
        # trwać bieżąca komenda ADB (timeout 10 s), stąd wspólny limit 15 s.
        deadline = time.monotonic() + 15.0
        for t in threads:
            remaining_ms = int(max(0.0, deadline - time.monotonic()) * 1000)
            if not t.wait(remaining_ms):
                logger.warning("Wątek nie zakończył się w czasie - zamykam mimo to")
        event.accept()


def main() -> None:
    logging.basicConfig(level=logging.INFO,
                         format="%(asctime)s [%(levelname)s] %(message)s")
    app = QApplication(sys.argv)
    theme.apply_theme(theme.saved_theme(), save=False)
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
