"""
dashboard.py
============
Strona "Przegląd": podsumowanie wszystkich instancji, akcje dla wszystkich
naraz i kafelek każdej instancji (podgląd na żywo, status, liczby,
przyciski). Sam panel nie steruje botami - wysyła sygnały, które obsługuje
MainWindow (ta sama logika co przyciski pojedynczej instancji).
"""

from __future__ import annotations

import cv2
import numpy as np
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QFrame, QGridLayout, QHBoxLayout, QLabel, QMenu, QPushButton, QScrollArea, QSpinBox,
    QToolButton, QVBoxLayout, QWidget,
)

from bot import DEPTH_MODES

THUMB_W, THUMB_H = 138, 245
SIDE_W = 236                      # szerokość kolumny z opisem i przyciskami
CARD_W = THUMB_W + SIDE_W + 36


def format_play_time(seconds: float) -> str:
    """Czas gry: "2 h 05 min" / "7 min"."""
    m = int(seconds // 60)
    return f"{m // 60} h {m % 60:02d} min" if m >= 60 else f"{m} min"


def _num(v) -> str:
    return "—" if v is None else f"{v:,}".replace(",", " ")


def _button(text: str, tip: str = "", accent: bool = False, danger: bool = False) -> QPushButton:
    b = QPushButton(text)
    if tip:
        b.setToolTip(tip)
    if accent:
        b.setProperty("accent", True)
    if danger:
        b.setProperty("danger", True)
    return b


# (atrybut BotStats / klucz, etykieta) - liczby na kafelku
_CARD_METRICS = [
    ("gear_level", "Gear"), ("depth", "Depth"), ("dust", "Dust"),
    ("play_seconds", "Czas gry"), ("cycles", "Plansze"), ("balls_collected", "Kulki"),
    ("packs_opened", "Paczki"), ("drops", "Dropy"), ("defeats", "Porażki"),
    ("salvaged", "Rozebrane"), ("errors", "Błędy"),
]


class InstanceCard(QFrame):
    action = Signal(str, str)  # (nazwa instancji, akcja: start|pause|stop|salvage|details)

    def __init__(self, name: str, serial: str, parent=None):
        super().__init__(parent)
        self.name = name
        self.setObjectName("card")
        self.setFixedWidth(CARD_W)
        # pełna wysokość (podgląd + przyciski) - siatka w przewijanym obszarze
        # inaczej ściska kafelki, gdy jest ich więcej niż mieści okno
        self.setMinimumHeight(THUMB_H + 22)

        root = QHBoxLayout(self)
        root.setContentsMargins(10, 10, 10, 10)
        root.setSpacing(12)

        self.thumb = QLabel("brak\npodglądu")
        self.thumb.setFixedSize(THUMB_W, THUMB_H)
        self.thumb.setAlignment(Qt.AlignCenter)
        self.thumb.setStyleSheet("background: #0b0e11; color: #5d6873; border-radius: 6px;")
        self.thumb.setCursor(Qt.PointingHandCursor)
        self.thumb.setToolTip("Kliknij, żeby otworzyć podgląd i ustawienia tej instancji")
        self.thumb.mousePressEvent = lambda _e: self.action.emit(self.name, "details")
        root.addWidget(self.thumb)

        side_box = QWidget()
        side_box.setFixedWidth(SIDE_W)
        side = QVBoxLayout(side_box)
        side.setContentsMargins(0, 0, 0, 0)
        side.setSpacing(6)
        root.addWidget(side_box)

        head = QHBoxLayout()
        head.setSpacing(6)
        self.title = QLabel(name)
        self.title.setObjectName("cardTitle")
        self.title.setWordWrap(True)
        head.addWidget(self.title, 1)
        self.pill = QLabel("zatrzymany")
        self.pill.setObjectName("pill")
        head.addWidget(self.pill, 0, Qt.AlignTop)
        side.addLayout(head)
        dev = QLabel(serial or "(brak urządzenia)")
        dev.setObjectName("muted")
        side.addWidget(dev)
        self.perf = QLabel("")
        self.perf.setObjectName("muted")
        self.perf.setToolTip("Średni czas zrzutu ekranu, narzut jednej akcji i liczba zrzutów na sekundę")
        side.addWidget(self.perf)

        # bieżąca czynność bota (pełny opis)
        self.status = QLabel("")
        self.status.setWordWrap(True)
        self.status.setMinimumHeight(32)
        self.status.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        side.addWidget(self.status)

        self.stats = QWidget()
        grid = QGridLayout(self.stats)
        grid.setContentsMargins(0, 2, 0, 2)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(1)
        self._metric: dict[str, QLabel] = {}
        for i, (key, label) in enumerate(_CARD_METRICS):
            n = QLabel(label); n.setObjectName("metricName")
            v = QLabel("—"); v.setObjectName("metricValue")
            grid.addWidget(n, (i // 3) * 2, i % 3)
            grid.addWidget(v, (i // 3) * 2 + 1, i % 3)
            self._metric[key] = v
        side.addWidget(self.stats)
        side.addStretch(1)

        row = QHBoxLayout()
        row.setSpacing(4)
        self.btn_start = _button("▶ Start", "Uruchom bota na tej instancji", accent=True)
        self.btn_pause = _button("⏸ Pauza", "Wstrzymaj / wznów klikanie")
        self.btn_stop = _button("■ Stop", "Zatrzymaj bota na tej instancji", danger=True)
        for b, act in ((self.btn_start, "start"), (self.btn_pause, "pause"), (self.btn_stop, "stop")):
            b.clicked.connect(lambda _=False, a=act: self.action.emit(self.name, a))
            row.addWidget(b)
        side.addLayout(row)
        row2 = QHBoxLayout()
        row2.setSpacing(4)
        self.btn_salvage = _button("🧹 Rozbierz", "Rozbierz przedmioty z góry listy Inventory (zapyta o liczbę)")
        details = _button("⚙ Szczegóły", "Podgląd i ustawienia tej instancji")
        for b, act in ((self.btn_salvage, "salvage"), (details, "details")):
            b.clicked.connect(lambda _=False, a=act: self.action.emit(self.name, a))
            row2.addWidget(b)
        side.addLayout(row2)
        self.set_running(False, paused=False)

    # ------------------------------------------------------------------ #
    def set_status(self, text: str, color: str) -> None:
        # pierwsza część opisu = stan (krótko na plakietce), reszta = czynność
        state, _, detail = text.partition(" – ")
        self.pill.setText(f"● {state.strip() or 'zatrzymany'}")
        self.pill.setStyleSheet(f"color: {color};")
        self.status.setText(detail.strip())

    def set_stats(self, stats) -> None:
        if stats is not None and getattr(stats, "capture_ms", 0):
            self.perf.setText(f"zrzut {stats.capture_ms:.0f} ms · akcja {stats.input_ms:.0f} ms · "
                              f"{stats.fps:.1f}/s".replace(".", ","))
        else:
            self.perf.setText("")
        for key, label in self._metric.items():
            if stats is None:
                label.setText("—")
                continue
            value = getattr(stats, key, None)
            label.setText(format_play_time(value or 0.0) if key == "play_seconds" else _num(value))

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


class _Kpi(QFrame):
    """Kafel z jedną liczbą w pasku podsumowania."""

    def __init__(self, label: str):
        super().__init__()
        self.setObjectName("panel")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(0)
        name = QLabel(label); name.setObjectName("metricName")
        self.value = QLabel("—")
        self.value.setStyleSheet("font-size: 20px; font-weight: 700;")
        lay.addWidget(name)
        lay.addWidget(self.value)


class DashboardWidget(QWidget):
    card_action = Signal(str, str)   # (nazwa, akcja) - z kafelka
    global_action = Signal(str)      # start_all|pause_all|resume_all|stop_all|salvage_all|packs_all|restart_all|turbo_on|turbo_off
    depth_all = Signal(str, int)     # (tryb głębokości, liczba: głębokość albo procent)

    KPIS = [("running", "Działa"), ("cycles", "Plansze"), ("balls_collected", "Kulki"),
            ("packs_opened", "Paczki"), ("drops", "Dropy"), ("defeats", "Porażki"), ("errors", "Błędy")]

    def __init__(self, parent=None):
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(16, 14, 16, 8)
        root.setSpacing(10)

        head = QHBoxLayout()
        title = QLabel("Przegląd")
        title.setObjectName("pageTitle")
        head.addWidget(title)
        head.addStretch(1)
        # najczęstsze akcje dla wszystkich - widoczne od razu
        for text, act, tip, accent, danger in (
            ("▶ Start wszystkich", "start_all", "Uruchom bota na wszystkich instancjach", True, False),
            ("⏸ Pauza", "pause_all", "Wstrzymaj wszystkie działające instancje", False, False),
            ("▶ Wznów", "resume_all", "Wznów wszystkie wstrzymane instancje", False, False),
            ("■ Stop wszystkich", "stop_all", "Zatrzymaj wszystkie instancje", False, True),
        ):
            b = _button(text, tip, accent=accent, danger=danger)
            b.clicked.connect(lambda _=False, a=act: self.global_action.emit(a))
            head.addWidget(b)
        # rzadsze akcje w menu
        more = QToolButton()
        more.setText("Więcej ▾")
        more.setPopupMode(QToolButton.InstantPopup)
        menu = QMenu(more)
        for text, act in (
            ("🧹 Rozbierz na wszystkich", "salvage_all"),
            ("📦 Otwórz paczki na wszystkich", "packs_all"),
            ("🔄 Restart gry na wszystkich", "restart_all"),
            ("⚡ Turbo na wszystkich", "turbo_on"),
            ("🐢 Normalne tempo na wszystkich", "turbo_off"),
        ):
            menu.addAction(text, lambda a=act: self.global_action.emit(a))
        more.setMenu(menu)
        head.addWidget(more)
        root.addLayout(head)

        kpis = QHBoxLayout()
        kpis.setSpacing(8)
        self._kpi: dict[str, _Kpi] = {}
        for key, label in self.KPIS:
            tile = _Kpi(label)
            self._kpi[key] = tile
            kpis.addWidget(tile, 1)
        root.addLayout(kpis)

        # głębokość farmienia dla wszystkich instancji naraz
        depth_box = QFrame()
        depth_box.setObjectName("panel")
        depth = QHBoxLayout(depth_box)
        depth.setContentsMargins(12, 8, 12, 8)
        depth.addWidget(QLabel("Głębokość na wszystkich"))
        self.depth_mode = QComboBox()
        for key, label in DEPTH_MODES.items():
            self.depth_mode.addItem(label, key)
        self.depth_mode.setCurrentIndex(self.depth_mode.findData("max_pct"))
        depth.addWidget(self.depth_mode)
        self.depth_value = QSpinBox()
        depth.addWidget(self.depth_value)
        apply_btn = _button("✔ Ustaw na wszystkich",
                            "Ustawia tryb głębokości na każdej instancji; boty wybiorą ją w Profile "
                            "przy najbliższym ekranie z paskiem nawigacji")
        apply_btn.clicked.connect(lambda: self.depth_all.emit(self.depth_mode.currentData(),
                                                              self.depth_value.value()))
        depth.addWidget(apply_btn)
        depth.addStretch(1)
        root.addWidget(depth_box)
        self.depth_mode.currentIndexChanged.connect(self._depth_mode_changed)
        self._depth_mode_changed()

        # tekstowe podsumowanie (zachowane dla zgodności; ukryte - liczby są na kaflach)
        self.summary = QLabel("")
        self.summary.setVisible(False)
        root.addWidget(self.summary)

        self.empty = QLabel("Brak instancji. Użyj „🔍 Wykryj emulatory” albo „➕ Dodaj” na pasku u góry.")
        self.empty.setObjectName("muted")
        self.empty.setAlignment(Qt.AlignCenter)
        root.addWidget(self.empty)

        self.area = QScrollArea()
        self.area.setWidgetResizable(True)
        self.area.setFrameShape(QFrame.NoFrame)
        self.container = QWidget()
        self.grid = QGridLayout(self.container)
        self.grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(10)
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
        self.empty.setVisible(not instances)
        self._relayout()

    def _relayout(self) -> None:
        while self.grid.count():
            self.grid.takeAt(0)
        card_w = CARD_W + self.grid.spacing()
        width = max(self.area.viewport().width(), self.width() - 40)
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

    def set_totals(self, totals: dict) -> None:
        """Liczby na kaflach podsumowania: {klucz: tekst lub liczba}."""
        for key, tile in self._kpi.items():
            value = totals.get(key)
            tile.value.setText(value if isinstance(value, str) else _num(value))
