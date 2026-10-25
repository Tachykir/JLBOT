"""
swipe_presets.py
================
Gotowe ustawienia przeciągania kulek (Dust Mine): czas, przytrzymanie na
DROP, liczba punktów toru, drżenie, wygięcie łuku i punkty chwytu/puszczenia.

Moduł nie zależy od Qt - preset to tylko słownik zakresów. `apply()` wpisuje
go do BotConfig, `values_from_config()` odwrotnie, a `sample_paths()` losuje
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
    duration: tuple[float, float]         # ms
    hold: tuple[float, float]             # ms, przytrzymanie na DROP
    points: tuple[int, int] | None        # None = automatycznie z długości drogi
    jitter: float                         # px
    grab: float                           # 0-1 promienia kulki
    release: float                        # 0-1 promienia DROP
    bend: tuple[float, float]             # ułamek długości drogi


PRESETS: list[SwipePreset] = [
    SwipePreset("default", "Domyślny", "Ustawienia fabryczne bota.",
                (800, 1300), (180, 350), None, 1.2, 0.4, 0.3, (0.04, 0.22)),
    SwipePreset("fast", "Szybki i prosty", "Krótki, prawie prosty gest - kulka trafia od razu.",
                (200, 300), (60, 120), (3, 4), 0.3, 0.2, 0.2, (0.02, 0.05)),
    SwipePreset("slow", "Powolny i płynny", "Długi, gładki ruch z wieloma punktami.",
                (1200, 1800), (250, 400), (10, 14), 0.5, 0.4, 0.3, (0.05, 0.12)),
    SwipePreset("nervous", "Nerwowy", "Szarpany tor, mocno drżący, z dużym wygięciem.",
                (500, 900), (120, 250), (5, 9), 2.5, 0.6, 0.5, (0.15, 0.30)),
    SwipePreset("precise", "Precyzyjny", "Chwyt i puszczenie blisko środków, prawie bez drżenia.",
                (700, 900), (300, 400), (6, 8), 0.2, 0.05, 0.05, (0.02, 0.04)),
    SwipePreset("wide_grab", "Szeroki chwyt", "Palec łapie kulkę i puszcza ją gdzieś na brzegu DROP.",
                (800, 1100), (200, 300), None, 1.0, 0.9, 0.8, (0.04, 0.15)),
    SwipePreset("s_curve", "Łuk S", "Wyraźnie wygięty tor, jak przy zamaszystym ruchu kciuka.",
                (900, 1200), (200, 300), (8, 10), 0.8, 0.4, 0.3, (0.20, 0.32)),
    SwipePreset("many_points", "Dużo punktów", "Tor z wieloma krótkimi krokami - bardzo płynny.",
                (1000, 1400), (200, 300), (16, 20), 0.6, 0.4, 0.3, (0.06, 0.14)),
    SwipePreset("long_hold", "Długie przytrzymanie", "Palec stoi na DROP dłużej, zanim puści kulkę.",
                (800, 1000), (500, 800), (5, 7), 1.0, 0.3, 0.2, (0.04, 0.10)),
    SwipePreset("chaos", "Chaos", "Losowo wszystko: czas, wygięcie, drżenie i punkty.",
                (300, 1500), (50, 400), (4, 12), 4.0, 0.8, 0.8, (0.10, 0.35)),
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
                                        jitter=values["jitter"], bend=values["bend"]))
    return paths
