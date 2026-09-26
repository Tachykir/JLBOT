"""
preview_widget.py
==================
Widget podglądu ekranu jednej instancji LDPlayer, z nałożoną
wizualizacją detekcji (DROP na czerwono, kulki na zielono, kafelki
Depth Collapse na pomarańczowo, przycisk CAST na błękitno) i trybem
edycji konfiguracji przez klikanie bezpośrednio na obrazie:

  - Klik lewym przyciskiem myszy w trybie "ustaw idle_point"
    -> zapisuje współrzędne punktu klikania w trybie idle.
  - Przeciągnięcie lewym przyciskiem w trybie "ustaw board_region"
    -> rysuje prostokąt, który staje się board_region.

Widget sam nie skaluje współrzędnych "na oko" - zawsze przelicza
pozycję kliknięcia z przestrzeni pikseli widgetu (który może być
przeskalowany/dopasowany do okna) z powrotem na przestrzeń pikseli
oryginalnego zrzutu ekranu, żeby zapisane współrzędne pasowały 1:1
do tego, czego bot używa w ADB tap/swipe.
"""

from __future__ import annotations

from enum import Enum, auto

import cv2
import numpy as np
from PySide6.QtCore import Qt, Signal, QPoint, QRect
from PySide6.QtGui import QImage, QPixmap, QPainter, QPen, QColor, QMouseEvent
from PySide6.QtWidgets import QLabel, QSizePolicy

import vision


class EditMode(Enum):
    NONE = auto()
    SET_IDLE_POINT = auto()
    SET_BOARD_REGION = auto()
    SET_NAMED_POINT = auto()   # dowolny punkt z listy (zakładki, karta...)


class PreviewWidget(QLabel):
    idle_point_picked = Signal(int, int)
    board_region_picked = Signal(int, int, int, int)
    # (klucz punktu, x, y, szerokość klatki, wysokość klatki)
    named_point_picked = Signal(str, int, int, int, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(220, 380)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet("background-color: #111; border: 1px solid #333;")
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(True)
        self.setText("Brak podglądu")

        self._edit_mode = EditMode.NONE
        self._last_frame: np.ndarray | None = None
        self._last_det: vision.DetectionResult | None = None
        self._idle_point: tuple[int, int] | None = None
        self._idle_point_radius: float = 0.0
        self._board_region: tuple[int, int, int, int] | None = None
        # punkty względne: (etykieta, x_frac, y_frac, r_frac, kolor BGR)
        self._named_points: list[tuple[str, float, float, float, tuple[int, int, int]]] = []
        self._pick_key: str | None = None

        self._drag_start_widget: QPoint | None = None
        self._drag_current_widget: QPoint | None = None

        # geometria ostatniego narysowanego pixmapa wewnątrz labela
        # (bo QLabel z KeepAspectRatio centrowanie zostawia marginesy)
        self._img_origin = (0, 0)
        self._img_scale = 1.0
        self._src_size = (0, 0)  # (w, h) oryginalnego frame'u

    # ------------------------------------------------------------------ #
    def set_named_points(self, points) -> None:
        self._named_points = list(points)
        self._redraw()

    def pick_named_point(self, key: str) -> None:
        """Następne kliknięcie na podglądzie ustawi punkt `key`."""
        self.set_edit_mode(EditMode.SET_NAMED_POINT)
        self._pick_key = key

    def set_edit_mode(self, mode: EditMode) -> None:
        self._edit_mode = mode
        self._pick_key = None
        self._drag_start_widget = None
        self._drag_current_widget = None
        cursor = Qt.CrossCursor if mode != EditMode.NONE else Qt.ArrowCursor
        self.setCursor(cursor)

    def set_known_config(self, idle_point: tuple[int, int] | None,
                          board_region: tuple[int, int, int, int] | None,
                          idle_point_radius: float = 0.0) -> None:
        self._idle_point = idle_point
        self._board_region = board_region
        self._idle_point_radius = idle_point_radius
        self._redraw()

    # ------------------------------------------------------------------ #
    def update_frame(self, frame_bgr: np.ndarray, det: vision.DetectionResult | None) -> None:
        self._last_frame = frame_bgr
        self._last_det = det
        self._src_size = (frame_bgr.shape[1], frame_bgr.shape[0])
        self._redraw()

    # ------------------------------------------------------------------ #
    def _compose_image(self) -> np.ndarray:
        """Rysuje na kopii ostatniej klatki: detekcję, zapisany idle_point,
        zapisany board_region, oraz - jeśli w trakcie przeciągania -
        aktualnie rysowany prostokąt."""
        vis = self._last_frame.copy()

        if self._last_det is not None:
            if self._last_det.drop is not None:
                d = self._last_det.drop
                cv2.circle(vis, d.center, d.r, (0, 0, 255), 3)
            for b in self._last_det.balls:
                cv2.circle(vis, b.center, b.r, (0, 255, 0), 3)
            for t in getattr(self._last_det, "tiles", []):
                cv2.rectangle(vis, (t.x, t.y), (t.x + t.w, t.y + t.h), (0, 165, 255), 3)
            for x, y in getattr(self._last_det, "tile_taps", []):
                cv2.drawMarker(vis, (x, y), (0, 165, 255), markerType=cv2.MARKER_CROSS,
                               markerSize=20, thickness=2)
            cast = getattr(self._last_det, "cast_button", None)
            if cast is not None:
                cv2.rectangle(vis, (cast.x, cast.y), (cast.x + cast.w, cast.y + cast.h), (255, 255, 0), 3)

        if self._idle_point is not None:
            x, y = self._idle_point
            cv2.drawMarker(vis, (x, y), (255, 0, 255), markerType=cv2.MARKER_CROSS,
                            markerSize=24, thickness=2)
            cv2.circle(vis, (x, y), 12, (255, 0, 255), 2)
            if self._idle_point_radius > 0:
                # obszar losowania klikania - okrąg przerywany (rysowany
                # jako seria krótkich łuków), żeby odróżnić go wizualnie
                # od twardych granic (board_region, DROP, kulki)
                radius = int(self._idle_point_radius)
                for start_deg in range(0, 360, 20):
                    cv2.ellipse(vis, (x, y), (radius, radius), 0,
                                start_deg, start_deg + 10, (255, 0, 255), 1)

        if self._board_region is not None:
            x1, y1, x2, y2 = self._board_region
            cv2.rectangle(vis, (x1, y1), (x2, y2), (255, 255, 0), 2)

        h, w = vis.shape[:2]
        for label, fx, fy, fr, color in self._named_points:
            x, y, r = int(fx * w), int(fy * h), max(3, int(fr * w))
            # obszar kliknięć - półprzezroczyste koło z obwódką
            overlay = vis.copy()
            cv2.circle(overlay, (x, y), r, color, -1)
            cv2.addWeighted(overlay, 0.25, vis, 0.75, 0, vis)
            cv2.circle(vis, (x, y), r, color, 2)
            cv2.drawMarker(vis, (x, y), color, markerType=cv2.MARKER_CROSS, markerSize=12, thickness=2)
            cv2.putText(vis, label, (min(x + r + 4, w - 120), y + 5), cv2.FONT_HERSHEY_SIMPLEX,
                        max(0.4, w / 1400), color, 2)

        return vis

    def _redraw(self) -> None:
        if self._last_frame is None:
            return

        vis = self._compose_image()
        h, w = vis.shape[:2]
        rgb = cv2.cvtColor(vis, cv2.COLOR_BGR2RGB)
        qimg = QImage(rgb.data, w, h, rgb.strides[0], QImage.Format_RGB888)
        pixmap = QPixmap.fromImage(qimg)

        target_size = self.size()
        scaled = pixmap.scaled(target_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)

        # oblicz offset centrowania i skalę do przeliczania kliknięć
        self._img_scale = scaled.width() / w if w else 1.0
        ox = (target_size.width() - scaled.width()) // 2
        oy = (target_size.height() - scaled.height()) // 2
        self._img_origin = (ox, oy)

        # jeśli trwa przeciąganie, narysuj podgląd prostokąta na finalnym pixmapie
        if self._drag_start_widget is not None and self._drag_current_widget is not None:
            painter = QPainter(scaled)
            pen = QPen(QColor(255, 255, 0), 2, Qt.DashLine)
            painter.setPen(pen)
            rect = QRect(self._drag_start_widget - QPoint(ox, oy),
                         self._drag_current_widget - QPoint(ox, oy))
            painter.drawRect(rect.normalized())
            painter.end()

        self.setPixmap(scaled)

    # ------------------------------------------------------------------ #
    def _widget_to_source(self, pos: QPoint) -> tuple[int, int] | None:
        """Przelicza współrzędne kliknięcia w widgetcie na współrzędne
        oryginalnego zrzutu ekranu (dokładnie te, których używa ADB)."""
        if self._img_scale <= 0 or self._src_size == (0, 0):
            return None
        ox, oy = self._img_origin
        sx = (pos.x() - ox) / self._img_scale
        sy = (pos.y() - oy) / self._img_scale
        w, h = self._src_size
        if sx < 0 or sy < 0 or sx >= w or sy >= h:
            return None
        return int(sx), int(sy)

    # ------------------------------------------------------------------ #
    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._redraw()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if self._edit_mode == EditMode.NONE or event.button() != Qt.LeftButton:
            return super().mousePressEvent(event)

        src = self._widget_to_source(event.pos())
        if src is None:
            return

        if self._edit_mode == EditMode.SET_IDLE_POINT:
            self._idle_point = src
            self.idle_point_picked.emit(*src)
            self._redraw()
        elif self._edit_mode == EditMode.SET_NAMED_POINT and self._pick_key is not None:
            key = self._pick_key
            w, h = self._src_size
            self.set_edit_mode(EditMode.NONE)
            self.named_point_picked.emit(key, src[0], src[1], w, h)
        elif self._edit_mode == EditMode.SET_BOARD_REGION:
            self._drag_start_widget = event.pos()
            self._drag_current_widget = event.pos()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._edit_mode == EditMode.SET_BOARD_REGION and self._drag_start_widget is not None:
            self._drag_current_widget = event.pos()
            self._redraw()
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if (self._edit_mode == EditMode.SET_BOARD_REGION
                and event.button() == Qt.LeftButton
                and self._drag_start_widget is not None):
            start_src = self._widget_to_source(self._drag_start_widget)
            end_src = self._widget_to_source(event.pos())
            self._drag_start_widget = None
            self._drag_current_widget = None

            if start_src is not None and end_src is not None:
                x1, y1 = start_src
                x2, y2 = end_src
                region = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
                if region[2] - region[0] > 10 and region[3] - region[1] > 10:
                    self._board_region = region
                    self.board_region_picked.emit(*region)
            self._redraw()
        else:
            super().mouseReleaseEvent(event)
