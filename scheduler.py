"""
scheduler.py
============
Harmonogram pracy bota: okna czasowe, losowe sesje gry z przerwami
wewnątrz okien i dzienny limit czasu gry.

Model:
  - Okno (ScheduleWindow) mówi, KIEDY bot w ogóle może grać, np.
    09:00-13:00 w dni robocze. Poza wszystkimi oknami bot jest offline.
  - W obrębie okna bot sam układa sobie sesje: gra przez losowy czas
    (session_minutes), potem robi losową przerwę (break_minutes), i tak
    do końca okna. Okno bez session_minutes = gra przez całe okno.
  - Granice okna są losowo "rozmyte" (jitter_minutes): start przesuwa
    się o 0..jitter minut później, koniec o 0..jitter minut wcześniej,
    osobno dla każdego dnia - bot nie zaczyna codziennie o tej samej
    minucie.
  - Dzienny limit (daily_limit_hours w BotConfig) - losowany raz na
    dzień kalendarzowy; po jego wyczerpaniu bot jest offline do północy.

Brak okien = bot gra zawsze (z ewentualnym limitem dziennym).

SchedulePlanner jest czystą logiką bez ADB i bez czekania - dostaje
aktualny czas i odpowiada "grać czy nie, i do kiedy", co pozwala go
łatwo testować.
"""

from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass


@dataclass
class RandomRange:
    """
    Zakres losowania (min, max) dla dowolnego parametru czasowego -
    zamiast stałej wartości, bot losuje z tego przedziału za każdym
    razem, żeby unikać w pełni mechanicznego, identycznego rytmu.

    Jeśli min == max, zachowuje się jak stała wartość (brak losowości).
    """
    min: float
    max: float

    def sample(self) -> float:
        if self.max <= self.min:
            return self.min
        return random.uniform(self.min, self.max)

    @staticmethod
    def from_value(value) -> "RandomRange":
        """
        Buduje RandomRange z wartości configu, obsługując trzy formaty:
          - liczba (int/float): stała wartość, min == max == value
          - [min, max] / (min, max): zakres
          - {"min": ..., "max": ...}: zakres jako słownik (jak w JSON)
        """
        if isinstance(value, RandomRange):
            return value
        if isinstance(value, dict):
            return RandomRange(float(value["min"]), float(value["max"]))
        if isinstance(value, (list, tuple)):
            return RandomRange(float(value[0]), float(value[1]))
        return RandomRange(float(value), float(value))

    def to_dict(self) -> dict:
        return {"min": self.min, "max": self.max}


def _optional_range(value) -> RandomRange | None:
    return RandomRange.from_value(value) if value is not None else None


@dataclass
class ScheduleWindow:
    """
    Jedno okno czasowe, w którym bot MOŻE grać.

    start / end: "HH:MM" (24h). Okno przechodzące przez północ (np.
    "22:00"-"02:00") jest obsługiwane. start == end oznacza pełną dobę.

    days: dni tygodnia, w które okno się ZACZYNA (0=poniedziałek ...
    6=niedziela). None = każdy dzień. Dla okna 22:00-02:00 z days=(4,)
    bot gra od piątku 22:00 do soboty 02:00.

    session_minutes: zakres długości jednej sesji gry (minuty). None =
    bez sesji, bot gra przez całe okno bez przerw.
    break_minutes: zakres długości przerwy między sesjami (minuty).
    jitter_minutes: maksymalne losowe przesunięcie startu (później) i
    końca (wcześniej) okna, losowane osobno każdego dnia. 0 = dokładne
    granice.
    """
    start: str
    end: str
    days: tuple[int, ...] | None = None
    session_minutes: RandomRange | None = None
    break_minutes: RandomRange | None = None
    jitter_minutes: float = 0.0

    def __post_init__(self) -> None:
        # Walidacja od razu przy tworzeniu - błędny wpis w configu ma
        # wywalić się czytelnym komunikatem przy wczytywaniu, a nie
        # wyjątkiem w pętli bota.
        self._parse(self.start)
        self._parse(self.end)
        if self.days is not None:
            for d in self.days:
                if not (isinstance(d, int) and 0 <= d <= 6):
                    raise ValueError(f"Nieprawidłowy dzień tygodnia w harmonogramie: {d!r} (dozwolone 0-6)")
        if self.session_minutes is not None and self.session_minutes.min <= 0:
            raise ValueError(f"Długość sesji musi być większa od 0 (okno {self.start}-{self.end})")
        if self.break_minutes is not None and self.break_minutes.min < 0:
            raise ValueError(f"Długość przerwy nie może być ujemna (okno {self.start}-{self.end})")
        if self.jitter_minutes < 0:
            raise ValueError(f"Rozmycie granic nie może być ujemne (okno {self.start}-{self.end})")

    @staticmethod
    def _parse(hhmm: str) -> tuple[int, int]:
        try:
            h, m = hhmm.split(":")
            h, m = int(h), int(m)
        except (AttributeError, ValueError):
            raise ValueError(f"Nieprawidłowa godzina w harmonogramie: {hhmm!r} (oczekiwano HH:MM)") from None
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError(f"Nieprawidłowa godzina w harmonogramie: {hhmm!r} (oczekiwano HH:MM)")
        return h, m

    def bounds_for(self, day: dt.date) -> tuple[dt.datetime, dt.datetime] | None:
        """Nominalne (bez rozmycia) granice okna zaczynającego się w dniu
        `day`, albo None, jeśli okno nie obowiązuje tego dnia."""
        if self.days is not None and day.weekday() not in self.days:
            return None
        sh, sm = self._parse(self.start)
        eh, em = self._parse(self.end)
        start = dt.datetime.combine(day, dt.time(sh, sm))
        end = dt.datetime.combine(day, dt.time(eh, em))
        if end <= start:
            end += dt.timedelta(days=1)
        return start, end


def schedule_from_list(raw: list | None) -> list[ScheduleWindow]:
    """Buduje listę ScheduleWindow z surowych danych JSON."""
    if not raw:
        return []
    windows = []
    for item in raw:
        days = item.get("days")
        windows.append(ScheduleWindow(
            start=item["start"],
            end=item["end"],
            days=tuple(days) if days is not None else None,
            session_minutes=_optional_range(item.get("session_minutes")),
            break_minutes=_optional_range(item.get("break_minutes")),
            jitter_minutes=float(item.get("jitter_minutes") or 0.0),
        ))
    return windows


def schedule_to_list(schedule: list[ScheduleWindow]) -> list[dict]:
    """Odwrotność schedule_from_list - do zapisu w JSON."""
    return [
        {
            "start": w.start,
            "end": w.end,
            "days": list(w.days) if w.days is not None else None,
            "session_minutes": w.session_minutes.to_dict() if w.session_minutes else None,
            "break_minutes": w.break_minutes.to_dict() if w.break_minutes else None,
            "jitter_minutes": w.jitter_minutes,
        }
        for w in schedule
    ]


# --------------------------------------------------------------------- #
# Planner
# --------------------------------------------------------------------- #

# Sesja krótsza niż to nie ma sensu (restart gry na 2 minuty) - jeśli
# do końca okna zostało mniej, bot zostaje offline; jeśli po planowanym
# końcu sesji zostałoby mniej, sesja jest przedłużana do końca okna.
MIN_SESSION = dt.timedelta(minutes=5)

# Maksymalny odstęp między wywołaniami decide(), który liczymy jako
# czas gry do limitu dziennego - dłuższa przerwa (np. uśpiony komputer)
# nie jest wliczana.
_MAX_ACCOUNT_GAP_S = 300.0


@dataclass
class Decision:
    play: bool
    # "always" | "session" | "outside" | "break" | "daily_limit"
    reason: str
    # Kiedy ten stan najpóźniej się zmieni (koniec sesji / przerwy /
    # start okna / północ). None = bez określonego końca.
    until: dt.datetime | None = None

    def describe(self) -> str:
        t = f"{self.until:%H:%M}" if self.until else None
        if self.reason == "always":
            return "gra (bez harmonogramu)"
        if self.reason == "session":
            return f"sesja do {t}"
        if self.reason == "break":
            return f"przerwa do {t}"
        if self.reason == "daily_limit":
            return f"limit dzienny wyczerpany (do {t})"
        if self.until is not None:
            day = "" if self.until.date() == dt.date.today() else f"{self.until:%d.%m} "
            return f"poza harmonogramem (start {day}{self.until:%H:%M})"
        return "poza harmonogramem"


class SchedulePlanner:
    """
    Decyduje, czy bot powinien teraz grać. Czyta harmonogram na żywo z
    obiektu configu (atrybuty `schedule` i `daily_limit_hours`), więc
    zmiany z GUI działają od razu.

    decide() trzeba wywoływać regularnie w trakcie gry - na tej podstawie
    liczony jest czas gry do limitu dziennego.
    """

    def __init__(self, config):
        self.config = config
        self._schedule_ref = None
        # (indeks okna, dzień startu) -> (start, koniec) po rozmyciu
        self._bounds_cache: dict[tuple[int, dt.date], tuple[dt.datetime, dt.datetime]] = {}
        self._session_key: tuple[int, dt.date] | None = None
        self._session_end: dt.datetime | None = None
        self._break_until: dt.datetime | None = None
        self._played_s: dict[dt.date, float] = {}
        self._limit_s: dict[dt.date, float] = {}
        self._last_tick: dt.datetime | None = None
        self._last_play = False

    # ------------------------------------------------------------------ #
    def played_today(self, now: dt.datetime) -> float:
        """Sekundy gry policzone w dniu `now`."""
        return self._played_s.get(now.date(), 0.0)

    def _reset_if_schedule_changed(self) -> None:
        if self.config.schedule is not self._schedule_ref:
            self._schedule_ref = self.config.schedule
            self._bounds_cache.clear()
            self._session_key = None
            self._session_end = None
            self._break_until = None

    def _account(self, now: dt.datetime) -> None:
        if self._last_tick is not None and self._last_play:
            delta = (now - self._last_tick).total_seconds()
            if 0 < delta <= _MAX_ACCOUNT_GAP_S:
                day = now.date()
                self._played_s[day] = self._played_s.get(day, 0.0) + delta
        self._last_tick = now

    def _daily_limit_s(self, day: dt.date) -> float | None:
        limit = getattr(self.config, "daily_limit_hours", None)
        if limit is None:
            return None
        if day not in self._limit_s:
            self._limit_s[day] = max(0.0, limit.sample()) * 3600
            # nie trzymaj historii w nieskończoność
            for old in [d for d in self._limit_s if d < day - dt.timedelta(days=2)]:
                del self._limit_s[old]
                self._played_s.pop(old, None)
        return self._limit_s[day]

    def _bounds(self, idx: int, window: ScheduleWindow, day: dt.date):
        key = (idx, day)
        if key not in self._bounds_cache:
            nominal = window.bounds_for(day)
            if nominal is None:
                return None
            start, end = nominal
            # rozmycie nie może "zjeść" więcej niż połowy okna
            length_min = (end - start).total_seconds() / 60
            jitter = min(window.jitter_minutes, length_min / 4)
            if jitter > 0:
                start += dt.timedelta(minutes=random.uniform(0, jitter))
                end -= dt.timedelta(minutes=random.uniform(0, jitter))
            self._bounds_cache[key] = (start, end)
            for old in [k for k in self._bounds_cache if k[1] < day - dt.timedelta(days=2)]:
                del self._bounds_cache[old]
        return self._bounds_cache[key]

    def _current_occurrence(self, now: dt.datetime):
        for idx, window in enumerate(self.config.schedule):
            for back in (1, 0):
                day = now.date() - dt.timedelta(days=back)
                b = self._bounds(idx, window, day)
                if b is not None and b[0] <= now < b[1]:
                    return (idx, day), window, b[0], b[1]
        return None

    def _next_start(self, now: dt.datetime) -> dt.datetime | None:
        best = None
        for idx, window in enumerate(self.config.schedule):
            for ahead in range(-1, 8):
                day = now.date() + dt.timedelta(days=ahead)
                b = self._bounds(idx, window, day)
                if b is not None and b[0] > now and (best is None or b[0] < best):
                    best = b[0]
        return best

    # ------------------------------------------------------------------ #
    def decide(self, now: dt.datetime | None = None) -> Decision:
        if now is None:
            now = dt.datetime.now()
        self._reset_if_schedule_changed()
        self._account(now)
        decision = self._decide(now)
        self._last_play = decision.play
        return decision

    def _decide(self, now: dt.datetime) -> Decision:
        limit = self._daily_limit_s(now.date())
        if limit is not None and self.played_today(now) >= limit:
            midnight = dt.datetime.combine(now.date() + dt.timedelta(days=1), dt.time())
            return Decision(False, "daily_limit", midnight)

        if not self.config.schedule:
            return Decision(True, "always")

        occ = self._current_occurrence(now)
        if occ is None:
            self._session_key = None
            self._session_end = None
            self._break_until = None
            return Decision(False, "outside", self._next_start(now))

        key, window, _start, end = occ
        if key != self._session_key:
            # nowe wystąpienie okna - zaczynamy od świeżej sesji
            self._session_key = key
            self._session_end = None
            self._break_until = None

        if self._break_until is not None:
            if now < self._break_until:
                return Decision(False, "break", self._break_until)
            self._break_until = None

        if self._session_end is not None and now >= self._session_end:
            # sesja się skończyła - przerwa (najwyżej do końca okna)
            self._session_end = None
            brk = window.break_minutes.sample() if window.break_minutes else 0.0
            self._break_until = min(now + dt.timedelta(minutes=brk), end)
            if self._break_until > now:
                return Decision(False, "break", self._break_until)
            self._break_until = None

        if self._session_end is None:
            if end - now < MIN_SESSION:
                # za mało czasu na sensowną sesję - offline do końca okna
                self._break_until = end
                return Decision(False, "break", end)
            if window.session_minutes is None:
                self._session_end = end
            else:
                session_end = now + dt.timedelta(minutes=window.session_minutes.sample())
                if end - session_end < MIN_SESSION:
                    session_end = end
                self._session_end = session_end

        return Decision(True, "session", self._session_end)
