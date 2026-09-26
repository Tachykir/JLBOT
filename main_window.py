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
from PySide6.QtGui import QColor, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QFormLayout,
    QListWidget, QListWidgetItem, QPushButton, QLabel, QLineEdit, QSpinBox,
    QDoubleSpinBox, QTextEdit, QSplitter, QGroupBox, QMessageBox, QFileDialog,
    QTimeEdit, QCheckBox, QDialog, QDialogButtonBox,
)

from bot import BotConfig, BotStats, RandomRange, ScheduleWindow
from gui_worker import BotWorker, SnapshotWorker
from preview_widget import PreviewWidget, EditMode
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
    "paused": "#aaaaaa",
    "stopped": "#777777",
    "error": "#d9534f",
}

STATUS_LABELS_PL = {
    "connecting": "łączenie...",
    "idle": "czekanie",
    "collecting": "zbieranie",
    "paused": "wstrzymany",
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
        self.list_item: QListWidgetItem | None = None
        self.snapshot_worker: SnapshotWorker | None = None


_DAY_LABELS = ["Pon", "Wt", "Śr", "Czw", "Pt", "Sob", "Nd"]


class ScheduleEditorDialog(QDialog):
    """
    Edytor harmonogramu pracy instancji - lista okien czasowych
    (start/koniec/dni tygodnia) z możliwością dodawania, edycji i
    usuwania. Bot pracuje TYLKO w obrębie zdefiniowanych okien; pusta
    lista oznacza "działa zawsze" (brak ograniczeń), co jest też
    zachowaniem domyślnym/wstecznie kompatybilnym.
    """

    def __init__(self, windows: list[ScheduleWindow], close_game_during_break: bool = True, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Harmonogram pracy")
        self.resize(520, 420)
        # kopia robocza - oryginał podmieniamy dopiero po "OK"
        self._windows: list[ScheduleWindow] = list(windows)

        root = QVBoxLayout(self)

        info = QLabel(
            "Bot pracuje TYLKO w oknach zdefiniowanych poniżej.\n"
            "Brak okien (pusta lista) = bot działa bez ograniczeń godzinowych."
        )
        info.setStyleSheet("color: #888; font-size: 11px;")
        root.addWidget(info)

        body = QHBoxLayout()
        root.addLayout(body)

        # --- lewa strona: lista okien --- #
        left = QVBoxLayout()
        body.addLayout(left, stretch=1)

        self.list_widget = QListWidget()
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

        hint = QLabel("Okno przechodzące przez północ (np. 22:00 -> 02:00)\njest obsługiwane poprawnie.")
        hint.setStyleSheet("color: #888; font-size: 11px;")
        form.addRow(hint)

        days_label = QLabel("Dni tygodnia (brak zaznaczenia = wszystkie):")
        right.addWidget(days_label)

        days_row = QHBoxLayout()
        self.day_checks: list[QCheckBox] = []
        for label in _DAY_LABELS:
            cb = QCheckBox(label)
            cb.stateChanged.connect(self._apply_current_window)
            days_row.addWidget(cb)
            self.day_checks.append(cb)
        right.addLayout(days_row)

        right.addStretch(1)

        self.editor_group = QWidget()
        # chowamy edytor prawej strony dopóki nic nie jest wybrane
        self._set_editor_enabled(False)

        self.check_close_game = QCheckBox("Zamykaj grę (force-stop) na czas przerwy między oknami")
        self.check_close_game.setChecked(close_game_during_break)
        self.check_close_game.setToolTip(
            "Gdy zaznaczone: gra jest zamykana na wejściu w przerwę i\n"
            "uruchamiana ponownie na starcie kolejnego okna pracy.\n"
            "Wymaga ustawionego game_package."
        )
        root.addWidget(self.check_close_game)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        self._refresh_list()

    # ------------------------------------------------------------------ #
    def _set_editor_enabled(self, enabled: bool) -> None:
        for w in (self.field_start, self.field_end, *self.day_checks):
            w.setEnabled(enabled)

    def _window_label(self, w: ScheduleWindow) -> str:
        if w.days is None:
            days_str = "codziennie"
        else:
            days_str = ",".join(_DAY_LABELS[d] for d in sorted(w.days))
        return f"{w.start} - {w.end}  ({days_str})"

    def _refresh_list(self, select_row: int | None = None) -> None:
        self.list_widget.blockSignals(True)
        self.list_widget.clear()
        for w in self._windows:
            self.list_widget.addItem(self._window_label(w))
        self.list_widget.blockSignals(False)
        if select_row is not None and 0 <= select_row < self.list_widget.count():
            self.list_widget.setCurrentRow(select_row)
        elif self._windows:
            self.list_widget.setCurrentRow(0)
        else:
            self._on_row_changed(-1)

    def _add_window(self) -> None:
        # domyślne nowe okno: 09:00-18:00, wszystkie dni - sensowny,
        # nieszkodliwy punkt startowy do dalszej edycji
        self._windows.append(ScheduleWindow(start="09:00", end="18:00", days=None))
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
        self._set_editor_enabled(True)
        w = self._windows[row]

        for widget in (self.field_start, self.field_end, *self.day_checks):
            widget.blockSignals(True)
        try:
            sh, sm = (int(x) for x in w.start.split(":"))
            eh, em = (int(x) for x in w.end.split(":"))
            self.field_start.setTime(QTime(sh, sm))
            self.field_end.setTime(QTime(eh, em))
            active_days = set(w.days) if w.days is not None else set()
            for i, cb in enumerate(self.day_checks):
                cb.setChecked(i in active_days)
        finally:
            for widget in (self.field_start, self.field_end, *self.day_checks):
                widget.blockSignals(False)

    def _apply_current_window(self) -> None:
        row = self.list_widget.currentRow()
        if row < 0 or row >= len(self._windows):
            return
        checked_days = [i for i, cb in enumerate(self.day_checks) if cb.isChecked()]
        self._windows[row] = ScheduleWindow(
            start=self.field_start.time().toString("HH:mm"),
            end=self.field_end.time().toString("HH:mm"),
            days=tuple(checked_days) if checked_days else None,
        )
        # odśwież etykietę w liście bez utraty zaznaczenia
        self.list_widget.blockSignals(True)
        self.list_widget.item(row).setText(self._window_label(self._windows[row]))
        self.list_widget.blockSignals(False)

    def result_windows(self) -> list[ScheduleWindow]:
        return self._windows

    def result_close_game_during_break(self) -> bool:
        return self.check_close_game.isChecked()


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Dust Mine Bot")
        self.resize(1280, 820)

        self.instances: dict[str, InstanceEntry] = {}
        self.selected_name: str | None = None
        self._config_path: Path | None = None

        self._build_ui()

        path = default_config_path()
        if path.exists():
            self._load_config_file(path)

    # ------------------------------------------------------------------ #
    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        top_split = QSplitter(Qt.Horizontal)
        root.addWidget(top_split, stretch=4)

        # --- lewa kolumna: lista instancji + przyciski globalne ---
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(4, 4, 4, 4)

        left_layout.addWidget(QLabel("<b>Instancje</b>"))
        self.instance_list = QListWidget()
        self.instance_list.currentItemChanged.connect(self._on_selection_changed)
        left_layout.addWidget(self.instance_list, stretch=1)

        btn_row = QHBoxLayout()
        self.btn_add = QPushButton("+ Dodaj")
        self.btn_add.clicked.connect(self._add_instance_dialog)
        self.btn_remove = QPushButton("- Usuń")
        self.btn_remove.clicked.connect(self._remove_selected_instance)
        btn_row.addWidget(self.btn_add)
        btn_row.addWidget(self.btn_remove)
        left_layout.addLayout(btn_row)

        self.btn_autodetect = QPushButton("🔍 Auto-wykryj z adb devices")
        self.btn_autodetect.clicked.connect(self._autodetect_instances)
        left_layout.addWidget(self.btn_autodetect)

        start_stop_row = QHBoxLayout()
        self.btn_start = QPushButton("▶ Start")
        self.btn_start.clicked.connect(self._start_selected)
        self.btn_stop = QPushButton("■ Stop")
        self.btn_stop.clicked.connect(self._stop_selected)
        self.btn_pause = QPushButton("⏸ Pauza")
        self.btn_pause.clicked.connect(self._toggle_pause_selected)
        self.btn_restart_app = QPushButton("🔄 Restart gry")
        self.btn_restart_app.clicked.connect(self._restart_app_selected)
        start_stop_row.addWidget(self.btn_start)
        start_stop_row.addWidget(self.btn_pause)
        start_stop_row.addWidget(self.btn_stop)
        start_stop_row.addWidget(self.btn_restart_app)
        left_layout.addLayout(start_stop_row)

        global_row = QHBoxLayout()
        self.btn_start_all = QPushButton("▶▶ Start wszystkie")
        self.btn_start_all.clicked.connect(self._start_all)
        self.btn_stop_all = QPushButton("■■ Stop wszystkie")
        self.btn_stop_all.clicked.connect(self._stop_all)
        global_row.addWidget(self.btn_start_all)
        global_row.addWidget(self.btn_stop_all)
        left_layout.addLayout(global_row)

        file_row = QHBoxLayout()
        self.btn_load = QPushButton("Wczytaj config")
        self.btn_load.clicked.connect(self._load_config_dialog)
        self.btn_save = QPushButton("Zapisz config")
        self.btn_save.clicked.connect(self._save_config_dialog)
        file_row.addWidget(self.btn_load)
        file_row.addWidget(self.btn_save)
        left_layout.addLayout(file_row)

        top_split.addWidget(left)

        # --- środek: podgląd na żywo ---
        mid = QWidget()
        mid_layout = QVBoxLayout(mid)
        mid_layout.setContentsMargins(4, 4, 4, 4)

        preview_header = QHBoxLayout()
        self.preview_title = QLabel("<b>Podgląd</b> - brak wybranej instancji")
        preview_header.addWidget(self.preview_title, stretch=1)
        mid_layout.addLayout(preview_header)

        self.preview = PreviewWidget()
        self.preview.idle_point_picked.connect(self._on_idle_point_picked)
        self.preview.board_region_picked.connect(self._on_board_region_picked)
        mid_layout.addWidget(self.preview, stretch=1)

        edit_row = QHBoxLayout()
        self.btn_edit_idle = QPushButton("🎯 Kliknij: ustaw idle_point")
        self.btn_edit_idle.setCheckable(True)
        self.btn_edit_idle.clicked.connect(self._toggle_edit_idle)
        self.btn_edit_region = QPushButton("▭ Przeciągnij: ustaw board_region")
        self.btn_edit_region.setCheckable(True)
        self.btn_edit_region.clicked.connect(self._toggle_edit_region)
        self.btn_refresh_preview = QPushButton("⟳ Odśwież teraz")
        self.btn_refresh_preview.clicked.connect(self._refresh_preview_once)
        edit_row.addWidget(self.btn_edit_idle)
        edit_row.addWidget(self.btn_edit_region)
        edit_row.addWidget(self.btn_refresh_preview)
        mid_layout.addLayout(edit_row)

        top_split.addWidget(mid)

        # --- prawa kolumna: panel konfiguracji instancji ---
        right = QGroupBox("Konfiguracja instancji")
        form = QFormLayout(right)

        self.field_name = QLineEdit()
        self.field_name.editingFinished.connect(self._apply_config_fields)
        form.addRow("Nazwa:", self.field_name)

        self.field_serial = QLineEdit()
        self.field_serial.editingFinished.connect(self._apply_config_fields)
        form.addRow("ADB serial:", self.field_serial)

        self.field_game_package = QLineEdit()
        self.field_game_package.setPlaceholderText("np. com.timhilgenstoehlerexpo.justlootapp")
        self.field_game_package.setToolTip(
            "Nazwa pakietu gry - potrzebna do restartu gry, watchdogów\n"
            "(czarny ekran, wypadnięcie z gry) i zamykania gry na przerwę.\n"
            "Puste = te funkcje są wyłączone."
        )
        self.field_game_package.editingFinished.connect(self._apply_config_fields)
        form.addRow("game_package:", self.field_game_package)

        self.field_game_activity = QLineEdit()
        self.field_game_activity.setPlaceholderText("opcjonalnie, np. pakiet/.MainActivity")
        self.field_game_activity.editingFinished.connect(self._apply_config_fields)
        form.addRow("game_activity:", self.field_game_activity)

        self.field_idle_x = QSpinBox(); self.field_idle_x.setRange(0, 9999)
        self.field_idle_y = QSpinBox(); self.field_idle_y.setRange(0, 9999)
        self.field_idle_x.valueChanged.connect(self._apply_config_fields)
        self.field_idle_y.valueChanged.connect(self._apply_config_fields)
        idle_row = QHBoxLayout()
        idle_row.addWidget(self.field_idle_x)
        idle_row.addWidget(self.field_idle_y)
        idle_container = QWidget(); idle_container.setLayout(idle_row)
        form.addRow("idle_point (x, y):", idle_container)

        self.field_idle_radius = QDoubleSpinBox()
        self.field_idle_radius.setRange(0.0, 300.0)
        self.field_idle_radius.setSingleStep(5.0)
        self.field_idle_radius.valueChanged.connect(self._apply_config_fields)
        form.addRow("idle_point_radius (px):", self.field_idle_radius)

        self.field_region_x1 = QSpinBox(); self.field_region_x1.setRange(0, 9999)
        self.field_region_y1 = QSpinBox(); self.field_region_y1.setRange(0, 9999)
        self.field_region_x2 = QSpinBox(); self.field_region_x2.setRange(0, 9999)
        self.field_region_y2 = QSpinBox(); self.field_region_y2.setRange(0, 9999)
        for f in (self.field_region_x1, self.field_region_y1,
                  self.field_region_x2, self.field_region_y2):
            f.valueChanged.connect(self._apply_config_fields)
        region_row1 = QHBoxLayout()
        region_row1.addWidget(QLabel("x1")); region_row1.addWidget(self.field_region_x1)
        region_row1.addWidget(QLabel("y1")); region_row1.addWidget(self.field_region_y1)
        region_row2 = QHBoxLayout()
        region_row2.addWidget(QLabel("x2")); region_row2.addWidget(self.field_region_x2)
        region_row2.addWidget(QLabel("y2")); region_row2.addWidget(self.field_region_y2)
        region_container = QWidget()
        region_v = QVBoxLayout(region_container)
        region_v.setContentsMargins(0, 0, 0, 0)
        region_v.addLayout(region_row1)
        region_v.addLayout(region_row2)
        form.addRow("board_region:", region_container)

        # --- Pola zakresów (min/max) zamiast pojedynczych wartości -
        # bot losuje z tego przedziału za każdym razem, żeby uniknąć
        # mechanicznego, identycznego rytmu klikania/przeciągania. ---
        def _make_range_row(minimum, maximum, step, decimals=None):
            spin_min = QDoubleSpinBox(); spin_min.setRange(minimum, maximum); spin_min.setSingleStep(step)
            spin_max = QDoubleSpinBox(); spin_max.setRange(minimum, maximum); spin_max.setSingleStep(step)
            if decimals is not None:
                spin_min.setDecimals(decimals)
                spin_max.setDecimals(decimals)
            spin_min.valueChanged.connect(self._apply_config_fields)
            spin_max.valueChanged.connect(self._apply_config_fields)
            row = QHBoxLayout()
            row.addWidget(QLabel("min")); row.addWidget(spin_min)
            row.addWidget(QLabel("max")); row.addWidget(spin_max)
            container = QWidget(); container.setLayout(row)
            return spin_min, spin_max, container

        self.field_idle_interval_min, self.field_idle_interval_max, idle_interval_container = \
            _make_range_row(0.1, 30.0, 0.1)
        form.addRow("idle_click_interval (s):", idle_interval_container)

        self.field_drag_ms_min, self.field_drag_ms_max, drag_ms_container = \
            _make_range_row(50, 5000, 50, decimals=0)
        form.addRow("drag_duration_ms:", drag_ms_container)

        self.field_between_drags_min, self.field_between_drags_max, between_drags_container = \
            _make_range_row(0.0, 10.0, 0.1)
        form.addRow("between_drags_delay (s):", between_drags_container)

        self.btn_edit_schedule = QPushButton("📅 Edytuj harmonogram")
        self.btn_edit_schedule.clicked.connect(self._open_schedule_editor)
        self.schedule_summary_label = QLabel("brak (działa zawsze)")
        self.schedule_summary_label.setStyleSheet("color: #888; font-size: 11px;")
        schedule_row = QVBoxLayout()
        schedule_row.setContentsMargins(0, 0, 0, 0)
        schedule_row.addWidget(self.btn_edit_schedule)
        schedule_row.addWidget(self.schedule_summary_label)
        schedule_container = QWidget(); schedule_container.setLayout(schedule_row)
        form.addRow("Harmonogram:", schedule_container)

        self.stats_label = QLabel(self._format_stats(None))
        form.addRow("Statystyki:", self.stats_label)

        note = QLabel("Wskazówka: zmiany w polach idle_point / board_region\n"
                       "są też ustawiane klikaniem na podglądzie po lewej.\n"
                       "Pola min/max to zakres losowania - min == max wyłącza\n"
                       "losowość dla danego parametru.")
        note.setStyleSheet("color: #888; font-size: 11px;")
        form.addRow(note)

        top_split.addWidget(right)
        top_split.setSizes([260, 640, 340])

        # --- dół: logi ---
        log_group = QGroupBox("Logi")
        log_layout = QVBoxLayout(log_group)
        self.log_view = QTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setStyleSheet("background-color: #1a1a1a; color: #ddd; "
                                     "font-family: Consolas, monospace; font-size: 11px;")
        log_layout.addWidget(self.log_view)
        root.addWidget(log_group, stretch=1)

        self._set_config_form_enabled(False)

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
        entry = InstanceEntry(config=config, serial="127.0.0.1:5555")
        self._register_instance(entry)

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
            entry = InstanceEntry(config=config, serial=serial)
            self._register_instance(entry)
            added += 1

        self._log_local(f"Auto-wykrywanie: znaleziono {len(serials)} urządzeń, dodano {added} nowych.")

    def _register_instance(self, entry: InstanceEntry) -> None:
        item = QListWidgetItem(self._format_list_label(entry))
        item.setData(Qt.UserRole, entry.config.name)
        self.instances[entry.config.name] = entry
        entry.list_item = item
        self.instance_list.addItem(item)
        self.instance_list.setCurrentItem(item)

    def _format_list_label(self, entry: InstanceEntry) -> str:
        status_pl = STATUS_LABELS_PL.get(entry.status, entry.status)
        return f"{entry.config.name}  [{status_pl}]"

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
        self.preview.set_known_config(entry.config.idle_point, entry.config.board_region, entry.config.idle_point_radius)

    def _load_fields_from_entry(self, entry: InstanceEntry) -> None:
        cfg = entry.config
        # Blokujemy sygnały na czas wypełniania pól - inaczej samo ustawienie
        # wartości w QSpinBox/QDoubleSpinBox wywołuje valueChanged/editingFinished,
        # które od razu (przedwcześnie) nadpisują config innej/nowej instancji
        # wartościami domyślnymi z widgetu, zanim zdążymy wczytać prawdziwe dane.
        fields = (self.field_name, self.field_serial, self.field_game_package,
                  self.field_game_activity, self.field_idle_x, self.field_idle_y,
                  self.field_idle_radius,
                  self.field_region_x1, self.field_region_y1, self.field_region_x2,
                  self.field_region_y2,
                  self.field_idle_interval_min, self.field_idle_interval_max,
                  self.field_drag_ms_min, self.field_drag_ms_max,
                  self.field_between_drags_min, self.field_between_drags_max)
        for w in fields:
            w.blockSignals(True)
        try:
            self.field_name.setText(cfg.name)
            self.field_serial.setText(entry.serial)
            self.field_game_package.setText(cfg.game_package or "")
            self.field_game_activity.setText(cfg.game_activity or "")
            self.field_idle_x.setValue(cfg.idle_point[0])
            self.field_idle_y.setValue(cfg.idle_point[1])
            self.field_idle_radius.setValue(cfg.idle_point_radius)
            if cfg.board_region:
                self.field_region_x1.setValue(cfg.board_region[0])
                self.field_region_y1.setValue(cfg.board_region[1])
                self.field_region_x2.setValue(cfg.board_region[2])
                self.field_region_y2.setValue(cfg.board_region[3])
            else:
                for f in (self.field_region_x1, self.field_region_y1,
                          self.field_region_x2, self.field_region_y2):
                    f.setValue(0)
            self.field_idle_interval_min.setValue(cfg.idle_click_interval.min)
            self.field_idle_interval_max.setValue(cfg.idle_click_interval.max)
            self.field_drag_ms_min.setValue(cfg.drag_duration_ms.min)
            self.field_drag_ms_max.setValue(cfg.drag_duration_ms.max)
            self.field_between_drags_min.setValue(cfg.between_drags_delay.min)
            self.field_between_drags_max.setValue(cfg.between_drags_delay.max)
        finally:
            for w in fields:
                w.blockSignals(False)
        self.schedule_summary_label.setText(self._schedule_summary_text(cfg.schedule))
        self.stats_label.setText(self._format_stats(entry.last_stats))

    @staticmethod
    def _format_stats(stats: BotStats | None) -> str:
        if stats is None:
            return "Cykle: 0   Kulki: 0   Przeciągnięcia: 0   Błędy: 0   Restarty: 0"
        return (f"Cykle: {stats.cycles}   Kulki: {stats.balls_collected}   "
                f"Przeciągnięcia: {stats.drags}   Błędy: {stats.errors}   Restarty: {stats.restarts}")

    def _schedule_summary_text(self, schedule: list[ScheduleWindow]) -> str:
        if not schedule:
            return "brak (działa zawsze)"
        if len(schedule) == 1:
            return f"1 okno: {self._format_window(schedule[0])}"
        return f"{len(schedule)} okna/okien: " + "; ".join(self._format_window(w) for w in schedule)

    def _format_window(self, w: ScheduleWindow) -> str:
        if w.days is None:
            return f"{w.start}-{w.end}"
        days_str = ",".join(_DAY_LABELS[d] for d in sorted(w.days))
        return f"{w.start}-{w.end} ({days_str})"

    def _open_schedule_editor(self) -> None:
        entry = self._current_entry()
        if entry is None:
            return
        dialog = ScheduleEditorDialog(
            entry.config.schedule,
            close_game_during_break=entry.config.close_game_during_break,
            parent=self,
        )
        if dialog.exec() == QDialog.Accepted:
            entry.config.schedule = dialog.result_windows()
            entry.config.close_game_during_break = dialog.result_close_game_during_break()
            self.schedule_summary_label.setText(self._schedule_summary_text(entry.config.schedule))
            self._log_local(
                f"[{entry.config.name}] zaktualizowano harmonogram: "
                f"{self._schedule_summary_text(entry.config.schedule)} "
                f"(zamykanie gry na przerwie: {'tak' if entry.config.close_game_during_break else 'nie'})"
            )

    def _set_config_form_enabled(self, enabled: bool) -> None:
        for w in (self.field_name, self.field_serial, self.field_game_package,
                  self.field_game_activity, self.field_idle_x, self.field_idle_y,
                  self.field_idle_radius,
                  self.field_region_x1, self.field_region_y1, self.field_region_x2,
                  self.field_region_y2,
                  self.field_idle_interval_min, self.field_idle_interval_max,
                  self.field_drag_ms_min, self.field_drag_ms_max,
                  self.field_between_drags_min, self.field_between_drags_max,
                  self.btn_edit_schedule):
            w.setEnabled(enabled)
        if not enabled:
            self.schedule_summary_label.setText("brak (działa zawsze)")

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
        cfg.idle_point = (self.field_idle_x.value(), self.field_idle_y.value())
        cfg.idle_point_radius = self.field_idle_radius.value()
        region = (self.field_region_x1.value(), self.field_region_y1.value(),
                  self.field_region_x2.value(), self.field_region_y2.value())
        cfg.board_region = region if any(region) else None
        # min/max: jeśli użytkownik wpisze max < min, przycinamy max do min
        # zamiast pozwolić na nonsensowny/ujemny zakres.
        cfg.idle_click_interval = RandomRange(
            self.field_idle_interval_min.value(),
            max(self.field_idle_interval_min.value(), self.field_idle_interval_max.value()),
        )
        cfg.drag_duration_ms = RandomRange(
            self.field_drag_ms_min.value(),
            max(self.field_drag_ms_min.value(), self.field_drag_ms_max.value()),
        )
        cfg.between_drags_delay = RandomRange(
            self.field_between_drags_min.value(),
            max(self.field_between_drags_min.value(), self.field_between_drags_max.value()),
        )
        entry.serial = self.field_serial.text().strip()

        # Działający bot trzyma ten sam obiekt BotConfig, więc zmiany
        # działają od razu, bez restartu wątku.
        self.preview.set_known_config(cfg.idle_point, cfg.board_region, cfg.idle_point_radius)

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
        if entry is None or entry.worker is None or not entry.worker.isRunning():
            return
        if entry.status == "paused":
            entry.worker.request_resume()
            self._log_local(f"[{entry.config.name}] wznowiono")
        else:
            entry.worker.request_pause()
            self._log_local(f"[{entry.config.name}] wstrzymano")

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

    @Slot(str, str)
    def _on_log_message(self, name: str, line: str) -> None:
        self.log_view.append(f"[{name}] {line}")
        self.log_view.moveCursor(QTextCursor.End)

    def _log_local(self, text: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self.log_view.append(f"{ts} [GUI] {text}")
        self.log_view.moveCursor(QTextCursor.End)

    @Slot(str, str)
    def _on_status_changed(self, name: str, status: str) -> None:
        entry = self.instances.get(name)
        if entry is None:
            return
        entry.status = status
        if entry.list_item is not None:
            entry.list_item.setText(self._format_list_label(entry))
            color = STATUS_COLORS.get(status, "#fff")
            entry.list_item.setForeground(QColor(color))

    @Slot(str, object)
    def _on_stats_updated(self, name: str, stats: object) -> None:
        entry = self.instances.get(name)
        if entry is None:
            return
        entry.last_stats = stats
        if name == self.selected_name:
            self.stats_label.setText(self._format_stats(stats))

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
        self._log_local(f"Wczytano config: {path} ({len(self.instances)} instancji)")

    def _save_config_dialog(self) -> None:
        default = str(self._config_path or default_config_path())
        path, _ = QFileDialog.getSaveFileName(self, "Zapisz config", default, "JSON (*.json)")
        if not path:
            return

        data = {"instances": [
            {"serial": entry.serial, **entry.config.to_dict()}
            for entry in self.instances.values()
        ]}

        try:
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
        except Exception as e:
            QMessageBox.critical(self, "Błąd zapisu", str(e))
            return

        self._config_path = Path(path)
        self._log_local(f"Zapisano config: {path}")

    # ------------------------------------------------------------------ #
    def closeEvent(self, event) -> None:
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
