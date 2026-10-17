"""
preview_widget.py
==================
Widget podglądu ekranu jednej instancji LDPlayer, z nałożoną
wizualizacją detekcji (DROP na czerwono, kulki na zielono). Obraz jest
dopasowywany do rozmiaru okna z zachowaniem proporcji.
"""

from __future__ import annotations

import cv2
import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QLabel, QSizePolicy

import vision


class PreviewWidget(QLabel):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(220, 380)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet("background-color: #111; border: 1px solid #333;")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setText("Brak podglądu")

        self._last_frame: np.ndarray | None = None
        self._last_det: vision.DetectionResult | None = None

    # ------------------------------------------------------------------ #
    def update_frame(self, frame_bgr: np.ndarray, det: vision.DetectionResult | None) -> None:
        self._last_frame = frame_bgr
        self._last_det = det
        self._redraw()

    # ------------------------------------------------------------------ #
    def _compose_image(self) -> np.ndarray:
        """Rysuje detekcję (DROP i kulki) na kopii ostatniej klatki."""
        vis = self._last_frame.copy()
        if self._last_det is not None:
            if self._last_det.drop is not None:
                d = self._last_det.drop
                cv2.circle(vis, d.center, d.r, (0, 0, 255), 3)
            for b in self._last_det.balls:
                cv2.circle(vis, b.center, b.r, (0, 255, 0), 3)
        return vis

    def _redraw(self) -> None:
        if self._last_frame is None:
            return
        vis = self._compose_image()
        h, w = vis.shape[:2]
        rgb = cv2.cvtColor(vis, cv2.COLOR_BGR2RGB)
        qimg = QImage(rgb.data, w, h, rgb.strides[0], QImage.Format_RGB888)
        pixmap = QPixmap.fromImage(qimg)
        self.setPixmap(pixmap.scaled(self.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation))

    # ------------------------------------------------------------------ #
    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._redraw()
