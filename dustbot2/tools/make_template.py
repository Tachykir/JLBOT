"""
Wycina nowy szablon ze zrzutu i dopisuje go do templates/manifest.json.

Przykład (prostokąt w pikselach podanego zrzutu, dowolna rozdzielczość):

    python -m dustbot2.tools.make_template --image zrzut.png \\
        --rect 186 885 353 913 --name keep_current --state item_popup \\
        --search 0 0.7 1 1 --threshold 0.8

--search to obszar szukania jako ułamki ekranu (x1 y1 x2 y2). Węższy
obszar = szybciej i mniej fałszywych trafień. Zostaw zapas na przesunięcie
od paska statusu (ok. 3% wysokości).
Bez --state szablon służy tylko do lokalizowania (jak drop_text).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2

from dustbot2.screens import TEMPLATES_DIR


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--image", required=True, type=Path)
    parser.add_argument("--rect", required=True, type=int, nargs=4, metavar=("X1", "Y1", "X2", "Y2"))
    parser.add_argument("--name", required=True)
    parser.add_argument("--state", default=None)
    parser.add_argument("--search", type=float, nargs=4, default=[0.0, 0.0, 1.0, 1.0])
    parser.add_argument("--threshold", type=float, default=0.8)
    parser.add_argument("--priority", type=int, default=0,
                        help="wyższy wygrywa, gdy pasuje kilka stanów (np. 10 dla popupów)")
    parser.add_argument("--dir", type=Path, default=TEMPLATES_DIR)
    args = parser.parse_args(argv)

    manifest_path = args.dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    reference_width = manifest.get("reference_width", 540)

    image = cv2.imread(str(args.image), cv2.IMREAD_GRAYSCALE)
    if image is None:
        print(f"Nie można wczytać {args.image}", file=sys.stderr)
        return 1
    factor = reference_width / image.shape[1]
    if abs(factor - 1.0) > 1e-3:
        image = cv2.resize(image, None, fx=factor, fy=factor, interpolation=cv2.INTER_AREA)
    x1, y1, x2, y2 = (round(v * factor) for v in args.rect)
    crop = image[y1:y2, x1:x2]
    if crop.size == 0:
        print("Pusty prostokąt - sprawdź --rect.", file=sys.stderr)
        return 1

    file_name = f"{args.name}.png"
    cv2.imwrite(str(args.dir / file_name), crop)
    entry = {
        "name": args.name,
        "state": args.state,
        "file": file_name,
        "search": args.search,
        "threshold": args.threshold,
        "priority": args.priority,
        "source": args.image.name,
        "source_rect": [x1, y1, x2, y2],
    }
    manifest["templates"] = [t for t in manifest["templates"] if t["name"] != args.name] + [entry]
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(f"Zapisano szablon {args.name} ({crop.shape[1]}x{crop.shape[0]} px) -> {args.dir / file_name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
