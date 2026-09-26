"""
calibrate.py
============
Narzędzie pomocnicze do kalibracji detekcji i punktu idle_click NA
TWOIM konkretnym ekranie LDPlayer, bez uruchamiania całego bota.

Użycie:

  1) Podgląd detekcji na zrzucie z pliku (np. własnoręcznie zapisany
     screenshot z LDPlayer):

       python calibrate.py --image sciezka/do/screena.png

     Zapisuje `detection_preview.png` z zaznaczonymi wykrytymi kulkami
     (zielone obwódki) i punktem DROP (czerwona obwódka) - służy do
     szybkiej weryfikacji, czy detekcja działa poprawnie na Twoim
     układzie/rozdzielczości przed odpaleniem bota.

  2) Podgląd na żywo ze zrzutu z podłączonej instancji ADB:

       python calibrate.py --serial 127.0.0.1:5555

     Robi jeden zrzut ekranu z urządzenia, pokazuje wynik detekcji
     i wypisuje sugerowany board_region do configu.

  3) Pomoc w ustaleniu współrzędnych idle_point / board_region:

       python calibrate.py --image screen.png --grid

     Nakłada siatkę współrzędnych na obraz, żeby łatwo odczytać punkty
     do config.json (np. gdzie kliknąć, żeby otworzyć eksplozję kopalni).
"""

from __future__ import annotations

import argparse
import sys

import cv2
import numpy as np

import vision
from adb_controller import ADBController


def draw_detection(img: np.ndarray, det: vision.DetectionResult) -> np.ndarray:
    vis = img.copy()
    if det.drop is not None:
        cv2.circle(vis, det.drop.center, det.drop.r, (0, 0, 255), 3)
        cv2.putText(vis, "DROP", (det.drop.x - 30, det.drop.y - det.drop.r - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 255), 2)
    for i, b in enumerate(det.balls):
        cv2.circle(vis, b.center, b.r, (0, 255, 0), 3)
        cv2.putText(vis, str(i), (b.x - 10, b.y + 5),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 255, 0), 2)
    return vis


def draw_grid(img: np.ndarray, step: int = 50) -> np.ndarray:
    vis = img.copy()
    h, w = vis.shape[:2]
    for x in range(0, w, step):
        cv2.line(vis, (x, 0), (x, h), (60, 60, 60), 1)
        cv2.putText(vis, str(x), (x + 2, 15), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (0, 200, 200), 1)
    for y in range(0, h, step):
        cv2.line(vis, (0, y), (w, y), (60, 60, 60), 1)
        cv2.putText(vis, str(y), (2, y + 12), cv2.FONT_HERSHEY_SIMPLEX, 0.35, (0, 200, 200), 1)
    return vis


def main() -> None:
    parser = argparse.ArgumentParser(description="Kalibracja detekcji Dust Mine Bot")
    parser.add_argument("--image", type=str, help="Ścieżka do pliku screenshotu")
    parser.add_argument("--serial", type=str, help="ADB serial (np. 127.0.0.1:5555) - pobierze zrzut na żywo")
    parser.add_argument("--grid", action="store_true", help="Nałóż siatkę współrzędnych")
    parser.add_argument("--region", type=str, default=None,
                         help="Region planszy 'x1,y1,x2,y2' do ograniczenia detekcji")
    parser.add_argument("--out", type=str, default="detection_preview.png",
                         help="Plik wyjściowy z wizualizacją")
    args = parser.parse_args()

    if args.image:
        img = cv2.imread(args.image)
        if img is None:
            print(f"Nie udało się wczytać obrazu: {args.image}", file=sys.stderr)
            sys.exit(1)
    elif args.serial:
        adb = ADBController(serial=args.serial)
        img = adb.screenshot()
        w, h = adb.get_screen_size()
        print(f"Rozmiar ekranu urządzenia: {w}x{h}")
    else:
        parser.error("Podaj --image <plik> albo --serial <adb-serial>")
        return

    region = None
    if args.region:
        parts = [int(p.strip()) for p in args.region.split(",")]
        if len(parts) != 4:
            parser.error("--region musi mieć format x1,y1,x2,y2")
        region = tuple(parts)

    det = vision.detect(img, region=region)

    print(f"DROP: {det.drop}")
    print(f"Znaleziono kulek: {len(det.balls)}")
    for i, b in enumerate(det.balls):
        print(f"  [{i}] x={b.x} y={b.y} r={b.r}")

    vis = draw_detection(img, det)
    if args.grid:
        vis = draw_grid(vis)
    if region:
        x1, y1, x2, y2 = region
        cv2.rectangle(vis, (x1, y1), (x2, y2), (255, 255, 0), 2)

    cv2.imwrite(args.out, vis)
    print(f"\nZapisano wizualizację: {args.out}")

    if det.drop or det.balls:
        all_x = [c.x for c in det.balls] + ([det.drop.x] if det.drop else [])
        all_y = [c.y for c in det.balls] + ([det.drop.y] if det.drop else [])
        margin = 60
        suggested = (
            max(0, min(all_x) - margin),
            max(0, min(all_y) - margin),
            max(all_x) + margin,
            max(all_y) + margin,
        )
        print(f"\nSugerowany board_region (na podstawie wykrytych obiektów): "
              f"{list(suggested)}")
        print("(dostosuj ręcznie tak, aby obejmował cały czarny prostokąt planszy "
              "na każdym możliwym układzie kulek)")


if __name__ == "__main__":
    main()
