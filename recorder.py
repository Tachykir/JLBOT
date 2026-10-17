"""
recorder.py
===========
Nagrywanie minigier jako materiału do budowy nowych modułów bota.

Jedno nagranie to folder:
    <data>_<instancja>_<etykieta>/
        video.mp4     - wideo z emulatora (`screenrecord`, płynne, do ok. 60 fps)
        touches.txt   - surowe zdarzenia dotyku (`getevent -lt`) z czasem
                        kernela: dokładne momenty i pozycje Twoich dotknięć
        frames/       - zrzuty ekranu w pełnej jakości; nazwa pliku = ms od startu
        meta.json     - instancja, rozdzielczość, czasy startu do synchronizacji

Synchronizacja: meta.json zawiera `device_uptime_at_start` (sekundy od
startu Androida, ta sama skala co znaczniki w touches.txt) oraz czas
startu hosta, więc dotyk, klatki i wideo da się ułożyć na jednej osi.

Nagrywanie niczego nie klika - w trakcie bot nie wysyła żadnych akcji,
więc możesz w tym czasie grać sam.

Nagrywanie ręczne (bez uruchamiania bota):
    python recorder.py --serial emulator-5556 --seconds 30 --label fishing
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

import cv2
import numpy as np

from adb_controller import ADBController, ADBError

# pod "bot", żeby komunikaty trafiały też do logu w GUI
logger = logging.getLogger("bot.recorder")

REMOTE_VIDEO = "/sdcard/jlbot_recording.mp4"
# screenrecord na Androidzie ma twardy limit 180 s na jedno nagranie
MAX_VIDEO_SECONDS = 180


def _safe(text: str) -> str:
    return "".join(c if c.isalnum() or c in "-_" else "_" for c in text)


def save_screenshot(frame: np.ndarray, folder: Path, instance_name: str) -> Path:
    """Zapisuje klatkę jako <data>_<czas>_<mikrosekundy>_<instancja>.png (jak w Unknown/)."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / (dt.datetime.now().strftime("%Y%m%d_%H%M%S_%f") + f"_{_safe(instance_name)}.png")
    if not cv2.imwrite(str(path), frame):
        raise OSError(f"Nie udało się zapisać {path}")
    return path


class MinigameRecorder:
    def __init__(self, adb: ADBController, out_root: Path, instance_name: str):
        self.adb = adb
        self.out_root = Path(out_root)
        self.instance_name = instance_name
        self.folder: Path | None = None
        self._video: subprocess.Popen | None = None
        self._touch: subprocess.Popen | None = None
        self._touch_file = None
        self._started_at = 0.0
        self._frames = 0
        self._meta: dict = {}

    @property
    def active(self) -> bool:
        return self.folder is not None

    def start(self, label: str, seconds: float) -> Path:
        stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        self.folder = self.out_root / f"{stamp}_{_safe(self.instance_name)}_{_safe(label)}"
        (self.folder / "frames").mkdir(parents=True, exist_ok=True)
        self._frames = 0

        uptime = None
        try:
            uptime = float(self.adb.shell("cat", "/proc/uptime").split()[0])
        except (ADBError, ValueError, IndexError) as e:
            logger.warning("[%s] nie udało się odczytać uptime urządzenia: %s", self.instance_name, e)
        try:
            width, height = self.adb.get_screen_size()
        except ADBError:
            width = height = None

        self._started_at = time.monotonic()
        self._meta = {
            "instance": self.instance_name,
            "label": label,
            "serial": self.adb.serial,
            "screen_size": [width, height],
            "started_at": dt.datetime.now().isoformat(timespec="milliseconds"),
            "device_uptime_at_start": uptime,
            "requested_seconds": seconds,
        }

        video_seconds = int(min(max(seconds, 1), MAX_VIDEO_SECONDS))
        try:
            self._video = self.adb.popen(
                ["shell", "screenrecord", "--time-limit", str(video_seconds), REMOTE_VIDEO])
        except ADBError as e:
            logger.warning("[%s] screenrecord niedostępny, zapisuję tylko klatki: %s", self.instance_name, e)
        try:
            self._touch_file = open(self.folder / "touches.txt", "wb")
            self._touch = self.adb.popen(["shell", "getevent", "-lt"], stdout=self._touch_file)
        except ADBError as e:
            logger.warning("[%s] getevent niedostępny, brak zapisu dotyku: %s", self.instance_name, e)

        logger.info("[%s] nagrywanie '%s' (%.0f s) -> %s", self.instance_name, label, seconds, self.folder)
        return self.folder

    def add_frame(self, frame: np.ndarray) -> None:
        if self.folder is None:
            return
        ms = int((time.monotonic() - self._started_at) * 1000)
        cv2.imwrite(str(self.folder / "frames" / f"{ms:07d}.png"), frame)
        self._frames += 1

    def stop(self) -> Path | None:
        if self.folder is None:
            return None
        folder = self.folder
        duration = time.monotonic() - self._started_at

        if self._video is not None:
            # SIGINT pozwala screenrecord poprawnie zamknąć plik mp4
            try:
                self.adb.shell("pkill", "-INT", "screenrecord")
            except ADBError:
                pass  # już się zakończył (np. po time-limit)
            try:
                self._video.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._video.kill()
            time.sleep(0.5)  # chwila na domknięcie pliku na urządzeniu
            try:
                self.adb.pull(REMOTE_VIDEO, str(folder / "video.mp4"))
                self.adb.shell("rm", "-f", REMOTE_VIDEO)
            except ADBError as e:
                logger.warning("[%s] nie udało się pobrać wideo: %s", self.instance_name, e)

        if self._touch is not None:
            self._touch.terminate()
            try:
                self._touch.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._touch.kill()
        if self._touch_file is not None:
            self._touch_file.close()

        self._meta.update({
            "duration_seconds": round(duration, 3),
            "frames": self._frames,
            "has_video": (folder / "video.mp4").exists(),
        })
        (folder / "meta.json").write_text(json.dumps(self._meta, indent=2, ensure_ascii=False), encoding="utf-8")
        logger.info("[%s] nagranie zapisane: %s (%d klatek, %.0f s)",
                    self.instance_name, folder, self._frames, duration)

        self.folder = None
        self._video = self._touch = self._touch_file = None
        return folder

    def record(self, label: str, seconds: float, should_stop=lambda: False,
               frame_interval: float = 0.0, on_frame=None) -> Path | None:
        """
        Nagrywa przez `seconds` sekund, zapisując klatki tak często, jak się da.
        `on_frame(frame)` dostaje każdą klatkę (np. do podglądu w GUI).
        """
        self.start(label, seconds)
        try:
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline and not should_stop():
                try:
                    frame = self.adb.screenshot()
                    self.add_frame(frame)
                    if on_frame is not None:
                        on_frame(frame)
                except ADBError as e:
                    logger.warning("[%s] pominięto klatkę: %s", self.instance_name, e)
                if frame_interval:
                    time.sleep(frame_interval)
        except KeyboardInterrupt:
            logger.info("[%s] przerwano - zapisuję to, co już nagrane", self.instance_name)
        finally:
            folder = self.stop()
        return folder


class UnknownScreenSaver:
    """
    Zapisuje zrzuty ekranów, których bot nie rozpoznaje (np. nowa minigra),
    w tym samym formacie nazw co dotychczasowe zrzuty w Unknown/.
    Pomija klatki prawie identyczne z ostatnio zapisaną i zapisuje nie
    częściej niż co `min_interval` sekund.
    """

    def __init__(self, folder: Path, instance_name: str, min_interval: float = 2.0,
                 min_difference: float = 6.0):
        self.folder = Path(folder)
        self.instance_name = instance_name
        self.min_interval = min_interval
        self.min_difference = min_difference
        self._last_thumb: np.ndarray | None = None
        self._last_saved = 0.0

    def offer(self, frame: np.ndarray) -> Path | None:
        now = time.monotonic()
        if now - self._last_saved < self.min_interval:
            return None
        thumb = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY), (64, 112), interpolation=cv2.INTER_AREA)
        if self._last_thumb is not None:
            if float(np.mean(cv2.absdiff(thumb, self._last_thumb))) < self.min_difference:
                return None
        path = save_screenshot(frame, self.folder, self.instance_name)
        self._last_thumb = thumb
        self._last_saved = now
        return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Nagrywa minigrę: wideo + dotyk + klatki.")
    parser.add_argument("--serial", required=True, help="np. emulator-5556 albo 127.0.0.1:5555")
    parser.add_argument("--seconds", type=float, default=30.0)
    parser.add_argument("--label", default="minigame", help="np. fishing")
    parser.add_argument("--out", type=Path, default=Path("recordings"))
    parser.add_argument("--name", default=None, help="nazwa instancji w nazwie folderu")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    try:
        adb = ADBController(serial=args.serial)
    except ADBError as e:
        print(f"Błąd połączenia: {e}", file=sys.stderr)
        return 1
    recorder = MinigameRecorder(adb, args.out, args.name or args.serial)
    print(f"Nagrywam {args.seconds:.0f} s - zagraj teraz w minigrę na emulatorze. Ctrl+C kończy wcześniej.")
    folder = recorder.record(args.label, args.seconds)
    print(f"Gotowe: {folder}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
