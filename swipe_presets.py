"""
swipe_presets.py
================
Gotowe ustawienia przeciągania kulek (Dust Mine). Każdy preset to inny
RODZAJ gestu: kształt toru (prosty, łuk w górę/dół, S, zygzak, przestrzelenie,
szeroki łuk), czas, przytrzymanie na DROP, liczba punktów, drżenie i punkty
chwytu/puszczenia.

Moduł nie zależy od Qt - preset to tylko dane. `apply()` wpisuje go do
BotConfig, `values_from_config()` odwrotnie, a `sample_paths()` losuje
przykładowe tory do podglądu.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

import human_motion as humanize
from scheduler import RandomRange

Point = tuple[float, float]


@dataclass(frozen=True)
class SwipePreset:
    key: str
    name: str            # tekst do tłumaczenia (klucz w translations_en.json)
    description: str
    shape: str           # "auto" | "straight" | "arc" | "s" | "zigzag"
    bend_sign: int       # 1 / -1 / 0 = losowo
    bend: tuple[float, float]             # ułamek długości drogi (dla zygzaka: amplituda)
    overshoot: float                      # szansa na przestrzelenie celu
    duration: tuple[float, float]         # ms
    hold: tuple[float, float]             # ms, przytrzymanie na DROP
    points: tuple[int, int] | None        # None = automatycznie z długości drogi
    jitter: float                         # px
    grab: float                           # 0-1 promienia kulki
    release: float                        # 0-1 promienia DROP


PRESETS: list[SwipePreset] = [
    SwipePreset("default", "Domyślny", "Ustawienia fabryczne bota - mieszanka łuków i prostych.",
                "auto", 0, (0.04, 0.22), 0.2, (800, 1300), (180, 350), None, 1.2, 0.4, 0.3),
    SwipePreset("fast_jump", "Prosty skok", "Krótki, prawie prosty gest - kulka leci od razu na DROP.",
                "straight", 0, (0.0, 0.01), 0.0, (200, 300), (60, 120), (2, 3), 0.2, 0.1, 0.1),
    SwipePreset("arc_up", "Łuk w górę", "Palec idzie łukiem w jedną stronę, wyraźnie wygięty.",
                "arc", -1, (0.25, 0.35), 0.1, (900, 1200), (200, 300), (8, 11), 0.5, 0.4, 0.3),
    SwipePreset("arc_down", "Łuk w dół", "Jak „Łuk w górę”, tylko w drugą stronę.",
                "arc", 1, (0.25, 0.35), 0.1, (900, 1200), (200, 300), (8, 11), 0.5, 0.4, 0.3),
    SwipePreset("s_curve", "Litera S", "Tor zmienia stronę w połowie drogi - ruch jak litera S.",
                "s", 0, (0.20, 0.30), 0.1, (900, 1200), (200, 300), (8, 10), 0.8, 0.4, 0.3),
    SwipePreset("zigzag", "Zygzak", "Palec przeskakuje naprzemiennie na obie strony prostej.",
                "zigzag", 0, (0.10, 0.16), 0.0, (1000, 1400), (200, 300), (9, 13), 0.5, 0.4, 0.3),
    SwipePreset("overshoot", "Przestrzelenie", "Palec mija DROP i wraca - zawsze, nie tylko czasem.",
                "straight", 0, (0.0, 0.01), 1.0, (400, 600), (80, 150), (3, 4), 0.3, 0.3, 0.6),
    SwipePreset("wide_arc", "Szeroki łuk", "Bardzo szeroki, długi łuk z dużą liczbą punktów.",
                "arc", 0, (0.35, 0.45), 0.1, (1500, 2000), (300, 400), (14, 18), 0.6, 0.4, 0.3),
    SwipePreset("nervous", "Nerwowy", "Szarpany tor, mocno drżący, często przestrzeliwany.",
                "auto", 0, (0.15, 0.30), 0.5, (500, 900), (120, 250), (5, 9), 3.0, 0.6, 0.5),
    SwipePreset("chaos", "Chaos", "Losowo wszystko: kształt, czas, drżenie i punkty chwytu.",
                "auto", 0, (0.10, 0.35), 0.4, (300, 1500), (50, 400), (4, 12), 4.0, 0.8, 0.8),
]

_BY_KEY = {p.key: p for p in PRESETS}


def get(key: str) -> SwipePreset:
    return _BY_KEY[key]


def apply(cfg, key: str) -> None:
    """Wpisuje preset do configu (wartości w ms i ułamkach, tak jak w BotConfig)."""
    p = get(key)
    cfg.drag_duration_ms = RandomRange(*p.duration)
    cfg.drag_end_hold_ms = RandomRange(*p.hold)
    cfg.drag_points = RandomRange(*p.points) if p.points else None
    cfg.drag_jitter_px = p.jitter
    cfg.drag_grab_radius = p.grab
    cfg.drag_release_radius = p.release
    cfg.drag_bend = RandomRange(*p.bend)
    cfg.drag_shape = p.shape
    cfg.drag_bend_sign = p.bend_sign
    cfg.drag_overshoot = p.overshoot


def values_from_config(cfg) -> dict:
    """Aktualne wartości configu w tej samej postaci co preset (do podglądu)."""
    return {
        "duration": (cfg.drag_duration_ms.min, cfg.drag_duration_ms.max),
        "hold": (cfg.drag_end_hold_ms.min, cfg.drag_end_hold_ms.max),
        "points": (cfg.drag_points.min, cfg.drag_points.max) if cfg.drag_points else None,
        "jitter": cfg.drag_jitter_px,
        "grab": cfg.drag_grab_radius,
        "release": cfg.drag_release_radius,
        "bend": (cfg.drag_bend.min, cfg.drag_bend.max),
        "shape": cfg.drag_shape,
        "sign": cfg.drag_bend_sign or None,
        "overshoot": cfg.drag_overshoot,
    }


def sample_paths(values: dict, n: int = 6, seed: int = 0,
                 start: Point = (0.0, 0.0), end: Point = (1.0, 0.0),
                 ball_r: float = 1.0, drop_r: float = 1.0) -> list[list[Point]]:
    """Losuje `n` torów przeciągnięcia od kulki do DROP. Współrzędne są
    w tych samych jednostkach co start/end; promienie jak w bocie."""
    rng = random.Random(seed)
    pts = values["points"]
    lo, hi = (int(pts[0]), int(pts[1])) if pts else (4, 6)
    paths = []
    for _ in range(n):
        s = humanize.random_point_in_circle(start[0], start[1], ball_r * values["grab"], rng=rng)
        e = humanize.random_point_in_circle(end[0], end[1], drop_r * values["release"], rng=rng)
        paths.append(humanize.drag_path(s, e, rng=rng, min_points=lo, max_points=max(lo, hi),
                                        jitter=values["jitter"], bend=values["bend"],
                                        shape=values["shape"], bend_sign=values["sign"],
                                        overshoot=values["overshoot"]))
    return paths
