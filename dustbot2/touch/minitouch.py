"""
minitouch.py
============
Klient minitouch: zdarzenia dotyku wysyłane przez gniazdo do natywnego
procesu `minitouch` działającego na emulatorze. Nie uruchamia procesu
`input` przy każdym geście, więc opóźnienie jest rzędu milisekund zamiast
setek, a gest może mieć dowolny kształt i własny czas.

MinitouchSession robi cały setup (wgranie binarki, uruchomienie przez su,
forward portu, połączenie). Na Androidzie 10+ minitouch potrzebuje roota,
bo bez niego nie ma dostępu do /dev/input.

Protokół tekstowy (wg devicefarmer/minitouch):
    serwer -> klient, po połączeniu:
        "v <wersja>"
        "^ <max_kontaktów> <max_x> <max_y> <max_nacisk>"
        "$ <pid>"            <- koniec nagłówka
    klient -> serwer:
        "d <id> <x> <y> <nacisk>"   wciśnięcie
        "m <id> <x> <y> <nacisk>"   przesunięcie
        "u <id>"                    puszczenie
        "c"                         zatwierdzenie - bez niego nic się nie dzieje
        "w <ms>"                    pauza wykonywana przez minitouch
        "r"                         reset wszystkich dotyków
    W jednym zatwierdzeniu może być najwyżej jedno d/m/u dla danego palca,
    więc każdy krok gestu kończy się własnym "c".

Współrzędne: zakres dotyku (max_x, max_y) zwykle NIE jest równy
rozdzielczości ekranu, dlatego klient przelicza piksele ekranu na
jednostki urządzenia dotykowego.
"""

from __future__ import annotations

import logging
import socket
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Protocol

from dustbot2.geometry import Screen

logger = logging.getLogger("bot.minitouch")

REMOTE_BINARY = "/data/local/tmp/minitouch"
SOCKET_NAME = "minitouch"


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


def read_banner(sock: socket.socket) -> Banner:
    reader = sock.makefile("r", encoding="ascii", newline="\n")
    lines: list[str] = []
    while True:
        line = reader.readline()
        if not line:
            raise TouchError("minitouch zamknął połączenie przed wysłaniem nagłówka.")
        lines.append(line)
        if line.startswith("$"):
            return parse_banner(lines)


class MinitouchClient:
    COMMIT = "c"
    RESET = "r"

    def __init__(self, transport: Transport, banner: Banner, screen: Screen | None = None):
        """
        `screen` - rozmiar ekranu w pikselach. Jeśli podany, współrzędne
        przekazywane do down/move są w pikselach ekranu i są przeliczane na
        zakres dotyku z nagłówka. Bez niego są używane wprost.
        """
        self._transport = transport
        self.banner = banner
        self.screen = screen

    # ------------------------------------------------------------------ #
    # Budowanie poleceń (czyste funkcje, łatwe do testowania)
    # ------------------------------------------------------------------ #
    def to_touch(self, x: float, y: float) -> tuple[int, int]:
        if self.screen is not None:
            x = x * self.banner.max_x / max(self.screen.width - 1, 1)
            y = y * self.banner.max_y / max(self.screen.height - 1, 1)
        return (
            min(max(int(round(x)), 0), self.banner.max_x),
            min(max(int(round(y)), 0), self.banner.max_y),
        )

    def down(self, contact: int, x: float, y: float, pressure: int = 50) -> str:
        tx, ty = self.to_touch(x, y)
        return f"d {contact} {tx} {ty} {self._pressure(pressure)}"

    def move(self, contact: int, x: float, y: float, pressure: int = 50) -> str:
        tx, ty = self.to_touch(x, y)
        return f"m {contact} {tx} {ty} {self._pressure(pressure)}"

    @staticmethod
    def up(contact: int) -> str:
        return f"u {contact}"

    @staticmethod
    def wait(ms: float) -> str:
        return f"w {max(int(round(ms)), 0)}"

    def _pressure(self, pressure: int) -> int:
        # część urządzeń dotykowych ma max_pressure 0 (brak czujnika nacisku)
        if self.banner.max_pressure <= 0:
            return 0
        return min(max(int(pressure), 1), self.banner.max_pressure)

    # ------------------------------------------------------------------ #
    # Wysyłka
    # ------------------------------------------------------------------ #
    def send(self, commands: list[str]) -> None:
        """
        Wysyła polecenia w jednym pakiecie. Pauzy "w" wykonuje minitouch na
        urządzeniu, więc czas gestu nie zależy od opóźnień ADB po stronie PC.
        Polecenia muszą zawierać własne zatwierdzenia "c".
        """
        payload = "\n".join(commands) + "\n"
        try:
            self._transport.sendall(payload.encode("ascii"))
        except OSError as e:
            raise TouchError(f"Nie udało się wysłać zdarzenia do minitouch: {e}") from e

    def tap(self, x: float, y: float, hold_ms: float = 60.0, contact: int = 0, pressure: int = 50) -> None:
        self.send([
            self.down(contact, x, y, pressure), self.COMMIT,
            self.wait(hold_ms),
            self.up(contact), self.COMMIT,
        ])

    def reset(self) -> None:
        self.send([self.RESET])


# ---------------------------------------------------------------------- #
# Uruchamianie na emulatorze
# ---------------------------------------------------------------------- #
def vendor_dir() -> Path:
    """Folder z binarkami (vendor/minitouch) - w trybie dev i w zbudowanym .exe."""
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).parent
        for candidate in (exe_dir / "vendor" / "minitouch", exe_dir / "_internal" / "vendor" / "minitouch"):
            if candidate.exists():
                return candidate
        return exe_dir / "_internal" / "vendor" / "minitouch"
    return Path(__file__).resolve().parents[2] / "vendor" / "minitouch"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _connect(port: int, timeout: float) -> tuple[socket.socket, Banner]:
    sock = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        return sock, read_banner(sock)
    except Exception:
        sock.close()
        raise


class MinitouchSession:
    """
    Wgrywa i uruchamia minitouch na jednej instancji oraz łączy się z nim.

        session = MinitouchSession(adb)
        client = session.start()      # TouchError, jeśli się nie da
        ...
        session.stop()

    `adb` to ADBController (albo obiekt z metodami shell/push/forward/popen
    i get_screen_size - w testach podmieniany).
    """

    def __init__(self, adb, binaries: Path | None = None, use_su: bool = True,
                 connect=_connect, startup_timeout: float = 5.0):
        self.adb = adb
        self.binaries = binaries or vendor_dir()
        self.use_su = use_su
        self._connect = connect
        self.startup_timeout = startup_timeout
        self.port: int | None = None
        self.client: MinitouchClient | None = None
        self._sock = None
        self._proc = None

    def _as_root(self, command: str) -> list[str]:
        return ["su", "-c", command] if self.use_su else [command]

    def start(self) -> MinitouchClient:
        abi = self.adb.shell("getprop", "ro.product.cpu.abi").strip()
        local = self.binaries / abi / "minitouch"
        if not local.exists():
            raise TouchError(f"Brak binarki minitouch dla architektury '{abi}' ({local}).")

        self.adb.push(str(local), REMOTE_BINARY)
        self.adb.shell("chmod", "755", REMOTE_BINARY)
        self._kill_remote()  # pozostałość po poprzednim uruchomieniu
        self._proc = self.adb.popen(["shell", *self._as_root(REMOTE_BINARY)])

        self.port = _free_port()
        self.adb.forward(f"tcp:{self.port}", f"localabstract:{SOCKET_NAME}")

        deadline = time.monotonic() + self.startup_timeout
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            try:
                self._sock, banner = self._connect(self.port, 2.0)
                break
            except (OSError, TouchError) as e:
                last_error = e
                time.sleep(0.2)
        else:
            self.stop()
            raise TouchError(
                f"minitouch nie odpowiada (root={'tak' if self.use_su else 'nie'}): {last_error}")

        width, height = self.adb.get_screen_size()
        self.client = MinitouchClient(self._sock, banner, Screen(width, height))
        if (banner.max_x > banner.max_y) != (width > height):
            logger.warning(
                "Zakres dotyku %dx%d ma inną orientację niż ekran %dx%d - osie mogą być "
                "zamienione. Sprawdź narzędziem: python -m dustbot2.touch.check",
                banner.max_x, banner.max_y, width, height)
        logger.info("minitouch gotowy: zakres %dx%d, nacisk 0-%d, ekran %dx%d, port %d",
                    banner.max_x, banner.max_y, banner.max_pressure, width, height, self.port)
        return self.client

    def _kill_remote(self) -> None:
        try:
            self.adb.shell(*self._as_root(f"pkill -f {REMOTE_BINARY}"))
        except Exception:
            pass  # nic nie działało - to normalne przy pierwszym starcie

    def stop(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self.client = None
        if self.port is not None:
            try:
                self.adb.forward("--remove", f"tcp:{self.port}")
            except Exception:
                pass
            self.port = None
        self._kill_remote()
        if self._proc is not None:
            try:
                self._proc.terminate()
            except Exception:
                pass
            self._proc = None
