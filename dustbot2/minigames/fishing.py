"""
fishing.py
==========
Minigra "Fishing Hole": turkusowy wskaźnik jeździ w tę i z powrotem po
poziomym pasku ze stałą prędkością, a gracz naciska CAST, gdy wskaźnik jest
w jaśniejszej strefie. Po naciśnięciu wskaźnik zatrzymuje się w miejscu
rzutu. Pudło kończy rzut bez nagrody.

Pomiary z nagrania (LDPlayer 1080x2320): prędkość ok. 0.95 px/ms, jeden
przejazd przez pasek ok. 0.9 s, strefa ok. 157 px, czyli wskaźnik jest w
niej przez ok. 165 ms na przejazd. Ruch jest w pełni przewidywalny, więc
bot nie reaguje na to, co widzi, tylko oblicza moment, w którym wskaźnik
będzie w wybranym punkcie strefy, i naciska CAST odpowiednio wcześniej.

Opóźnienie (od zrobienia zrzutu do dotarcia tapu do gry) jest nieznane i
zależy od maszyny, dlatego LatencyCalibrator uczy się go z miejsc, w
których wskaźnik się zatrzymał po kolejnych rzutach.

Wszystkie współrzędne są w pikselach oryginalnej klatki, czas w ms.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

import cv2
import numpy as np


# ---------------------------------------------------------------------- #
# Detekcja na pojedynczej klatce
# ---------------------------------------------------------------------- #
@dataclass(frozen=True)
class FishingFrame:
    indicator_x: float                     # środek wskaźnika
    indicator_y: float
    indicator_w: float
    bar: tuple[float, float]               # wnętrze paska (lewa, prawa krawędź)
    zone: tuple[float, float] | None       # strefa; None, jeśli nie widać
    zone_partial: bool                     # wskaźnik zasłania krawędź strefy
    stopped: bool = False                  # wskaźnik biały = gra przyjęła rzut


def _indicator_mask(frame: np.ndarray) -> np.ndarray:
    """Turkusowy wskaźnik; po rzucie robi się biały z turkusową obwódką."""
    b = frame[..., 0].astype(np.int16)
    g = frame[..., 1].astype(np.int16)
    r = frame[..., 2].astype(np.int16)
    cyan = (b > 200) & (g > 180) & (r < 170)
    white = (b > 200) & (g > 200) & (r > 200)
    return (cyan | white).astype(np.uint8)


def detect(frame: np.ndarray) -> FishingFrame | None:
    """Znajduje wskaźnik, pasek i strefę. None, jeśli to nie ten ekran."""
    width = frame.shape[1]
    mask = _indicator_mask(frame)
    count, _, stats, _ = cv2.connectedComponentsWithStats(mask)
    best = None
    for i in range(1, count):
        x, y, w, h, area = stats[i]
        # wypełniona, pionowa "pigułka" - odróżnia ją od ramki przycisku CAST
        if not (0.05 * width <= h <= 0.2 * width and 0.015 * width <= w <= 0.08 * width):
            continue
        if h < 1.8 * w or area < 0.5 * w * h:
            continue
        if best is None or area > best[4]:
            best = (x, y, w, h, area)
    if best is None:
        return None
    px, py, pw, ph, _ = best
    cx, cy = px + pw / 2, py + ph / 2
    pill = frame[py:py + ph, px:px + pw].astype(np.int16)
    white = (pill[..., 0] > 200) & (pill[..., 1] > 200) & (pill[..., 2] > 200)
    stopped = bool(white.mean() > 0.4)

    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.int16)
    band = gray[int(cy - ph * 0.25): int(cy + ph * 0.25) + 1]
    column = np.median(band, axis=0)

    # wskaźnik i jego poświata zasłaniają kolumny wokół siebie
    hidden = np.zeros(width, dtype=bool)
    h0, h1 = max(int(px - pw), 0), min(int(px + 2 * pw) + 1, width)
    hidden[h0:h1] = True
    visible = column[~hidden]
    background = float(np.median(visible)) if visible.size else float(column.min())
    bright = column >= background + 6

    def scan(step: int) -> tuple[int, list[tuple[int, int]]]:
        """Idzie od wskaźnika na zewnątrz. Strefa to równo jasny pas; za
        krawędzią paska jest cienka ramka i ciemniejszy panel, więc pas o
        niskiej medianie jasności oznacza koniec paska."""
        x = h1 if step > 0 else h0 - 1
        runs: list[tuple[int, int]] = []
        while 0 < x < width - 1:
            if not bright[x]:
                x += step
                continue
            run_start = x
            while 0 < x < width - 1 and bright[x]:
                x += step
            a, b = sorted((run_start, x - step))
            if float(np.median(column[a:b + 1])) < background + 15:
                return run_start, runs
            runs.append((a, b))
        return x, runs

    right_edge, right_runs = scan(1)
    left_edge, left_runs = scan(-1)
    bar = (float(left_edge + 1), float(right_edge - 1))

    touching = [r for r in left_runs + right_runs if r[0] == h1 or r[1] == h0 - 1]
    if touching:
        # strefa przechodzi pod wskaźnikiem - łączymy obie strony
        z0 = min(min(r[0] for r in touching), h0)
        z1 = max(max(r[1] for r in touching), h1 - 1)
        if not any(r[1] == h0 - 1 for r in touching):
            z0 = h0
        if not any(r[0] == h1 for r in touching):
            z1 = h1 - 1
        zone, partial = (float(z0), float(z1)), True
    else:
        wide = [r for r in left_runs + right_runs if r[1] - r[0] >= 0.02 * width]
        if not wide:
            return FishingFrame(cx, cy, pw, bar, None, False, stopped)
        z0, z1 = max(wide, key=lambda r: r[1] - r[0])
        zone, partial = (float(z0), float(z1)), False
    return FishingFrame(cx, cy, pw, bar, zone, partial, stopped)


# ---------------------------------------------------------------------- #
# Model ruchu: tam i z powrotem ze stałą prędkością
# ---------------------------------------------------------------------- #
def _triangle(t: np.ndarray, lo: float, hi: float, speed: float, phase: np.ndarray) -> np.ndarray:
    """Pozycja wskaźnika w chwili t; `phase` to chwila, w której był w `lo`
    i ruszał w prawo."""
    span = hi - lo
    u = np.mod(t - phase, 2 * span / speed) * speed
    return np.where(u <= span, lo + u, lo + 2 * span - u)


@dataclass
class FishingTracker:
    """
    Zbiera obserwacje (czas, pozycja) jednego rzutu i przewiduje ruch
    wskaźnika: tam i z powrotem między `bounds` ze stałą prędkością.

    Pojedyncze klatki mają kilkadziesiąt ms szumu w czasie, a bot robi
    zrzuty dość rzadko (kilka na sekundę), więc model nie opiera się na
    ostatniej klatce. Prędkość i fazę dopasowujemy do WSZYSTKICH obserwacji
    naraz (przeszukanie siatki, błąd każdej klatki ograniczony, żeby
    pojedyncze złe klatki nie psuły wyniku). Granice ruchu pochodzą z
    wykrytego paska (mediana z klatek), poszerzone do skrajnych
    zaobserwowanych pozycji.

    `speed_per_width` - prędkość jako ułamek szerokości ekranu na ms (z
    nagrania: 0.99 px/ms przy 1080, taka sama w obu kierunkach i na
    wszystkich odcinkach). Dopasowanie szuka tylko w +-`speed_range`, bo
    przy kilku obserwacjach swobodna prędkość jest niedokładna, a jej błąd
    mnoży się przez czas do przewidywanego momentu.
    """

    speed_per_width: float = 0.99 / 1080
    speed_range: float = 0.03
    frame_width: float = 1080.0
    observations: list[tuple[float, float]] = field(default_factory=list)
    zone: tuple[float, float] | None = None
    _bars: list[tuple[float, float]] = field(default_factory=list)
    _half_width: float = 0.0
    _zones: list[tuple[float, float]] = field(default_factory=list)
    _fit_cache: tuple[int, tuple[float, float, float] | None] | None = None

    def update(self, t_ms: float, detection: FishingFrame) -> None:
        self._bars.append(detection.bar)
        self._half_width = detection.indicator_w / 2
        if detection.zone is not None and not detection.zone_partial:
            self._zones.append(detection.zone)
            self.zone = (float(np.median([z[0] for z in self._zones])),
                         float(np.median([z[1] for z in self._zones])))
        self.observations.append((t_ms, detection.indicator_x))

    @property
    def bounds(self) -> tuple[float, float] | None:
        if not self._bars:
            return None
        lo = float(np.median([b[0] for b in self._bars])) + self._half_width
        hi = float(np.median([b[1] for b in self._bars])) - self._half_width
        # wskaźnik nie wyjeżdża poza granice ruchu - jeśli go tam widzieliśmy,
        # to granica z wykrytego paska jest za wąska
        xs = [x for _, x in self.observations]
        if xs:
            lo, hi = min(lo, min(xs)), max(hi, max(xs))
        return lo, hi

    def fit(self) -> tuple[float, float, float] | None:
        """(prędkość px/ms, faza ms, średni błąd px) albo None przy zbyt małej
        liczbie obserwacji (min. 3 obejmujące 150 ms)."""
        if self._fit_cache and self._fit_cache[0] == len(self.observations):
            return self._fit_cache[1]
        result = None
        if len(self.observations) >= 3 and self.bounds is not None:
            t = np.array([a for a, _ in self.observations])
            x = np.array([b for _, b in self.observations])
            if np.ptp(t) >= 150:
                lo, hi = self.bounds
                base = self.speed_per_width * self.frame_width
                best = None
                for speed in base * np.linspace(1 - self.speed_range, 1 + self.speed_range, 13):
                    period = 2 * (hi - lo) / speed
                    phases = t[-1] - np.arange(0.0, period, 2.0)
                    model = _triangle(t[None, :], lo, hi, speed, phases[:, None])
                    cost = np.minimum(np.abs(model - x[None, :]), 60.0).mean(axis=1)
                    i = int(np.argmin(cost))
                    if best is None or cost[i] < best[2]:
                        best = (float(speed), float(phases[i]), float(cost[i]))
                speed, phase, _ = best
                fine = phase + np.arange(-2.0, 2.0, 0.25)
                model = _triangle(t[None, :], lo, hi, speed, fine[:, None])
                cost = np.minimum(np.abs(model - x[None, :]), 60.0).mean(axis=1)
                i = int(np.argmin(cost))
                result = (speed, float(fine[i]), float(cost[i]))
        self._fit_cache = (len(self.observations), result)
        return result

    @property
    def speed(self) -> float | None:
        fitted = self.fit()
        return fitted[0] if fitted else None

    def position_at(self, t_ms: float) -> float | None:
        fitted = self.fit()
        if fitted is None:
            return None
        lo, hi = self.bounds
        return float(_triangle(np.array(t_ms), lo, hi, fitted[0], np.array(fitted[1])))

    def next_time_at(self, target_x: float, after_ms: float) -> tuple[float, int] | None:
        """Najbliższa chwila >= after_ms, w której środek wskaźnika minie
        target_x, oraz kierunek ruchu w tej chwili (+1 w prawo, -1 w lewo)."""
        fitted = self.fit()
        if fitted is None:
            return None
        speed, phase, _ = fitted
        lo, hi = self.bounds
        span = hi - lo
        period = 2 * span / speed
        offset = min(max(target_x - lo, 0.0), span)
        best = None
        for direction, u in ((1, offset), (-1, 2 * span - offset)):
            first = phase + u / speed
            k = np.ceil((after_ms - first) / period)
            when = first + k * period
            if best is None or when < best[0]:
                best = (float(when), direction)
        return best


# ---------------------------------------------------------------------- #
# Celowanie i kalibracja opóźnienia
# ---------------------------------------------------------------------- #
@dataclass
class AimProfile:
    """
    Gdzie w strefie celować. Odchylenie jest ułamkiem szerokości strefy
    (0 = zawsze środek). `miss_rate` > 0 celowo pudłuje część rzutów -
    przydatne w testach detekcji, żeby wyniki nie były idealne.
    """

    spread: float = 0.18
    miss_rate: float = 0.0


@dataclass
class CastPlan:
    tap_at_ms: float        # kiedy wysłać tap (zegar obserwacji)
    target_x: float
    hit_at_ms: float        # kiedy wskaźnik powinien być w target_x
    direction: int


def plan_cast(tracker: FishingTracker, now_ms: float, latency_ms: float,
              aim: AimProfile = AimProfile(), rng: random.Random | None = None,
              min_lead_ms: float = 40.0) -> CastPlan | None:
    """
    Wybiera punkt w strefie i najbliższy moment, w którym da się w niego
    trafić, uwzględniając opóźnienie. None, jeśli jeszcze za mało danych.
    """
    rng = rng or random.Random()
    if tracker.zone is None:
        return None
    z0, z1 = tracker.zone
    width = z1 - z0
    center = (z0 + z1) / 2
    if aim.miss_rate and rng.random() < aim.miss_rate:
        lo, hi = tracker.bounds or (z0 - width, z1 + width)
        outside = [x for x in (z0 - 0.6 * width, z1 + 0.6 * width) if lo < x < hi]
        target = rng.choice(outside) if outside else center
    else:
        offset = max(min(rng.gauss(0, aim.spread), 0.4), -0.4)  # zostajemy w strefie
        target = center + offset * width
    crossing = tracker.next_time_at(target, now_ms + latency_ms + min_lead_ms)
    if crossing is None:
        return None
    hit_at, direction = crossing
    return CastPlan(hit_at - latency_ms, target, hit_at, direction)


@dataclass
class LatencyCalibrator:
    """
    Po rzucie wskaźnik staje w miejscu, w którym gra przyjęła tap. Różnica
    między tym miejscem a celem, podzielona przez prędkość, to błąd
    opóźnienia - poprawiamy szacunek średnią kroczącą.
    """

    latency_ms: float = 120.0
    gain: float = 0.5
    max_step_ms: float = 150.0  # jeden zły pomiar nie rozreguluje opóźnienia
    history: list[float] = field(default_factory=list)

    def update(self, plan: CastPlan, stopped_x: float, speed: float) -> float:
        error_ms = (stopped_x - plan.target_x) * plan.direction / speed
        self.history.append(error_ms)
        step = max(min(self.gain * error_ms, self.max_step_ms), -self.max_step_ms)
        self.latency_ms = max(self.latency_ms + step, 0.0)
        return error_ms


def stopped_position(observations: list[tuple[float, float]], tolerance_px: float = 4.0,
                     min_span_ms: float = 50.0) -> float | None:
    """
    Pozycja, w której wskaźnik się zatrzymał (po naciśnięciu CAST), albo
    None, jeśli nadal jedzie. Sprawdza dwie ostatnie obserwacje.
    """
    if len(observations) < 2:
        return None
    (t0, x0), (t1, x1) = observations[-2:]
    if t1 - t0 >= min_span_ms and abs(x1 - x0) <= tolerance_px:
        return x1
    return None

