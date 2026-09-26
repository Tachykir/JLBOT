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

    def __init__(self, serial: str, adb_path: str | None = None, connect_timeout: float = 10.0):
        self.serial = serial
        self.adb_path = adb_path if adb_path is not None else default_adb_path()
        self._connect(connect_timeout)

    # ------------------------------------------------------------------ #
    # Połączenie
    # ------------------------------------------------------------------ #
    def _connect(self, timeout: float) -> None:
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

        # Ostateczna weryfikacja: urządzenie musi też realnie figurować
        # w `adb devices` ze statusem "device" (a nie "offline" czy nieobecne
        # wcale) - to wyłapuje przypadki, gdzie `connect` zgłasza pozorny
        # sukces, ale urządzenie i tak nie odpowiada na komendy.
        try:
            devices = list_ldplayer_devices(self.adb_path)
        except Exception:
            devices = None
        if devices is not None and self.serial not in devices:
            raise ADBError(
                f"Połączono z {self.serial}, ale urządzenie nie widnieje jako aktywne "
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
    def tap(self, x: int, y: int) -> None:
        self._run(["shell", "input", "tap", str(int(x)), str(int(y))])

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 400) -> None:
        """
        Przeciągnięcie od (x1,y1) do (x2,y2).
        duration_ms wpływa na to, jak "naturalnie" wygląda drag - gra
        może wymagać minimalnego czasu trwania, żeby uznać to za drag
        a nie klik; 300-600ms zwykle działa dobrze dla mechanik drag&drop.
        """
        self._run([
            "shell", "input", "swipe",
            str(int(x1)), str(int(y1)), str(int(x2)), str(int(y2)),
            str(int(duration_ms)),
        ])

    def swipe_multi_point(self, points: list[tuple[int, int]], step_delay_ms: int = 16) -> None:
        """
        Bardziej precyzyjny "drag" złożony z wielu segmentów swipe,
        przydatny gdy gra wymaga wielu punktów pośrednich, żeby
        zarejestrować gest jako przeciąganie obiektu (niektóre silniki
        gier - np. Unity - lepiej wykrywają drag, gdy zdarzenia ruchu
        docierają częściej niż jeden skok start->koniec).

        UWAGA: `adb shell input swipe` sam w sobie generuje pośrednie
        eventy MotionEvent, więc w większości przypadków wystarczy
        zwykłe swipe(). Ta metoda to fallback na trudniejsze gry.
        """
        for i in range(len(points) - 1):
            x1, y1 = points[i]
            x2, y2 = points[i + 1]
            self.swipe(x1, y1, x2, y2, duration_ms=max(step_delay_ms, 50))

    # ------------------------------------------------------------------ #
    # Wyjście: screenshot
    # ------------------------------------------------------------------ #
    def screenshot(self) -> np.ndarray:
        """
        Zwraca screenshot jako obraz BGR (numpy array, kompatybilny z OpenCV).
        Używa `exec-out screencap -p`, co zwraca surowe bajty PNG bez
        zapisywania pliku na urządzeniu ani na dysku hosta - szybciej
        i bez śmiecenia w /sdcard.
        """
        cmd = [self.adb_path, "-s", self.serial, "exec-out", "screencap", "-p"]
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
        png_bytes = result.stdout
        arr = np.frombuffer(png_bytes, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is None:
            raise ADBError(f"Nie udało się zdekodować screenshotu z {self.serial}")
        return img

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
