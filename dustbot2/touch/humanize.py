"""
humanize.py
===========
Generator przeciągnięć o zmiennym przebiegu. Zamiast linii prostej o
stałej prędkości, każdy gest to krzywa Béziera z losowym łukiem, zmienną
prędkością (przyspieszenie i hamowanie) i drobnym drżeniem palca.

Parametry są w jednym miejscu (DragProfile) i można je logować razem
z wynikami, żeby porównywać profile w testach detekcji anticheatu.
"""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(frozen=True)
class DragProfile:
    # Całkowity czas gestu (ms) - rozkład jednostajny między min a max.
    duration_ms: tuple[float, float] = (500.0, 900.0)
    # Odstęp między kolejnymi klatkami dotyku (ms).
    frame_ms: tuple[float, float] = (8.0, 14.0)
    # Maksymalne odchylenie łuku, jako ułamek długości gestu.
    arc_fraction: float = 0.06
    # Drżenie palca w pikselach (odchylenie standardowe), wewnątrz gestu.
    jitter_px: float = 1.2
    # Przesunięcie punktu startowego i końcowego (px) - nigdy nie trafiamy
    # dokładnie w ten sam piksel.
    endpoint_px: float = 3.0
    # Nacisk palca (zakres).
    pressure: tuple[int, int] = (40, 70)
    # Zaokrąglenie zmiennej prędkości: 0 = liniowo, 1 = pełne ease-in-out.
    easing: float = 0.7


@dataclass(frozen=True)
class Step:
    delay_ms: float
    x: float
    y: float
    pressure: int


def _ease(t: float, amount: float) -> float:
    """Mieszanka liniowego czasu i ease-in-out (smoothstep)."""
    smooth = t * t * (3 - 2 * t)
    return (1 - amount) * t + amount * smooth


def _bezier(p0, p1, p2, p3, t: float) -> tuple[float, float]:
    u = 1 - t
    x = u**3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t**3 * p3[0]
    y = u**3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t**3 * p3[1]
    return x, y


def plan_drag(
    start: tuple[int, int],
    end: tuple[int, int],
    profile: DragProfile = DragProfile(),
    rng: random.Random | None = None,
) -> list[Step]:
    """
    Zwraca kolejne kroki gestu. Pierwszy krok ma delay_ms=0 i oznacza
    wciśnięcie; ostatni oznacza puszczenie punktu końcowego.
    """
    rng = rng or random.Random()

    sx = start[0] + rng.gauss(0, profile.endpoint_px)
    sy = start[1] + rng.gauss(0, profile.endpoint_px)
    ex = end[0] + rng.gauss(0, profile.endpoint_px)
    ey = end[1] + rng.gauss(0, profile.endpoint_px)

    dx, dy = ex - sx, ey - sy
    length = (dx * dx + dy * dy) ** 0.5 or 1.0
    # prostopadła do kierunku gestu - tu będzie odchylony łuk
    nx, ny = -dy / length, dx / length
    bend = rng.uniform(-1, 1) * profile.arc_fraction * length
    p1 = (sx + dx / 3 + nx * bend, sy + dy / 3 + ny * bend)
    p2 = (sx + 2 * dx / 3 - nx * bend * 0.5, sy + 2 * dy / 3 - ny * bend * 0.5)

    duration = rng.uniform(*profile.duration_ms)
    frame_ms = rng.uniform(*profile.frame_ms)
    count = max(int(duration / frame_ms), 2)

    steps: list[Step] = [Step(0.0, sx, sy, rng.randint(*profile.pressure))]
    prev_t = 0.0
    for i in range(1, count + 1):
        t_lin = i / count
        t = _ease(t_lin, profile.easing)
        x, y = _bezier((sx, sy), p1, p2, (ex, ey), t)
        if i < count:
            x += rng.gauss(0, profile.jitter_px)
            y += rng.gauss(0, profile.jitter_px)
        else:
            x, y = ex, ey
        delay = (t - prev_t) * duration
        prev_t = t
        steps.append(Step(delay, x, y, rng.randint(*profile.pressure)))
    return steps


def to_commands(steps: list[Step], client, contact: int = 0) -> list[str]:
    """Zamienia kroki na polecenia minitouch (d / m / w / u)."""
    commands: list[str] = []
    first = steps[0]
    commands.append(client.down(contact, round(first.x), round(first.y), first.pressure))
    for step in steps[1:]:
        commands.append(client.wait(step.delay_ms))
        commands.append(client.move(contact, round(step.x), round(step.y), step.pressure))
    commands.append(client.up(contact))
    return commands
