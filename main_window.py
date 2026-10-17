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

from bot import BotConfig, RandomRange, ScheduleWindow, schedule_from_list, schedule_to_list
from gui_worker import BotWorker
from preview_widget import PreviewWidget, EditMode
from adb_controller import list_ldplayer_devices
import vision

logger = logging.getLogger("gui")

STATUS_COLORS = {
    "connecting": "#f0ad4e",
    "idle": "#5bc0de",
    "collecting": "#5cb85c",
    "recording": "#d9534f",
    "paused": "#aaaaaa",
    "stopped": "#777777",
    "error": "#d9534f",
}

STATUS_LABELS_PL = {
    "connecting": "łączenie...",
    "idle": "czekanie",
    "collecting": "zbieranie",
    "recording": "⏺ nagrywanie",
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
        self.list_item: QListWidgetItem | None = None


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
        self.btn_record = QPushButton("⏺ Nagraj 30 s")
        self.btn_record.setToolTip("Nagrywa wideo, dotyk i klatki przez 30 s. W tym czasie bot "
                                   "nic nie klika - zagraj ręcznie w minigrę.")
        self.btn_record.clicked.connect(self._record_selected)
        start_stop_row.addWidget(self.btn_start)
        start_stop_row.addWidget(self.btn_pause)
        start_stop_row.addWidget(self.btn_stop)
        start_stop_row.addWidget(self.btn_restart_app)
        start_stop_row.addWidget(self.btn_record)
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
        self.btn_screenshot = QPushButton("📸 Zrzut ekranu (F9)")
        self.btn_screenshot.setShortcut("F9")
        self.btn_screenshot.setToolTip("Zapisuje aktualny widok wybranej instancji do "
                                       "captures/Screenshots/ - działa też przy zatrzymanym bocie.")
        self.btn_screenshot.clicked.connect(self._save_screenshot)
        edit_row.addWidget(self.btn_edit_idle)
        edit_row.addWidget(self.btn_edit_region)
        edit_row.addWidget(self.btn_refresh_preview)
        edit_row.addWidget(self.btn_screenshot)
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

        self.stats_label = QLabel("Cykle: 0   Kulki: 0   Błędy: 0")
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
        fields = (self.field_name, self.field_serial, self.field_idle_x, self.field_idle_y,
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
        for w in (self.field_name, self.field_serial, self.field_idle_x, self.field_idle_y,
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

        self.preview.set_known_config(cfg.idle_point, cfg.board_region, cfg.idle_point_radius)

        if entry.worker is not None and entry.worker.isRunning():
            entry.worker.update_config(idle_point=cfg.idle_point, board_region=cfg.board_region)

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
        """Pobiera jeden zrzut ekranu natychmiast (przydatne, gdy bot
        jest zatrzymany i nie generuje klatek automatycznie)."""
        entry = self._current_entry()
        if entry is None:
            return
        from adb_controller import ADBController, ADBError
        try:
            adb = ADBController(serial=entry.serial)
            frame = adb.screenshot()
            det = vision.detect(frame, region=entry.config.board_region)
            entry.last_frame = frame
            entry.last_det = det
            self.preview.update_frame(frame, det)
            self._log_local(f"[{entry.config.name}] odświeżono podgląd ręcznie "
                             f"(DROP={'tak' if det.drop else 'nie'}, kulki={len(det.balls)})")
        except ADBError as e:
            QMessageBox.critical(self, "Błąd ADB", str(e))

    def _save_screenshot(self) -> None:
        """Zapisuje świeży zrzut wybranej instancji (bez nakładek z podglądu)."""
        entry = self._current_entry()
        if entry is None:
            return
        from adb_controller import ADBController, ADBError
        from recorder import save_screenshot
        try:
            frame = ADBController(serial=entry.serial).screenshot()
        except ADBError as e:
            if entry.last_frame is None:
                QMessageBox.critical(self, "Błąd ADB", str(e))
                return
            frame = entry.last_frame  # ostatnia klatka z bota, jeśli ADB chwilowo nie odpowiada
            self._log_local(f"[{entry.config.name}] ADB nie odpowiada, zapisuję ostatnią klatkę z podglądu")
        try:
            path = save_screenshot(frame, Path(entry.config.capture_dir) / "Screenshots", entry.config.name)
        except OSError as e:
            QMessageBox.critical(self, "Błąd zapisu", str(e))
            return
        entry.last_frame = frame
        if entry.config.name == self.selected_name:
            self.preview.update_frame(frame, None)  # świeża klatka - bez starych obwódek detekcji
        self._log_local(f"[{entry.config.name}] zapisano zrzut: {path}")

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

    def _record_selected(self) -> None:
        """Nagranie minigry na wybranej instancji (bot musi działać)."""
        entry = self._current_entry()
        if entry is None:
            return
        if entry.worker is None or not entry.worker.isRunning():
            QMessageBox.information(
                self, "Bot nieaktywny",
                f"Instancja '{entry.config.name}' nie jest uruchomiona - "
                f"uruchom bota, żeby nagrywać."
            )
            return
        entry.worker.request_recording(30.0)
        self._log_local(
            f"[{entry.config.name}] nagrywanie 30 s - zagraj teraz ręcznie. "
            f"Zapis: {Path(entry.config.capture_dir) / 'recordings'}"
        )

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
                f"Ustaw game_package w konfiguracji tej instancji (np. w pliku "
                f"config.json) i uruchom bota ponownie."
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
        import time
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

    @Slot(str, int, int, int)
    def _on_stats_updated(self, name: str, cycles: int, balls: int, errors: int) -> None:
        if name == self.selected_name:
            self.stats_label.setText(f"Cykle: {cycles}   Kulki: {balls}   Błędy: {errors}")

    @Slot(str, str)
    def _on_connection_failed(self, name: str, message: str) -> None:
        self._log_local(f"[{name}] BŁĄD POŁĄCZENIA: {message}")
        QMessageBox.critical(self, "Błąd połączenia ADB",
                              f"Nie udało się połączyć z instancją '{name}':\n{message}")

    # ------------------------------------------------------------------ #
    # Config JSON
    # ------------------------------------------------------------------ #
    def _load_config_dialog(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Wczytaj config", "", "JSON (*.json)")
        if not path:
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            QMessageBox.critical(self, "Błąd wczytywania", str(e))
            return

        self.instance_list.clear()
        self.instances.clear()

        for inst in data.get("instances", []):
            cfg = BotConfig(
                name=inst["name"],
                idle_point=tuple(inst["idle_point"]),
                idle_point_radius=inst.get("idle_point_radius", 50.0),
                idle_click_interval=RandomRange.from_value(inst.get("idle_click_interval", [0.8, 1.4])),
                board_region=tuple(inst["board_region"]) if inst.get("board_region") else None,
                drag_duration_ms=RandomRange.from_value(inst.get("drag_duration_ms", [800, 1300])),
                between_drags_delay=RandomRange.from_value(inst.get("between_drags_delay", [0.5, 1.1])),
                empty_detections_to_exit=inst.get("empty_detections_to_exit", 2),
                max_seconds_per_game_screen=inst.get("max_seconds_per_game_screen", 60.0),
                idle_poll_interval=inst.get("idle_poll_interval", 0.5),
                schedule=schedule_from_list(inst.get("schedule")),
                schedule_poll_interval=inst.get("schedule_poll_interval", 15.0),
                close_game_during_break=inst.get("close_game_during_break", True),
                game_package=inst.get("game_package"),
                game_activity=inst.get("game_activity"),
                black_screen_timeout=inst.get("black_screen_timeout", 20.0),
                black_screen_brightness_threshold=inst.get("black_screen_brightness_threshold", 10.0),
                restart_settle_delay=inst.get("restart_settle_delay", 10.0),
                foreign_app_packages=tuple(inst.get(
                    "foreign_app_packages",
                    ["com.android.vending", "com.android.chrome", "com.android.browser"],
                )),
                foreground_check_interval=inst.get("foreground_check_interval", 3.0),
                capture_unknown_screens=inst.get("capture_unknown_screens", True),
                record_unknown_seconds=inst.get("record_unknown_seconds", 0.0),
                record_cooldown=inst.get("record_cooldown", 300.0),
                capture_dir=inst.get("capture_dir", "captures"),
                touch_backend=inst.get("touch_backend", "adb"),
            )
            entry = InstanceEntry(config=cfg, serial=inst["serial"])
            self._register_instance(entry)

        self._config_path = Path(path)
        self._log_local(f"Wczytano config: {path} ({len(self.instances)} instancji)")

    def _save_config_dialog(self) -> None:
        default = str(self._config_path) if self._config_path else "config.json"
        path, _ = QFileDialog.getSaveFileName(self, "Zapisz config", default, "JSON (*.json)")
        if not path:
            return

        data = {"instances": []}
        for entry in self.instances.values():
            cfg = entry.config
            data["instances"].append({
                "serial": entry.serial,
                "name": cfg.name,
                "idle_point": list(cfg.idle_point),
                "idle_point_radius": cfg.idle_point_radius,
                "board_region": list(cfg.board_region) if cfg.board_region else None,
                "idle_click_interval": cfg.idle_click_interval.to_dict(),
                "drag_duration_ms": cfg.drag_duration_ms.to_dict(),
                "between_drags_delay": cfg.between_drags_delay.to_dict(),
                "empty_detections_to_exit": cfg.empty_detections_to_exit,
                "max_seconds_per_game_screen": cfg.max_seconds_per_game_screen,
                "idle_poll_interval": cfg.idle_poll_interval,
                "schedule": schedule_to_list(cfg.schedule),
                "schedule_poll_interval": cfg.schedule_poll_interval,
                "close_game_during_break": cfg.close_game_during_break,
                "game_package": cfg.game_package,
                "game_activity": cfg.game_activity,
                "black_screen_timeout": cfg.black_screen_timeout,
                "black_screen_brightness_threshold": cfg.black_screen_brightness_threshold,
                "restart_settle_delay": cfg.restart_settle_delay,
                "foreign_app_packages": list(cfg.foreign_app_packages),
                "foreground_check_interval": cfg.foreground_check_interval,
                "capture_unknown_screens": cfg.capture_unknown_screens,
                "record_unknown_seconds": cfg.record_unknown_seconds,
                "record_cooldown": cfg.record_cooldown,
                "capture_dir": cfg.capture_dir,
                "touch_backend": cfg.touch_backend,
            })

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
        for entry in self.instances.values():
            if entry.worker is not None and entry.worker.isRunning():
                entry.worker.request_stop()
        for entry in self.instances.values():
            if entry.worker is not None:
                entry.worker.wait(3000)
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
