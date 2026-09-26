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

from dataclasses import dataclass
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
class DetectionResult:
    drop: Circle | None
    balls: list[Circle]


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
        for r in range(r_lo, r_hi, 2):
            ring_mask = np.zeros_like(gray, dtype=np.uint8)
            cv2.circle(ring_mask, (int(cx), int(cy)), r, 255, thickness=4)
            ys_r, xs_r = np.where(ring_mask == 255)
            if ys_r.size == 0:
                continue
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

        inner_mask = np.zeros_like(gray, dtype=np.uint8)
        cv2.circle(inner_mask, (x, y), max(1, int(r * 0.7)), 255, thickness=-1)

        inner_gray_vals = gray[inner_mask == 255]
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
    offset_x, offset_y = 0, 0
    work = bgr
    if region is not None:
        x1, y1, x2, y2 = region
        work = bgr[y1:y2, x1:x2]
        offset_x, offset_y = x1, y1

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
