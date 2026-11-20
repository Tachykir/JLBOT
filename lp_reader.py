"""
lp_reader.py
============
Odczyt liczb (LP) z ekranu leaderboardu Just Loot.

Cyfry czcionki gry są porównywane z wzorami wyciętymi z prawdziwych zrzutów
(templates/digits.npz - kilka próbek każdej cyfry w różnych rozmiarach,
grubościach i kolorach). Ekran jest najpierw sprowadzany do szerokości
WORK_WIDTH, w której zrobiono wzory.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

WORK_WIDTH = 1080
GLYPH = (16, 24)  # (szer., wys.) znormalizowanej cyfry
DIGITS_PATH = Path(__file__).resolve().parent / "templates" / "digits.npz"
NAME_PATH = Path(__file__).resolve().parent / "templates" / "mossg.png"


# --------------------------------------------------------------------- #
# Segmentacja
# --------------------------------------------------------------------- #
def text_mask(crop_bgr: np.ndarray) -> np.ndarray:
    """Maska tekstu (1 = tekst) - działa dla jasnego tekstu na ciemnym tle
    i ciemnego na jasnym (karta #1 jest biała)."""
    g = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
    _t, m = cv2.threshold(g, 0, 1, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    if m.mean() > 0.5:  # tekstu jest mniej niż tła
        m = 1 - m
    return m.astype(np.uint8)


def glyphs(mask: np.ndarray) -> tuple[list[tuple[int, int, int, int]], int]:
    """Znaki linii tekstu (bbox x, y, w, h) od lewej i wysokość wielkich
    znaków. Pomija przecinki/kropki (niskie, poniżej górnej linii) i małe
    litery; zlepione cyfry (za szerokie) dzieli na równe części."""
    n, _l, st, _c = cv2.connectedComponentsWithStats(mask, connectivity=8)
    comps = [tuple(int(v) for v in st[i][:4]) for i in range(1, n) if st[i][4] >= 3]
    if not comps:
        return [], 0
    max_h = max(c[3] for c in comps)
    if max_h < 6:
        return [], 0
    top = min(c[1] for c in comps if c[3] >= 0.8 * max_h)
    out = []
    for x, y, w, h in sorted(comps):
        if h < 0.6 * max_h or y > top + 0.25 * max_h:
            continue
        parts = int(round(w / (0.72 * h))) if w > 1.15 * h else 1
        for k in range(parts):
            out.append((x + k * w // parts, y, w // parts, h))
    return out, max_h


def glyph_feature(mask: np.ndarray, box) -> tuple[np.ndarray, float]:
    x, y, w, h = box
    g = mask[y:y + h, x:x + w].astype(np.float32)
    return cv2.resize(g, GLYPH, interpolation=cv2.INTER_AREA), w / h


# --------------------------------------------------------------------- #
# Klasyfikacja (cyfry + litery czcionki, żeby wiedzieć, gdzie kończy się liczba)
# --------------------------------------------------------------------- #
class DigitReader:
    def __init__(self, path: Path = DIGITS_PATH):
        d = np.load(path)
        self.feats = d["feats"].astype(np.float32)
        self.aspects = d["aspects"].astype(np.float32)
        self.labels = d["labels"]

    def classify(self, feat: np.ndarray, aspect: float) -> tuple[str, float]:
        dist = np.abs(self.feats - feat).mean(axis=(1, 2)) + 0.5 * np.abs(self.aspects - aspect)
        i = int(np.argmin(dist))
        return str(self.labels[i]), float(dist[i])

    def read_number(self, crop_bgr: np.ndarray, max_dist: float = 0.25) -> int | None:
        """Pierwsza liczba w linii tekstu (np. "92,854 LP" -> 92854)."""
        mask = text_mask(crop_bgr)
        boxes, h = glyphs(mask)
        s, prev_end = "", None
        for box in boxes:
            if s and prev_end is not None and box[0] - prev_end > 1.0 * h:
                break  # duży odstęp po liczbie
            ch, d = self.classify(*glyph_feature(mask, box))
            if ch.isdigit():
                if d > max_dist:
                    return None  # niepewna cyfra - lepiej nic niż zła liczba
                s += ch
            elif s:
                break  # litera po liczbie ("LP")
            prev_end = box[0] + box[2]
        return int(s) if s else None


# --------------------------------------------------------------------- #
# Ekran leaderboardu
# --------------------------------------------------------------------- #
@dataclass
class Leaderboard:
    my_lp: int | None
    rival_lp: int | None
    rival_y: int | None = None  # do debugowania


def to_work(img: np.ndarray) -> np.ndarray:
    h, w = img.shape[:2]
    if w == WORK_WIDTH:
        return img
    return cv2.resize(img, (WORK_WIDTH, int(round(h * WORK_WIDTH / w))),
                      interpolation=cv2.INTER_AREA if w > WORK_WIDTH else cv2.INTER_CUBIC)


class LeaderboardReader:
    def __init__(self):
        self.digits = DigitReader()
        name = cv2.imread(str(NAME_PATH), cv2.IMREAD_GRAYSCALE)
        if name is None:
            raise FileNotFoundError(NAME_PATH)
        self.name_tpls = [cv2.resize(name, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
                          for s in np.linspace(0.9, 1.1, 5)]

    def find_my_card(self, img: np.ndarray) -> tuple[int, int, int, int] | None:
        """Moja karta w rankingu ma grubą fioletową ramkę."""
        H, W = img.shape[:2]
        hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
        purple = ((hsv[..., 0] >= 120) & (hsv[..., 0] <= 160) & (hsv[..., 1] > 70)
                  & (hsv[..., 2] > 120)).astype(np.uint8)
        n, _l, st, _c = cv2.connectedComponentsWithStats(purple)
        cards = [tuple(int(v) for v in st[i][:4]) for i in range(1, n)
                 if st[i][2] > 0.6 * W and 0.12 * W < st[i][3] < 0.5 * W]
        return max(cards, key=lambda c: c[2] * c[3]) if cards else None

    def find_rival(self, img: np.ndarray) -> tuple[float, tuple[int, int, int, int] | None]:
        g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        best = (0.0, None)
        for src in (g, 255 - g):  # nazwa na jasnej albo ciemnej karcie
            for t in self.name_tpls:
                r = cv2.matchTemplate(src, t, cv2.TM_CCOEFF_NORMED)
                _, mv, _, ml = cv2.minMaxLoc(r)
                if mv > best[0]:
                    best = (float(mv), (ml[0], ml[1], t.shape[1], t.shape[0]))
        return best

    @staticmethod
    def _lines(mask: np.ndarray) -> list[tuple[int, int]]:
        rows = np.where(mask.sum(1) > 0)[0]
        if not len(rows):
            return []
        lines, start, prev = [], rows[0], rows[0]
        for r in rows[1:]:
            if r - prev > 3:
                lines.append((int(start), int(prev)))
                start = r
            prev = r
        lines.append((int(start), int(prev)))
        return lines

    def read_left_lp(self, img: np.ndarray, cy: float, span: float) -> int | None:
        """LP z lewej kolumny rankingu ("#1" / "92,854 LP"): linia z liczbą
        >= 100 najbliższa wysokości cy (w promieniu span)."""
        H, W = img.shape[:2]
        y1, y2 = max(0, int(cy - span)), min(H, int(cy + span))
        col = img[y1:y2, 0:int(0.19 * W)]
        g = cv2.cvtColor(col, cv2.COLOR_BGR2GRAY)
        best = None
        for ly1, ly2 in self._lines((g > 70).astype(np.uint8)):
            if ly2 - ly1 < 6:
                continue
            v = self.digits.read_number(col[max(0, ly1 - 3):ly2 + 4])
            if v is None or v < 100:
                continue
            dist = abs(y1 + (ly1 + ly2) / 2 - cy)
            if best is None or dist < best[0]:
                best = (dist, v)
        return best[1] if best else None

    def read(self, frame: np.ndarray, name_threshold: float = 0.8) -> Leaderboard:
        img = to_work(frame)
        card = self.find_my_card(img)
        my = self.read_left_lp(img, card[1] + card[3] / 2, card[3] / 2) if card else None
        score, box = self.find_rival(img)
        rival = None
        if box and score >= name_threshold:
            rival = self.read_left_lp(img, box[1] + box[3] / 2, 4 * box[3])
        return Leaderboard(my, rival, box[1] if box and score >= name_threshold else None)
