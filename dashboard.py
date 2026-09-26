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
    QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea, QSizePolicy,
    QVBoxLayout, QWidget,
)

THUMB_W, THUMB_H = 150, 266
SIDE_W = 215                      # szerokość kolumny z opisem i przyciskami
CARD_W = THUMB_W + SIDE_W + 28


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
        self.stats.setText(
            f"Plansze {stats.cycles} · Kulki {stats.balls_collected}<br>"
            f"Porażki {stats.defeats} · Dropy {stats.drops}<br>"
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
    global_action = Signal(str)      # start_all|pause_all|resume_all|stop_all|salvage_all|restart_all

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
            ("🔄 Restart gry na wszystkich", "restart_all", "Zrestartuj grę na wszystkich działających instancjach"),
        ):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.clicked.connect(lambda _=False, a=act: self.global_action.emit(a))
            bar.addWidget(b)
        bar.addStretch(1)
        root.addLayout(bar)

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
