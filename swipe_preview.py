"""
swipe_preview.py
================
Wizualizacja przeciągania kulki: kilka losowych torów od kulki do DROP
dla wybranych ustawień (patrz swipe_presets.py).
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QBrush
from PySide6.QtWidgets import QWidget

import swipe_presets

_W, _H = 360.0, 170.0
_BALL = QPointF(70.0, _H / 2)
_DROP = QPointF(_W - 70.0, _H / 2)
_BALL_R, _DROP_R = 24.0, 32.0


class SwipePreviewWidget(QWidget):
    """Rysuje przykładowe tory dla podanych wartości (dict jak w swipe_presets)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(int(_W), int(_H))
        self.setMaximumHeight(int(_H) + 10)
        self._values: dict | None = None
        self._seed = 0

    def set_values(self, values: dict) -> None:
        self._values = values
        self._seed += 1  # nowe losowanie przy każdej zmianie
        self.update()

    def paintEvent(self, event) -> None:
        if self._values is None:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        # skala z okna do logicznego układu _W x _H
        scale = min(self.width() / _W, self.height() / _H)
        p.translate((self.width() - _W * scale) / 2, (self.height() - _H * scale) / 2)
        p.scale(scale, scale)

        pal = self.palette()
        p.setPen(Qt.NoPen)
        p.setBrush(pal.color(pal.ColorRole.AlternateBase))
        p.drawRoundedRect(0, 0, _W, _H, 8, 8)

        # kulka (żółta) i DROP (czerwony przerywany okrąg) - jak na planszy
        p.setBrush(QColor("#f5c542"))
        p.drawEllipse(_BALL, _BALL_R, _BALL_R)
        pen = QPen(QColor("#e33"), 2, Qt.DashLine)
        p.setPen(pen)
        p.setBrush(Qt.NoBrush)
        p.drawEllipse(_DROP, _DROP_R, _DROP_R)

        paths = swipe_presets.sample_paths(
            self._values, n=6, seed=self._seed,
            start=(_BALL.x(), _BALL.y()), end=(_DROP.x(), _DROP.y()),
            ball_r=_BALL_R, drop_r=_DROP_R)
        for i, path in enumerate(paths):
            qp = QPainterPath(QPointF(*path[0]))
            for x, y in path[1:]:
                qp.lineTo(x, y)
            color = QColor("#3fb8e0")
            color.setAlpha(170)
            p.setPen(QPen(color, 1.8))
            p.setBrush(Qt.NoBrush)
            p.drawPath(qp)
            # punkty toru - każdy punkt to osobny ruch palca
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(color))
            for x, y in path[1:-1]:
                p.drawEllipse(QPointF(x, y), 1.8, 1.8)
        p.end()
