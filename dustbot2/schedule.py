"""
schedule.py
===========
Dzienny plan gry: użytkownik ustawia tylko okno (np. 09:00-23:30) i ile
godzin bot ma grać w ciągu dnia. Bot sam układa sesje i przerwy:

  - długości sesji są losowane (rozkład log-normalny w zadanym zakresie),
  - czas wolny dzielony jest na przerwy o nieregularnych długościach
    (rozkład gamma), z krótszym zapasem na początku i końcu okna,
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


@dataclass(frozen=True)
class DailyPlayTarget:
    window_start: str                              # "HH:MM"
    window_end: str                                # "HH:MM"
    play_hours: float
    session_minutes: tuple[float, float] = (20.0, 90.0)
    min_break_minutes: float = 5.0
    days: tuple[int, ...] | None = None            # 0 = poniedziałek; None = codziennie

    def to_dict(self) -> dict:
        return {
            "window_start": self.window_start,
            "window_end": self.window_end,
            "play_hours": self.play_hours,
            "session_minutes": list(self.session_minutes),
            "min_break_minutes": self.min_break_minutes,
            "days": list(self.days) if self.days is not None else None,
        }

    @staticmethod
    def from_dict(raw: dict | None) -> "DailyPlayTarget | None":
        if not raw:
            return None
        days = raw.get("days")
        return DailyPlayTarget(
            window_start=raw["window_start"],
            window_end=raw["window_end"],
            play_hours=float(raw["play_hours"]),
            session_minutes=tuple(raw.get("session_minutes", (20.0, 90.0))),
            min_break_minutes=float(raw.get("min_break_minutes", 5.0)),
            days=tuple(days) if days is not None else None,
        )


@dataclass(frozen=True)
class Session:
    start: dt.datetime
    end: dt.datetime

    @property
    def minutes(self) -> float:
        return (self.end - self.start).total_seconds() / 60


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


def plan_window(target: DailyPlayTarget, start: dt.datetime, end: dt.datetime,
                rng: random.Random, played_seconds: float = 0.0) -> list[Session]:
    """Sesje od `start` do `end` na pozostałe (play_hours - played) godzin."""
    available = (end - start).total_seconds()
    remaining = max(target.play_hours * 3600 - played_seconds, 0.0)
    if remaining <= 60 or available <= 60:
        return []
    min_break = target.min_break_minutes * 60
    remaining = min(remaining, available)

    # długości sesji: log-normalnie wokół średniej geometrycznej zakresu
    low, high = (m * 60 for m in target.session_minutes)
    median = math.sqrt(low * high)
    lengths: list[float] = []
    while sum(lengths) < remaining:
        lengths.append(min(max(median * math.exp(rng.gauss(0, 0.45)), low), high))
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

    # za mało czasu na przerwy - łączymy sesje (dłuższe, ale mieszczą się)
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
    return sessions


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

    def sessions(self, now: dt.datetime) -> list[Session]:
        window = window_containing_or_next(self.target, now)
        if window is None:
            return []
        day, start, end = window
        if self._plan_key != start:
            rng = random.Random(f"{self.instance}|{day.isoformat()}|{now.isoformat(timespec='minutes')}")
            self._sessions = plan_window(self.target, max(start, now), end, rng, self.ledger.played(day))
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
