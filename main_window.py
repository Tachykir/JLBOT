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

import json
import logging
import sys
import time
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt, Slot, QTime
from PySide6.QtCore import QUrl
from PySide6.QtGui import QAction, QColor, QDesktopServices, QKeySequence
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QFormLayout, QGridLayout,
    QListWidget, QListWidgetItem, QPushButton, QLabel, QLineEdit, QSpinBox,
    QDoubleSpinBox, QPlainTextEdit, QSplitter, QGroupBox, QMessageBox, QFileDialog,
    QTimeEdit, QCheckBox, QDialog, QDialogButtonBox, QScrollArea, QFrame, QTabWidget, QComboBox,
    QInputDialog,
)

from bot import (BotConfig, BotStats, RandomRange, ScheduleWindow, POINT_DEFAULTS, DROP_CHOICES, DEPTH_MODES,
                 unknown_screens_dir)
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

STATUS_COLORS = {
    "connecting": "#f0ad4e",
    "idle": "#5bc0de",
    "collecting": "#5cb85c",
    "debris": "#e0a040",
    "fishing": "#40c8e0",
    "paused": "#aaaaaa",
    "break": "#b58bd6",
    "offline": "#8a7f6a",
    "stopped": "#777777",
    "error": "#d9534f",
}

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
    ("debris_games", "Depth Collapse"), ("fishing_skipped", "Fishing pominięte"), ("defeats", "Porażki"),
    ("pack_rounds", "Rundy paczek"), ("drops", "Dropy po walce"), ("salvaged", "Rozebrane"),
    ("restarts", "Restarty gry"), ("errors", "Błędy"),
]

STATUS_LABELS_PL = {
    "connecting": "łączenie...",
    "idle": "czekanie",
    "collecting": "zbieranie",
    "debris": "Depth Collapse",
    "fishing": "Fishing Hole",
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
        self.schedule_text: str = ""
        self.list_item: QListWidgetItem | None = None
        self.snapshot_worker: SnapshotWorker | None = None


_DAY_LABELS = ["Pon", "Wt", "Śr", "Czw", "Pt", "Sob", "Nd"]


class ScheduleEditorDialog(QDialog):
    """
    Edytor harmonogramu pracy instancji - lista okien czasowych, w
    których bot może grać. Każde okno ma własne ustawienia sesji: bot
    gra przez losowy czas, robi losową przerwę i tak do końca okna, a
    granice okna są losowo rozmyte. Do tego opcjonalny limit dzienny.
    Pusta lista okien = bot gra bez ograniczeń godzinowych.
    """

    def __init__(self, windows: list[ScheduleWindow], close_game_during_break: bool = True,
                 daily_limit_hours: RandomRange | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Harmonogram pracy")
        self.resize(680, 560)
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

        self.check_close_game = QCheckBox("Zamykaj grę (force-stop) na przerwach, poza oknami i po limicie dziennym")
        self.check_close_game.setChecked(close_game_during_break)
        self.check_close_game.setToolTip(
            "Gdy zaznaczone: gra jest zamykana na początku przerwy i\n"
            "uruchamiana ponownie na start kolejnej sesji.\n"
            "Wymaga ustawionego game_package."
        )
        root.addWidget(self.check_close_game)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._refresh_list()

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

    def _remove_window(self) -> None:
        row = self.list_widget.currentRow()
        if row < 0 or row >= len(self._windows):
            return
        del self._windows[row]
        self._refresh_list(select_row=min(row, len(self._windows) - 1))

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

    def result_windows(self) -> list[ScheduleWindow]:
        return self._windows

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
        label.setStyleSheet("color: #888; font-size: 11px;")
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

    def _build_ui(self) -> None:
        self._build_toolbar()

        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(6, 6, 6, 6)

        vsplit = QSplitter(Qt.Vertical)
        root.addWidget(vsplit)

        top = QSplitter(Qt.Horizontal)
        top.addWidget(self._build_instances_panel())
        top.addWidget(self._build_preview_panel())
        top.addWidget(self._build_settings_panel())
        top.setStretchFactor(0, 0)
        top.setStretchFactor(1, 1)
        top.setStretchFactor(2, 0)
        top.setSizes([230, 510, 520])

        # karty: przegląd wszystkich instancji / szczegóły wybranej
        self.dashboard = DashboardWidget()
        self.dashboard.card_action.connect(self._on_card_action)
        self.dashboard.global_action.connect(self._on_global_action)
        self.dashboard.depth_all.connect(self._on_depth_all)
        self.main_tabs = QTabWidget()
        self.main_tabs.addTab(self.dashboard, "📋 Wszystkie instancje")
        self.main_tabs.addTab(top, "⚙ Szczegóły instancji")
        self.main_tabs.setCurrentIndex(0)
        # miniatury odśwież od razu po przejściu na przegląd
        self.main_tabs.currentChanged.connect(lambda i: i == 0 and self._rebuild_dashboard())
        vsplit.addWidget(self.main_tabs)
        vsplit.addWidget(self._build_log_panel())
        vsplit.setStretchFactor(0, 4)
        vsplit.setStretchFactor(1, 1)
        vsplit.setSizes([620, 160])

        self.statusBar().showMessage("Wybierz instancję z listy albo wykryj emulatory przyciskiem na pasku narzędzi.")
        self._set_config_form_enabled(False)

    def _build_toolbar(self) -> None:
        tb = self.addToolBar("Główne")
        tb.setMovable(False)
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
        action("➕ Dodaj", self._add_instance_dialog, "Dodaj pustą instancję (ADB serial ustawisz w zakładce Ogólne)")
        action("🗑 Usuń", self._remove_selected_instance, "Usuń zaznaczoną instancję")
        tb.addSeparator()
        action("▶▶ Start wszystkich", self._start_all, "Uruchom bota na wszystkich instancjach")
        action("■■ Stop wszystkich", self._stop_all, "Zatrzymaj bota na wszystkich instancjach")

    def _build_instances_panel(self) -> QWidget:
        panel = QWidget()
        panel.setMinimumWidth(200)
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(4, 4, 4, 4)

        layout.addWidget(QLabel("<b>Instancje</b>"))
        self.instance_list = QListWidget()
        self.instance_list.setWordWrap(True)
        self.instance_list.setSpacing(2)
        self.instance_list.setStyleSheet("QListWidget::item { padding: 4px; }")
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
        self.btn_restart_app = QPushButton("🔄 Restart gry")
        self.btn_restart_app.setToolTip("Zamknij i uruchom grę ponownie (wymaga pakietu gry)")
        self.btn_restart_app.clicked.connect(self._restart_app_selected)
        self.btn_salvage_now = QPushButton("🧹 Rozbierz teraz")
        self.btn_salvage_now.setToolTip("Rozbierz przedmioty z góry listy Inventory (zapyta o liczbę)")
        self.btn_salvage_now.clicked.connect(lambda: self._salvage_entries([self._current_entry()]))
        for i, b in enumerate((self.btn_start, self.btn_pause, self.btn_stop, self.btn_restart_app,
                               self.btn_salvage_now)):
            b.setMinimumHeight(30)
            grid.addWidget(b, i // 2, i % 2)
        layout.addWidget(box)
        return panel

    def _build_preview_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(4, 4, 4, 4)

        self.preview_title = QLabel("<b>Podgląd</b> – wybierz instancję")
        layout.addWidget(self.preview_title)
        legend = QLabel(
            "<span style='color:#e33'>●</span> DROP &nbsp; "
            "<span style='color:#3c3'>●</span> kulki &nbsp; "
            "<span style='color:#f90'>●</span> kafelki &nbsp; "
            "<span style='color:#0cf'>●</span> CAST"
        )
        legend.setWordWrap(True)
        legend.setStyleSheet("font-size: 11px;")
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
        layout.addWidget(QLabel("<b>Ustawienia wybranej instancji</b>"))

        self.tabs = QTabWidget()
        self.tabs.setUsesScrollButtons(False)  # wszystkie zakładki zawsze widoczne
        self.tabs.addTab(self._scroll_tab(self._tab_general()), "Ogólne")
        self.tabs.addTab(self._scroll_tab(self._tab_points()), "Punkty")
        self.tabs.addTab(self._scroll_tab(self._tab_tempo()), "Tempo")
        self.tabs.addTab(self._scroll_tab(self._tab_minigames()), "Minigry")
        self.tabs.addTab(self._scroll_tab(self._tab_packs()), "Głębokość i paczki")
        self.tabs.addTab(self._scroll_tab(self._tab_schedule()), "Plan")
        self.tabs.addTab(self._scroll_tab(self._tab_protection()), "Ochrona")
        self.tabs.setTabToolTip(1, "Obszary kliknięć: punkt awaryjny, zakładki, karty")
        self.tabs.setTabToolTip(5, "Harmonogram (osobny dla każdej instancji)")
        self.tabs.setTabToolTip(6, "Automatyczny restart gry przy problemach")
        self.tabs.currentChanged.connect(lambda _i: self._update_preview_points())
        layout.addWidget(self._hint("Ustawienia są wspólne dla wszystkich instancji - zmiana tutaj "
                                    "ustawia je na każdej. Osobne są tylko nazwa, urządzenie ADB "
                                    "i harmonogram (zakładka Plan)."))
        layout.addWidget(self.tabs, stretch=1)

        stats_box = QGroupBox("Statystyki")
        grid = QGridLayout(stats_box)
        self.stat_labels: dict[str, QLabel] = {}
        for i, (key, title) in enumerate(_STAT_FIELDS):
            name = QLabel(title + ":")
            name.setStyleSheet("color: #888;")
            value = QLabel("0")
            value.setStyleSheet("font-weight: bold;")
            grid.addWidget(name, i // 3, (i % 3) * 2)
            grid.addWidget(value, i // 3, (i % 3) * 2 + 1)
            self.stat_labels[key] = value
        layout.addWidget(stats_box)
        return panel

    def _tab_general(self) -> QWidget:
        w = QWidget()
        form = self._form()
        w.setLayout(form)

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

        form.addRow(self._section("Klikanie w trybie czekania"))
        self.field_idle_just_loot = QCheckBox("Klikaj JUST LOOT (bot sam go znajduje)")
        self.field_idle_just_loot.setToolTip(
            "Bot na każdej klatce szuka przycisku JUST LOOT, a po walce PROCEED.\n"
            "Gdy nie rozpozna ekranu, nic nie klika (chyba że włączysz punkt awaryjny w zakładce Punkty).")
        self.field_idle_just_loot.toggled.connect(self._apply_config_fields)
        form.addRow(self.field_idle_just_loot)

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

        self.field_idle_interval_min, self.field_idle_interval_max, box = self._range_row(0.1, 30.0, 0.1, " s")
        form.addRow("Odstęp między kliknięciami:", box)
        self.field_drag_ms_min, self.field_drag_ms_max, box = self._range_row(50, 5000, 50, " ms", decimals=0)
        form.addRow("Czas przeciągania kulki:", box)
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
        form.addRow(self._hint("Palec jedzie po losowej krzywej, przyspiesza i zwalnia, łapie kulkę w "
                               "losowym miejscu, a kulki zbiera w losowej kolejności. Wyłączone: "
                               "prosta linia środek–środek, kulki po kolei."))
        return w

    def _tab_minigames(self) -> QWidget:
        w = QWidget()
        form = self._form()
        w.setLayout(form)

        self.field_play_debris = QCheckBox("Depth Collapse – klikaj wszystkie kafelki")
        self.field_play_debris.toggled.connect(self._apply_config_fields)
        form.addRow(self.field_play_debris)
        form.addRow(self._hint("Brązowe kafelki „TAP” - bot klika wszystkie przed końcem czasu."))
        self.field_skip_fishing = QCheckBox("Fishing Hole – od razu CAST (pomiń minigrę)")
        self.field_skip_fishing.toggled.connect(self._apply_config_fields)
        form.addRow(self.field_skip_fishing)
        form.addRow(self._hint("Bot nie celuje wskaźnikiem, tylko od razu klika CAST, co kończy minigrę."))
        return w

    def _tab_packs(self) -> QWidget:
        w = QWidget()
        form = self._form()
        w.setLayout(form)
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
        self.field_depth_interval = QSpinBox(); self.field_depth_interval.setRange(1, 1440)
        self.field_depth_interval.setSuffix(" min")
        self.field_depth_interval.valueChanged.connect(self._apply_config_fields)
        form.addRow("Sprawdzaj głębokość co:", self.field_depth_interval)
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
                               "(zakładka Ogólne)."))
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
        label = QLabel(f"<b>{title}</b>")
        label.setContentsMargins(0, 8, 0, 0)
        return label

    def _build_log_panel(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(4, 0, 4, 0)
        header = QHBoxLayout()
        header.addWidget(QLabel("<b>Logi</b>"))
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
        self.log_view.setStyleSheet("background-color: #1a1a1a; color: #ddd; "
                                    "font-family: Consolas, 'DejaVu Sans Mono', monospace; font-size: 11px;")
        clear.clicked.connect(self.log_view.clear)
        layout.addWidget(self.log_view)
        return panel

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
        if self.instances:
            config.copy_shared_from(next(iter(self.instances.values())).config)
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
            if self.instances:
                config.copy_shared_from(next(iter(self.instances.values())).config)
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
        status_pl = STATUS_LABELS_PL.get(entry.status, entry.status)
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
        entry.list_item.setToolTip(f"{entry.config.name} ({entry.serial})")
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

        self.preview_title.setText(f"<b>Podgląd</b> - {name}")
        self._load_fields_from_entry(entry)
        self._set_config_form_enabled(True)

        if entry.last_frame is not None:
            self.preview.update_frame(entry.last_frame, entry.last_det)
        self.preview.set_known_config(self._shown_idle_point(entry.config), entry.config.board_region,
                                      entry.config.idle_point_radius)
        self._update_preview_points()

    def _config_widgets(self) -> tuple:
        return (self.field_name, self.field_serial, self.field_game_package, self.field_game_activity,
                self.field_idle_just_loot, self.field_drop_choice, self.field_idle_x, self.field_idle_y, self.field_idle_radius,
                self.field_region_x1, self.field_region_y1, self.field_region_x2, self.field_region_y2,
                self.field_idle_interval_min, self.field_idle_interval_max,
                self.field_drag_ms_min, self.field_drag_ms_max,
                self.field_between_drags_min, self.field_between_drags_max, self.field_human_drag,
                self.field_reaction_min, self.field_reaction_max, self.field_hold_min, self.field_hold_max,
                self.field_mistake,
                self.field_play_debris, self.field_skip_fishing,
                self.field_depth_mode, self.field_farm_depth, self.field_depth_pct, self.field_depth_interval,
                self.field_auto_packs, self.field_packs_interval, self.field_packs_defeats, self.field_packs_rounds,
                self.field_salvage_full, self.field_salvage_items, self.field_salvage_hold_min,
                self.field_salvage_hold_max,
                self.field_black_timeout, self.field_fg_lost, self.field_settle,
                self.field_idle_fallback, self.field_save_unknown, self.field_unknown_after,
                *[pw[1] for pw in self.point_widgets.values()])

    def _load_fields_from_entry(self, entry: InstanceEntry) -> None:
        cfg = entry.config
        # Blokujemy sygnały na czas wypełniania pól - inaczej samo ustawienie
        # wartości wywołuje _apply_config_fields i nadpisuje config wartościami
        # z formularza, zanim zdążymy wczytać prawdziwe dane.
        fields = self._config_widgets()
        for w in fields:
            w.blockSignals(True)
        try:
            self.field_name.setText(cfg.name)
            self.field_serial.setText(entry.serial)
            self.field_game_package.setText(cfg.game_package or "")
            self.field_game_package.setCursorPosition(0)
            self.field_game_activity.setText(cfg.game_activity or "")
            self.field_idle_just_loot.setChecked(cfg.idle_target == "just_loot")
            self.field_drop_choice.setCurrentIndex(max(0, self.field_drop_choice.findData(cfg.drop_choice)))
            self.field_idle_x.setValue(cfg.idle_point[0])
            self.field_idle_y.setValue(cfg.idle_point[1])
            self.field_idle_radius.setValue(cfg.idle_point_radius)
            region = cfg.board_region or (0, 0, 0, 0)
            for f, v in zip((self.field_region_x1, self.field_region_y1,
                             self.field_region_x2, self.field_region_y2), region):
                f.setValue(v)
            self.field_idle_interval_min.setValue(cfg.idle_click_interval.min)
            self.field_idle_interval_max.setValue(cfg.idle_click_interval.max)
            self.field_drag_ms_min.setValue(cfg.drag_duration_ms.min)
            self.field_drag_ms_max.setValue(cfg.drag_duration_ms.max)
            self.field_between_drags_min.setValue(cfg.between_drags_delay.min)
            self.field_between_drags_max.setValue(cfg.between_drags_delay.max)
            self.field_human_drag.setChecked(cfg.drag_style == "human")
            self.field_reaction_min.setValue(cfg.reaction_delay.min)
            self.field_reaction_max.setValue(cfg.reaction_delay.max)
            self.field_hold_min.setValue(cfg.tap_hold_ms.min)
            self.field_hold_max.setValue(cfg.tap_hold_ms.max)
            self.field_mistake.setValue(cfg.mistake_chance * 100)
            self.field_play_debris.setChecked(cfg.play_debris)
            self.field_skip_fishing.setChecked(cfg.skip_fishing)
            self.field_depth_mode.setCurrentIndex(max(0, self.field_depth_mode.findData(cfg.depth_mode)))
            self.field_farm_depth.setValue(max(0, cfg.farm_depth))
            self.field_depth_pct.setValue(int(round(cfg.depth_below_max_pct)))
            self.field_depth_interval.setValue(max(1, int(cfg.depth_check_interval_min)))
            self.field_auto_packs.setChecked(cfg.auto_packs)
            self.field_packs_interval.setValue(int(cfg.packs_check_interval_min))
            self.field_packs_defeats.setValue(cfg.packs_defeat_streak)
            self.field_packs_rounds.setValue(cfg.packs_max_rounds)
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
            for key, (value, radius, _pick, _reset) in self.point_widgets.items():
                x, y, r = getattr(cfg, key)
                value.setText(f"x {x * 100:.1f}% · y {y * 100:.1f}% ekranu"
                              + ("  (domyślny)" if (x, y, r) == POINT_DEFAULTS[key] else ""))
                radius.setValue(r * 100)
        finally:
            for w in fields:
                w.blockSignals(False)
        self._update_dependent_enabled()
        if cfg.board_region is not None:
            self.btn_advanced.setChecked(True)  # pokaż, że obszar jest ustawiony
        self.schedule_summary_label.setText(self._schedule_summary_text(cfg))
        self._show_stats(entry.last_stats)

    def _show_stats(self, stats: BotStats | None) -> None:
        for key, _title in _STAT_FIELDS:
            self.stat_labels[key].setText(str(getattr(stats, key, 0) if stats is not None else 0))

    def _update_dependent_enabled(self) -> None:
        """Wyszarza pola, które przy obecnych ustawieniach nic nie robią."""
        enabled = self.field_name.isEnabled()
        packs = enabled and self.field_auto_packs.isChecked()
        for w in (self.field_packs_interval, self.field_packs_defeats, self.field_packs_rounds):
            w.setEnabled(packs)
        fixed = self.field_depth_mode.currentData() in (None, "fixed")
        self.field_farm_depth.setEnabled(enabled and fixed)
        self.field_depth_pct.setEnabled(enabled and self.field_depth_mode.currentData() == "max_pct")
        self.field_depth_interval.setEnabled(enabled and (not fixed or self.field_farm_depth.value() > 0))
        fallback = self.field_idle_fallback.isChecked() or not self.field_idle_just_loot.isChecked()
        self.idle_point_box.setVisible(fallback)
        self.field_unknown_after.setEnabled(enabled and self.field_save_unknown.isChecked())
        salvage = enabled and self.field_salvage_full.isChecked()
        for w in (self.field_salvage_items, self.field_salvage_hold_min, self.field_salvage_hold_max):
            w.setEnabled(salvage)
        protection = enabled and bool(self.field_game_package.text().strip())
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
            parent=self,
        )
        if dialog.exec() == QDialog.Accepted:
            entry.config.schedule = dialog.result_windows()
            entry.config.close_game_during_break = dialog.result_close_game_during_break()
            entry.config.daily_limit_hours = dialog.result_daily_limit_hours()
            self.schedule_summary_label.setText(self._schedule_summary_text(entry.config))
            self._mark_dirty()
            self._log_local(f"[{entry.config.name}] zaktualizowano harmonogram")

    def _set_config_form_enabled(self, enabled: bool) -> None:
        point_buttons = tuple(b for pw in self.point_widgets.values() for b in pw[2:])
        for w in self._config_widgets() + point_buttons + (self.btn_edit_schedule, self.btn_edit_idle, self.btn_edit_region,
                                           self.btn_clear_region, self.btn_refresh_preview, self.btn_start,
                                           self.btn_pause, self.btn_stop, self.btn_restart_app,
                                           self.btn_salvage_now):
            w.setEnabled(enabled)
        if not enabled:
            self.schedule_summary_label.setText("—")
            self.preview_title.setText("<b>Podgląd</b> – wybierz instancję")
            self._show_stats(None)
        self._update_dependent_enabled()

    def _apply_config_fields(self) -> None:
        entry = self._current_entry()
        if entry is None:
            return
        cfg = entry.config

        new_name = self.field_name.text().strip()
        if new_name and new_name != cfg.name:
            self._rename_entry(entry, new_name)

        cfg.game_package = self.field_game_package.text().strip() or None
        cfg.game_activity = self.field_game_activity.text().strip() or None
        cfg.idle_target = "just_loot" if self.field_idle_just_loot.isChecked() else "fixed"
        cfg.drop_choice = self.field_drop_choice.currentData() or "keep"
        cfg.idle_point = (self.field_idle_x.value(), self.field_idle_y.value())
        cfg.idle_point_radius = self.field_idle_radius.value()
        region = (self.field_region_x1.value(), self.field_region_y1.value(),
                  self.field_region_x2.value(), self.field_region_y2.value())
        # niepełny / odwrócony prostokąt = cały ekran
        cfg.board_region = region if region[2] > region[0] and region[3] > region[1] else None
        # "do" mniejsze niż "od" przycinamy do "od"
        cfg.idle_click_interval = RandomRange(
            self.field_idle_interval_min.value(),
            max(self.field_idle_interval_min.value(), self.field_idle_interval_max.value()))
        cfg.drag_duration_ms = RandomRange(
            self.field_drag_ms_min.value(), max(self.field_drag_ms_min.value(), self.field_drag_ms_max.value()))
        cfg.between_drags_delay = RandomRange(
            self.field_between_drags_min.value(),
            max(self.field_between_drags_min.value(), self.field_between_drags_max.value()))
        cfg.drag_style = "human" if self.field_human_drag.isChecked() else "straight"
        cfg.reaction_delay = RandomRange(
            self.field_reaction_min.value(), max(self.field_reaction_min.value(), self.field_reaction_max.value()))
        cfg.tap_hold_ms = RandomRange(
            self.field_hold_min.value(), max(self.field_hold_min.value(), self.field_hold_max.value()))
        cfg.mistake_chance = self.field_mistake.value() / 100.0
        cfg.play_debris = self.field_play_debris.isChecked()
        cfg.skip_fishing = self.field_skip_fishing.isChecked()
        cfg.depth_mode = self.field_depth_mode.currentData() or "fixed"
        cfg.farm_depth = self.field_farm_depth.value()
        cfg.depth_below_max_pct = float(self.field_depth_pct.value())
        cfg.depth_check_interval_min = float(self.field_depth_interval.value())
        cfg.auto_packs = self.field_auto_packs.isChecked()
        cfg.packs_check_interval_min = float(self.field_packs_interval.value())
        cfg.packs_defeat_streak = self.field_packs_defeats.value()
        cfg.packs_max_rounds = self.field_packs_rounds.value()
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
        for key, (_value, radius, _pick, _reset) in self.point_widgets.items():
            x, y, _r = getattr(cfg, key)
            setattr(cfg, key, (x, y, radius.value() / 100.0))
        new_serial = self.field_serial.text().strip()
        if new_serial != entry.serial:
            entry.serial = new_serial
            self._rebuild_dashboard()
        self._sync_shared(entry)

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

    def _sync_shared(self, source: InstanceEntry) -> None:
        """Wspólne ustawienia (wszystko poza nazwą, urządzeniem i
        harmonogramem) z `source` na pozostałe instancje. Działające boty
        trzymają te same obiekty configu, więc zmiana działa od razu."""
        for entry in self.instances.values():
            if entry is not source:
                entry.config.copy_shared_from(source.config)

    def _update_preview_points(self) -> None:
        entry = self._current_entry()
        on_points_tab = self.tabs.tabText(self.tabs.currentIndex()) == "Punkty"
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
        self._sync_shared(entry)
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
        self._sync_shared(entry)
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
        self.setWindowTitle(f"Dust Mine Bot – {name}{' *' if self._dirty else ''}")

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
        self.preview_title.setText(f"<b>Podgląd</b> - {new_name}")
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
        if name == self.selected_name:
            self.preview.update_frame(frame, det)
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
            status = self._format_list_label(entry).split("\n", 1)[-1].strip()
            card.set_status(status, STATUS_COLORS.get(entry.status, "#ddd"))
            card.set_stats(entry.last_stats)
            card.set_running(self._running(entry) and entry.status not in ("stopped", "error"),
                             entry.status == "paused")
        running = sum(1 for e in self.instances.values() if self._running(e))

        def total(attr: str) -> int:
            return sum(getattr(e.last_stats, attr, 0) for e in self.instances.values() if e.last_stats)
        self.dashboard.set_summary(
            f"Działa: <b>{running}/{len(self.instances)}</b> &nbsp;·&nbsp; łącznie: plansze {total('cycles')}, "
            f"kulki {total('balls_collected')}, porażki {total('defeats')}, dropy {total('drops')}, "
            f"rozebrane {total('salvaged')}, błędy {total('errors')}")

    def _rebuild_dashboard(self) -> None:
        self.dashboard.rebuild([(n, e.serial) for n, e in self.instances.items()])
        for e in self.instances.values():
            self._update_dashboard(e)
            if e.last_frame is not None:
                self.dashboard.card(e.config.name).set_frame(e.last_frame)


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
        if name == self.selected_name:
            self.preview.update_frame(frame, det)
        # miniatury w przeglądzie - najwyżej 2 razy na sekundę na instancję
        now = time.monotonic()
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

        self._config_path = path
        self._dirty = False
        self._update_title()
        self._log_local(f"Wczytano config: {path} ({len(self.instances)} instancji)")

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
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
