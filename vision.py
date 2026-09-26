"""
vision.py
=========
Wykrywanie elementów gry "Dust Mine Explosion" na screenshocie:
  1. Punkt DROP - przerywany biały okrąg z napisem "DROP" na czarnym tle.
  2. Kolorowe kulki (fragmenty pyłu) - jednolicie wypełnione koła.

Podejście:
- DROP wykrywamy przez wyszukanie konturu w masce "prawie białych, cienkich"
  krawędzi, który jest w przybliżeniu kołowy (dużo białych pikseli na
  obwodzie okręgu, mało w środku - bo obwódka jest przerywana i pusta
  w środku). To odróżnia go od pełnych, jednokolorowych kulek.
- Kulki wykrywamy przez HoughCircles na obrazie w skali szarości
  (kulki są gładkimi, wypełnionymi kołami o wyraźnym kontraście wobec
  czarnego tła), a następnie odfiltrowujemy okrąg DROP (bo też jest
  okrągły) po tym, że DROP ma pusty/ciemny środek i przerywaną krawędź,
  a kulki mają jednolicie wypełniony środek innym kolorem niż tło.

Całość nie zależy od konkretnych zrzutów ekranu (nie ma template
matchingu) - działa dla dowolnego układu kulek i dowolnej pozycji DROP,
dopóki styl graficzny gry (ciemne tło, jednolite koła, przerywany biały
okrąg) się nie zmieni drastycznie.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import numpy as np
import cv2


@dataclass
class Circle:
    x: int
    y: int
    r: int

    @property
    def center(self) -> tuple[int, int]:
        return (self.x, self.y)


@dataclass
class Rect:
    x: int
    y: int
    w: int
    h: int

    @property
    def center(self) -> tuple[int, int]:
        return (self.x + self.w // 2, self.y + self.h // 2)


@dataclass
class DetectionResult:
    drop: Circle | None
    balls: list[Circle]
    # Minigra "Depth Collapse": brązowe kafelki "TAP" do klikania.
    # tiles = obszary (pojedyncze albo zlane nachodzące na siebie
    # kafelki), tile_taps = punkty do kliknięcia (po jednym na kafelek).
    tiles: list[Rect] = field(default_factory=list)
    tile_taps: list[tuple[int, int]] = field(default_factory=list)
    tile_size: int = 0
    # Minigra "Fishing Hole": przycisk CAST.
    cast_button: Rect | None = None


def _ring_crop(h: int, w: int, cx: int, cy: int, r: int, thickness: int):
    """
    Rysuje okrąg (cv2.circle) w małym wycinku obrazu h x w obejmującym
    go w całości. Zwraca (maska_wycinka, offset_x, offset_y). Piksele
    są dokładnie te same, co przy rysowaniu na pełnym obrazie h x w
    (łącznie z przycięciem na krawędziach obrazu).
    """
    pad = r + thickness + 2
    x0, y0 = max(0, cx - pad), max(0, cy - pad)
    x1, y1 = min(w, cx + pad + 1), min(h, cy + pad + 1)
    if x1 <= x0 or y1 <= y0:
        return np.zeros((0, 0), np.uint8), x0, y0
    crop = np.zeros((y1 - y0, x1 - x0), dtype=np.uint8)
    cv2.circle(crop, (cx - x0, cy - y0), r, 255, thickness=thickness)
    return crop, x0, y0


def _find_drop_point(bgr: np.ndarray, gray: np.ndarray,
                      value_thresh: int = 200, sat_thresh: int = 25) -> Circle | None:
    """
    Szuka punktu DROP: przerywanego BIAŁEGO okręgu z tekstem "DROP" w środku.

    Kluczowa obserwacja: kolorowe kulki bywają na tyle jasne w skali
    szarości, że prosty próg jasności (grayscale > 150) fałszywie łapie
    ich brzegi. Odróżniamy prawdziwą biel od jasnego koloru przez HSV:
    biel = wysoka jasność (V) ORAZ NISKA saturacja (S) - kulki mają
    wysoką saturację (są wyraźnie kolorowe), a biała kreska/tekst DROP
    ma saturację bliską zeru.

    Dalej:
      1. Maska = piksele "prawie białe" (niska saturacja, wysoka jasność).
      2. Dylatacja łączy przerywaną kreskę + tekst "DROP" w jeden spójny
         obszar (kontur), odizolowany od kulek (które do maski w ogóle
         nie wchodzą, bo są nasycone kolorem).
      3. Centroid jasnych pikseli w obszarze konturu = środek okręgu
         (tekst "DROP" siedzi centralnie, więc symetrycznie dociąga
         centroid do prawdziwego środka).
      4. Skan promieniowy (profil jasności w funkcji promienia) wokół
         centroidu - szukamy promienia z pikiem pokrycia obwodu,
         rozłożonym po większości kątów (odróżnia okrąg od skupionego
         w jednym miejscu tekstu).
    """
    h, w = gray.shape
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    sat = hsv[:, :, 1]
    val = hsv[:, :, 2]
    mask = ((val > value_thresh) & (sat < sat_thresh)).astype(np.uint8) * 255

    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (21, 21))
    dilated = cv2.dilate(mask, kernel, iterations=1)

    contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    best = None
    best_score = 0.0

    for c in contours:
        area = cv2.contourArea(c)
        if area < 3000:  # za mały obszar, by być okręgiem DROP (odsiewa drobny UI tekst)
            continue

        x, y, bw, bh = cv2.boundingRect(c)
        if bw == 0 or bh == 0:
            continue
        aspect = bw / bh
        if aspect < 0.6 or aspect > 1.6:
            continue

        pad = 10
        y1, y2 = max(0, y - pad), min(h, y + bh + pad)
        x1, x2 = max(0, x - pad), min(w, x + bw + pad)
        sub_mask = mask[y1:y2, x1:x2]
        ys, xs = np.where(sub_mask > 0)
        if xs.size < 20:
            continue
        cx = float(xs.mean()) + x1
        cy = float(ys.mean()) + y1

        approx_r = max(bw, bh) / 2.0
        r_lo = max(10, int(approx_r * 0.45))
        r_hi = int(approx_r * 1.15)

        peak_score = 0.0
        peak_r = None
        icx, icy = int(cx), int(cy)
        for r in range(r_lo, r_hi, 2):
            # Pierścień rysujemy tylko w wycinku wokół okręgu (a nie na
            # masce wielkości całego obrazu) - rasteryzacja okręgu o
            # całkowitym środku jest niezmiennicza względem przesunięcia,
            # więc zbiór pikseli jest identyczny, a koszt ~20x mniejszy.
            ring_mask, ox, oy = _ring_crop(h, w, icx, icy, r, thickness=4)
            ys_r, xs_r = np.nonzero(ring_mask)
            if ys_r.size == 0:
                continue
            ys_r = ys_r + oy
            xs_r = xs_r + ox
            vals_on_ring = mask[ys_r, xs_r]
            ratio = float(np.mean(vals_on_ring > 0))

            bright_idx = vals_on_ring > 0
            if bright_idx.sum() == 0:
                angular_coverage = 0.0
            else:
                bx = xs_r[bright_idx] - cx
                by = ys_r[bright_idx] - cy
                angles = np.arctan2(by, bx)
                sector = ((angles + np.pi) / (2 * np.pi) * 16).astype(int) % 16
                angular_coverage = len(np.unique(sector)) / 16.0

            combined = ratio * angular_coverage
            if combined > peak_score:
                peak_score = combined
                peak_r = r

        # Prawdziwy DROP: przerywana kreska pokrywa realistycznie
        # 15-70% obwodu (reszta to przerwy), rozłożona po większości
        # kątów (>=55% sektorów) - odróżnia okrąg od punktowego tekstu.
        if peak_r is not None and peak_score > best_score:
            best_score = peak_score
            best = Circle(int(cx), int(cy), peak_r)

    if best is not None and best_score >= 0.55 * 0.55:
        # próg złożony: sensowny ratio * sensowny angular coverage
        return best
    return None


def _find_balls(bgr: np.ndarray, gray: np.ndarray, hough_circles: np.ndarray | None,
                 drop: Circle | None) -> list[Circle]:
    """
    Spośród kandydatów HoughCircles wybiera te, które mają jednolicie
    wypełnione, kolorowe (nie czarne, nie białe) wnętrze - to są kulki.
    Odrzuca kandydata pokrywającego się z pozycją DROP.
    """
    if hough_circles is None:
        return []

    h, w = bgr.shape[:2]
    balls: list[Circle] = []

    for c in hough_circles[0]:
        x, y, r = c
        x, y, r = int(x), int(y), int(r)
        if r < 10 or x - r < 0 or y - r < 0 or x + r >= w or y + r >= h:
            continue

        if drop is not None:
            dist = np.hypot(x - drop.x, y - drop.y)
            if dist < max(drop.r, r) * 0.8:
                continue  # to ten sam obiekt co DROP

        # wnętrze kulki - wycinek wokół niej zamiast maski całego obrazu
        inner_r = max(1, int(r * 0.7))
        x0, y0 = max(0, x - inner_r - 1), max(0, y - inner_r - 1)
        x1, y1 = min(w, x + inner_r + 2), min(h, y + inner_r + 2)
        inner_mask = np.zeros((y1 - y0, x1 - x0), dtype=np.uint8)
        cv2.circle(inner_mask, (x - x0, y - y0), inner_r, 255, thickness=-1)

        inner_gray_vals = gray[y0:y1, x0:x1][inner_mask == 255]
        if inner_gray_vals.size == 0:
            continue
        inner_brightness = float(np.mean(inner_gray_vals))

        # odrzuć prawie czarne (tło) i prawie białe (tekst/UI) wnętrza
        if inner_brightness < 40 or inner_brightness > 235:
            continue

        # sprawdź spójność koloru wewnątrz (kulki są jednolite - niska wariancja)
        std = float(np.std(inner_gray_vals))
        if std > 45:
            continue

        balls.append(Circle(x, y, r))

    # deduplikacja bliskich sobie wykryć (Hough czasem zwraca duplikaty)
    deduped: list[Circle] = []
    for b in balls:
        if all(np.hypot(b.x - o.x, b.y - o.y) > max(b.r, o.r) for o in deduped):
            deduped.append(b)

    return deduped


def _crop(bgr: np.ndarray, region: tuple[int, int, int, int] | None):
    """Wycina region (x1, y1, x2, y2) przycięty do granic obrazu - config
    z innej rozdzielczości nie może wywrócić detekcji. Zwraca
    (wycinek, offset_x, offset_y); wycinek może być pusty."""
    if region is None:
        return bgr, 0, 0
    h, w = bgr.shape[:2]
    x1, y1, x2, y2 = region
    x1, x2 = max(0, min(w, x1)), max(0, min(w, x2))
    y1, y2 = max(0, min(h, y1)), max(0, min(h, y2))
    return bgr[y1:y2, x1:x2], x1, y1


def detect(bgr: np.ndarray, region: tuple[int, int, int, int] | None = None) -> DetectionResult:
    """
    Główna funkcja detekcji.

    bgr: pełny screenshot (numpy array BGR).
    region: opcjonalny obszar (x1, y1, x2, y2), w którym szukamy elementów
            gry - warto ograniczyć do obszaru "planszy" (czarny prostokąt
            widoczny na screenach), żeby uniknąć fałszywych trafień w UI.

    Zwraca DetectionResult z pozycją DROP (lub None jeśli nie znaleziono)
    i listą wykrytych kulek (we współrzędnych PEŁNEGO screenshotu, nie
    regionu - czyli gotowych do bezpośredniego użycia w tap/swipe).
    """
    work, offset_x, offset_y = _crop(bgr, region)
    if work.size == 0:
        return DetectionResult(drop=None, balls=[])

    gray = cv2.cvtColor(work, cv2.COLOR_BGR2GRAY)
    gray_blur = cv2.medianBlur(gray, 5)

    circles = cv2.HoughCircles(
        gray_blur,
        cv2.HOUGH_GRADIENT,
        dp=1.2,
        minDist=40,
        param1=80,
        param2=28,
        minRadius=15,
        maxRadius=90,
    )
    if circles is not None:
        circles = np.uint16(np.around(circles))

    # DROP wykrywany osobną metodą (patrz docstring _find_drop_point) -
    # radzi sobie z przerywaną linią i odróżnia biel od jasnych kolorów.
    drop = _find_drop_point(work, gray)
    balls = _find_balls(work, gray, circles, drop)

    # przesuń współrzędne z powrotem na pełny obraz
    if drop is not None:
        drop = Circle(drop.x + offset_x, drop.y + offset_y, drop.r)
    balls = [Circle(b.x + offset_x, b.y + offset_y, b.r) for b in balls]

    return DetectionResult(drop=drop, balls=balls)


# --------------------------------------------------------------------- #
# Minigra "Depth Collapse" - kafelki do klikania
# --------------------------------------------------------------------- #

# Brązowy kafelek (HSV w skali OpenCV: H 0-180). Zmierzone na zrzucie:
# wnętrze H=17 S=102 V=140, jaśniejsza obwódka H=16 S~60 V~175.
_TILE_HSV_LO = (10, 40, 90)
_TILE_HSV_HI = (28, 200, 255)


# Kafelek ma ~13% szerokości ekranu (zmierzone: 120 px przy 924 px).
_TILE_WIDTH_RATIO = 0.13


def _find_debris_tiles(bgr: np.ndarray, screen_width: int | None = None, in_game: bool = False
                       ) -> tuple[list[Rect], list[tuple[int, int]], int]:
    """
    Szuka brązowych, kwadratowych kafelków z ciemnym napisem "TAP".
    Zwraca (obszary, punkty_kliknięcia, rozmiar_kafelka_px).

    Nachodzące na siebie kafelki zlewają się w masce w jeden obszar -
    dzielimy go na siatkę o oczku równym rozmiarowi kafelka i klikamy w
    te oczka, które faktycznie leżą na kafelku.

    in_game=False (rozpoznawanie ekranu): wymagany co najmniej jeden
    pojedynczy, wyraźny kafelek - chroni przed fałszywym uznaniem innego
    ekranu za minigrę. in_game=True (wiadomo, że trwa minigra): gdy
    zostały już tylko nachodzące na siebie kafelki, rozmiar kafelka
    bierzemy ze znanej proporcji do szerokości ekranu.
    """
    h, w = bgr.shape[:2]
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, _TILE_HSV_LO, _TILE_HSV_HI)

    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    min_side = 0.06 * w
    max_side = 0.9 * w  # zlane kafelki mogą być duże; pojedyncze filtrujemy niżej

    blobs = []
    for c in contours:
        x, y, bw, bh = cv2.boundingRect(c)
        if min(bw, bh) < min_side or min(bw, bh) > max_side:
            continue
        filled = np.zeros((bh, bw), np.uint8)
        cv2.drawContours(filled, [c - [x, y]], -1, 255, thickness=-1)
        filled_area = int(np.count_nonzero(filled))
        mask_area = int(np.count_nonzero(mask[y:y + bh, x:x + bw] & filled))
        if filled_area == 0:
            continue
        blobs.append((x, y, bw, bh, filled, filled_area / (bw * bh), 1 - mask_area / filled_area))

    # Pojedynczy kafelek: prawie kwadrat, wypełnia >=85% prostokąta
    # (zaokrąglone rogi; kulka-koło to ~78%), z dziurami po napisie TAP.
    singles = [b for b in blobs
               if 0.8 <= b[2] / b[3] <= 1.25 and b[5] >= 0.85 and 0.005 <= b[6] <= 0.25
               and max(b[2], b[3]) <= 0.25 * w]
    if singles:
        size = int(np.median([min(b[2], b[3]) for b in singles]))
    elif in_game:
        size = int(_TILE_WIDTH_RATIO * (screen_width or w))
        # tylko plamy z napisem TAP, co najmniej wielkości kafelka
        blobs = [b for b in blobs if b[6] >= 0.005 and min(b[2], b[3]) >= 0.8 * size]
        if not blobs:
            return [], [], 0
    else:
        return [], [], 0

    rects: list[Rect] = []
    taps: list[tuple[int, int]] = []
    for x, y, bw, bh, filled, fill_ratio, _holes in blobs:
        # zlane kafelki mogą tworzyć dowolny kształt (schodki, L) - nie
        # filtrujemy ich po wypełnieniu, bo punkty kliknięć i tak muszą
        # leżeć na kafelku (sprawdzane niżej)
        if max(bw, bh) > 8 * size:
            continue
        if max(bw, bh) < 0.8 * size:
            continue
        nx = max(1, int(round(bw / size)))
        ny = max(1, int(round(bh / size)))
        xs = np.linspace(size / 2, bw - size / 2, nx) if nx > 1 else [bw / 2]
        ys = np.linspace(size / 2, bh - size / 2, ny) if ny > 1 else [bh / 2]
        points = [(int(px), int(py)) for py in ys for px in xs
                  if filled[min(bh - 1, int(py)), min(bw - 1, int(px))]]
        if not points:
            continue
        rects.append(Rect(x, y, bw, bh))
        taps.extend((x + px, y + py) for px, py in points)
    return rects, taps, size


# --------------------------------------------------------------------- #
# Minigra "Fishing Hole" - przycisk CAST
# --------------------------------------------------------------------- #

# Cyjan przycisku (zmierzone: H=93-95, S~130-150, V~245).
_CAST_HSV_LO = (80, 90, 150)
_CAST_HSV_HI = (100, 255, 255)


def _find_cast_button(bgr: np.ndarray) -> Rect | None:
    """
    Szuka przycisku CAST: szerokiego, pustego w środku prostokąta
    obrysowanego cyjanem (ramka, nie wypełnienie - odróżnia go od
    pełnego cyjanowego wskaźnika nad nim).
    """
    h, w = bgr.shape[:2]
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, _CAST_HSV_LO, _CAST_HSV_HI)
    # domknij drobne przerwy w cienkiej ramce (kompresja, skalowanie)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    n, _labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    best = None
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        if bw < 0.2 * w or bw > 0.7 * w or bh < 0.02 * w:
            continue
        if not (2.0 <= bw / bh <= 6.0):
            continue
        if area / (bw * bh) > 0.35:  # ramka, a nie pełny prostokąt
            continue
        if best is None or bw * bh > best.w * best.h:
            best = Rect(int(x), int(y), int(bw), int(bh))
    return best


def detect_all(bgr: np.ndarray, region: tuple[int, int, int, int] | None = None,
               in_debris_game: bool = False) -> DetectionResult:
    """
    Pełna detekcja ekranu: "Dust Mine" (kulki + DROP) oraz minigry
    "Depth Collapse" (kafelki) i "Fishing Hole" (przycisk CAST).
    Współrzędne we współrzędnych PEŁNEGO screenshotu.
    """
    det = detect(bgr, region)
    work, ox, oy = _crop(bgr, region)
    if work.size == 0:
        return det

    rects, taps, size = _find_debris_tiles(work, screen_width=bgr.shape[1], in_game=in_debris_game)
    det.tiles = [Rect(r.x + ox, r.y + oy, r.w, r.h) for r in rects]
    det.tile_taps = [(x + ox, y + oy) for x, y in taps]
    det.tile_size = size

    cast = _find_cast_button(work)
    if cast is not None:
        det.cast_button = Rect(cast.x + ox, cast.y + oy, cast.w, cast.h)
    return det


def screen_kind(det: DetectionResult) -> str | None:
    """Który ekran gry jest widoczny: "dust" | "debris" | "fishing" | None."""
    if has_game_screen(det):
        return "dust"
    if det.tile_taps:
        return "debris"
    if det.cast_button is not None:
        return "fishing"
    return None


def has_game_screen(detection: DetectionResult, min_balls: int = 1) -> bool:
    """Czy uznajemy, że aktualnie widoczny jest ekran z kulkami do przeciągnięcia."""
    return detection.drop is not None and len(detection.balls) >= min_balls


def is_black_screen(bgr: np.ndarray, brightness_threshold: float = 10.0) -> bool:
    """
    Sprawdza, czy klatka jest (w praktyce) całkowicie czarna - typowy
    objaw zawieszenia gry, crashu do pulpitu Androida z czarnym tłem,
    zgaszonego ekranu emulatora albo animacji przejścia utkniętej na
    czarnej klatce.

    Liczy średnią jasność (skala szarości 0-255) całej klatki i uznaje
    ją za "czarny ekran", jeśli średnia jest niższa niż
    `brightness_threshold`. Próg 10.0 jest celowo nisko - normalny UI
    gry, nawet z ciemnym motywem, ma jakieś jasne elementy (tekst,
    przyciski, kulki) podnoszące średnią znacznie powyżej tej wartości.
    """
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    return float(gray.mean()) < brightness_threshold
