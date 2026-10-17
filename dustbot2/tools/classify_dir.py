"""
Klasyfikuje wszystkie zrzuty w folderze i wypisuje stan każdego z nich.

    python -m dustbot2.tools.classify_dir Unknown
    python -m dustbot2.tools.classify_dir Unknown --sort-into posortowane

--sort-into kopiuje pliki do podfolderów <stan>/ - wygodne do przejrzenia
zrzutów "unknown" i wycięcia z nich nowych szablonów.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from collections import Counter
from pathlib import Path

import cv2

from dustbot2.screens import ScreenClassifier


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("folder", type=Path)
    parser.add_argument("--sort-into", type=Path, default=None)
    args = parser.parse_args(argv)

    clf = ScreenClassifier.load()
    counts: Counter[str] = Counter()
    for path in sorted(args.folder.glob("*.png")):
        frame = cv2.imread(str(path))
        if frame is None:
            print(f"{path.name}: nie można wczytać", file=sys.stderr)
            continue
        result = clf.classify(frame)
        counts[result.state] += 1
        best = result.match.name if result.match else "-"
        print(f"{path.name}\t{result.state}\t{result.score:.3f}\t{best}")
        if args.sort_into is not None:
            target = args.sort_into / result.state
            target.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target / path.name)

    print()
    for state, count in counts.most_common():
        print(f"{state}: {count}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
