"""
Obraz z emulatora przez scrcpy - zamiast zrzutu `adb exec-out screencap`.

Na urządzeniu działa serwer scrcpy (vendor/scrcpy/scrcpy-server, wersja
SERVER_VERSION, licencja Apache 2.0), który koduje ekran jako wideo H.264 już
przeskalowane do szerokości przetwarzania bota (np. 540 px). Gra działa w
swojej normalnej rozdzielczości - zmniejszony jest tylko obraz dla bota.

Strumień idzie jednym połączeniem (adb forward na lokalny port), a dekoder
(PyAV) w osobnym wątku trzyma zawsze najnowszą klatkę. Zamiast 150-400 ms na
zrzut bot dostaje gotową klatkę w kilka-kilkadziesiąt ms.

Protokół (scrcpy 3.x, tunel "forward"): po połączeniu serwer wysyła 1 bajt,
64 bajty nazwy urządzenia, metadane kodeka (id, szerokość, wysokość - po 4
bajty), a potem pakiety: 8 bajtów PTS/flag (bit 63 = pakiet konfiguracyjny),
4 bajty długości i dane.
"""
from __future__ import annotations

import logging
import random
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import numpy as np

logger = logging.getLogger("scrcpy")

SERVER_VERSION = "3.1"
DEVICE_SERVER_PATH = "/data/local/tmp/dmb-scrcpy-server.jar"
_FLAG_CONFIG = 1 << 63

if sys.platform == "win32":
    _FLAGS = subprocess.CREATE_NO_WINDOW
else:
    _FLAGS = 0


class StreamError(RuntimeError):
    pass


def server_path() -> Path:
    """scrcpy-server dołączony do programu (vendor/scrcpy)."""
    base = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
    for candidate in (base / "vendor" / "scrcpy" / "scrcpy-server",
                      base / "_internal" / "vendor" / "scrcpy" / "scrcpy-server"):
        if candidate.exists():
            return candidate
    raise StreamError("brak pliku vendor/scrcpy/scrcpy-server")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise StreamError("połączenie zamknięte")
        buf += chunk
    return bytes(buf)


class ScrcpyStream:
    """Strumień wideo jednego urządzenia. Użycie: start(), latest(), close()."""

    def __init__(self, adb_path: str, serial: str, max_size: int, max_fps: int = 30,
                 bit_rate: int = 4_000_000):
        self.adb_path = adb_path
        self.serial = serial
        self.max_size = max(240, int(max_size) // 8 * 8)
        self.max_fps = max(5, int(max_fps))
        self.bit_rate = bit_rate
        self.scid = random.randint(0, 0x7FFFFFFF)
        self.port = 0
        self.size = (0, 0)               # rozmiar klatek (po przeskalowaniu)
        self._server: subprocess.Popen | None = None
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._cond = threading.Condition()
        self._frame: np.ndarray | None = None
        self._frame_at = 0.0
        self._frame_seq = 0
        self.last_frame_at = 0.0         # time.monotonic() klatki ostatnio oddanej przez latest()
        self._error: Exception | None = None
        self._closed = False

    # ------------------------------------------------------------------ #
    def _adb(self, *args: str, timeout: float = 15.0) -> subprocess.CompletedProcess:
        result = subprocess.run([self.adb_path, "-s", self.serial, *args], capture_output=True,
                                timeout=timeout, creationflags=_FLAGS)
        if result.returncode != 0:
            raise StreamError(f"adb {' '.join(args[:2])}: {result.stderr.decode(errors='replace').strip()}")
        return result

    def start(self, connect_timeout: float = 10.0) -> None:
        local = server_path()
        self._adb("push", str(local), DEVICE_SERVER_PATH, timeout=30)
        self.port = _free_port()
        name = f"scrcpy_{self.scid:08x}"
        self._adb("forward", f"tcp:{self.port}", f"localabstract:{name}")
        options = [
            f"scid={self.scid:08x}", "log_level=warn", "video=true", "audio=false", "control=false",
            "tunnel_forward=true", "video_codec=h264", f"max_size={self.max_size}",
            f"max_fps={self.max_fps}", f"video_bit_rate={self.bit_rate}", "cleanup=true",
            "stay_awake=false", "power_off_on_close=false",
        ]
        cmd = (f"CLASSPATH={DEVICE_SERVER_PATH} app_process / com.genymobile.scrcpy.Server "
               f"{SERVER_VERSION} " + " ".join(options))
        self._server = subprocess.Popen([self.adb_path, "-s", self.serial, "shell", cmd],
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                        creationflags=_FLAGS)
        # wyjście serwera czytamy na bieżąco, żeby przy braku połączenia pokazać,
        # co serwer napisał, zamiast czekać aż się zakończy
        self._server_log: list[str] = []
        threading.Thread(target=self._read_server_log, daemon=True).start()
        self._sock = self._connect(connect_timeout)
        self._sock.settimeout(None)
        _recv_exact(self._sock, 64)                   # nazwa urządzenia
        codec, w, h = struct.unpack(">III", _recv_exact(self._sock, 12))
        if codec != 0x68323634:                       # "h264"
            raise StreamError(f"nieobsługiwany kodek {codec:#x}")
        self.size = (w, h)
        self._thread = threading.Thread(target=self._decode_loop, name=f"scrcpy-{self.serial}", daemon=True)
        self._thread.start()
        logger.info("[%s] strumień scrcpy %dx%d, do %d kl./s", self.serial, w, h, self.max_fps)

    def _read_server_log(self) -> None:
        for raw in iter(self._server.stdout.readline, b""):
            self._server_log.append(raw.decode(errors="replace").rstrip())
            del self._server_log[:-20]

    def _server_output(self) -> str:
        lines = [line for line in getattr(self, "_server_log", []) if line]
        return " | ".join(lines[-5:])[-400:] or "brak wyjścia serwera"

    def _connect(self, timeout: float) -> socket.socket:
        # adb forward przyjmuje połączenie od razu, nawet zanim serwer zacznie
        # nasłuchiwać - wtedy zamyka je bez danych; dopiero bajt "dummy"
        # potwierdza, że po drugiej stronie jest serwer scrcpy
        deadline = time.monotonic() + timeout
        last = None
        while time.monotonic() < deadline:
            if self._server is not None and self._server.poll() is not None:
                raise StreamError(f"serwer scrcpy zakończył się: {self._server_output()}")
            s = socket.create_connection(("127.0.0.1", self.port), timeout=2)
            try:
                s.settimeout(2)
                if s.recv(1):
                    return s
            except OSError as e:
                last = e
            s.close()
            time.sleep(0.1)
        if last is None:
            # port przyjmował połączenia, ale serwer nie wysłał bajtu potwierdzenia
            raise StreamError(f"serwer nie odpowiada na połączenie (proces żyje, brak bajtu "
                              f"potwierdzenia) - wyjście serwera: {self._server_output()}")
        raise StreamError(f"nie udało się połączyć ze strumieniem ({last}) - "
                          f"wyjście serwera: {self._server_output()}")

    def _decode_loop(self) -> None:
        try:
            import av
            codec = av.CodecContext.create("h264", "r")
            config = b""
            while not self._closed:
                pts_flags, size = struct.unpack(">QI", _recv_exact(self._sock, 12))
                data = _recv_exact(self._sock, size)
                if pts_flags & _FLAG_CONFIG:
                    config = data                     # SPS/PPS - doklejane do następnej klatki
                    continue
                if config:
                    data, config = config + data, b""
                for frame in codec.decode(av.Packet(data)):
                    img = frame.to_ndarray(format="bgr24")
                    with self._cond:
                        self._frame = img
                        self._frame_at = time.monotonic()
                        self._frame_seq += 1
                        self._cond.notify_all()
        except Exception as e:  # noqa: BLE001 - każdy błąd kończy strumień, bot wraca do screencap
            if not self._closed:
                self._error = e
                logger.warning("[%s] strumień scrcpy przerwany: %s", self.serial, e)
            with self._cond:
                self._cond.notify_all()

    # ------------------------------------------------------------------ #
    @property
    def alive(self) -> bool:
        return not self._closed and self._error is None and self._thread is not None and self._thread.is_alive()

    def latest(self, fresh_after: float | None = None, wait: float = 0.12, timeout: float = 3.0) -> np.ndarray:
        """
        Najnowsza klatka. fresh_after: czas (time.monotonic), po którym klatka
        powinna przyjść - np. chwila wywołania, żeby po akcji nie dostać
        obrazu sprzed niej. scrcpy wysyła klatki tylko przy zmianie ekranu,
        więc na nieruchomym ekranie po `wait` s oddajemy ostatnią klatkę.
        """
        deadline = time.monotonic() + timeout
        fresh_deadline = time.monotonic() + wait
        with self._cond:
            while True:
                if self._error is not None:
                    raise StreamError(str(self._error))
                if self._frame is not None:
                    if fresh_after is None or self._frame_at >= fresh_after or time.monotonic() >= fresh_deadline:
                        self.last_frame_at = self._frame_at
                        return self._frame
                remaining = (fresh_deadline if self._frame is not None else deadline) - time.monotonic()
                if self._frame is None and remaining <= 0:
                    raise StreamError("brak klatek ze strumienia")
                self._cond.wait(max(0.005, min(remaining, 0.05)))

    def close(self) -> None:
        self._closed = True
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        if self._server is not None and self._server.poll() is None:
            try:
                self._server.kill()
            except OSError:
                pass
        if self.port:
            try:
                subprocess.run([self.adb_path, "-s", self.serial, "forward", "--remove", f"tcp:{self.port}"],
                               capture_output=True, timeout=5, creationflags=_FLAGS)
            except (OSError, subprocess.SubprocessError):
                pass
