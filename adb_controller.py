"""
adb_controller.py
==================
Cienka warstwa nad ADB do sterowania pojedynczą instancją LDPlayer.

Działa całkowicie w tle - nie wymaga focusa okna, ponieważ komendy
`adb shell input` oraz `adb exec-out screencap` operują bezpośrednio
na buforze systemu Android wewnątrz emulatora, niezależnie od tego,
czy okno LDPlayer jest widoczne, zminimalizowane, czy przysłonięte.

Każda instancja LDPlayer nasłuchuje ADB na innym porcie (domyślnie
LDPlayer przydziela porty parami zaczynając od 5555, 5557, 5559, ...
dla kolejnych instancji - ale najpewniej jest to sprawdzić przez
`adb devices` albo `ldconsole.exe list2`).
"""

from __future__ import annotations

import random
import subprocess
import sys
import os
from pathlib import Path

import numpy as np
import cv2
import time
import logging

logger = logging.getLogger("adb")

# --------------------------------------------------------------------- #
# Ukrywanie okna konsoli na Windowsie.
#
# subprocess.run domyślnie tworzy nowe okno konsoli dla każdego procesu
# potomnego, jeśli wywołujący proces (np. spakowany PyInstallerem .exe
# bez konsoli, tzw. windowed/GUI mode) go nie ma - stąd migające czarne
# okienko cmd przy każdym wywołaniu adb.exe. CREATE_NO_WINDOW każe
# Windowsowi nie tworzyć żadnego okna dla procesu potomnego.
#
# Na Linux/macOS ta flaga nie istnieje w module subprocess, więc
# używamy jej wyłącznie na Windowsie - gdzie indziej _SUBPROCESS_FLAGS
# zostaje 0 (brak dodatkowych flag, zachowanie bez zmian).
# --------------------------------------------------------------------- #
if sys.platform == "win32":
    _SUBPROCESS_FLAGS = subprocess.CREATE_NO_WINDOW
else:
    _SUBPROCESS_FLAGS = 0



def default_adb_path() -> str:
    """
    Ustala, którego `adb` użyć domyślnie:

      1. Jeśli aplikacja jest uruchomiona jako zbudowany exe (PyInstaller)
         i gdzieś obok niej jest dołączony `adb.exe` (razem z
         AdbWinApi.dll / AdbWinUsbApi.dll) w podfolderze `vendor/adb/`,
         użyj go - to czyni paczkę samowystarczalną, bez wymogu
         instalowania ADB osobno na komputerze użytkownika.
         Sprawdzane są dwie możliwe lokalizacje, bo PyInstaller w trybie
         --onedir domyślnie umieszcza dołączone pliki binarne w
         podfolderze `_internal/` obok samego .exe, a nie bezpośrednio
         obok niego:
           - <folder_exe>/vendor/adb/adb.exe
           - <folder_exe>/_internal/vendor/adb/adb.exe
      2. W przeciwnym razie zwróć po prostu "adb" i pozwól systemowi
         znaleźć je w PATH (np. z instalacji LDPlayer albo Platform Tools).

    Ta sama logika działa identycznie w trybie deweloperskim (`python
    main_window.py`) i w zbudowanym .exe.
    """
    adb_name = "adb.exe" if os.name == "nt" else "adb"

    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).parent
        candidates = [
            exe_dir / "vendor" / "adb" / adb_name,
            exe_dir / "_internal" / "vendor" / "adb" / adb_name,
        ]
    else:
        dev_dir = Path(__file__).resolve().parent
        candidates = [dev_dir / "vendor" / "adb" / adb_name]

    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return "adb"


class ADBError(RuntimeError):
    pass


class ADBController:
    """
    Owija pojedyncze urządzenie ADB (jedną instancję LDPlayer).

    serial: np. "127.0.0.1:5555" - identyfikator urządzenia w `adb devices`
    """

    max_width = 0     # domyślnie pełna rozdzielczość (patrz __init__)
    scale = 1.0

    def __init__(self, serial: str, adb_path: str | None = None, connect_timeout: float = 10.0):
        self.serial = serial
        self.adb_path = adb_path if adb_path is not None else default_adb_path()
        # Surowy screencap (bez PNG) jest dużo szybszy; wyłączany na stałe
        # dla tej instancji, jeśli urządzenie zwróci nieobsługiwany format.
        self._raw_screencap_ok = True
        # Oszczędzanie CPU: zrzuty szersze niż max_width są pomniejszane
        # (0 = pełna rozdzielczość). Bot pracuje na pomniejszonym obrazie,
        # a współrzędne kliknięć są tu przeliczane z powrotem (scale).
        self.max_width = 0
        self.scale = 1.0
        # Czy `input motionevent` jest dostępne (None = jeszcze nie sprawdzono).
        self._motionevent_ok: bool | None = None
        # Szacowany czas jednego wywołania `input ...` po stronie Androida
        # (uruchomienie procesu Java) - korygowany po każdym przeciągnięciu.
        self._input_latency_s = 0.15
        self._connect(connect_timeout)

    # ------------------------------------------------------------------ #
    # Połączenie
    # ------------------------------------------------------------------ #
    def _connect(self, timeout: float) -> None:
        # `adb connect` ma sens tylko dla urządzeń sieciowych ("host:port").
        # Seriale typu "emulator-5556" (lokalny emulator) albo seriale USB
        # są widoczne w `adb devices` same z siebie - `adb connect` na nich
        # próbowałby rozwiązać "emulator-5556" jako nazwę hosta i zgłosiłby
        # błąd, więc dla nich tylko sprawdzamy obecność na liście urządzeń.
        if ":" not in self.serial:
            self._verify_in_device_list()
            return

        # `adb connect` jest idempotentne - bezpiecznie wywoływać wielokrotnie.
        # UWAGA: `adb connect` na nieosiągalny adres często zwraca returncode 0
        # mimo błędu (komunikat "cannot connect" / "failed to connect" trafia
        # do stdout, nie do stderr/returncode) - dlatego samo sprawdzenie
        # returncode nie wystarczy, trzeba też przejrzeć treść odpowiedzi.
        try:
            result = subprocess.run(
                [self.adb_path, "connect", self.serial],
                capture_output=True,
                text=True,
                timeout=timeout,
                creationflags=_SUBPROCESS_FLAGS,
            )
        except subprocess.TimeoutExpired as e:
            raise ADBError(f"Timeout łączenia z {self.serial}") from e
        except FileNotFoundError as e:
            raise ADBError(
                f"Nie znaleziono programu '{self.adb_path}'. Upewnij się, że ADB "
                f"jest zainstalowane i dostępne w PATH (albo podaj pełną ścieżkę "
                f"do adb.exe z folderu instalacyjnego LDPlayer)."
            ) from e
        except OSError as e:
            raise ADBError(f"Nie udało się uruchomić '{self.adb_path}': {e}") from e

        output = (result.stdout or "") + (result.stderr or "")
        logger.info("connect(%s) -> %s", self.serial, output.strip())

        failure_markers = ("cannot connect", "failed to connect", "unable to connect",
                            "no route to host", "connection refused", "timed out")
        lowered = output.lower()
        connected = result.returncode == 0 and not any(m in lowered for m in failure_markers)

        if not connected:
            raise ADBError(f"Nie udało się połączyć z {self.serial}: {output.strip() or '(brak odpowiedzi)'}")

        self._verify_in_device_list()

    def _verify_in_device_list(self) -> None:
        # Ostateczna weryfikacja: urządzenie musi też realnie figurować
        # w `adb devices` ze statusem "device" (a nie "offline" czy nieobecne
        # wcale) - to wyłapuje przypadki, gdzie `connect` zgłasza pozorny
        # sukces, ale urządzenie i tak nie odpowiada na komendy.
        try:
            devices = list_ldplayer_devices(self.adb_path)
        except FileNotFoundError as e:
            raise ADBError(
                f"Nie znaleziono programu '{self.adb_path}'. Upewnij się, że ADB "
                f"jest zainstalowane i dostępne w PATH."
            ) from e
        except Exception:
            devices = None
        if devices is not None and self.serial not in devices:
            raise ADBError(
                f"Urządzenie {self.serial} nie widnieje jako aktywne "
                f"w `adb devices` (może być offline). Sprawdź, czy instancja LDPlayer "
                f"działa i ma włączone ADB debugging."
            )

    def _run(self, args: list[str], timeout: float = 10.0) -> subprocess.CompletedProcess:
        cmd = [self.adb_path, "-s", self.serial] + args
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=timeout, creationflags=_SUBPROCESS_FLAGS)
        except subprocess.TimeoutExpired as e:
            raise ADBError(f"Timeout komendy: {' '.join(cmd)}") from e
        except FileNotFoundError as e:
            raise ADBError(
                f"Nie znaleziono programu '{self.adb_path}'. Upewnij się, że ADB "
                f"jest zainstalowane i dostępne w PATH."
            ) from e
        except OSError as e:
            raise ADBError(f"Nie udało się uruchomić '{self.adb_path}': {e}") from e
        if result.returncode != 0:
            raise ADBError(
                f"Błąd ADB ({' '.join(cmd)}): {result.stderr.decode(errors='replace')}"
            )
        return result

    # ------------------------------------------------------------------ #
    # Wejście: tap / swipe
    # ------------------------------------------------------------------ #
    def _s(self, v: float) -> int:
        """Współrzędna z (pomniejszonego) zrzutu -> piksel urządzenia."""
        return int(round(v * self.scale))

    def tap(self, x: int, y: int) -> None:
        self._run(["shell", "input", "tap", str(self._s(x)), str(self._s(y))])

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 400) -> None:
        """
        Przeciągnięcie od (x1,y1) do (x2,y2).
        duration_ms wpływa na to, jak "naturalnie" wygląda drag - gra
        może wymagać minimalnego czasu trwania, żeby uznać to za drag
        a nie klik; 300-600ms zwykle działa dobrze dla mechanik drag&drop.
        """
        self._run([
            "shell", "input", "swipe",
            str(self._s(x1)), str(self._s(y1)), str(self._s(x2)), str(self._s(y2)),
            str(int(duration_ms)),
        ])

    def tap_sequence(self, taps: list[tuple[int, int, int]], gaps_s: list[float]) -> None:
        """
        Seria tapnięć jednym poleceniem `adb shell` (bez narzutu
        uruchamiania adb.exe przy każdym kliknięciu). `taps` to
        (x, y, czas_dotyku_ms) - dotyk jako mikro-swipe o 0-2 px, jak w
        pojedynczym tapnięciu; `gaps_s` - przerwy między kolejnymi.
        """
        if not taps:
            return
        parts = []
        for i, (x, y, hold) in enumerate(taps):
            if i and i - 1 < len(gaps_s) and gaps_s[i - 1] > 0:
                parts.append(f"sleep {gaps_s[i - 1]:.3f}")
            dx, dy = random.randint(-1, 1), random.randint(-1, 1)
            x, y = self._s(x), self._s(y)
            parts.append(f"input swipe {x} {y} {x + dx} {y + dy} {max(1, int(hold))}")
        total = sum(gaps_s) + len(taps) * 0.5
        self._run(["shell", "; ".join(parts)], timeout=10 + total)

    def _probe_motionevent(self) -> bool:
        """Sprawdza, czy `input motionevent` jest dostępne na urządzeniu
        (zależy od wersji Androida)."""
        try:
            result = subprocess.run(
                [self.adb_path, "-s", self.serial, "shell", "input 2>&1"],
                capture_output=True, timeout=10, creationflags=_SUBPROCESS_FLAGS,
            )
            out = (result.stdout or b"").decode(errors="replace") + (result.stderr or b"").decode(errors="replace")
        except (subprocess.TimeoutExpired, OSError):
            return False
        ok = "motionevent" in out
        logger.info("[%s] przeciąganie po krzywej: %s", self.serial,
                    "dostępne (input motionevent)" if ok
                    else "NIEDOSTĘPNE na tej wersji Androida - używam prostego swipe")
        return ok

    def drag_path(self, points: list[tuple[int, int]], duration_ms: float) -> None:
        """
        Przeciąga palcem po łamanej `points` jako JEDEN ciągły dotyk
        (DOWN na pierwszym punkcie, MOVE po kolejnych, UP na ostatnim),
        w łącznym czasie ok. `duration_ms`.

        Wymaga `input motionevent` (nowsze Androidy). Gdy go brak,
        wykonuje zwykły prosty swipe od pierwszego do ostatniego punktu.
        Cała sekwencja idzie jednym poleceniem `adb shell`, więc między
        punktami nie ma narzutu uruchamiania adb.exe.
        """
        if len(points) < 2:
            return
        if self._motionevent_ok is None:
            self._motionevent_ok = self._probe_motionevent()
        if not self._motionevent_ok:
            (x1, y1), (x2, y2) = points[0], points[-1]
            self.swipe(x1, y1, x2, y2, duration_ms=int(duration_ms))
            return

        moves = points[1:]
        segment_s = duration_ms / 1000.0 / len(moves)
        sleep_s = segment_s - self._input_latency_s
        if sleep_s < 0.01:
            sleep_s = 0.0

        x, y = points[0]
        parts = [f"input motionevent DOWN {self._s(x)} {self._s(y)}"]
        for x, y in moves:
            if sleep_s:
                parts.append(f"sleep {sleep_s:.3f}")
            parts.append(f"input motionevent MOVE {self._s(x)} {self._s(y)}")
        parts.append(f"input motionevent UP {self._s(x)} {self._s(y)}")

        start = time.monotonic()
        self._run(["shell", "; ".join(parts)], timeout=15 + 3 * duration_ms / 1000.0)
        elapsed = time.monotonic() - start

        # dopasuj szacunek opóźnienia jednego `input`, żeby kolejne
        # przeciągnięcia trwały bliżej zadanego czasu
        calls = len(moves) + 2
        measured = max(0.0, elapsed - sleep_s * len(moves)) / calls
        self._input_latency_s = 0.7 * self._input_latency_s + 0.3 * measured

    # ------------------------------------------------------------------ #
    # Wyjście: screenshot
    # ------------------------------------------------------------------ #
    def screenshot(self) -> np.ndarray:
        """
        Zwraca screenshot jako obraz BGR (numpy array, kompatybilny z OpenCV).

        Domyślnie używa surowego `exec-out screencap` (bez `-p`): Android
        nie musi kodować PNG, a my dekodować - to zwykle kilka razy
        szybciej. Jeśli urządzenie zwróci nieobsługiwany format, instancja
        na stałe przełącza się na `screencap -p` (PNG).
        """
        if self._raw_screencap_ok:
            data = self._exec_out(["screencap"])
            res = decode_raw_screencap(data, self.max_width)
            if res is not None:
                if isinstance(res, tuple):
                    img, full_w = res
                else:
                    img, full_w = res, res.shape[1]
                self.scale = full_w / img.shape[1]
                return img
            self._raw_screencap_ok = False
            logger.info("[%s] surowy screencap nieobsługiwany (%d B) - przełączam na PNG",
                        self.serial, len(data))

        data = self._exec_out(["screencap", "-p"])
        img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            raise ADBError(f"Nie udało się zdekodować screenshotu z {self.serial}")
        small = _shrink(img, self.max_width)
        self.scale = img.shape[1] / small.shape[1]
        return small

    def _exec_out(self, args: list[str]) -> bytes:
        cmd = [self.adb_path, "-s", self.serial, "exec-out"] + args
        try:
            result = subprocess.run(cmd, capture_output=True, timeout=10, creationflags=_SUBPROCESS_FLAGS)
        except subprocess.TimeoutExpired as e:
            raise ADBError(f"Timeout pobierania screenshotu z {self.serial}") from e
        except FileNotFoundError as e:
            raise ADBError(
                f"Nie znaleziono programu '{self.adb_path}'. Upewnij się, że ADB "
                f"jest zainstalowane i dostępne w PATH."
            ) from e
        except OSError as e:
            raise ADBError(f"Nie udało się uruchomić '{self.adb_path}': {e}") from e
        if result.returncode != 0 or not result.stdout:
            raise ADBError(f"Nie udało się pobrać screenshotu z {self.serial}")
        return result.stdout

    # ------------------------------------------------------------------ #
    # Sterowanie aplikacją: stop / start / restart
    # ------------------------------------------------------------------ #
    def stop_app(self, package_name: str) -> None:
        """
        Twarde zatrzymanie aplikacji (odpowiednik `am force-stop`) -
        zabija cały proces gry, tak jakby użytkownik zamknął ją
        z listy ostatnich aplikacji.
        """
        self._run(["shell", "am", "force-stop", package_name])

    def start_app(self, package_name: str, activity_name: str | None = None) -> None:
        """
        Uruchamia aplikację. Jeśli `activity_name` nie zostanie podane,
        używa `monkey`, który wystarczy tylko podać nazwę pakietu i
        sam znajduje jego domyślną activity uruchomieniową (LAUNCHER) -
        wygodne, gdy nie znamy dokładnej nazwy głównej activity.

        Jeśli `activity_name` jest podane (format "pakiet/.MainActivity"
        lub pełny "pakiet/pakiet.MainActivity"), używa `am start`, co
        jest szybsze i bardziej przewidywalne niż monkey.
        """
        if activity_name:
            component = activity_name if "/" in activity_name else f"{package_name}/{activity_name}"
            self._run(["shell", "am", "start", "-n", component])
        else:
            self._run([
                "shell", "monkey",
                "-p", package_name,
                "-c", "android.intent.category.LAUNCHER",
                "1",
            ])

    def restart_app(self, package_name: str, activity_name: str | None = None, wait_s: float = 2.0) -> None:
        """
        Zatrzymuje i ponownie uruchamia aplikację (force-stop + start).
        `wait_s` to pauza między zabiciem procesu a ponownym uruchomieniem -
        daje systemowi Android chwilę na faktyczne posprzątanie procesu,
        zanim spróbujemy go odpalić od nowa.
        """
        self.stop_app(package_name)
        time.sleep(wait_s)
        self.start_app(package_name, activity_name)

    def get_foreground_package(self) -> str | None:
        """
        Zwraca nazwę pakietu aplikacji aktualnie widocznej na pierwszym
        planie (np. "com.timhilgenstoehlerexpo.justlootapp" albo
        "com.android.vending" gdy otwarty jest Google Play), albo None
        jeśli nie udało się tego ustalić.

        Używa `dumpsys window` zamiast dopasowywania obrazu - to
        szybsze i dużo pewniejsze źródło prawdy niż analiza pikseli,
        bo pyta system Android wprost "co jest teraz na ekranie",
        zamiast zgadywać na podstawie wyglądu.
        """
        result = self._run(["shell", "dumpsys", "window"])
        out = result.stdout.decode(errors="replace")
        # Szukamy linii w stylu:
        # mCurrentFocus=Window{... com.package.name/com.package.name.MainActivity}
        # albo mFocusedApp=... ActivityRecord{... com.package.name/...}
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("mCurrentFocus=") or line.startswith("mFocusedApp="):
                if "/" not in line:
                    continue
                # wyciągnij fragment "com.package.name/..." z linii
                before_slash = line.rsplit("/", 1)[0]
                # nazwa pakietu to ostatni "token" (po spacji) przed "/"
                package = before_slash.split()[-1]
                if package and "." in package:
                    return package
        return None

    def get_screen_size(self) -> tuple[int, int]:
        """Zwraca (width, height) ekranu urządzenia."""
        result = self._run(["shell", "wm", "size"])
        out = result.stdout.decode(errors="replace")
        # przykładowy output: "Physical size: 900x1600"
        for line in out.splitlines():
            if "size" in line.lower() and "x" in line:
                part = line.split(":")[-1].strip()
                w, h = part.split("x")
                return int(w), int(h)
        raise ADBError(f"Nie udało się odczytać rozmiaru ekranu: {out}")


# Formaty pikseli Androida (android.graphics.PixelFormat), 4 bajty/piksel
_RAW_FORMATS = {
    1: cv2.COLOR_RGBA2BGR,  # RGBA_8888
    2: cv2.COLOR_RGBA2BGR,  # RGBX_8888
    5: cv2.COLOR_BGRA2BGR,  # BGRA_8888
}


def _shrink(img: np.ndarray, max_width: int) -> np.ndarray:
    h, w = img.shape[:2]
    if not max_width or w <= max_width * 1.05:
        return img
    return cv2.resize(img, (max_width, int(round(h * max_width / w))), interpolation=cv2.INTER_AREA)


def decode_raw_screencap(data: bytes, max_width: int = 0):
    """
    Dekoduje wynik `screencap` bez `-p`: nagłówek (szerokość, wysokość,
    format jako uint32 little-endian, na Androidzie 9+ dodatkowo 4 bajty
    przestrzeni kolorów) i surowe piksele. Zwraca obraz BGR albo None,
    jeśli format jest nieobsługiwany lub dane się nie zgadzają.
    """
    if len(data) < 16:
        return None
    w, h, fmt = (int(v) for v in np.frombuffer(data[:12], dtype="<u4"))
    if fmt not in _RAW_FORMATS or w <= 0 or h <= 0 or w > 10000 or h > 10000:
        return None
    header = len(data) - w * h * 4
    if header not in (12, 16):
        return None
    pixels = np.frombuffer(data, dtype=np.uint8, offset=header).reshape(h, w, 4)
    if not max_width:
        return cv2.cvtColor(pixels, _RAW_FORMATS[fmt])
    # pomniejszenie przed konwersją kolorów - mniej pracy
    return cv2.cvtColor(_shrink(pixels, max_width), _RAW_FORMATS[fmt]), w


def list_ldplayer_devices(adb_path: str | None = None) -> list[str]:
    """
    Zwraca listę serial'i podłączonych urządzeń (np. LDPlayer instancji)
    z `adb devices`. Pomocnicze do auto-wykrywania instancji.
    """
    if adb_path is None:
        adb_path = default_adb_path()
    result = subprocess.run(
        [adb_path, "devices"], capture_output=True, text=True, timeout=10,
        creationflags=_SUBPROCESS_FLAGS,
    )
    lines = result.stdout.strip().splitlines()[1:]  # pomiń nagłówek "List of devices attached"
    devices = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            devices.append(parts[0])
    return devices
