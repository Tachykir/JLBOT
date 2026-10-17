"""
geometry.py
===========
Współrzędne niezależne od rozdzielczości.

Config przechowuje punkty jako ułamki ekranu (0.0-1.0), np. idle_point
= (0.60, 0.78). Dzięki temu ta sama konfiguracja działa na każdej
rozdzielczości, a przeliczenie na piksele następuje dopiero po odczycie
rozmiaru ekranu z urządzenia.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Screen:
    width: int
    height: int

    def to_px(self, fx: float, fy: float) -> tuple[int, int]:
        """Zamienia ułamki ekranu na piksele (z przycięciem do granic)."""
        x = min(max(round(fx * (self.width - 1)), 0), self.width - 1)
        y = min(max(round(fy * (self.height - 1)), 0), self.height - 1)
        return x, y

    def to_frac(self, x: int, y: int) -> tuple[float, float]:
        """Odwrotność to_px - przydatne do zapisu współrzędnych z GUI."""
        return x / (self.width - 1), y / (self.height - 1)
