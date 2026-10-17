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

import math
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
    # Jak bardzo drżenie w kolejnej klatce zależy od poprzedniej (0-1).
    # Prawdziwe drżenie ręki jest wolnozmienne; 0 dałoby poszarpany zygzak.
    jitter_smoothing: float = 0.85
    # Przesunięcie punktu startowego i końcowego (px) - nigdy nie trafiamy
    # dokładnie w ten sam piksel.
    endpoint_px: float = 3.0
    # Nacisk palca (zakres). Startowa wartość jest losowana z zakresu, a potem
    # zmienia się płynnie o najwyżej pressure_step na klatkę.
    pressure: tuple[int, int] = (40, 70)
    pressure_step: int = 2
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

    # Klatki dotyku mają równe odstępy w czasie (jak próbkowanie ekranu),
    # a easing przesuwa tylko POZYCJĘ - dzięki temu palec przyspiesza na
    # starcie i hamuje przy DROP.
    delay = duration / count
    low, high = profile.pressure
    pressure = rng.randint(low, high)
    # drżenie: proces AR(1) o stałym odchyleniu jitter_px, wygaszany na
    # początku i końcu gestu, żeby nie przesuwać punktów końcowych
    a = profile.jitter_smoothing
    noise_scale = profile.jitter_px * (1 - a * a) ** 0.5
    jx = jy = 0.0

    steps: list[Step] = [Step(0.0, sx, sy, pressure)]
    for i in range(1, count + 1):
        t_lin = i / count
        x, y = _bezier((sx, sy), p1, p2, (ex, ey), _ease(t_lin, profile.easing))
        jx = a * jx + rng.gauss(0, noise_scale)
        jy = a * jy + rng.gauss(0, noise_scale)
        taper = math.sin(math.pi * t_lin)
        if i < count:
            x += jx * taper
            y += jy * taper
        else:
            x, y = ex, ey
        pressure = min(max(pressure + rng.randint(-profile.pressure_step, profile.pressure_step), low), high)
        steps.append(Step(delay, x, y, pressure))
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
