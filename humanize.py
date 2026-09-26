"""
humanize.py
===========
Generowanie "ludzkich" ruchów palcem do przeciągania kulek.

Prawdziwy palec nie jeździ po linii prostej ze stałą prędkością:
  - tor jest lekko zakrzywiony (łuk w kształcie C, czasem S),
  - ruch przyspiesza na starcie i zwalnia pod koniec (ease-in-out),
  - czasem lekko "przestrzeli" cel i wraca,
  - dłoń drży o piksel czy dwa,
  - palec nie łapie kulki idealnie w środku ani nie puszcza jej
    idealnie w środku celu.

Moduł jest czystą geometrią (bez ADB) - wynik to lista punktów, którą
ADBController.drag_path() odtwarza jako jeden ciągły dotyk.
"""

from __future__ import annotations

import math
import random

Point = tuple[int, int]


def human_delay(lo: float, hi: float, rng=random) -> float:
    """
    Losowy czas z zakresu [lo, hi] o rozkładzie jak u człowieka: zwykle
    bliżej dolnej granicy, czasem wyraźnie dłużej (rozkład beta(2, 4) -
    prawostronnie skośny, średnio ~1/3 zakresu), zamiast równomiernego,
    przy którym każda wartość jest tak samo prawdopodobna.
    """
    if hi <= lo:
        return lo
    return lo + (hi - lo) * rng.betavariate(2.0, 4.0)


def random_point_in_circle(cx: float, cy: float, radius: float, rng=random) -> Point:
    """Losowy punkt w kole, gęściej przy środku (rozkład normalny obcięty
    do promienia) - tak łapie się obiekt palcem."""
    if radius <= 0:
        return int(round(cx)), int(round(cy))
    dist = min(abs(rng.gauss(0.0, radius / 2.0)), radius)
    angle = rng.uniform(0.0, 2 * math.pi)
    return int(round(cx + dist * math.cos(angle))), int(round(cy + dist * math.sin(angle)))


def _ease_in_out(t: float) -> float:
    # smoothstep: wolny start, szybki środek, wolny koniec
    return t * t * (3 - 2 * t)


def _bezier(p0, p1, p2, p3, t: float) -> tuple[float, float]:
    u = 1 - t
    x = u**3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t**3 * p3[0]
    y = u**3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t**3 * p3[1]
    return x, y


def drag_path(start: Point, end: Point, rng=random,
              min_points: int = 4, max_points: int = 7) -> list[Point]:
    """
    Zwraca listę punktów od `start` do `end` (włącznie) po losowej,
    naturalnie wyglądającej krzywej. Liczba punktów pośrednich jest
    losowana z [min_points, max_points] - każdy punkt to osobny event
    ruchu, więc nie ma sensu generować ich setek.
    """
    x0, y0 = start
    x3, y3 = end
    dx, dy = x3 - x0, y3 - y0
    dist = math.hypot(dx, dy)
    if dist < 2:
        return [start, end]

    # wektor prostopadły do kierunku ruchu - w tę stronę wyginamy łuk
    nx, ny = -dy / dist, dx / dist
    bend = rng.uniform(0.04, 0.22) * dist * rng.choice((-1, 1))
    style = rng.random()
    if style < 0.25:
        b1, b2 = bend, -bend * rng.uniform(0.5, 1.0)       # S
    elif style < 0.85:
        b1, b2 = bend, bend * rng.uniform(0.6, 1.2)        # C
    else:
        b1 = b2 = bend * 0.15                               # prawie prosto

    t1, t2 = rng.uniform(0.2, 0.4), rng.uniform(0.6, 0.8)
    p1 = (x0 + dx * t1 + nx * b1, y0 + dy * t1 + ny * b1)
    p2 = (x0 + dx * t2 + nx * b2, y0 + dy * t2 + ny * b2)

    n = rng.randint(min_points, max_points)
    points: list[Point] = [start]
    for i in range(1, n + 1):
        t = _ease_in_out(i / (n + 1))
        x, y = _bezier(start, p1, p2, end, t)
        # drżenie dłoni
        x += rng.gauss(0.0, 1.2)
        y += rng.gauss(0.0, 1.2)
        points.append((int(round(x)), int(round(y))))

    if rng.random() < 0.2:
        # lekkie "przestrzelenie" celu i powrót
        over = rng.uniform(0.03, 0.08) * dist
        side = rng.gauss(0.0, 0.02 * dist)
        points.append((int(round(x3 + dx / dist * over + nx * side)),
                       int(round(y3 + dy / dist * over + ny * side))))

    points.append(end)
    return points
