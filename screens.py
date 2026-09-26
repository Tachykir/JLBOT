"""
screens.py
==========
Rozpoznawanie ekranów gry potrzebnych do automatycznego otwierania
paczek (zakładki Profile / Craft, okno potwierdzenia, karty z paczek,
ekran porażki w walce).

Gra ma motywy kolorystyczne (np. iOS: akcent błękitny, Android: żółty),
więc detektory opierają się na kształtach i kontrastach (np. "szeroki,
pełny, nasycony przycisk przy dole ekranu", "tekst nasycony vs szary"),
a nie na konkretnym kolorze akcentu. Wszystkie progi są względne do
rozmiaru ekranu, więc działają w dowolnej rozdzielczości.

Status weryfikacji (na prawdziwych zrzutach):
  - find_buy_button, find_plus_button, find_open_dialog: zrzuty z Androida,
  - pozostałe: odtworzone ze zrzutów z iOS - DO POTWIERDZENIA na Androidzie.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from vision import Circle, Rect


def _components(mask: np.ndarray):
    n, _labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    for i in range(1, n):
        x, y, w, h, area = (int(v) for v in stats[i])
        yield x, y, w, h, area


# --------------------------------------------------------------------- #
# Craft -> Loot Packs
# --------------------------------------------------------------------- #

def find_buy_button(bgr: np.ndarray) -> Rect | None:
    """
    Niebieski, pełny przycisk z ceną paczki ("60 Dust") pod licznikiem
    ilości. Kolor paczki Seeker jest ten sam w obu motywach. Gdy nad
    ekranem jest okno potwierdzenia, przycisk jest przyciemniony i nie
    jest wykrywany.
    """
    H, W = bgr.shape[:2]
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (95, 120, 180), (115, 255, 255))
    best = None
    for x, y, w, h, area in _components(mask):
        if not (0.12 * W <= w <= 0.6 * W and 0.025 * H <= h <= 0.08 * H):
            continue
        if not (2.0 <= w / h <= 7.0) or area < 0.8 * w * h:
            continue
        r = Rect(x, y, w, h)
        if best is None or abs(r.center[0] - W / 2) < abs(best.center[0] - W / 2):
            best = r
    return best


def find_plus_button(bgr: np.ndarray, buy: Rect) -> tuple[int, int] | None:
    """
    Przycisk "+" w wierszu ilości nad przyciskiem z ceną: najbardziej na
    prawo wysunięty jasny znak w tym wierszu (na lewo jest "-", na
    środku liczba).
    """
    H, W = bgr.shape[:2]
    cx = buy.center[0]
    y1 = max(0, int(buy.y - 2.0 * buy.h))
    y2 = max(0, int(buy.y - 0.1 * buy.h))
    x1, x2 = int(cx + 0.03 * W), min(W, int(cx + 0.3 * W))
    if y2 <= y1 or x2 <= x1:
        return None
    hsv = cv2.cvtColor(bgr[y1:y2, x1:x2], cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (0, 0, 110), (180, 255, 255))
    best = None
    for x, y, w, h, area in _components(mask):
        if w > 0.08 * W or h > 0.08 * W or area < 6:
            continue
        c = (x1 + x + w // 2, y1 + y + h // 2)
        if best is None or c[0] > best[0]:
            best = c
    return best


@dataclass
class Dialog:
    box: Rect
    open_button: tuple[int, int]


def find_open_dialog(bgr: np.ndarray) -> Dialog | None:
    """
    Okno potwierdzenia "Open Seeker Pack ×N?" (systemowe okno Androida:
    biały prostokąt, przyciski tekstowe CANCEL / OPEN w prawym dolnym
    rogu). Zwraca pozycję OPEN = ostatnie słowo w dolnym wierszu.
    """
    H, W = bgr.shape[:2]
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    white = cv2.inRange(hsv, (0, 0, 235), (180, 30, 255))
    # tekst w oknie robi dziury w masce - domknij je
    white = cv2.morphologyEx(white, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    box = None
    for x, y, w, h, area in _components(white):
        if w >= 0.55 * W and 0.06 * H <= h <= 0.5 * H and area >= 0.85 * w * h:
            if box is None or w * h > box.w * box.h:
                box = Rect(x, y, w, h)
    if box is None:
        return None

    # dolne 40% okna: kolorowy tekst przycisków
    by1 = box.y + int(box.h * 0.6)
    region = hsv[by1:box.y + box.h, box.x:box.x + box.w]
    colored = (region[:, :, 1] > 80) & (region[:, :, 2] > 60)
    cols = np.where(colored.any(axis=0))[0]
    if cols.size == 0:
        # brak kolorowych przycisków - fallback: prawy dolny róg okna
        return Dialog(box, (box.x + box.w - int(0.08 * W), box.y + int(box.h * 0.85)))
    # podział na słowa po przerwach w kolumnach
    gap = max(3, int(0.02 * W))
    words = [[cols[0], cols[0]]]
    for c in cols[1:]:
        if c - words[-1][1] > gap:
            words.append([c, c])
        else:
            words[-1][1] = c
    wx1, wx2 = words[-1]
    rows = np.where(colored[:, wx1:wx2 + 1].any(axis=1))[0]
    cy = by1 + int(rows.mean()) if rows.size else box.y + int(box.h * 0.85)
    return Dialog(box, (int(box.x + (wx1 + wx2) // 2), int(cy)))


# --------------------------------------------------------------------- #
# Pasek nawigacji: okrągły przycisk JUST LOOT
# --------------------------------------------------------------------- #

def find_just_loot(bgr: np.ndarray) -> Circle | None:
    """
    Duży, pełny, okrągły przycisk JUST LOOT na środku paska nawigacji
    (kolor zależy od motywu). Napis w środku robi dziury w masce - liczymy
    wypełnienie po obrysie.
    """
    H, W = bgr.shape[:2]
    y0 = int(0.7 * H)
    hsv = cv2.cvtColor(bgr[y0:], cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (0, 100, 150), (180, 255, 255))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best = None
    for c in contours:
        x, y, w, h = cv2.boundingRect(c)
        if not (0.09 * W <= w <= 0.4 * W and 0.85 <= w / h <= 1.18):
            continue
        r = (w + h) / 4
        if cv2.contourArea(c) < 0.85 * np.pi * r * r:
            continue
        cx, cy = x + w / 2, y0 + y + h / 2
        # przycisk siedzi w pasku nawigacji na samym dole (91-96% wysokości
        # na zmierzonych ekranach) - wyżej bywają np. kulki Dust Mine
        if abs(cx - W / 2) > 0.12 * W or cy < 0.86 * H:
            continue
        if best is None or r > best.r:
            best = Circle(int(cx), int(cy), int(r))
    return best


# --------------------------------------------------------------------- #
# Po wygranej walce: odkrycie dropu ("?") i wybór (Keep Current itd.)
# --------------------------------------------------------------------- #
#
# Panele w tym motywie są tylko odrobinę jaśniejsze od tła (szarość ~20
# vs ~16), a ich obwódki / linie podziału to cienkie linie ~30-45.
# Surowy zrzut ekranu jest bezstratny, więc takie linie da się pewnie
# znaleźć: szukamy długich, ciągłych odcinków w poziomie.

def _horizontal_lines(gray: np.ndarray, y1: int, y2: int, min_len: float,
                      max_len: float | None = None) -> list[tuple[int, int, int]]:
    """Cienkie, długie linie poziome w pasie [y1, y2): (y, x1, x2)."""
    H, W = gray.shape
    band = gray[y1:y2]
    mask = (band >= 26) & (band <= 90)
    rows = []
    for i, row in enumerate(mask):
        if row.sum() < min_len:
            continue
        # najdłuższy ciągły odcinek w wierszu
        padded = np.concatenate(([0], row.astype(np.int8), [0]))
        edges = np.flatnonzero(np.diff(padded))
        starts, ends = edges[0::2], edges[1::2]
        k = int(np.argmax(ends - starts))
        length = int(ends[k] - starts[k])
        if length >= min_len and (max_len is None or length <= max_len):
            rows.append((y1 + i, int(starts[k]), int(ends[k])))
    # sąsiednie wiersze (linia grubości 1-3 px) scal w jedną linię
    lines: list[tuple[int, int, int]] = []
    for y, a, b in rows:
        if lines and y - lines[-1][0] <= 3:
            continue
        lines.append((y, a, b))
    return lines


def find_drop_choice(bgr: np.ndarray) -> list[Rect] | None:
    """
    Panel wyboru po wygranej walce (Replace / Equip & Salvage / Salvage /
    Keep Current): >=4 długie linie poziome w równych odstępach w dolnej
    połowie ekranu (obwódka panelu + linie między przyciskami). Zwraca
    wiersze-przyciski od góry do dołu.
    """
    H, W = bgr.shape[:2]
    # Ekran dropu nie ma paska nawigacji - na ekranach z JUST LOOT (np.
    # lista przedmiotów w Inventory) podobne linie tworzą obwódki kart.
    if find_just_loot(bgr) is not None:
        return None
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    lines = _horizontal_lines(gray, int(0.5 * H), H, 0.6 * W)
    best: list[tuple[int, int, int]] = []
    for i in range(len(lines)):
        chain = [lines[i]]
        for ln in lines[i + 1:]:
            prev = chain[-1]
            same_x = abs(ln[1] - prev[1]) < 0.04 * W and abs(ln[2] - prev[2]) < 0.04 * W
            gap = ln[0] - prev[0]
            if not same_x or not (0.03 * H <= gap <= 0.09 * H):
                continue
            if len(chain) >= 2 and abs(gap - (chain[1][0] - chain[0][0])) > 0.3 * (chain[1][0] - chain[0][0]):
                continue
            chain.append(ln)
        if len(chain) > len(best):
            best = chain
    # panel ma 3-5 przycisków (4-6 linii)
    if not (4 <= len(best) <= 6):
        return None
    x1 = min(l[1] for l in best)
    x2 = max(l[2] for l in best)
    return [Rect(x1, a[0], x2 - x1, b[0] - a[0]) for a, b in zip(best, best[1:])]


def find_reveal_card(bgr: np.ndarray) -> Rect | None:
    """
    Zakryta karta dropu ("?", "TAP ANYWHERE TO REVEAL"): prawie kwadratowa,
    pusta w środku karta na środku ekranu (górna i dolna krawędź to długie
    linie o tej samej szerokości).
    """
    H, W = bgr.shape[:2]
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    lines = _horizontal_lines(gray, int(0.15 * H), int(0.85 * H), 0.35 * W, 0.85 * W)
    # jednolicie wypełniony panel (np. odkryta karta z paczki) daje
    # "linię" co 4 wiersze - z takiego bloku zostaw tylko górną i dolną
    # krawędź, inaczej par do sprawdzenia są dziesiątki tysięcy
    edges: list[tuple[int, int, int]] = []
    for k, line in enumerate(lines):
        joined_prev = k > 0 and line[0] - lines[k - 1][0] <= 4
        joined_next = k + 1 < len(lines) and lines[k + 1][0] - line[0] <= 4
        if not (joined_prev and joined_next):
            edges.append(line)
    lines = edges
    for i, top in enumerate(lines):
        for bottom in lines[i + 1:]:
            if abs(top[1] - bottom[1]) > 0.03 * W or abs(top[2] - bottom[2]) > 0.03 * W:
                continue
            w = top[2] - top[1]
            h = bottom[0] - top[0]
            if not (0.8 <= h / w <= 1.25) or abs((top[1] + top[2]) / 2 - W / 2) > 0.1 * W:
                continue
            inner = gray[top[0] + 4:bottom[0] - 3, top[1] + 4:top[2] - 4]
            if inner.size == 0:
                continue
            # pusta karta: jednolite wnętrze, jasny tylko mały znak "?" na
            # środku (a nie tekst rozsiany po całej powierzchni)
            ys, xs = np.nonzero(inner > 70)
            if xs.size == 0 or xs.size > 0.05 * inner.size or float(np.median(inner)) >= 60:
                continue
            ih, iw = inner.shape
            glyph_w, glyph_h = xs.max() - xs.min(), ys.max() - ys.min()
            cx, cy = xs.mean() / iw, ys.mean() / ih
            if glyph_w < 0.3 * iw and glyph_h < 0.3 * ih and abs(cx - 0.5) < 0.15 and abs(cy - 0.5) < 0.15:
                return Rect(top[1], top[0], w, h)
    return None


def find_item_card(bgr: np.ndarray) -> Rect | None:
    """
    Odkryta karta przedmiotu z paczki ("TAP ANYWHERE TO CONTINUE"):
    pionowa albo kwadratowa (w trakcie odkrywania: tylko rzadkość, typ
    i jakość) karta, odrobinę jaśniejsza od ciemnego tła, na środku
    ekranu, z tekstem w środku, bez paska nawigacji (JUST LOOT). Obwódka
    ma kolor rzadkości (szara, niebieska, fioletowa...), więc szukamy
    wypełnionego prostokąta karty, a nie jego linii.
    """
    H, W = bgr.shape[:2]
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    edges = np.concatenate((gray[:int(0.12 * H)].ravel(), gray[int(0.88 * H):].ravel()))
    bg = float(np.median(edges))
    if bg > 40:
        return None
    mask = (cv2.medianBlur(gray, 5) > bg + 5).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    for x, y, w, h, area in _components(mask):
        if not (0.4 * W <= w <= 0.75 * W and 0.9 <= h / w <= 1.8):
            continue
        if abs(x + w / 2 - W / 2) > 0.05 * W or area < 0.85 * w * h:
            continue
        inner = gray[y + 8:y + h - 8, x + 8:x + w - 8]
        if inner.size == 0 or not (0.003 <= float((inner > 120).mean()) <= 0.15):
            continue  # pusta karta (np. zakryta "?") albo jasny panel
        # poza kartą ciemne tło (tylko nagłówek paczki i napis pod kartą)
        sides = np.concatenate((gray[y:y + h, :max(1, x - 5)].ravel(), gray[y:y + h, x + w + 5:].ravel()))
        if float((sides > bg + 25).mean()) > 0.02:
            continue
        if find_just_loot(bgr) is not None:
            return None  # ekran z paskiem nawigacji (np. Loot Packs)
        return Rect(int(x), int(y), int(w), int(h))
    return None


# --------------------------------------------------------------------- #
# Inventory: przyciski "+x DUST" (rozebranie przedmiotu, przytrzymanie)
# --------------------------------------------------------------------- #

def find_salvage_buttons(bgr: np.ndarray) -> list[tuple[int, int]]:
    """
    Przyciski "+x DUST" w prawym pasku kart przedmiotów na liście
    Inventory: pomarańczowy tekst przy prawej krawędzi, poniżej filtrów
    i powyżej paska nawigacji. Zwraca punkty (x, y) od góry listy.
    """
    H, W = bgr.shape[:2]
    y_top, y_bottom = int(0.25 * H), int(0.9 * H)
    nav = find_just_loot(bgr)
    if nav is not None:
        y_bottom = min(y_bottom, int(nav.y - 1.2 * nav.r))
    x_left = int(0.82 * W)
    hsv = cv2.cvtColor(bgr[y_top:y_bottom, x_left:], cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (10, 120, 150), (35, 255, 255))
    glyphs = [(x_left + x + w / 2, y_top + y + h / 2) for x, y, w, h, area in _components(mask)
              if area >= 8 and h <= 0.03 * H]
    # znaki jednego przycisku ("+8", "DUST") leżą blisko siebie w pionie
    glyphs.sort(key=lambda g: g[1])
    buttons: list[list[tuple[float, float]]] = []
    for g in glyphs:
        if buttons and g[1] - buttons[-1][-1][1] < 0.03 * H:
            buttons[-1].append(g)
        else:
            buttons.append([g])
    return [(int(np.mean([g[0] for g in b])), int(np.mean([g[1] for g in b]))) for b in buttons]


# --------------------------------------------------------------------- #
# Karty z paczek / koniec walki: szeroki przycisk przy dole
# --------------------------------------------------------------------- #

def find_wide_bottom_button(bgr: np.ndarray) -> Rect | None:
    """
    Szeroki, pełny, nasycony przycisk w dolnej części ekranu - "Next Card"
    przy odkrywaniu kart z paczki albo "PROCEED" po walce. (Okrągły
    przycisk JUST LOOT w pasku nawigacji jest za wąski.)
    """
    H, W = bgr.shape[:2]
    y0 = int(0.7 * H)
    hsv = cv2.cvtColor(bgr[y0:], cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (0, 100, 150), (180, 255, 255))
    best = None
    for x, y, w, h, area in _components(mask):
        if w >= 0.6 * W and 0.025 * H <= h <= 0.12 * H and area >= 0.8 * w * h:
            r = Rect(x, y0 + y, w, h)
            if best is None or r.w * r.h > best.w * best.h:
                best = r
    return best


def find_center_button(bgr: np.ndarray) -> Rect | None:
    """
    Pełny, nasycony przycisk na środku ekranu, węższy niż PROCEED - np.
    "Done" na ekranie "Pack Opened" po przejrzeniu kart z paczki. Tylko
    na ciemnym ekranie bez paska nawigacji (JUST LOOT).
    """
    H, W = bgr.shape[:2]
    y0, y1 = int(0.3 * H), int(0.9 * H)
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    if float(np.median(gray)) > 40:
        return None
    hsv = cv2.cvtColor(bgr[y0:y1], cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, (0, 100, 150), (180, 255, 255))
    best = None
    for x, y, w, h, area in _components(mask):
        if (0.4 * W <= w <= 0.85 * W and 0.03 * H <= h <= 0.08 * H and area >= 0.8 * w * h
                and abs(x + w / 2 - W / 2) < 0.05 * W):
            r = Rect(x, y0 + y, w, h)
            if best is None or r.w * r.h > best.w * best.h:
                best = r
    if best is None or find_just_loot(bgr) is not None:
        return None
    return best


def has_defeat_text(bgr: np.ndarray, button: Rect | None = None) -> bool:
    """
    Czerwony napis "Defeated." kończący log walki, nad przyciskiem
    PROCEED. W trakcie walki log też bywa czerwony (np. obrażenia), a na
    dole może być szeroki przycisk, więc napis musi:
      - być krótkim jednym słowem (5-11 liter, ~0.07-0.2 szerokości
        ekranu) przy lewej krawędzi logu,
      - być OSTATNIM wierszem logu - pod nim, aż do przycisku, nie ma
        żadnego jasnego tekstu,
      - leżeć tuż nad przyciskiem (do 0.1 wysokości ekranu).
    Bez przycisku PROCEED to nie jest koniec walki (np. czerwony napis
    "Mythic" w profilu).
    """
    return _final_log_word(bgr, button, ((0, 140, 150), (8, 255, 255)), ((172, 140, 150), (180, 255, 255)))


def has_victory_text(bgr: np.ndarray, button: Rect | None = None) -> bool:
    """Zielony napis "Victory." kończący log walki (te same warunki co
    przy "Defeated.")."""
    return _final_log_word(bgr, button, ((45, 100, 150), (85, 255, 255)))


def _final_log_word(bgr: np.ndarray, button: Rect | None, *ranges) -> bool:
    if button is None:
        button = find_wide_bottom_button(bgr)
    if button is None:
        return False
    H, W = bgr.shape[:2]
    y1 = max(0, int(button.y - 0.4 * H))
    hsv = cv2.cvtColor(bgr[y1:button.y], cv2.COLOR_BGR2HSV)
    color = np.zeros(hsv.shape[:2], np.uint8)
    for lo, hi in ranges:
        color |= cv2.inRange(hsv, lo, hi)
    letters = [(x, y, w, h) for x, y, w, h, area in _components(color)
               if 0.006 * H <= h <= 0.03 * H and w <= 0.05 * W and area >= 8]
    # jasny tekst dowolnego koloru (biały, zielony, żółty, czerwony...)
    bright = (hsv[..., 2] >= 130).astype(np.uint8)
    for _x, y, _w, h in letters:
        row = [l for l in letters if abs((l[1] + l[3] / 2) - (y + h / 2)) < 0.5 * h]
        if not (5 <= len(row) <= 11):
            continue
        x_min = min(l[0] for l in row)
        x_max = max(l[0] + l[2] for l in row)
        bottom = max(l[1] + l[3] for l in row)
        if not (0.07 * W <= x_max - x_min <= 0.2 * W) or x_min > 0.15 * W:
            continue
        if (button.y - y1) - bottom > 0.1 * H:
            continue
        # pod napisem (bez obwódki logu i przycisku) nie może być tekstu
        below = bright[bottom + int(0.006 * H):max(bottom + 1, (button.y - y1) - int(0.012 * H)),
                       int(0.05 * W):int(0.95 * W)]
        if below.size and int(below.sum()) > 0.002 * below.size + 10:
            continue
        return True
    return False


# --------------------------------------------------------------------- #
# Profile: czy wrogowie mają bonus (za słaby gear)
# --------------------------------------------------------------------- #

def frontier_warning(bgr: np.ndarray) -> bool | None:
    """
    Na zakładce Profile pod dużą liczbą "CHOOSE DEPTH" gra pisze:
      - "Gear Level X · enemies at normal strength" (szary tekst) - OK,
      - "Gear Level X · enemies +N% stronger — find newer gear" (kolorowy
        tekst) - wrogowie dostają bonus, trzeba lepszego gearu.

    Zwraca True (ostrzeżenie), False (normalna siła) albo None (nie
    znaleziono selektora głębokości - np. inna zakładka albo trzeba
    przewinąć stronę).
    """
    H, W = bgr.shape[:2]
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)

    # 1) duża biała liczba głębokości: wysokie, białe cyfry na środku ekranu
    white = cv2.inRange(hsv, (0, 0, 200), (180, 50, 255))
    tall = [(x, y, w, h) for x, y, w, h, area in _components(white)
            if 0.05 * W <= h <= 0.15 * W and w <= 0.12 * W]
    number = None
    for _x, y, _w, h in tall:
        row = [t for t in tall if abs(t[1] - y) < 0.3 * h and abs(t[3] - h) < 0.3 * h]
        # głębokość ma 2+ cyfry (np. "87" albo "59.584"), wyśrodkowana;
        # pojedynczy wysoki element to np. wskaźnik w Fishing Hole
        if len(row) >= 2:
            x1 = min(t[0] for t in row)
            x2 = max(t[0] + t[2] for t in row)
            y2 = max(t[1] + t[3] for t in row)
            if abs((x1 + x2) / 2 - W / 2) > 0.15 * W:
                continue
            cand = (x1, y, x2 - x1, y2 - y)
            if number is None or cand[3] > number[3]:
                number = cand
    selector = find_depth_selector(bgr)
    if selector is not None:
        ny2 = selector.bottom  # kafelki (do 79) albo duża liczba ze strzałkami
    elif number is None:
        return None
    else:
        ny2 = number[1] + number[3]

    # 2) pierwszy wiersz tekstu pod liczbą
    y1, y2 = ny2 + int(0.01 * W), min(H, ny2 + int(0.3 * W))
    band = hsv[y1:y2, int(0.03 * W):int(0.97 * W)]
    if band.size == 0:
        return None
    text = band[:, :, 2] > 90
    rows = np.where(text.sum(axis=1) >= 3)[0]
    if rows.size == 0:
        return None
    end = rows[0]
    for r in rows[1:]:
        if r - end > 2:
            break
        end = r
    line = band[rows[0]:end + 1]
    line_text = line[:, :, 2] > 90
    if line_text.sum() < 20:
        return None
    saturated = (line[:, :, 1] > 100) & line_text
    return bool(saturated.sum() / line_text.sum() > 0.5)


# --------------------------------------------------------------------- #
# Profile: wybór głębokości ("CHOOSE DEPTH")
# --------------------------------------------------------------------- #
#
# Dwa układy:
#   - do głębokości 79: rząd kafelków z kolejnymi numerami (wybrany ma
#     kolorową obwódkę) między strzałkami "‹" i "›",
#   - od 80: jedna duża liczba między strzałkami (każde kliknięcie = 1).
# Cyfry czytamy przez porównanie z wzorami czcionki gry (Kode Mono).

@dataclass
class DepthTile:
    number: int | None
    rect: Rect
    selected: bool


@dataclass
class DepthSelector:
    kind: str                  # "tiles" | "big"
    left: Rect                 # strzałka "‹"
    right: Rect                # strzałka "›"
    tiles: list[DepthTile]     # przy "big" - jedna pozycja (sama liczba)
    bottom: int                # dolna krawędź rzędu (pod nim opis siły wrogów)
    left_found: bool = True    # False = strzałka nieaktywna (ledwo widoczna)
    right_found: bool = True

    @property
    def current(self) -> int | None:
        for t in self.tiles:
            if t.selected:
                return t.number
        return None


def read_number(mask: np.ndarray) -> int | None:
    """Odczytuje liczbę z maski jasnych pikseli (cyfry Kode Mono)."""
    import digit_templates as dt
    comps = [c for c in _components(mask.astype(np.uint8)) if c[4] >= 10]
    if not comps:
        return None
    max_h = max(c[3] for c in comps)
    comps = sorted((c for c in comps if c[3] >= 0.6 * max_h), key=lambda c: c[0])
    tw, th = dt.SIZE
    digits = ""
    for x, y, w, h, _area in comps:
        glyph = mask[y:y + h, x:x + w].astype(np.float32)
        norm = cv2.resize(glyph, (tw, th), interpolation=cv2.INTER_AREA)
        aspect = w / h
        scores = [float(np.abs(norm - t).mean()) + 0.5 * abs(aspect - a)
                  for t, a in zip(dt.TEMPLATES, dt.ASPECTS)]
        best = int(np.argmin(scores))
        if scores[best] > 0.3:
            return None
        digits += dt.DIGITS[best]
    if not digits or len(digits) > 4:
        return None
    return int(digits)


def find_depth_selector(bgr: np.ndarray) -> DepthSelector | None:
    H, W = bgr.shape[:2]
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    bg = float(np.median(gray))
    if bg > 40:
        return None
    panels = (cv2.medianBlur(gray, 5) > bg + 4).astype(np.uint8)
    squares = [Rect(x, y, w, h) for x, y, w, h, area in _components(panels)
               if 0.07 * W <= w <= 0.2 * W and 0.75 <= h / w <= 1.3 and area >= 0.7 * w * h]
    nav = find_just_loot(bgr)
    y_limit = nav.y - nav.r if nav is not None else H
    # strzałki: kwadraty przy lewej / prawej krawędzi, większe od slotów
    # ekwipunku (te mają ~0.083 szerokości ekranu)
    arrows = [s for s in squares if s.w >= 0.092 * W and s.y + s.h < y_limit
              and (s.x < 0.06 * W or s.x + s.w > 0.94 * W)]
    for arrow in sorted(arrows, key=lambda s: -s.y):
        cy = arrow.y + arrow.h / 2
        same_row = [s for s in arrows if abs(s.y + s.h / 2 - cy) < 0.03 * W]
        left = next((s for s in same_row if s.x < 0.06 * W), None)
        right = next((s for s in same_row if s.x + s.w > 0.94 * W), None)
        # nieaktywna strzałka jest ledwo widoczna - lustrzane odbicie drugiej
        left_found, right_found = left is not None, right is not None
        if left is None:
            left = Rect(W - right.x - right.w, right.y, right.w, right.h)
        if right is None:
            right = Rect(W - left.x - left.w, left.y, left.w, left.h)
        bottom = max(left.y + left.h, right.y + right.h)

        tiles = []
        for s in sorted(squares, key=lambda s: s.x):
            if (abs(s.y + s.h / 2 - cy) < 0.03 * W and s.x > left.x + left.w
                    and s.x + s.w < right.x and s.w <= 0.15 * W):
                m = int(0.18 * s.w)
                inner = hsv[s.y + m:s.y + s.h - m, s.x + m:s.x + s.w - m]
                number = read_number(inner[..., 2] > 100)
                ring = np.concatenate((hsv[s.y:s.y + 3, s.x:s.x + s.w].reshape(-1, 3),
                                       hsv[s.y + s.h - 3:s.y + s.h, s.x:s.x + s.w].reshape(-1, 3)))
                selected = float(((ring[:, 1] > 90) & (ring[:, 2] > 120)).mean()) > 0.3
                tiles.append(DepthTile(number, s, selected))
        if tiles:
            return DepthSelector("tiles", left, right, tiles, bottom, left_found, right_found)

        # duża biała liczba między strzałkami
        band_y1 = int(max(0, cy - 0.12 * W))
        band_y2 = int(min(H, cy + 0.12 * W))
        x1, x2 = left.x + left.w, right.x
        white = cv2.inRange(hsv[band_y1:band_y2, x1:x2], (0, 0, 200), (180, 60, 255))
        comps = [c for c in _components(white) if c[3] >= 0.05 * W]
        if comps:
            number = read_number(white > 0)
            gx1 = min(c[0] for c in comps)
            gy1 = min(c[1] for c in comps)
            gx2 = max(c[0] + c[2] for c in comps)
            gy2 = max(c[1] + c[3] for c in comps)
            rect = Rect(x1 + gx1, band_y1 + gy1, gx2 - gx1, gy2 - gy1)
            return DepthSelector("big", left, right, [DepthTile(number, rect, True)],
                                 max(bottom, rect.y + rect.h), left_found, right_found)
    return None
