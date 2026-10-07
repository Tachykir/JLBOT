"""
dashboard.py
============
Panel "Wszystkie instancje": kafelek dla każdej instancji (podgląd na
żywo, status, statystyki, przyciski) i pasek akcji dla wszystkich naraz.
Sam panel nie steruje botami - wysyła sygnały, które obsługuje
MainWindow (ta sama logika co przyciski pojedynczej instancji).
"""

from __future__ import annotations

import cv2
import numpy as np
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy,
    QSpinBox, QVBoxLayout, QWidget,
)

from bot import DEPTH_MODES

THUMB_W, THUMB_H = 150, 266
SIDE_W = 215                      # szerokość kolumny z opisem i przyciskami
CARD_W = THUMB_W + SIDE_W + 28


def format_play_time(seconds: float) -> str:
    """Czas gry: "2 h 05 min" / "7 min"."""
    m = int(seconds // 60)
    return f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{m} min"


class InstanceCard(QFrame):
    action = Signal(str, str)  # (nazwa instancji, akcja: start|pause|stop|salvage|details)

    def __init__(self, name: str, serial: str, parent=None):
        super().__init__(parent)
        self.name = name
        self.setFrameShape(QFrame.StyledPanel)
        self.setStyleSheet("InstanceCard { border: 1px solid #555; border-radius: 6px; }")
        self.setFixedWidth(CARD_W)

        root = QHBoxLayout(self)
        root.setContentsMargins(6, 6, 6, 6)
        root.setSpacing(10)

        self.thumb = QLabel("brak\npodglądu")
        self.thumb.setFixedSize(THUMB_W, THUMB_H)
        self.thumb.setAlignment(Qt.AlignCenter)
        self.thumb.setStyleSheet("background: #111; color: #666; border: 1px solid #333;")
        self.thumb.setCursor(Qt.PointingHandCursor)
        self.thumb.mousePressEvent = lambda _e: self.action.emit(self.name, "details")
        root.addWidget(self.thumb)

        side_box = QWidget()
        side_box.setFixedWidth(SIDE_W)
        side = QVBoxLayout(side_box)
        side.setContentsMargins(0, 0, 0, 0)
        root.addWidget(side_box)
        self.title = QLabel(f"<b>{name}</b>")
        self.title.setWordWrap(True)
        side.addWidget(self.title)
        dev = QLabel(serial or "(brak urządzenia)")
        dev.setStyleSheet("color: #888; font-size: 11px;")
        dev.setWordWrap(True)
        side.addWidget(dev)
        self.status = QLabel("zatrzymany")
        self.status.setWordWrap(True)
        side.addWidget(self.status)
        self.stats = QLabel("")
        self.stats.setWordWrap(True)
        self.stats.setStyleSheet("font-size: 11px;")
        side.addWidget(self.stats)
        side.addStretch(1)

        grid = QGridLayout()
        grid.setSpacing(3)
        self.btn_start = QPushButton("▶ Start")
        self.btn_pause = QPushButton("⏸ Pauza")
        self.btn_stop = QPushButton("■ Stop")
        self.btn_salvage = QPushButton("🧹 Rozbierz")
        self.btn_salvage.setToolTip("Rozbierz przedmioty z góry listy Inventory (zapyta o liczbę)")
        details = QPushButton("⚙ Szczegóły")
        details.setToolTip("Podgląd i ustawienia tej instancji")
        for i, (b, act) in enumerate(((self.btn_start, "start"), (self.btn_pause, "pause"),
                                      (self.btn_stop, "stop"), (self.btn_salvage, "salvage"),
                                      (details, "details"))):
            b.clicked.connect(lambda _=False, a=act: self.action.emit(self.name, a))
            grid.addWidget(b, i // 2, i % 2)
        side.addLayout(grid)
        self.set_running(False, paused=False)

    # ------------------------------------------------------------------ #
    def set_status(self, text: str, color: str) -> None:
        self.status.setText(f"<span style='color:{color}'>●</span> {text}")

    def set_stats(self, stats) -> None:
        if stats is None:
            self.stats.setText("")
            return
        def num(v):
            return "?" if v is None else f"{v:,}".replace(",", " ")
        self.stats.setText(
            f"<b>Gear</b> {num(getattr(stats, 'gear_level', None))} · "
            f"<b>Depth</b> {num(getattr(stats, 'depth', None))} · "
            f"<b>Dust</b> {num(getattr(stats, 'dust', None))}<br>"
            f"<b>Czas gry</b> {format_play_time(getattr(stats, 'play_seconds', 0.0))}<br>"
            f"Plansze {stats.cycles} · Kulki {stats.balls_collected}<br>"
            f"Porażki {stats.defeats} · Dropy {stats.drops}<br>"
            f"Paczki {getattr(stats, 'packs_opened', 0)}<br>"
            f"Rozebrane {stats.salvaged} · Błędy {stats.errors}")

    def set_running(self, running: bool, paused: bool) -> None:
        self.btn_start.setEnabled(not running)
        self.btn_pause.setEnabled(running)
        self.btn_stop.setEnabled(running)
        self.btn_salvage.setEnabled(running)
        self.btn_pause.setText("▶ Wznów" if paused else "⏸ Pauza")

    def set_frame(self, frame: np.ndarray) -> None:
        h, w = frame.shape[:2]
        scale = min(THUMB_W / w, THUMB_H / h)
        small = cv2.resize(frame, (max(1, int(w * scale)), max(1, int(h * scale))), interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
        img = QImage(rgb.data, rgb.shape[1], rgb.shape[0], rgb.strides[0], QImage.Format_RGB888)
        self.thumb.setPixmap(QPixmap.fromImage(img.copy()))


class DashboardWidget(QWidget):
    card_action = Signal(str, str)   # (nazwa, akcja) - z kafelka
    global_action = Signal(str)      # start_all|pause_all|resume_all|stop_all|salvage_all|packs_all|restart_all
    depth_all = Signal(str, int)     # (tryb głębokości, liczba: głębokość albo procent)

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(4, 4, 4, 4)

        bar = QHBoxLayout()
        for text, act, tip in (
            ("▶ Start wszystkich", "start_all", "Uruchom bota na wszystkich instancjach"),
            ("⏸ Pauza wszystkich", "pause_all", "Wstrzymaj wszystkie działające instancje"),
            ("▶ Wznów wszystkie", "resume_all", "Wznów wszystkie wstrzymane instancje"),
            ("■ Stop wszystkich", "stop_all", "Zatrzymaj wszystkie instancje"),
            ("🧹 Rozbierz na wszystkich", "salvage_all", "Rozbierz przedmioty na wszystkich działających instancjach"),
            ("📦 Otwórz paczki na wszystkich", "packs_all", "Otwórz podaną liczbę paczek na wszystkich działających instancjach"),
            ("🔄 Restart gry na wszystkich", "restart_all", "Zrestartuj grę na wszystkich działających instancjach"),
            ("⚡ Turbo", "turbo_on", "Tryb turbo (maksymalna szybkość) na wszystkich instancjach"),
            ("🐢 Normalne tempo", "turbo_off", "Wyłącz tryb turbo na wszystkich instancjach (wraca tempo z zakładki Tempo)"),
        ):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(lambda _=False, a=act: self.global_action.emit(a))
            bar.addWidget(b)
        bar.addStretch(1)
        root.addLayout(bar)

        # głębokość farmienia dla wszystkich instancji naraz
        depth = QHBoxLayout()
        depth.addWidget(QLabel("Głębokość na wszystkich:"))
        self.depth_mode = QComboBox()
        for key, label in DEPTH_MODES.items():
            self.depth_mode.addItem(label, key)
        self.depth_mode.setCurrentIndex(self.depth_mode.findData("max_pct"))
        depth.addWidget(self.depth_mode)
        self.depth_value = QSpinBox()
        depth.addWidget(self.depth_value)
        apply_btn = QPushButton("✔ Ustaw na wszystkich")
        apply_btn.setToolTip("Ustawia tryb głębokości na każdej instancji; boty wybiorą ją w Profile "
                             "przy najbliższym ekranie z paskiem nawigacji")
        apply_btn.clicked.connect(lambda: self.depth_all.emit(self.depth_mode.currentData(),
                                                              self.depth_value.value()))
        depth.addWidget(apply_btn)
        depth.addStretch(1)
        root.addLayout(depth)
        self.depth_mode.currentIndexChanged.connect(self._depth_mode_changed)
        self._depth_mode_changed()

        self.summary = QLabel("")
        self.summary.setWordWrap(True)
        root.addWidget(self.summary)

        self.area = QScrollArea()
        self.area.setWidgetResizable(True)
        self.area.setFrameShape(QFrame.NoFrame)
        self.container = QWidget()
        self.grid = QGridLayout(self.container)
        self.grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.grid.setSpacing(8)
        self.area.setWidget(self.container)
        root.addWidget(self.area, 1)

        self.cards: dict[str, InstanceCard] = {}
        self._order: list[str] = []

    def _depth_mode_changed(self) -> None:
        mode = self.depth_mode.currentData()
        self.depth_value.setVisible(mode in ("fixed", "max_pct"))
        if mode == "max_pct":
            self.depth_value.setRange(1, 90)
            self.depth_value.setSuffix(" % poniżej maks.")
            self.depth_value.setSpecialValueText("")
            self.depth_value.setValue(10)
        elif mode == "fixed":
            self.depth_value.setRange(0, 9999)
            self.depth_value.setSuffix("")
            self.depth_value.setSpecialValueText("nie zmieniaj")
            self.depth_value.setValue(1)

    # ------------------------------------------------------------------ #
    def rebuild(self, instances: list[tuple[str, str]]) -> None:
        """Tworzy kafelki od nowa: lista (nazwa, serial)."""
        for card in self.cards.values():
            card.setParent(None)
            card.deleteLater()
        self.cards = {}
        self._order = [name for name, _ in instances]
        for name, serial in instances:
            card = InstanceCard(name, serial)
            card.action.connect(self.card_action)
            self.cards[name] = card
        self._relayout()

    def _relayout(self) -> None:
        while self.grid.count():
            self.grid.takeAt(0)
        card_w = CARD_W + self.grid.spacing()
        width = max(self.area.viewport().width(), self.width() - 30)
        cols = max(1, width // card_w)
        for i, name in enumerate(self._order):
            self.grid.addWidget(self.cards[name], i // cols, i % cols)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._relayout()

    def showEvent(self, event) -> None:
        # przy starcie karta jest ukryta i ma zerową szerokość - przelicz
        # kolumny, gdy panel zostanie pokazany
        super().showEvent(event)
        self._relayout()

    def card(self, name: str) -> InstanceCard | None:
        return self.cards.get(name)

    def set_summary(self, text: str) -> None:
        self.summary.setText(text)
