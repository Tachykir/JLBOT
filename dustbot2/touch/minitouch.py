"""
minitouch.py
============
Klient minitouch: zdarzenia dotyku wysyłane przez gniazdo do natywnego
procesu `minitouch` działającego na emulatorze. Nie uruchamia procesu
`input` przy każdym geście, więc opóźnienie jest rzędu milisekund zamiast
setek.

Setup (raz na instancję, robi to deployer, nie ta klasa):
    adb push minitouch /data/local/tmp/minitouch
    adb shell chmod 755 /data/local/tmp/minitouch
    adb forward tcp:<port> localabstract:minitouch
    adb shell /data/local/tmp/minitouch &

Protokół tekstowy (wg openstf/minitouch):
    serwer -> klient, po połączeniu:
        "v <wersja>"
        "^ <max_kontaktów> <max_x> <max_y> <max_nacisk>"
        "$ <pid>"            <- koniec nagłówka
    klient -> serwer:
        "d <id> <x> <y> <nacisk>"   wciśnięcie
        "m <id> <x> <y> <nacisk>"   przesunięcie
        "u <id>"                    puszczenie
        "w <ms>"                    pauza
        "c"                         zatwierdzenie klatki
"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from typing import Iterable, Protocol


class TouchError(RuntimeError):
    pass


@dataclass(frozen=True)
class Banner:
    version: int
    max_contacts: int
    max_x: int
    max_y: int
    max_pressure: int


def parse_banner(lines: Iterable[str]) -> Banner:
    """Parsuje nagłówek wysyłany przez minitouch przy połączeniu."""
    version: int | None = None
    limits: tuple[int, int, int, int] | None = None
    for raw in lines:
        line = raw.strip()
        if line.startswith("v "):
            version = int(line.split()[1])
        elif line.startswith("^ "):
            parts = [int(p) for p in line.split()[1:5]]
            if len(parts) != 4:
                raise TouchError(f"Nieprawidłowa linia limitów minitouch: {line!r}")
            limits = (parts[0], parts[1], parts[2], parts[3])
        elif line.startswith("$"):
            break
    if version is None or limits is None:
        raise TouchError("Nagłówek minitouch jest niepełny (brak 'v' lub '^').")
    return Banner(version, *limits)


class Transport(Protocol):
    """Minimalny interfejs gniazda - pozwala podmienić je w testach."""

    def sendall(self, data: bytes) -> None: ...


class MinitouchClient:
    def __init__(self, transport: Transport, banner: Banner):
        self._transport = transport
        self.banner = banner

    @classmethod
    def connect(cls, port: int, timeout: float = 5.0) -> "MinitouchClient":
        sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
        reader = sock.makefile("r", encoding="ascii", newline="\n")
        lines: list[str] = []
        while True:
            line = reader.readline()
            if not line:
                raise TouchError("minitouch zamknął połączenie przed wysłaniem nagłówka.")
            lines.append(line)
            if line.startswith("$"):
                break
        return cls(sock, parse_banner(lines))

    # ------------------------------------------------------------------ #
    # Budowanie poleceń (czyste funkcje, łatwe do testowania)
    # ------------------------------------------------------------------ #
    def clamp(self, x: int, y: int) -> tuple[int, int]:
        return (
            min(max(int(x), 0), self.banner.max_x),
            min(max(int(y), 0), self.banner.max_y),
        )

    def down(self, contact: int, x: int, y: int, pressure: int = 50) -> str:
        x, y = self.clamp(x, y)
        return f"d {contact} {x} {y} {self._pressure(pressure)}"

    def move(self, contact: int, x: int, y: int, pressure: int = 50) -> str:
        x, y = self.clamp(x, y)
        return f"m {contact} {x} {y} {self._pressure(pressure)}"

    @staticmethod
    def up(contact: int) -> str:
        return f"u {contact}"

    @staticmethod
    def wait(ms: float) -> str:
        return f"w {max(int(round(ms)), 0)}"

    def _pressure(self, pressure: int) -> int:
        return min(max(int(pressure), 1), self.banner.max_pressure)

    # ------------------------------------------------------------------ #
    # Wysyłka
    # ------------------------------------------------------------------ #
    def send_frame(self, commands: list[str]) -> None:
        """Wysyła polecenia i zatwierdza je jedną klatką ('c')."""
        payload = "\n".join(commands + ["c"]) + "\n"
        try:
            self._transport.sendall(payload.encode("ascii"))
        except OSError as e:
            raise TouchError(f"Nie udało się wysłać zdarzenia do minitouch: {e}") from e

    def tap(self, x: int, y: int, hold_ms: float = 60.0, contact: int = 0) -> None:
        self.send_frame([self.down(contact, x, y), self.wait(hold_ms), self.up(contact)])
