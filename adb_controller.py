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

import queue
import random
import subprocess
import sys
import os
import threading
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


class _ShellSession:
    """
    Jedna stale otwarta sesja `adb shell`, przez którą idą akcje (tap,
    swipe, przeciągnięcia). Każde osobne wywołanie adb.exe to nowy proces i
    nowe połączenie z serwerem ADB (na Windowsie zwykle 30-100 ms) - tu
    polecenie trafia od razu do działającej powłoki.

    Koniec polecenia rozpoznajemy po znaczniku wypisanym przez `echo`
    (razem z kodem wyjścia).
    """

    def __init__(self, adb_path: str, serial: str):
        self.proc = subprocess.Popen(
            [adb_path, "-s", serial, "shell"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=_SUBPROCESS_FLAGS, bufsize=0,
        )
        self._lines: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._n = 0
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self) -> None:
        try:
            for line in iter(self.proc.stdout.readline, b""):
                self._lines.put(line)
        finally:
            self._lines.put(None)  # sesja zamknięta

    def run(self, command: str, timeout: float) -> str:
        with self._lock:
            self._n += 1
            tag = f"__DMB_END_{self._n}__"
            self.proc.stdin.write(f"{command}; echo {tag} $?\n".encode())
            self.proc.stdin.flush()
            deadline = time.monotonic() + timeout
            out = []
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(command)
                try:
                    line = self._lines.get(timeout=remaining)
                except queue.Empty:
                    raise TimeoutError(command) from None
                if line is None:
                    raise EOFError("sesja adb shell zamknięta")
                text = line.decode(errors="replace").strip()
                if text.startswith(tag):
                    code = text[len(tag):].strip()
                    if code not in ("", "0"):
                        raise RuntimeError(f"kod {code}: {' '.join(out)[-200:]}")
                    return "\n".join(out)
                out.append(text)

    def close(self) -> None:
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.kill()
        except OSError:
            pass



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
    # ostatnie akcje (czas, rodzaj, punkty we współrzędnych zrzutu) - do
    # rysowania na podglądzie w GUI; zapis jest tani i nie spowalnia bota
    trace = None
    # opcjonalny odbiorca każdej akcji wejścia (log akcji do CSV):
    # on_action(kind, punkty we współrzędnych zrzutu, {duration_ms, hold_ms, path})
    on_action = None
    # akcje przez stałą sesję `adb shell` (włączane w __init__; obiekty
    # tworzone bez __init__, np. w testach, używają osobnych wywołań)
    persistent_shell = False
    _shell: _ShellSession | None = None
    _shell_retry_at = 0.0
    # pomiary wydajności (średnie kroczące): zrzut, akcja, zrzuty na sekundę
    perf = None
    _last_capture_at = 0.0

    def _perf(self, key: str, value: float) -> None:
        if self.perf is None:
            self.perf = {"capture_ms": 0.0, "input_ms": 0.0, "fps": 0.0, "frame_kb": 0.0}
        old = self.perf.get(key) or 0.0
        self.perf[key] = value if old == 0.0 else 0.8 * old + 0.2 * value

    def _input(self, args: list[str], timeout: float = 10.0, measure: bool = False) -> None:
        """Polecenie `adb shell ...` dla akcji wejścia: przez stałą sesję,
        a gdy jej brak albo się zepsuła - zwykłym wywołaniem adb."""
        start = time.monotonic()
        done = False
        if self.persistent_shell and start >= self._shell_retry_at:
            try:
                if self._shell is None or self._shell.proc.poll() is not None:
                    self._shell = _ShellSession(self.adb_path, self.serial)
                self._shell.run(" ".join(args), timeout)
                done = True
            except (TimeoutError, RuntimeError) as e:
                # polecenie mogło się już (częściowo) wykonać - nie powtarzamy go
                self.close()
                raise ADBError(f"[{self.serial}] akcja przez adb shell nie powiodła się: {e}") from e
            except (OSError, EOFError) as e:
                logger.warning("[%s] sesja adb shell: %s - wracam do osobnych wywołań na 60 s", self.serial, e)
                self.close()
                self._shell_retry_at = time.monotonic() + 60
        if not done:
            self._run(["shell"] + args, timeout=timeout)
        if measure:
            self._perf("input_ms", (time.monotonic() - start) * 1000)

    def close(self) -> None:
        """Zamyka stałą sesję (przy zatrzymaniu bota)."""
        if self._shell is not None:
            self._shell.close()
            self._shell = None

    def mark(self, kind: str, points, **meta) -> None:
        if self.trace is None:
            import collections
            self.trace = collections.deque(maxlen=40)
        self.trace.append((time.monotonic(), kind, [(int(x), int(y)) for x, y in points]))
        if self.on_action is not None and kind != "rect":
            try:
                self.on_action(kind, points, meta)
            except Exception:  # log nie może przerwać bota
                logger.debug("on_action failed", exc_info=True)

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
        # akcje przez jedną stale otwartą sesję `adb shell` (szybciej niż
        # osobny proces adb.exe na każde kliknięcie)
        self.persistent_shell = True
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
        self.mark("tap", [(x, y)])
        self._input(["input", "tap", str(self._s(x)), str(self._s(y))], measure=True)

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 400) -> None:
        """
        Przeciągnięcie od (x1,y1) do (x2,y2).
        duration_ms wpływa na to, jak "naturalnie" wygląda drag - gra
        może wymagać minimalnego czasu trwania, żeby uznać to za drag
        a nie klik; 300-600ms zwykle działa dobrze dla mechanik drag&drop.
        """
        is_tap = abs(x2 - x1) + abs(y2 - y1) <= 4
        self.mark("tap" if is_tap else "swipe", [(x1, y1), (x2, y2)],
                  duration_ms=int(duration_ms), hold_ms=int(duration_ms) if is_tap else 0)
        start = time.monotonic()
        self._input([
            "input", "swipe",
            str(self._s(x1)), str(self._s(y1)), str(self._s(x2)), str(self._s(y2)),
            str(int(duration_ms)),
        ])
        if is_tap:  # narzut jednej akcji = czas wykonania minus sam dotyk
            self._perf("input_ms", max(0.0, (time.monotonic() - start) * 1000 - duration_ms))

    def tap_sequence(self, taps: list[tuple[int, int, int]], gaps_s: list[float]) -> None:
        """
        Seria tapnięć jednym poleceniem `adb shell` (bez narzutu
        uruchamiania adb.exe przy każdym kliknięciu). `taps` to
        (x, y, czas_dotyku_ms) - dotyk jako mikro-swipe o 0-2 px, jak w
        pojedynczym tapnięciu; `gaps_s` - przerwy między kolejnymi.
        """
        if not taps:
            return
        for x, y, h in taps:
            self.mark("tap", [(x, y)], hold_ms=int(h), duration_ms=int(h))
        parts = []
        for i, (x, y, hold) in enumerate(taps):
            if i and i - 1 < len(gaps_s) and gaps_s[i - 1] > 0:
                parts.append(f"sleep {gaps_s[i - 1]:.3f}")
            dx, dy = random.randint(-1, 1), random.randint(-1, 1)
            x, y = self._s(x), self._s(y)
            parts.append(f"input swipe {x} {y} {x + dx} {y + dy} {max(1, int(hold))}")
        total = sum(gaps_s) + len(taps) * 0.5
        self._input(["; ".join(parts)], timeout=10 + total)

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

    def drag_path(self, points: list[tuple[int, int]], duration_ms: float,
                  end_hold_ms: float = 0.0) -> None:
        """
        Przeciąga palcem po łamanej `points` jako JEDEN ciągły dotyk
        (DOWN na pierwszym punkcie, MOVE po kolejnych, UP na ostatnim),
        w łącznym czasie ok. `duration_ms`.

        Wymaga `input motionevent` (nowsze Androidy). Gdy go brak,
        wykonuje zwykły prosty swipe od pierwszego do ostatniego punktu.
        Cała sekwencja idzie jednym poleceniem `adb shell`, więc między
        punktami nie ma narzutu uruchamiania adb.exe.

        end_hold_ms: jak długo palec stoi na ostatnim punkcie przed
        puszczeniem (z drobnym drżeniem) - gra przesuwa obiekt za palcem z
        opóźnieniem, a puszczenie od razu po dojechaniu upuszcza go przed celem.
        """
        if len(points) < 2:
            return
        step = max(1, len(points) // 16)
        preview = list(points[::step]) + [points[-1]]  # podgląd w GUI - wystarczy kilkanaście punktów
        if self._motionevent_ok is None:
            self._motionevent_ok = self._probe_motionevent()
        if not self._motionevent_ok:
            (x1, y1), (x2, y2) = points[0], points[-1]
            if end_hold_ms > 0:
                # zwykły swipe puszcza palec od razu po dojechaniu - wolniej
                # (mniejsze opóźnienie kulki za palcem) i odrobinę za cel
                dist = float(np.hypot(x2 - x1, y2 - y1))
                if dist > 1:
                    over = min(0.04 * dist, 15.0)
                    x2 = int(round(x2 + (x2 - x1) / dist * over))
                    y2 = int(round(y2 + (y2 - y1) / dist * over))
                duration_ms = duration_ms * 1.5
            self.mark("drag", [(x1, y1), (x2, y2)], duration_ms=int(duration_ms), hold_ms=0,
                      path=[(x1, y1), (x2, y2)])
            self._input(["input", "swipe", str(self._s(x1)), str(self._s(y1)),
                         str(self._s(x2)), str(self._s(y2)), str(int(duration_ms))])
            return
        self.mark("drag", preview, duration_ms=int(duration_ms), hold_ms=int(end_hold_ms), path=list(points))

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
        hold_calls = 0
        if end_hold_ms > 0:
            # palec stoi na celu: 2 drobne ruchy w miejscu (jak drżenie dłoni)
            step = max(0.0, end_hold_ms / 1000.0 / 2 - self._input_latency_s)
            for _ in range(2):
                if step:
                    parts.append(f"sleep {step:.3f}")
                parts.append(f"input motionevent MOVE {self._s(x) + random.randint(-1, 1)} "
                             f"{self._s(y) + random.randint(-1, 1)}")
                hold_calls += 1
        parts.append(f"input motionevent UP {self._s(x)} {self._s(y)}")

        start = time.monotonic()
        self._input(["; ".join(parts)], timeout=15 + 3 * (duration_ms + end_hold_ms) / 1000.0)
        elapsed = time.monotonic() - start

        # dopasuj szacunek opóźnienia jednego `input`, żeby kolejne
        # przeciągnięcia trwały bliżej zadanego czasu
        calls = len(moves) + 2 + hold_calls
        measured = max(0.0, elapsed - sleep_s * len(moves) - end_hold_ms / 1000.0 * (hold_calls > 0)) / calls
        self._input_latency_s = 0.7 * self._input_latency_s + 0.3 * measured

    # ------------------------------------------------------------------ #
    # Wyjście: screenshot
    # ------------------------------------------------------------------ #
    device_width = 0   # szerokość ekranu urządzenia z ostatniego zrzutu

    def screenshot(self) -> np.ndarray:
        """Zrzut ekranu (BGR) z pomiarem czasu, rozmiaru danych i liczby
        zrzutów na sekundę (perf - widoczne w GUI)."""
        start = time.monotonic()
        img = self._screenshot()
        now = time.monotonic()
        self._perf("capture_ms", (now - start) * 1000)
        if self._last_capture_at:
            gap = now - self._last_capture_at
            if 0 < gap < 10:
                self._perf("fps", 1.0 / gap)
        self._last_capture_at = now
        self.device_width = int(round(img.shape[1] * self.scale))
        return img

    def _screenshot(self) -> np.ndarray:
        """
        Zwraca screenshot jako obraz BGR (numpy array, kompatybilny z OpenCV).

        Domyślnie używa surowego `exec-out screencap` (bez `-p`): Android
        nie musi kodować PNG, a my dekodować - to zwykle kilka razy
        szybciej. Jeśli urządzenie zwróci nieobsługiwany format, instancja
        na stałe przełącza się na `screencap -p` (PNG).
        """
        if self._raw_screencap_ok:
            data = self._exec_out(["screencap"])
            self._perf("frame_kb", len(data) / 1024)
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
        else:
            # activity uruchomieniowa z systemu - `monkey` bywa niedostępny
            # (np. LDPlayer 14: "monkey: inaccessible or not found")
            component = self._launcher_component(package_name)
        if component:
            result = self._run(["shell", "am", "start", "-n", component])
            out = (result.stdout or b"").decode(errors="replace")
            if "Error" not in out:
                return
            logger.info("[%s] am start %s: %s", self.serial, component, out.strip()[:200])
        self._run([
            "shell", "monkey",
            "-p", package_name,
            "-c", "android.intent.category.LAUNCHER",
            "1",
        ])

    def _launcher_component(self, package_name: str) -> str | None:
        """Activity uruchomieniowa pakietu ("pakiet/.MainActivity") wg
        systemu (`cmd package resolve-activity`), zapamiętana po pierwszym
        odczycie. None, gdy nie udało się jej ustalić."""
        cache = self.__dict__.setdefault("_launcher_cache", {})
        if package_name in cache:
            return cache[package_name]
        component = None
        try:
            result = self._run(["shell", "cmd", "package", "resolve-activity", "--brief",
                                "-a", "android.intent.action.MAIN",
                                "-c", "android.intent.category.LAUNCHER", package_name])
            for line in (result.stdout or b"").decode(errors="replace").splitlines()[::-1]:
                line = line.strip()
                if line.startswith(package_name + "/"):
                    component = line
                    break
        except ADBError as e:
            logger.info("[%s] resolve-activity nie zadziałało: %s", self.serial, e)
        if component:
            cache[package_name] = component
            logger.info("[%s] activity startowa gry: %s", self.serial, component)
        return component

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
