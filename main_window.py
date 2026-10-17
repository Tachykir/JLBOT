"""
main_window.py
==============
Główne okno aplikacji Dust Mine Bot GUI.

Layout:
  +------------------+---------------------------+
  | Lista instancji   |  Podgląd wybranej         |
  | (status, Start/   |  instancji (żywy zrzut +  |
  |  Stop/Pauza)      |  detekcja, zrzut, nagranie)|
  |                   +---------------------------+
  |                   |  Panel configu instancji   |
  |                   |  (wejście, harmonogram,...)|
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
from pathlib import Path

import numpy as np
from PySide6.QtCore import Qt, Slot, QTime
from PySide6.QtGui import QColor, QTextCursor
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QHBoxLayout, QVBoxLayout, QFormLayout,
    QListWidget, QListWidgetItem, QPushButton, QLabel, QLineEdit,
    QDoubleSpinBox, QTextEdit, QSplitter, QGroupBox, QMessageBox, QFileDialog,
    QTimeEdit, QCheckBox, QDialog, QDialogButtonBox, QComboBox,
)

from bot import BotConfig, ScheduleWindow, schedule_from_list, schedule_to_list
from dustbot2.schedule import DailyPlayTarget, plan_day, window_for
from gui_worker import BotWorker, RecordThread
from preview_widget import PreviewWidget
from adb_controller import list_ldplayer_devices
import vision

logger = logging.getLogger("gui")

STATUS_COLORS = {
    "connecting": "#f0ad4e",
    "idle": "#5bc0de",
    "collecting": "#5cb85c",
    "recording": "#d9534f",
    "fishing": "#5cb85c",
    "minigame": "#5cb85c",
    "paused": "#aaaaaa",
    "stopped": "#777777",
    "error": "#d9534f",
}

STATUS_LABELS_PL = {
    "connecting": "łączenie...",
    "idle": "czekanie",
    "collecting": "zbieranie",
    "recording": "⏺ nagrywanie",
    "fishing": "🎣 fishing",
    "minigame": "minigra",
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
        self.record_thread: RecordThread | None = None
        self.resume_after_recording = False


_DAY_LABELS = ["Pon", "Wt", "Śr", "Czw", "Pt", "Sob", "Nd"]


class DailyPlayDialog(QDialog):
    """
    Harmonogram dzienny: okno czasowe + ile godzin dziennie bot ma grać.
    Sesje i przerwy bot układa sam (dustbot2/schedule.py), z nieregularnymi
    odstępami. Podgląd pokazuje przykładowy plan na dziś.
    """

    def __init__(self, target: DailyPlayTarget | None, instance: str,
                 close_game_during_break: bool = True, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Harmonogram dzienny")
        self.resize(560, 560)
        self._instance = instance
        self._preview_seed = 0
        current = target or DailyPlayTarget("09:00", "23:00", play_hours=6.0)

        root = QVBoxLayout(self)
        self.check_enabled = QCheckBox("Włącz harmonogram (bez niego bot gra bez przerwy)")
        self.check_enabled.setChecked(target is not None)
        root.addWidget(self.check_enabled)

        form = QFormLayout()
        root.addLayout(form)
        self.field_start = QTimeEdit(QTime.fromString(current.window_start, "HH:mm"))
        self.field_start.setDisplayFormat("HH:mm")
        self.field_end = QTimeEdit(QTime.fromString(current.window_end, "HH:mm"))
        self.field_end.setDisplayFormat("HH:mm")
        window_row = QHBoxLayout()
        window_row.addWidget(QLabel("od")); window_row.addWidget(self.field_start)
        window_row.addWidget(QLabel("do")); window_row.addWidget(self.field_end)
        window_box = QWidget(); window_box.setLayout(window_row)
        form.addRow("Okno gry:", window_box)

        self.field_hours = QDoubleSpinBox()
        self.field_hours.setRange(0.5, 24.0)
        self.field_hours.setSingleStep(0.5)
        self.field_hours.setSuffix(" h")
        self.field_hours.setValue(current.play_hours)
        form.addRow("Godzin gry dziennie:", self.field_hours)

        days_row = QHBoxLayout()
        self.day_checks: list[QCheckBox] = []
        for i, label in enumerate(_DAY_LABELS):
            cb = QCheckBox(label)
            cb.setChecked(current.days is not None and i in current.days)
            days_row.addWidget(cb)
            self.day_checks.append(cb)
        days_box = QWidget(); days_box.setLayout(days_row)
        form.addRow("Dni (brak = codziennie):", days_box)

        self.check_close_game = QCheckBox("Zamykaj grę (force-stop) na czas przerw")
        self.check_close_game.setChecked(close_game_during_break)
        root.addWidget(self.check_close_game)

        preview_head = QHBoxLayout()
        preview_head.addWidget(QLabel("<b>Przykładowy plan</b> - bot układa go sam, każdego dnia inaczej\n"
                                      "(sesje od ok. 10 min do 2 h, nieregularne przerwy):"))
        self.btn_reroll = QPushButton("🎲 Inny przykład")
        self.btn_reroll.clicked.connect(self._reroll)
        preview_head.addWidget(self.btn_reroll)
        root.addLayout(preview_head)
        self.preview = QTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setStyleSheet("font-family: Consolas, monospace; font-size: 11px;")
        root.addWidget(self.preview, stretch=1)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

        for w in (self.field_start, self.field_end):
            w.timeChanged.connect(self._update_preview)
        self.field_hours.valueChanged.connect(self._update_preview)
        for w in (self.check_enabled, *self.day_checks):
            w.stateChanged.connect(self._update_preview)
        self._update_preview()

    def _target(self) -> DailyPlayTarget:
        days = tuple(i for i, cb in enumerate(self.day_checks) if cb.isChecked()) or None
        return DailyPlayTarget(
            window_start=self.field_start.time().toString("HH:mm"),
            window_end=self.field_end.time().toString("HH:mm"),
            play_hours=self.field_hours.value(),
            days=days,
        )

    def _reroll(self) -> None:
        self._preview_seed += 1
        self._update_preview()

    def _update_preview(self) -> None:
        enabled = self.check_enabled.isChecked()
        for w in (self.field_start, self.field_end, self.field_hours, self.btn_reroll, *self.day_checks):
            w.setEnabled(enabled)
        if not enabled:
            self.preview.setPlainText("Harmonogram wyłączony - bot gra bez przerwy.")
            return
        target = self._target()
        today = dt.date.today()
        start, end = window_for(target, today)
        rng = random.Random(f"{self._instance}|{today}|{self._preview_seed}")
        plan = plan_day(target, start, end, rng)
        sessions = plan.sessions
        window_hours = (end - start).total_seconds() / 3600
        lines = [f"Okno {start:%H:%M}-{end:%H:%M} ({window_hours:.1f} h), styl dnia: {plan.style}"]
        if target.play_hours > window_hours:
            lines.append(f"Uwaga: {target.play_hours:.1f} h gry nie mieści się w oknie - bot zagra całe okno.")
        lines.append("")
        previous_end = None
        for i, session in enumerate(sessions, 1):
            if previous_end is not None:
                gap = (session.start - previous_end).total_seconds() / 60
                lines.append(f"      przerwa {gap:5.0f} min")
            lines.append(f"{i:2d}. gra {session.start:%H:%M}-{session.end:%H:%M} ({session.minutes:.0f} min)")
            previous_end = session.end
        total = sum(x.minutes for x in sessions) / 60
        lines += ["", f"Razem: {total:.2f} h w {len(sessions)} sesjach"]
        self.preview.setPlainText("\n".join(lines))

    def result_target(self) -> DailyPlayTarget | None:
        return self._target() if self.check_enabled.isChecked() else None

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
        mid_layout.addWidget(self.preview, stretch=1)

        edit_row = QHBoxLayout()
        self.btn_refresh_preview = QPushButton("⟳ Odśwież teraz")
        self.btn_refresh_preview.clicked.connect(self._refresh_preview_once)
        self.btn_screenshot = QPushButton("📸 Zrzut ekranu (F9)")
        self.btn_screenshot.setShortcut("F9")
        self.btn_screenshot.setToolTip("Zapisuje aktualny widok wybranej instancji do "
                                       "captures/Screenshots/ - działa też przy zatrzymanym bocie.")
        self.btn_screenshot.clicked.connect(self._save_screenshot)
        self.btn_record = QPushButton()
        self.btn_record.clicked.connect(self._toggle_recording)
        self._update_record_button(None)
        edit_row.addWidget(self.btn_refresh_preview)
        edit_row.addWidget(self.btn_screenshot)
        edit_row.addWidget(self.btn_record)
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

        self.combo_touch = QComboBox()
        self.combo_touch.addItem("adb input (prosta linia)", "adb")
        self.combo_touch.addItem("minitouch (zróżnicowane gesty, wymaga roota)", "minitouch")
        self.combo_touch.currentIndexChanged.connect(self._apply_config_fields)
        form.addRow("Wejście dotykowe:", self.combo_touch)

        self.field_tap_min = QDoubleSpinBox()
        self.field_tap_max = QDoubleSpinBox()
        tap_row = QHBoxLayout()
        tap_row.setContentsMargins(0, 0, 0, 0)
        for label, spin in (("od", self.field_tap_min), ("do", self.field_tap_max)):
            spin.setRange(0.05, 10.0)
            spin.setDecimals(2)
            spin.setSingleStep(0.05)
            spin.setSuffix(" s")
            spin.editingFinished.connect(self._apply_config_fields)
            tap_row.addWidget(QLabel(label))
            tap_row.addWidget(spin)
        tap_container = QWidget(); tap_container.setLayout(tap_row)
        form.addRow("Odstęp tapów JUST LOOT:", tap_container)

        self.btn_edit_schedule = QPushButton("📅 Edytuj harmonogram")
        self.btn_edit_schedule.clicked.connect(self._open_schedule_editor)
        self.schedule_summary_label = QLabel("brak (gra bez przerwy)")
        self.schedule_summary_label.setStyleSheet("color: #888; font-size: 11px;")
        schedule_row = QVBoxLayout()
        schedule_row.setContentsMargins(0, 0, 0, 0)
        schedule_row.addWidget(self.btn_edit_schedule)
        schedule_row.addWidget(self.schedule_summary_label)
        schedule_container = QWidget(); schedule_container.setLayout(schedule_row)
        form.addRow("Harmonogram:", schedule_container)

        self.stats_label = QLabel("Cykle: 0   Kulki: 0   Fishing: 0/0   Błędy: 0")
        form.addRow("Statystyki:", self.stats_label)

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
        self._update_record_button(entry)

    def _load_fields_from_entry(self, entry: InstanceEntry) -> None:
        cfg = entry.config
        # Blokujemy sygnały na czas wypełniania pól - inaczej samo ustawienie
        # wartości w QSpinBox/QDoubleSpinBox wywołuje valueChanged/editingFinished,
        # które od razu (przedwcześnie) nadpisują config innej/nowej instancji
        # wartościami domyślnymi z widgetu, zanim zdążymy wczytać prawdziwe dane.
        fields = (self.field_name, self.field_serial, self.combo_touch, self.field_tap_min, self.field_tap_max)
        for w in fields:
            w.blockSignals(True)
        try:
            self.field_name.setText(cfg.name)
            self.field_serial.setText(entry.serial)
            self.combo_touch.setCurrentIndex(max(self.combo_touch.findData(cfg.touch_backend), 0))
            self.field_tap_min.setValue(min(cfg.just_loot_interval))
            self.field_tap_max.setValue(max(cfg.just_loot_interval))
        finally:
            for w in fields:
                w.blockSignals(False)
        self.schedule_summary_label.setText(self._schedule_summary_text(cfg))

    def _schedule_summary_text(self, cfg: BotConfig) -> str:
        target = cfg.daily_play
        if target is not None:
            days = "codziennie" if target.days is None else ",".join(_DAY_LABELS[d] for d in target.days)
            return (f"{target.play_hours:g} h gry w oknie {target.window_start}-{target.window_end} "
                    f"({days}), przerwy losowe")
        schedule = cfg.schedule
        if not schedule:
            return "brak (gra bez przerwy)"
        return "stary harmonogram okien: " + "; ".join(self._format_window(w) for w in schedule)

    def _format_window(self, w: ScheduleWindow) -> str:
        if w.days is None:
            return f"{w.start}-{w.end}"
        days_str = ",".join(_DAY_LABELS[d] for d in sorted(w.days))
        return f"{w.start}-{w.end} ({days_str})"

    def _open_schedule_editor(self) -> None:
        entry = self._current_entry()
        if entry is None:
            return
        dialog = DailyPlayDialog(
            entry.config.daily_play, entry.config.name,
            close_game_during_break=entry.config.close_game_during_break,
            parent=self,
        )
        if dialog.exec() == QDialog.Accepted:
            entry.config.daily_play = dialog.result_target()
            entry.config.close_game_during_break = dialog.result_close_game_during_break()
            self.schedule_summary_label.setText(self._schedule_summary_text(entry.config))
            running = entry.worker is not None and entry.worker.isRunning()
            self._log_local(
                f"[{entry.config.name}] harmonogram: {self._schedule_summary_text(entry.config)} "
                f"(zamykanie gry na przerwie: {'tak' if entry.config.close_game_during_break else 'nie'})"
                + (" - zadziała po ponownym uruchomieniu bota" if running else "")
            )

    def _set_config_form_enabled(self, enabled: bool) -> None:
        for w in (self.field_name, self.field_serial, self.btn_edit_schedule, self.combo_touch,
                  self.field_tap_min, self.field_tap_max):
            w.setEnabled(enabled)
        if not enabled:
            self.schedule_summary_label.setText("brak (gra bez przerwy)")

    def _apply_config_fields(self) -> None:
        entry = self._current_entry()
        if entry is None:
            return
        cfg = entry.config
        cfg.touch_backend = self.combo_touch.currentData() or "adb"
        low, high = sorted((self.field_tap_min.value(), self.field_tap_max.value()))
        cfg.just_loot_interval = (low, high)
        entry.serial = self.field_serial.text().strip()

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
            entry.last_frame = frame
            entry.last_det = None
            self.preview.update_frame(frame, None)
            self._log_local(f"[{entry.config.name}] odświeżono podgląd ręcznie")
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

    # Nagrywanie ręczne - działa przy włączonym i wyłączonym bocie
    MAX_RECORDING_SECONDS = 180  # limit `screenrecord` na Androidzie

    def _update_record_button(self, entry: InstanceEntry | None) -> None:
        recording = entry is not None and entry.record_thread is not None
        if recording:
            self.btn_record.setText("⏹ Zatrzymaj nagrywanie (F10)")
            self.btn_record.setToolTip("Kończy nagranie i zapisuje wideo, dotyk i klatki.")
        else:
            self.btn_record.setText("⏺ Nagrywaj (F10)")
            self.btn_record.setToolTip(
                "Nagrywa wideo, dotyk i klatki wybranej instancji (maks. 3 min). Jeśli bot "
                "działa, jest wstrzymany na czas nagrania - możesz grać ręcznie.")
        self.btn_record.setShortcut("F10")

    def _toggle_recording(self) -> None:
        entry = self._current_entry()
        if entry is None:
            return
        if entry.record_thread is not None:
            entry.record_thread.request_stop()
            self.btn_record.setEnabled(False)  # do czasu zapisania plików
            self._log_local(f"[{entry.config.name}] kończę nagrywanie, zapisuję pliki...")
            return

        bot_running = entry.worker is not None and entry.worker.isRunning()
        entry.resume_after_recording = bot_running and entry.status != "paused"
        if entry.resume_after_recording:
            entry.worker.request_pause()

        thread = RecordThread(entry.config.name, entry.serial,
                              Path(entry.config.capture_dir) / "recordings",
                              self.MAX_RECORDING_SECONDS)
        thread.frame_ready.connect(self._on_recording_frame)
        thread.recording_finished.connect(self._on_recording_finished)
        thread.recording_failed.connect(self._on_recording_failed)
        entry.record_thread = thread
        thread.start()
        self._update_record_button(entry)
        self._log_local(
            f"[{entry.config.name}] nagrywanie - zagraj teraz ręcznie, potem kliknij "
            f"'Zatrzymaj' (maks. {self.MAX_RECORDING_SECONDS // 60} min)"
            + (" - bot wstrzymany na czas nagrania" if entry.resume_after_recording else ""))

    @Slot(str, np.ndarray)
    def _on_recording_frame(self, name: str, frame: np.ndarray) -> None:
        entry = self.instances.get(name)
        if entry is None:
            return
        entry.last_frame = frame
        if name == self.selected_name:
            self.preview.update_frame(frame, None)

    def _end_recording(self, name: str) -> InstanceEntry | None:
        entry = self.instances.get(name)
        if entry is None:
            return None
        if entry.record_thread is not None:
            entry.record_thread.wait(1000)
        entry.record_thread = None
        if entry.resume_after_recording and entry.worker is not None and entry.worker.isRunning():
            entry.worker.request_resume()
            self._log_local(f"[{name}] bot wznowiony po nagraniu")
        entry.resume_after_recording = False
        if name == self.selected_name:
            self._update_record_button(entry)
            self.btn_record.setEnabled(True)
        return entry

    @Slot(str, str)
    def _on_recording_finished(self, name: str, folder: str) -> None:
        self._end_recording(name)
        self._log_local(f"[{name}] nagranie zapisane: {folder}" if folder else f"[{name}] nagranie puste")

    @Slot(str, str)
    def _on_recording_failed(self, name: str, error: str) -> None:
        self._end_recording(name)
        QMessageBox.critical(self, "Błąd nagrywania", f"[{name}] {error}")

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

    @Slot(str, int, int, int, int, int)
    def _on_stats_updated(self, name: str, cycles: int, balls: int, errors: int,
                          fishing_hits: int, fishing_casts: int) -> None:
        if name == self.selected_name:
            self.stats_label.setText(f"Cykle: {cycles}   Kulki: {balls}   "
                                     f"Fishing: {fishing_hits}/{fishing_casts}   Błędy: {errors}")

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
                just_loot_interval=tuple(inst.get("just_loot_interval", (0.25, 0.7))),
                just_loot_pause_chance=inst.get("just_loot_pause_chance", 0.03),
                fishing_latency_ms=inst.get("fishing_latency_ms"),
                fishing_aim_spread=inst.get("fishing_aim_spread", 0.18),
                daily_play=DailyPlayTarget.from_dict(inst.get("daily_play")),
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
                "just_loot_interval": list(cfg.just_loot_interval),
                "just_loot_pause_chance": cfg.just_loot_pause_chance,
                "fishing_latency_ms": cfg.fishing_latency_ms,
                "fishing_aim_spread": cfg.fishing_aim_spread,
                "daily_play": cfg.daily_play.to_dict() if cfg.daily_play else None,
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
            if entry.record_thread is not None:
                entry.record_thread.request_stop()
        for entry in self.instances.values():
            if entry.worker is not None:
                entry.worker.wait(3000)
            if entry.record_thread is not None:
                entry.record_thread.wait(20000)  # pobranie wideo z emulatora
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
