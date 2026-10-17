"""
manager.py
==========
Uruchamia wiele instancji DustMineBot równolegle - po jednym procesie
na każdą instancję LDPlayer. Użycie osobnych procesów (multiprocessing)
zamiast wątków jest ważne, bo:
  - OpenCV (dekodowanie PNG, HoughCircles) jest CPU-bound, więc wątki
    Pythona i tak nie dałyby prawdziwej równoległości (GIL),
  - awaria/wyjątek w jednej instancji nie wywraca pozostałych.

Konfiguracja instancji wczytywana jest z pliku config.json (patrz
config.example.json) - dla każdej instancji LDPlayer wystarczy jej adb
serial i nazwa; pozostałe pola są opcjonalne (domyślne w BotConfig).

Uruchomienie:
    python manager.py --config config.json
    python manager.py --auto            # auto-wykrywanie z `adb devices`
"""

from __future__ import annotations

import argparse
import json
import logging
import multiprocessing as mp
import signal
import sys
import time

from adb_controller import ADBController, list_ldplayer_devices
from dustbot2.schedule import DailyPlayTarget
from bot import DustMineBot, BotConfig, RandomRange, schedule_from_list

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("manager")


def _run_instance(serial: str, config_dict: dict) -> None:
    """
    Funkcja uruchamiana w osobnym procesie potomnym. Tworzy własne
    połączenie ADB (ADBController nie jest bezpieczne do współdzielenia
    między procesami) i uruchamia pętlę bota.
    """
    # każdy proces ma własny logger, ale współdzielony format
    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s [%(levelname)s] [{config_dict['name']}] %(message)s",
    )
    try:
        adb = ADBController(serial=serial)
        cfg = BotConfig(
            name=config_dict["name"],
            idle_click_interval=RandomRange.from_value(config_dict.get("idle_click_interval", [0.8, 1.4])),
            drag_duration_ms=RandomRange.from_value(config_dict.get("drag_duration_ms", [800, 1300])),
            between_drags_delay=RandomRange.from_value(config_dict.get("between_drags_delay", [0.5, 1.1])),
            empty_detections_to_exit=config_dict.get("empty_detections_to_exit", 2),
            max_seconds_per_game_screen=config_dict.get("max_seconds_per_game_screen", 60.0),
            idle_poll_interval=config_dict.get("idle_poll_interval", 0.5),
            schedule=schedule_from_list(config_dict.get("schedule")),
            schedule_poll_interval=config_dict.get("schedule_poll_interval", 15.0),
            close_game_during_break=config_dict.get("close_game_during_break", True),
            game_package=config_dict.get("game_package"),
            game_activity=config_dict.get("game_activity"),
            black_screen_timeout=config_dict.get("black_screen_timeout", 20.0),
            black_screen_brightness_threshold=config_dict.get("black_screen_brightness_threshold", 10.0),
            restart_settle_delay=config_dict.get("restart_settle_delay", 10.0),
            foreign_app_packages=tuple(config_dict.get(
                "foreign_app_packages",
                ["com.android.vending", "com.android.chrome", "com.android.browser"],
            )),
            foreground_check_interval=config_dict.get("foreground_check_interval", 3.0),
            capture_unknown_screens=config_dict.get("capture_unknown_screens", True),
            record_unknown_seconds=config_dict.get("record_unknown_seconds", 0.0),
            record_cooldown=config_dict.get("record_cooldown", 300.0),
            capture_dir=config_dict.get("capture_dir", "captures"),
            touch_backend=config_dict.get("touch_backend", "adb"),
            fishing_latency_ms=config_dict.get("fishing_latency_ms"),
            fishing_aim_spread=config_dict.get("fishing_aim_spread", 0.18),
            daily_play=DailyPlayTarget.from_dict(config_dict.get("daily_play")),
        )
        bot = DustMineBot(adb, cfg)

        def _handle_sigterm(signum, frame):
            bot.stop()

        signal.signal(signal.SIGTERM, _handle_sigterm)
        bot.run()
    except Exception:
        logging.exception("Instancja %s zakończona błędem krytycznym", serial)


class BotManager:
    def __init__(self, instances: list[dict]):
        """
        instances: lista słowników postaci:
          {
            "serial": "127.0.0.1:5555",
            "name": "instance-1",
            ...opcjonalnie inne pola BotConfig...
          }
        """
        self.instances = instances
        self.processes: dict[str, mp.Process] = {}

    def start_all(self) -> None:
        for inst in self.instances:
            serial = inst["serial"]
            name = inst.get("name", serial)
            p = mp.Process(
                target=_run_instance,
                args=(serial, inst),
                name=name,
                daemon=False,
            )
            p.start()
            self.processes[name] = p
            logger.info("Uruchomiono proces bota '%s' (pid=%d, serial=%s)", name, p.pid, serial)

    def stop_all(self, timeout: float = 5.0) -> None:
        for name, p in self.processes.items():
            if p.is_alive():
                logger.info("Zatrzymuję '%s' (pid=%d)...", name, p.pid)
                p.terminate()
        deadline = time.monotonic() + timeout
        for name, p in self.processes.items():
            remaining = max(0.0, deadline - time.monotonic())
            p.join(timeout=remaining)
            if p.is_alive():
                logger.warning("'%s' nie zakończył się w czasie - kill()", name)
                p.kill()

    def wait(self) -> None:
        try:
            while any(p.is_alive() for p in self.processes.values()):
                time.sleep(1.0)
        except KeyboardInterrupt:
            logger.info("Przerwano (Ctrl+C) - zatrzymuję wszystkie instancje...")
            self.stop_all()


def load_config(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data["instances"]


def build_auto_config() -> list[dict]:
    """Auto-wykrywa podłączone urządzenia ADB i tworzy dla nich domyślny config."""
    serials = list_ldplayer_devices()
    if not serials:
        logger.error("Nie znaleziono żadnych urządzeń w `adb devices`. "
                      "Upewnij się, że LDPlayer ma włączone debugowanie ADB "
                      "i porty są znane (Ustawienia LDPlayer -> Inne -> ADB debugging).")
        sys.exit(1)
    instances = []
    for i, serial in enumerate(serials):
        instances.append({
            "serial": serial,
            "name": f"instance-{i+1}",
        })
    return instances


def main() -> None:
    parser = argparse.ArgumentParser(description="Manager wielu instancji DustMineBot")
    parser.add_argument("--config", type=str, help="Ścieżka do pliku config.json")
    parser.add_argument("--auto", action="store_true",
                         help="Auto-wykryj instancje z `adb devices`")
    args = parser.parse_args()

    if args.config:
        instances = load_config(args.config)
    elif args.auto:
        instances = build_auto_config()
    else:
        parser.error("Podaj --config <plik.json> albo --auto")
        return

    logger.info("Startuję %d instancji bota...", len(instances))
    manager = BotManager(instances)
    manager.start_all()
    manager.wait()


if __name__ == "__main__":
    mp.set_start_method("spawn", force=True)  # bezpieczne na Windows i Linux
    main()
