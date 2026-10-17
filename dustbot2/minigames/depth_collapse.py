"""
depth_collapse.py
=================
Minigra "Depth Collapse": kilka brązowych kafelków "TAP" w losowych
miejscach (część na sobie), timer ok. 5.5 s, nagroda tylko za kliknięcie
wszystkich.

Kafelki wykrywamy po kolorze. Nachodzące kafelki zlewają się w jeden
kształt, więc dla każdego kształtu wybieramy punkt najdalszy od jego
brzegu - zawsze leży wewnątrz któregoś kafelka (zwykle tego na wierzchu
albo w części wspólnej). Po kliknięciu bot robi nowy zrzut i powtarza,
aż kafelki znikną.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

# brązowy kafelek: HSV ok. (18, 96, 133); jaśniejsza ramka ok. (17, 56, 173)
TILE_HSV_LOW = (8, 40, 90)
TILE_HSV_HIGH = (30, 170, 230)


@dataclass(frozen=True)
class TileGroup:
    x: int            # punkt do kliknięcia
    y: int
    area: int         # pole kształtu (kilka kafelków = większe)


def find_tiles(frame: np.ndarray, top_fraction: float = 0.15) -> list[TileGroup]:
    """Kształty kafelków poniżej nagłówka, od największego."""
    height, width = frame.shape[:2]
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, TILE_HSV_LOW, TILE_HSV_HIGH)
    mask[: int(top_fraction * height)] = 0
    size = max(3, int(0.02 * width)) | 1
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (size, size)))

    min_area = 0.5 * (0.08 * width) ** 2  # kafelek ma ok. 0.15 szerokości ekranu
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    groups = []
    for i in range(1, count):
        area = int(stats[i, cv2.CC_STAT_AREA])
        if area < min_area:
            continue
        component = (labels == i).astype(np.uint8)
        distance = cv2.distanceTransform(component, cv2.DIST_L2, 5)
        _, _, _, (x, y) = cv2.minMaxLoc(distance)
        groups.append(TileGroup(int(x), int(y), area))
    return sorted(groups, key=lambda g: -g.area)
