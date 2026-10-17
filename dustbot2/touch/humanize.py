"""
humanize.py
===========
Generator przeciągnięć o zmiennym przebiegu.

Każdy gest składa się z trzech niezależnych części:
  1. KSZTAŁT ścieżki - losowany z wagami spośród wariantów zaobserwowanych
     w nagraniach prawdziwych przeciągnięć (prawie prosto, łuk, S, haczyk
     z przelotem za cel, "L" z zakrętem, krótki rzut). Ścieżka to gładka
     krzywa (centripetal Catmull-Rom) przez kilka punktów pośrednich.
  2. PRĘDKOŚĆ wzdłuż ścieżki - przyspieszenie i hamowanie z losowym
     przesunięciem szczytu; nakładana na długość łuku, więc nie zależy
     od kształtu.
  3. SZUM - wolnozmienne drżenie palca i płynnie zmieniający się nacisk.

Parametry są w jednym miejscu (DragProfile), a wynik (DragPlan) zawiera
nazwę kształtu - można je logować razem z wynikami, żeby porównywać
profile w testach detekcji anticheatu.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass

import numpy as np

SHAPES = ("straight", "arc", "s_curve", "hook", "elbow", "flick")


@dataclass(frozen=True)
class DragProfile:
    # Czas gestu (ms): rozkład log-normalny wokół mediany, ograniczony do
    # zakresu przez ponowne losowanie (przycinanie zbierałoby wartości na
    # granicy w sztuczny pik). Jeśli duration_ms jest ustawione, losujemy
    # jednostajnie z niego.
    duration_median_ms: float = 650.0
    duration_sigma: float = 0.35
    duration_clamp: tuple[float, float] = (180.0, 1300.0)
    duration_ms: tuple[float, float] | None = None
    # Odstęp między kolejnymi klatkami dotyku (ms).
    frame_ms: tuple[float, float] = (8.5, 11.0)
    # Wagi kształtów ścieżki (nazwa, waga). "flick" puszcza kulkę przed
    # celem w pełnym ruchu - działa tylko, jeśli gra przenosi pęd kulki.
    shapes: tuple[tuple[str, float], ...] = (
        ("straight", 0.30),
        ("arc", 0.25),
        ("s_curve", 0.15),
        ("hook", 0.12),
        ("elbow", 0.08),
        ("flick", 0.10),
    )
    # Drżenie palca w pikselach (odchylenie standardowe), wewnątrz gestu.
    jitter_px: float = 1.2
    # Jak bardzo drżenie w kolejnej klatce zależy od poprzedniej (0-1).
    # Prawdziwe drżenie ręki jest wolnozmienne; 0 dałoby poszarpany zygzak.
    jitter_smoothing: float = 0.85
    # Rozrzut punktu startu (na kulce) i puszczenia (w otworze DROP), px.
    start_px: float = 4.0
    target_px: float = 8.0
    # Nacisk palca (zakres). Startowa wartość jest losowana z zakresu, a potem
    # zmienia się płynnie o najwyżej pressure_step na klatkę.
    pressure: tuple[int, int] = (40, 70)
    pressure_step: int = 2
    # Kształt prędkości: easing 0 = stała prędkość, 1 = pełne ease-in-out;
    # skew < 1 przesuwa szczyt prędkości na początek, > 1 pod koniec gestu.
    easing: tuple[float, float] = (0.4, 1.0)
    skew: tuple[float, float] = (0.75, 1.35)


@dataclass(frozen=True)
class Step:
    delay_ms: float
    x: float
    y: float
    pressure: int


@dataclass(frozen=True)
class DragPlan:
    shape: str
    steps: list[Step]

    @property
    def duration_ms(self) -> float:
        return sum(s.delay_ms for s in self.steps)


def _ease(t: float, amount: float) -> float:
    """Mieszanka liniowego czasu i ease-in-out (smoothstep)."""
    smooth = t * t * (3 - 2 * t)
    return (1 - amount) * t + amount * smooth


def _catmull_rom(points: np.ndarray, samples: int = 40) -> np.ndarray:
    """Gładka krzywa przez wszystkie punkty (wariant centripetal - bez pętli)."""
    if len(points) == 2:
        return np.linspace(points[0], points[1], samples)
    padded = np.vstack([2 * points[0] - points[1], points, 2 * points[-1] - points[-2]])
    out = []
    for i in range(len(padded) - 3):
        p0, p1, p2, p3 = padded[i:i + 4]
        t0 = 0.0
        t1 = t0 + max(np.linalg.norm(p1 - p0), 1e-6) ** 0.5
        t2 = t1 + max(np.linalg.norm(p2 - p1), 1e-6) ** 0.5
        t3 = t2 + max(np.linalg.norm(p3 - p2), 1e-6) ** 0.5
        last = i == len(padded) - 4
        for t in np.linspace(t1, t2, samples, endpoint=last):
            a1 = (t1 - t) / (t1 - t0) * p0 + (t - t0) / (t1 - t0) * p1
            a2 = (t2 - t) / (t2 - t1) * p1 + (t - t1) / (t2 - t1) * p2
            a3 = (t3 - t) / (t3 - t2) * p2 + (t - t2) / (t3 - t2) * p3
            b1 = (t2 - t) / (t2 - t0) * a1 + (t - t0) / (t2 - t0) * a2
            b2 = (t3 - t) / (t3 - t1) * a2 + (t - t1) / (t3 - t1) * a3
            out.append((t2 - t) / (t2 - t1) * b1 + (t - t1) / (t2 - t1) * b2)
    return np.array(out)


def _waypoints(shape: str, s: np.ndarray, e: np.ndarray, rng: random.Random) -> tuple[str, np.ndarray]:
    """
    Punkty, przez które przechodzi ścieżka danego kształtu. Zwraca też nazwę
    kształtu, bo "L" przy gestach prawie wzdłuż osi ekranu zamienia się w łuk.
    """
    d = e - s
    length = float(np.linalg.norm(d)) or 1.0
    u = d / length                      # kierunek gestu
    n = np.array([-u[1], u[0]])         # prostopadła do kierunku
    side = rng.choice((-1.0, 1.0))

    if shape == "straight":
        bend = side * rng.uniform(0.0, 0.06) * length
        return shape, np.array([s, s + d * rng.uniform(0.4, 0.6) + n * bend, e])
    if shape in ("arc", "flick"):
        bend = side * rng.uniform(0.08, 0.28) * length
        if shape == "flick":
            bend *= 0.5
        return shape, np.array([s, s + d * rng.uniform(0.35, 0.65) + n * bend, e])
    if shape == "s_curve":
        bend = side * rng.uniform(0.06, 0.16) * length
        return shape, np.array([s, s + d / 3 + n * bend, s + 2 * d / 3 - n * bend, e])
    if shape == "hook":
        # przelot za cel i powrót - "zawijas" przy DROP
        overshoot = e + u * rng.uniform(0.05, 0.14) * length + n * side * rng.uniform(0.03, 0.1) * length
        mid = s + d * rng.uniform(0.45, 0.6) + n * -side * rng.uniform(0.0, 0.08) * length
        return shape, np.array([s, mid, overshoot, e])
    if shape == "elbow":
        # najpierw wzdłuż jednej osi ekranu, potem drugiej
        corner = np.array([s[0], e[1]]) if rng.random() < 0.5 else np.array([e[0], s[1]])
        # gest prawie wzdłuż osi: zakręt wypadłby przy starcie albo celu,
        # a krzywa zrobiłaby pętlę - wtedy rysujemy zwykły łuk
        if min(np.linalg.norm(corner - s), np.linalg.norm(corner - e)) < 0.25 * length:
            return _waypoints("arc", s, e, rng)
        corner = corner + np.array([rng.gauss(0, 0.04), rng.gauss(0, 0.04)]) * length
        return shape, np.array([s, corner, e])
    raise ValueError(f"Nieznany kształt: {shape}")


def _inside(curve: np.ndarray, bounds: tuple[float, float, float, float], margin: float) -> bool:
    x1, y1, x2, y2 = bounds
    return bool(
        (curve[:, 0] >= x1 + margin).all() and (curve[:, 0] <= x2 - margin).all()
        and (curve[:, 1] >= y1 + margin).all() and (curve[:, 1] <= y2 - margin).all()
    )


def sample_duration(profile: DragProfile, rng: random.Random) -> float:
    """Czas gestu (ms) z rozkładu profilu - używany też dla adb input swipe."""
    if profile.duration_ms is not None:
        return rng.uniform(*profile.duration_ms)
    low, high = profile.duration_clamp
    for _ in range(100):
        value = profile.duration_median_ms * math.exp(rng.gauss(0, profile.duration_sigma))
        if low <= value <= high:
            return value
    return profile.duration_median_ms


def plan_drag(
    start: tuple[int, int],
    end: tuple[int, int],
    profile: DragProfile = DragProfile(),
    rng: random.Random | None = None,
    shape: str | None = None,
    bounds: tuple[float, float, float, float] | None = None,
) -> DragPlan:
    """
    Planuje gest od `start` (kulka) do `end` (DROP). `shape` wymusza kształt;
    domyślnie losowany z wag w profilu. `bounds` (x1, y1, x2, y2, np. obszar
    planszy) - ścieżka, która by z nich wyszła, jest losowana od nowa.
    Pierwszy krok ma delay_ms=0 i oznacza wciśnięcie, ostatni - miejsce
    puszczenia.
    """
    rng = rng or random.Random()
    requested = shape
    for attempt in range(10):
        shape = requested
        if shape is None:
            names, weights = zip(*profile.shapes)
            shape = rng.choices(names, weights=weights)[0]
        if attempt == 9 and bounds is not None:
            shape = "straight"  # ostatnia próba: najkrótsza możliwa ścieżka

        s = np.array([start[0] + rng.gauss(0, profile.start_px), start[1] + rng.gauss(0, profile.start_px)])
        e = np.array([end[0] + rng.gauss(0, profile.target_px), end[1] + rng.gauss(0, profile.target_px)])
        shape, points = _waypoints(shape, s, e, rng)
        curve = _catmull_rom(points)
        if bounds is None or _inside(curve, bounds, margin=3 * profile.jitter_px):
            break
    seg = np.linalg.norm(np.diff(curve, axis=0), axis=1)
    arc = np.concatenate([[0.0], np.cumsum(seg)])
    total = arc[-1] or 1.0

    duration = sample_duration(profile, rng)
    # rzut: krótszy, puszczony przed celem, bez hamowania (palec wciąż przyspiesza)
    release_at = 1.0
    if shape == "flick":
        duration = max(duration * rng.uniform(0.3, 0.5), profile.duration_clamp[0])
        release_at = rng.uniform(0.55, 0.85)
    frame_ms = rng.uniform(*profile.frame_ms)
    count = max(int(duration / frame_ms), 2)
    delay = duration / count  # klatki w równych odstępach jak próbkowanie ekranu

    easing = rng.uniform(*profile.easing)
    skew = rng.uniform(*profile.skew)
    flick_power = rng.uniform(1.3, 1.8)

    low, high = profile.pressure
    pressure = rng.randint(low, high)
    # drżenie: proces AR(1) o stałym odchyleniu jitter_px, wygaszany na
    # początku i końcu gestu, żeby nie przesuwać punktów końcowych
    a = profile.jitter_smoothing
    noise_scale = profile.jitter_px * (1 - a * a) ** 0.5
    jx = jy = 0.0

    steps: list[Step] = [Step(0.0, float(curve[0][0]), float(curve[0][1]), pressure)]
    for i in range(1, count + 1):
        t = i / count
        if shape == "flick":
            progress = release_at * t ** flick_power
        else:
            progress = _ease(t ** skew, easing)
        dist = progress * total
        x = float(np.interp(dist, arc, curve[:, 0]))
        y = float(np.interp(dist, arc, curve[:, 1]))
        jx = a * jx + rng.gauss(0, noise_scale)
        jy = a * jy + rng.gauss(0, noise_scale)
        if i < count or shape == "flick":
            # rzut kończy się w ruchu, więc drżenie nie musi tam wygasać
            taper = math.sin(math.pi * min(t, 0.5)) if shape == "flick" else math.sin(math.pi * t)
            x += jx * taper
            y += jy * taper
        pressure = min(max(pressure + rng.randint(-profile.pressure_step, profile.pressure_step), low), high)
        steps.append(Step(delay, x, y, pressure))
    return DragPlan(shape, steps)


def straightness(steps: list[Step]) -> float:
    """Odległość start-koniec podzielona przez długość ścieżki (1.0 = linia prosta)."""
    path = sum(math.hypot(b.x - a.x, b.y - a.y) for a, b in zip(steps, steps[1:]))
    chord = math.hypot(steps[-1].x - steps[0].x, steps[-1].y - steps[0].y)
    return chord / path if path else 1.0


def to_commands(steps: list[Step], client, contact: int = 0) -> list[str]:
    """
    Zamienia kroki na polecenia minitouch. Każdy krok jest osobno
    zatwierdzany ("c") - protokół nie pozwala na dwa zdarzenia tego samego
    palca w jednym zatwierdzeniu, a bez "c" nic nie trafia na ekran.
    """
    first = steps[0]
    commands = [client.down(contact, first.x, first.y, first.pressure), "c"]
    for step in steps[1:]:
        commands += [client.wait(step.delay_ms), client.move(contact, step.x, step.y, step.pressure), "c"]
    commands += [client.up(contact), "c"]
    return commands
