"""
schedule.py
===========
Dzienny plan gry: użytkownik ustawia tylko okno (np. 09:00-23:30), ile
godzin bot ma grać w ciągu dnia i opcjonalnie dni tygodnia. Resztę układa
bot, każdego dnia inaczej:

  - losuje "styl dnia" (dużo krótkich sesji / mieszany / dłuższe bloki),
  - długości sesji: mini-sesje 8-20 min i zwykłe sesje do 2 h,
  - czas wolny dzieli na przerwy o nieregularnych długościach (rozkład
    gamma), z krótszym zapasem na początku i końcu okna,
  - suma sesji = zadana liczba godzin (minus to, co już rozegrano dziś).

Plan jest powtarzalny dla danej instancji i dnia (ziarno z nazwy i daty),
a po restarcie bota w trakcie dnia układany jest od nowa tylko dla
pozostałego czasu i pozostałych godzin (PlayLedger pamięta rozegrany czas).

Okno przechodzące przez północ (np. 20:00-02:00) należy do dnia, w którym
się zaczyna.
"""

from __future__ import annotations

import datetime as dt
import json
import math
import random
from dataclasses import dataclass
from pathlib import Path

MIN_SESSION_MINUTES = 8.0
MAX_SESSION_MINUTES = 120.0
SHORT_SESSION_MINUTES = (8.0, 20.0)


@dataclass(frozen=True)
class DayStyle:
    name: str
    weight: float             # jak często ten styl jest losowany
    short_share: float        # udział mini-sesji (8-20 min)
    median_minutes: float     # mediana zwykłych sesji
    min_break_minutes: float


DAY_STYLES = (
    DayStyle("dużo krótkich sesji", 0.30, 0.55, 28.0, 4.0),
    DayStyle("mieszany", 0.45, 0.25, 45.0, 5.0),
    DayStyle("dłuższe bloki", 0.25, 0.10, 75.0, 8.0),
)


@dataclass(frozen=True)
class DailyPlayTarget:
    window_start: str                              # "HH:MM"
    window_end: str                                # "HH:MM"
    play_hours: float
    days: tuple[int, ...] | None = None            # 0 = poniedziałek; None = codziennie

    def to_dict(self) -> dict:
        return {
            "window_start": self.window_start,
            "window_end": self.window_end,
            "play_hours": self.play_hours,
            "days": list(self.days) if self.days is not None else None,
        }

    @staticmethod
    def from_dict(raw: dict | None) -> "DailyPlayTarget | None":
        """Starsze pola (session_minutes, min_break_minutes) są pomijane."""
        if not raw:
            return None
        days = raw.get("days")
        return DailyPlayTarget(
            window_start=raw["window_start"],
            window_end=raw["window_end"],
            play_hours=float(raw["play_hours"]),
            days=tuple(days) if days is not None else None,
        )


@dataclass(frozen=True)
class Session:
    start: dt.datetime
    end: dt.datetime

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60


@dataclass(frozen=True)
class DayPlan:
    style: str
    sessions: list[Session]


def _parse(hhmm: str) -> dt.time:
    h, m = hhmm.split(":")
    return dt.time(int(h), int(m))


def window_for(target: DailyPlayTarget, day: dt.date) -> tuple[dt.datetime, dt.datetime]:
    start = dt.datetime.combine(day, _parse(target.window_start))
    end = dt.datetime.combine(day, _parse(target.window_end))
    if end <= start:
        end += dt.timedelta(days=1)
    return start, end


def window_containing_or_next(target: DailyPlayTarget, now: dt.datetime) -> tuple[dt.date, dt.datetime, dt.datetime] | None:
    """Okno, w którym jest `now`, albo najbliższe następne (z uwzględnieniem dni)."""
    for offset in range(-1, 8):
        day = now.date() + dt.timedelta(days=offset)
        if target.days is not None and day.weekday() not in target.days:
            continue
        start, end = window_for(target, day)
        if end > now:
            return day, start, end
    return None


def _session_length(style: DayStyle, median: float, rng: random.Random) -> float:
    if rng.random() < style.short_share:
        return rng.uniform(*SHORT_SESSION_MINUTES) * 60
    minutes = median * math.exp(rng.gauss(0, 0.4))
    return min(max(minutes, SHORT_SESSION_MINUTES[1]), MAX_SESSION_MINUTES) * 60


def plan_day(target: DailyPlayTarget, start: dt.datetime, end: dt.datetime,
             rng: random.Random, played_seconds: float = 0.0) -> DayPlan:
    """Plan od `start` do `end` na pozostałe (play_hours - played) godzin."""
    style = rng.choices(DAY_STYLES, weights=[s.weight for s in DAY_STYLES])[0]
    available = (end - start).total_seconds()
    remaining = max(target.play_hours * 3600 - played_seconds, 0.0)
    if remaining <= 60 or available <= 60:
        return DayPlan(style.name, [])
    min_break = style.min_break_minutes * 60
    remaining = min(remaining, available)
    low = MIN_SESSION_MINUTES * 60

    median = style.median_minutes * rng.uniform(0.85, 1.2)  # dzień do dnia trochę inaczej
    lengths: list[float] = []
    while sum(lengths) < remaining:
        lengths.append(_session_length(style, median, rng))
    lengths[-1] -= sum(lengths) - remaining
    if lengths[-1] < low and len(lengths) > 1:
        # ostatnia sesja wyszła za krótka - dopełniamy ją do minimum kosztem
        # pozostałych (proporcjonalnie do ich zapasu ponad minimum)
        deficit = low - lengths[-1]
        headroom = [max(x - low, 0.0) for x in lengths[:-1]]
        if sum(headroom) >= deficit:
            for i, room in enumerate(headroom):
                lengths[i] -= deficit * room / sum(headroom)
            lengths[-1] = low
        else:
            tail = lengths.pop()  # nie da się - doklejamy do poprzedniej
            lengths[-1] += tail

    # za mało czasu na przerwy - łączymy najkrótsze sesje; dopiero wtedy
    # sesja może przekroczyć 2 h (godziny gry prawie równe długości okna)
    while len(lengths) > 1 and available - remaining < (len(lengths) - 1) * min_break:
        lengths.sort()
        shortest = lengths.pop(0)
        lengths[0] += shortest
    rng.shuffle(lengths)

    free = available - sum(lengths)
    internal = len(lengths) - 1
    spare = max(free - internal * min_break, 0.0)
    # wagi przerw: wewnętrzne większe niż zapas na początku i końcu okna
    weights = [rng.gammavariate(0.8, 0.5)] + [rng.gammavariate(1.6, 1.0) for _ in range(internal)] \
        + [rng.gammavariate(0.8, 0.5)]
    total = sum(weights) or 1.0
    gaps = [spare * w / total for w in weights]
    for i in range(1, internal + 1):
        gaps[i] += min_break

    sessions = []
    cursor = start + dt.timedelta(seconds=gaps[0])
    for i, length in enumerate(lengths):
        session_end = cursor + dt.timedelta(seconds=length)
        sessions.append(Session(cursor, session_end))
        cursor = session_end + dt.timedelta(seconds=gaps[i + 1])
    return DayPlan(style.name, sessions)


def plan_window(target: DailyPlayTarget, start: dt.datetime, end: dt.datetime,
                rng: random.Random, played_seconds: float = 0.0) -> list[Session]:
    return plan_day(target, start, end, rng, played_seconds).sessions


class PlayLedger:
    """Rozegrany czas na dzień okna, zapisywany w pliku JSON."""

    def __init__(self, path: Path | None, instance: str):
        self.path = Path(path) if path is not None else None
        self.instance = instance
        self._data: dict[str, dict[str, float]] = {}
        if self.path is not None and self.path.exists():
            try:
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._data = {}

    def played(self, day: dt.date) -> float:
        return float(self._data.get(self.instance, {}).get(day.isoformat(), 0.0))

    def add(self, day: dt.date, seconds: float) -> None:
        days = self._data.setdefault(self.instance, {})
        days[day.isoformat()] = days.get(day.isoformat(), 0.0) + seconds
        # trzymamy tylko ostatnie 14 dni
        for old in sorted(days)[:-14]:
            del days[old]

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self._data, indent=2), encoding="utf-8")


class PlayScheduler:
    """
    Odpowiada na pytanie "czy teraz grać?" według dziennego planu.
    Plan okna jest układany przy pierwszym pytaniu w danym oknie, od chwili
    pytania, na godziny pozostałe po tym, co już zapisano w PlayLedger.
    """

    def __init__(self, target: DailyPlayTarget, instance: str, ledger: PlayLedger | None = None):
        self.target = target
        self.instance = instance
        self.ledger = ledger or PlayLedger(None, instance)
        self._plan_key: dt.datetime | None = None
        self._plan_day: dt.date | None = None
        self._sessions: list[Session] = []
        self.style: str | None = None

    def sessions(self, now: dt.datetime) -> list[Session]:
        window = window_containing_or_next(self.target, now)
        if window is None:
            return []
        day, start, end = window
        if self._plan_key != start:
            rng = random.Random(f"{self.instance}|{day.isoformat()}|{now.isoformat(timespec='minutes')}")
            plan = plan_day(self.target, max(start, now), end, rng, self.ledger.played(day))
            self._sessions, self.style = plan.sessions, plan.style
            self._plan_key, self._plan_day = start, day
        return self._sessions

    def is_play_time(self, now: dt.datetime) -> bool:
        return any(s.start <= now < s.end for s in self.sessions(now))

    def next_session(self, now: dt.datetime) -> Session | None:
        for session in self.sessions(now):
            if session.end > now:
                return session
        return None

    def record_play(self, seconds: float) -> None:
        if self._plan_day is not None and seconds > 0:
            self.ledger.add(self._plan_day, seconds)
