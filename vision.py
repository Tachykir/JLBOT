"""
vision.py
=========
Wykrywanie elementów gry "Dust Mine Explosion" na screenshocie:
  1. Punkt DROP - przerywany biały okrąg z napisem "DROP" na czarnym tle.
  2. Kolorowe kulki (fragmenty pyłu) - jednolicie wypełnione koła.

Podejście:
- DROP: maska "prawdziwej bieli" (jasne, bez nasycenia - kulki są
  kolorowe), a do kresek okręgu dopasowujemy okrąg metodą najmniejszych
  kwadratów. Wystarczy fragment okręgu, więc działa też, gdy kulki
  częściowo zasłaniają DROP.
- Kulki: wszystko, co odróżnia się od tła planszy, i kolejne
  najgłębsze punkty tej maski (transformata odległości) - działa dla
  kulek nachodzących na siebie i z napisem w środku.

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


def _white_mask(hsv: np.ndarray) -> np.ndarray:
    """Prawdziwa biel (kreski i napis DROP): jasne i prawie bez nasycenia -
    odróżnia je od jasnych, ale kolorowych kulek."""
    return ((hsv[:, :, 2] > 200) & (hsv[:, :, 1] < 40)).astype(np.uint8) * 255


def _fit_circle(xs: np.ndarray, ys: np.ndarray) -> tuple[float, float, float] | None:
    """Dopasowanie okręgu metodą najmniejszych kwadratów (Kåsa)."""
    if xs.size < 10:
        return None
    A = np.column_stack([xs, ys, np.ones_like(xs)]).astype(np.float64)
    rhs = (xs.astype(np.float64) ** 2 + ys.astype(np.float64) ** 2)
    try:
        (a, b, c), *_ = np.linalg.lstsq(A, rhs, rcond=None)
    except np.linalg.LinAlgError:
        return None
    cx, cy = a / 2, b / 2
    r2 = c + cx * cx + cy * cy
    if r2 <= 0:
        return None
    return cx, cy, float(np.sqrt(r2))


def _dash_pattern(angles: np.ndarray, sectors: int = 64) -> tuple[int, int]:
    """
    Regularne kreski na okręgu: kreska zajmuje 1-7 sektorów (z 64), a
    przerwa za nią 1-5 (luz na zaokrąglenia przy małej rozdzielczości). Zwraca (liczba takich kresek, najdłuższa seria
    pod rząd). Przerywany okrąg DROP daje ~13 kresek, ciągła obwódka
    kulki - 0, przypadkowe przecięcia tekstu - nieregularne odcinki.
    """
    occ = np.zeros(sectors, bool)
    occ[((angles + np.pi) / (2 * np.pi) * sectors).astype(int) % sectors] = True
    if occ.all() or not occ.any():
        return 0, 0
    occ = np.roll(occ, -int(np.argmin(occ)))  # zacznij od przerwy
    segs = []  # [czy_kreska, długość]
    for v in occ:
        if segs and segs[-1][0] == v:
            segs[-1][1] += 1
        else:
            segs.append([v, 1])
    total = best = run = 0
    for i, (is_dash, length) in enumerate(segs):
        if not is_dash:
            continue
        gap_after = segs[i + 1][1] if i + 1 < len(segs) else segs[0][1]
        if 1 <= length <= 7 and 1 <= gap_after <= 5:
            total += 1
            run += 1
            best = max(best, run)
        else:
            run = 0
    return total, best


def _find_drop_point(hsv: np.ndarray, known: Circle | None = None, thick_only: bool = False) -> Circle | None:
    """
    Szuka punktu DROP: przerywanego BIAŁEGO okręgu z napisem "DROP" w środku.

      1. Maska "prawdziwej bieli" (jasne, niskie nasycenie), pomniejszona
         do ~450 px szerokości.
      2. Kandydaci na okręgi z transformaty Hougha na tej masce - radzi
         sobie z przerywaną linią i z fragmentem okręgu (DROP częściowo
         zasłonięty przez kulki).
      3. Weryfikacja kandydata - jeden z dwóch warunków:
         a) biały napis w środku (odróżnia DROP od jasnej obwódki kulki),
            pusty pierścień między napisem a kreskami (odróżnia go od
            bloków tekstu i białych okien), kreski na >= połowie obwodu;
         b) albo - gdy kulka leży na DROP-ie i zasłania napis - co
            najmniej 5 regularnych kresek, w tym 3 pod rząd (patrz
            _dash_pattern) - działa też, gdy kulki zasłaniają DROP z
            kilku stron.
      4. Dopasowanie końcowe metodą najmniejszych kwadratów do kresek.

    known: ostatnia znana pozycja DROP (we współrzędnych `hsv`) - dodawana
    jako kandydat, bo mocno zasłonięty okrąg bywa pomijany przez Hougha.
    Przechodzi tę samą weryfikację, nie jest używana "na ślepo".
    """
    h, w = hsv.shape[:2]
    mask = _white_mask(hsv)
    if thick_only:
        # bez cienkich (1 px) jasnych obwódek kulek - zostają kreski DROP;
        # kulka leżąca na DROP-ie ma obwódkę prawie w tym samym miejscu
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    scale = min(1.0, 450.0 / w)
    if scale < 1.0:
        mask = cv2.resize(mask, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA)
    h, w = mask.shape[:2]
    ys, xs = np.nonzero(mask > 60)
    if xs.size < 30:
        return None
    xs = xs.astype(np.float64)
    ys = ys.astype(np.float64)
    r_min, r_max = 0.04 * w, 0.25 * w

    candidates = cv2.HoughCircles(
        cv2.GaussianBlur(mask, (5, 5), 0), cv2.HOUGH_GRADIENT, dp=1.5, minDist=r_min,
        param1=100, param2=12, minRadius=int(r_min), maxRadius=int(r_max),
    )
    candidates = [] if candidates is None else [tuple(c) for c in candidates[0][:40]]
    known_c = None
    if known is not None:
        known_c = (known.x * scale, known.y * scale, known.r * scale)
        candidates.insert(0, known_c)
    if not candidates:
        return None

    best = None
    best_count = 0
    for cx, cy, r in candidates:
        d = np.hypot(xs - cx, ys - cy)
        # Hough potrafi trafić środek o kilka pikseli obok - dopasuj okrąg do
        # białych punktów przy pierścieniu, zanim ocenisz, czy to DROP
        if (cx, cy, r) != known_c:
            for _ in range(2):
                near = np.abs(d - r) < 0.15 * r
                if int(near.sum()) < 20:
                    break
                fit = _fit_circle(xs[near], ys[near])
                if fit is None or np.hypot(fit[0] - cx, fit[1] - cy) >= 0.3 * r or not 0.8 * r < fit[2] < 1.2 * r:
                    break
                cx, cy, r = fit
                d = np.hypot(xs - cx, ys - cy)
        on_ring = np.abs(d - r) < max(3.0, 0.08 * r)
        count = int(on_ring.sum())
        if count <= best_count or count < 20:
            continue
        # kreski DROP to cienki, równy pierścień (rozrzut ~0.02 r), a wokół
        # nich jest pusto; tekst daje grube "pierścienie" (~0.08 r) i dużo
        # bieli dookoła (np. nazwa i statystyki przedmiotu)
        if float(np.std(d[on_ring] - r)) > 0.05 * r:
            continue
        if int(((d > 1.2 * r) & (d < 1.6 * r)).sum()) > 0.5 * count:
            continue
        ang = np.arctan2(ys[on_ring] - cy, xs[on_ring] - cx)
        coverage = len(np.unique(((ang + np.pi) / (2 * np.pi) * 32).astype(int) % 32)) / 32
        has_text = int((d < 0.55 * r).sum()) >= 10
        empty_between = int(((d > 0.62 * r) & (d < 0.86 * r)).sum()) <= 0.15 * count
        # kreski DROP leżą na okręgu, a nie obok (blok tekstu dopasowany
        # okręgiem ma w pasie wokół niego dużo bieli poza linią)
        ring_band = int((np.abs(d - r) < 0.2 * r).sum())
        with_label = has_text and empty_between and coverage >= 0.5 and count >= 0.75 * ring_band
        if not with_label:
            dashes, in_row = _dash_pattern(ang)
            # "czystość" pierścienia: białe piksele w pasie wokół okręgu
            # leżą na nim (kreski DROP: ~0.95), a nie obok (tekst: ~0.5)
            band = int((np.abs(d - r) < 0.2 * r).sum())
            # w znanym miejscu DROP (z poprzednich plansz) wystarczą 3 kreski -
            # np. gdy stos kulek zasłania prawie cały okrąg
            need, need_row = (3, 2) if (cx, cy, r) == known_c else (5, 3)
            if thick_only and (cx, cy, r) != known_c:
                # same grube kreski (bez obwódek kulek): 4 regularne kreski
                # na prawie idealnie czystym pierścieniu
                need, need_row = 4, 3
                if count < 0.9 * band or float(np.std(d[on_ring] - r)) > 0.04 * r:
                    continue
            if dashes < need or in_row < need_row or count < 0.8 * band:
                continue
        best, best_count = (xs[on_ring], ys[on_ring], (float(cx), float(cy), float(r))), count

    if best is None:
        return None
    rx, ry, fallback = best
    refined = _fit_circle(rx, ry)
    cx, cy, r = refined if refined is not None else fallback
    return Circle(int(round(cx / scale)), int(round(cy / scale)), int(round(r / scale)))


def _find_balls(bgr: np.ndarray, hsv: np.ndarray, drop: Circle | None) -> list[Circle]:
    """
    Kulki: wszystko, co odróżnia się od tła planszy (i nie jest białym
    DROP-em), a potem kolejne "najgłębsze" punkty maski (transformata
    odległości) - środek kulki leżącej na wierzchu to punkt najdalej od
    krawędzi. Po znalezieniu kulki wycinamy jej koło i szukamy dalej.

    Dzięki temu nachodzące na siebie kulki (stos) dają osobne punkty
    chwytu, zawsze leżące na kulce. Kulka prawie całkiem schowana pod
    inną może nie zostać znaleziona - pojawi się po zebraniu tej z góry.
    """
    h0, w0 = bgr.shape[:2]
    # liczymy na pomniejszonym obrazie (~400 px szerokości) - kilka razy
    # szybciej, a precyzja w zupełności wystarcza do chwycenia kulki
    scale = min(1.0, 400.0 / w0)
    if scale < 1.0:
        bgr = cv2.resize(bgr, (int(w0 * scale), int(h0 * scale)), interpolation=cv2.INTER_AREA)
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        if drop is not None:
            drop = Circle(int(drop.x * scale), int(drop.y * scale), int(drop.r * scale))
    h, w = bgr.shape[:2]
    # kolor tła = mediana (tło zajmuje większość planszy)
    bg = np.median(bgr.reshape(-1, 3), axis=0)
    diff = np.linalg.norm(bgr.astype(np.int16) - bg.astype(np.int16), axis=2)
    fg = (diff > 60).astype(np.uint8) * 255
    fg[_white_mask(hsv) > 0] = 0
    # (wnętrza DROP nie wycinamy - kulka może leżeć na DROP-ie; biały
    # napis i kreski odpadają przez maskę bieli, a ich cienkie otoczki
    # przez otwarcie morfologiczne i minimalną "grubość" kulki niżej)
    # wypełnij dziury po napisach "+208" w kulkach
    contours, _ = cv2.findContours(fg, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    filled = np.zeros_like(fg)
    cv2.drawContours(filled, contours, -1, 255, thickness=-1)
    filled = cv2.morphologyEx(filled, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))

    dist = cv2.distanceTransform(filled, cv2.DIST_L2, 5)
    radius = float(dist.max())
    min_r = max(8.0, 0.025 * w)
    if radius < min_r or radius > 0.2 * w:
        return []

    balls: list[Circle] = []
    work = filled
    for _ in range(40):
        _, peak, _, (px, py) = cv2.minMaxLoc(dist)
        # fragment kulki musi mieć sensowną "grubość", żeby dało się go chwycić
        if peak < max(min_r, 0.4 * radius):
            break
        balls.append(Circle(int(round(px / scale)), int(round(py / scale)), int(round(peak / scale))))
        cv2.circle(work, (int(px), int(py)), int(round(radius * 1.05)), 0, thickness=-1)
        dist = cv2.distanceTransform(work, cv2.DIST_L2, 5)
    return balls


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


def detect(bgr: np.ndarray, region: tuple[int, int, int, int] | None = None,
           drop_hint: Circle | None = None, use_hint_as_fallback: bool = True) -> DetectionResult:
    """
    Główna funkcja detekcji.

    bgr: pełny screenshot (numpy array BGR).
    region: opcjonalny obszar (x1, y1, x2, y2), w którym szukamy elementów
            gry - warto ograniczyć do obszaru "planszy" (czarny prostokąt
            widoczny na screenach), żeby uniknąć fałszywych trafień w UI.

    drop_hint: ostatnia znana pozycja DROP (współrzędne pełnego ekranu) -
            sprawdzana jako dodatkowy kandydat (mocno zasłonięty DROP);
            przy use_hint_as_fallback=True używana też wprost, gdy DROP
            jest całkiem zasłonięty (w trakcie zbierania wiadomo, że to
            ekran Dust Mine).

    Zwraca DetectionResult z pozycją DROP (lub None jeśli nie znaleziono)
    i listą wykrytych kulek (we współrzędnych PEŁNEGO screenshotu, nie
    regionu - czyli gotowych do bezpośredniego użycia w tap/swipe).
    """
    panel = None
    if region is None:
        # bez ręcznie ustawionego obszaru: sam panel planszy, jeśli go widać
        # (DROP bywa w dowolnym miejscu planszy, także tuż pod nagłówkiem)
        region = panel = board_panel(bgr)
    work, offset_x, offset_y = _crop(bgr, region)
    if work.size == 0:
        return DetectionResult(drop=None, balls=[])

    hsv = cv2.cvtColor(work, cv2.COLOR_BGR2HSV)
    known = None
    if drop_hint is not None:
        known = Circle(drop_hint.x - offset_x, drop_hint.y - offset_y, drop_hint.r)
    drop = _find_drop_point(hsv, known)
    if drop is None and panel is not None:
        # tylko na planszy Dust Mine: DROP przykryty kulką z jasną obwódką
        drop = _find_drop_point(hsv, known, thick_only=True)
    if drop is None and drop_hint is not None and use_hint_as_fallback:
        drop = Circle(drop_hint.x - offset_x, drop_hint.y - offset_y, drop_hint.r)
    # bez DROP to nie jest ekran Dust Mine - kulki nie są potrzebne
    balls = _find_balls(work, hsv, drop) if drop is not None else []

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
_CAST_HSV_LO = (80, 50, 150)  # S od 50: krawędź ramki bywa wygładzona prawie do bieli
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
        # CAST: wyśrodkowany w poziomie, proporcje ~3.4 (np. zakładki
        # "RING (LEFT)/(RIGHT)" na ekranie dropu są z boku i węższe)
        if not (2.2 <= bw / bh <= 5.0) or abs(x + bw / 2 - w / 2) > 0.08 * w:
            continue
        if area / (bw * bh) > 0.35:  # ramka, a nie pełny prostokąt
            continue
        rect = Rect(int(x), int(y), int(bw), int(bh))
        if not _cast_context(hsv, mask, rect):
            continue
        if best is None or bw * bh > best.w * best.h:
            best = rect
    return best


def _cast_context(hsv: np.ndarray, mask: np.ndarray, r: Rect) -> bool:
    """
    Dodatkowe warunki, żeby ekran walki (obramowane przyciski, paski)
    nie udawał Fishing Hole:
      - ramka zamknięta: wszystkie 4 boki obrysowane,
      - w środku krótki napis (3-6 liter "CAST") na środku ramki,
      - nad przyciskiem tor ze wskaźnikiem: pionowa, wypełniona
        "pigułka" (cyjan albo biel) o proporcjach ~1:3.
    """
    h, w = mask.shape[:2]
    # po kompresji ramka potrafi się rozpaść na dwie części - oceniaj
    # całą maskę w lekko powiększonym prostokącie, a nie jedną składową
    pad = max(2, r.h // 6)
    x1, y1 = max(0, r.x - pad), max(0, r.y - pad)
    x2, y2 = min(w, r.x + r.w + pad), min(h, r.y + r.h + pad)
    frame = mask[y1:y2, x1:x2] > 0
    rows, cols = np.nonzero(frame)
    if rows.size == 0:
        return False
    frame = frame[rows.min():rows.max() + 1, cols.min():cols.max() + 1]
    r = Rect(x1 + int(cols.min()), y1 + int(rows.min()), frame.shape[1], frame.shape[0])
    t = max(2, r.h // 12)
    sides = (frame[:t].any(axis=0).mean(), frame[-t:].any(axis=0).mean(),
             frame[:, :t].any(axis=1).mean(), frame[:, -t:].any(axis=1).mean())
    if min(sides) < 0.6:
        return False
    m = max(t * 2, r.h // 5)
    inner = frame[m:r.h - m, m:r.w - m].astype(np.uint8)
    n, _l, st, _c = cv2.connectedComponentsWithStats(inner)
    letters = [st[i] for i in range(1, n) if st[i][4] >= 4 and st[i][3] >= 0.2 * inner.shape[0]]
    if not (3 <= len(letters) <= 6):
        return False
    lx1 = min(l[0] for l in letters)
    lx2 = max(l[0] + l[2] for l in letters)
    if abs((lx1 + lx2) / 2 - inner.shape[1] / 2) > 0.15 * inner.shape[1] or lx2 - lx1 > 0.7 * inner.shape[1]:
        return False
    # wskaźnik w torze nad przyciskiem
    y1 = max(0, int(r.y - 0.3 * w))
    band = hsv[y1:r.y]
    pill = mask[y1:r.y] | cv2.inRange(band, (0, 0, 200), (180, 60, 255))
    n, _l, st, _c = cv2.connectedComponentsWithStats(pill)
    for i in range(1, n):
        _x, _y, pw, ph, area = st[i]
        if 0.04 * w <= ph <= 0.15 * w and 2.0 <= ph / max(pw, 1) <= 5.0 and area >= 0.5 * pw * ph:
            return True
    return False


def board_panel(bgr: np.ndarray) -> tuple[int, int, int, int] | None:
    """
    Panel planszy Dust Mine (duży, zaokrąglony prostokąt prawie na całą
    szerokość, odrobinę jaśniejszy od tła) jako (x1, y1, x2, y2), albo None.
    Nad nim jest biały nagłówek ("DUST MINE EXPLOSION", "+0 dust · 4 left"),
    który przy szukaniu DROP na całym ekranie zabiera miejsce kandydatom.
    """
    H, W = bgr.shape[:2]
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    bg = float(np.median(gray[:int(0.1 * H)]))
    mask = (cv2.medianBlur(gray, 5) > bg + 3).astype(np.uint8)
    n, _l, stats, _c = cv2.connectedComponentsWithStats(mask)
    for i in range(1, n):
        x, y, w, h, _a = (int(v) for v in stats[i])
        if w >= 0.85 * W and h >= 0.55 * H and 0.1 * H < y < 0.35 * H:
            return x, y, x + w, y + h
    return None


def board_drop_hint(bgr: np.ndarray) -> Circle | None:
    """
    Przewidywane położenie DROP na planszy Dust Mine: plansza to duży,
    zaokrąglony panel prawie na całą szerokość; DROP leży na środku, na
    ~79% jej wysokości (zmierzone na iOS i Androidzie).
    """
    H, W = bgr.shape[:2]
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    bg = float(np.median(gray[:int(0.1 * H)]))
    mask = (cv2.medianBlur(gray, 5) > bg + 3).astype(np.uint8)
    n, _l, stats, _c = cv2.connectedComponentsWithStats(mask)
    for i in range(1, n):
        x, y, w, h, _a = (int(v) for v in stats[i])
        if w >= 0.85 * W and h >= 0.55 * H and 0.1 * H < y < 0.35 * H:
            return Circle(x + w // 2, int(y + 0.792 * h), int(0.061 * W))  # od X 2026 mniejszy okrąg (było 0.087 W)
    return None


def detect_all(bgr: np.ndarray, region: tuple[int, int, int, int] | None = None,
               in_debris_game: bool = False, drop_hint: Circle | None = None,
               require_panel: bool = False) -> DetectionResult:
    """
    Pełna detekcja ekranu: "Dust Mine" (kulki + DROP) oraz minigry
    "Depth Collapse" (kafelki) i "Fishing Hole" (przycisk CAST).
    Współrzędne we współrzędnych PEŁNEGO screenshotu.

    require_panel: bez ręcznego `region` szukaj kulek i DROP tylko, gdy
    widać panel planszy (board_panel) - na innych ekranach pomija
    najdroższą część (Hough), bo planszy Dust Mine tam nie ma.
    """
    if require_panel and region is None and board_panel(bgr) is None:
        det = DetectionResult(drop=None, balls=[])
    else:
        # w idle podpowiedź DROP jest tylko dodatkowym kandydatem - ekran musi
        # sam potwierdzić, że to Dust Mine
        det = detect(bgr, region, drop_hint=drop_hint, use_hint_as_fallback=False)
    if det.drop is None and region is None:  # (bez panelu board_drop_hint i tak zwraca None)
        # DROP mocno zasłonięty kulkami (np. zaraz po starcie bota, bez
        # znanej pozycji): spróbuj w miejscu, gdzie leży na planszy
        board_hint = board_drop_hint(bgr)
        if board_hint is not None:
            # obecny (mniejszy) i dawny rozmiar okręgu DROP
            for hint in (board_hint, Circle(board_hint.x, board_hint.y, int(0.087 * bgr.shape[1]))):
                if hint == drop_hint:
                    continue
                det2 = detect(bgr, None, drop_hint=hint, use_hint_as_fallback=False)
                if det2.drop is not None:
                    det = det2
                    break
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


def is_empty_screen(bgr: np.ndarray) -> bool:
    """
    Ciemny ekran gry bez treści - np. zawieszona gra, na której został
    tylko dymek "+26 RAID XP". Poza paskiem statusu (górne 4%) i skrajnymi
    paskami po bokach prawie żaden piksel nie odstaje od tła, a jeśli
    odstaje, to wszystko mieści się w jednym małym prostokącie (dymek).
    Uwaga: zakryta karta "?" też jest prawie pusta - wywołujący musi ją
    wykluczyć.
    """
    H, W = bgr.shape[:2]
    gray = cv2.cvtColor(bgr[int(0.04 * H):, int(0.03 * W):int(0.97 * W)], cv2.COLOR_BGR2GRAY)
    small = cv2.resize(gray, (gray.shape[1] // 2 or 1, gray.shape[0] // 2 or 1), interpolation=cv2.INTER_AREA)
    bg = float(np.median(small))
    if bg > 50:
        return False
    diff = np.abs(small.astype(np.int16) - bg) > 25
    if float(diff.mean()) < 0.004:
        return True
    if float(diff.mean()) > 0.03:
        return False
    # wszystko, co odstaje, w jednym małym obszarze (sam dymek z nagrodą)
    ys, xs = np.nonzero(diff)
    sh, sw = small.shape
    return (xs.max() - xs.min() <= 0.5 * sw) and (ys.max() - ys.min() <= 0.12 * sh)


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


_DIGIT_LOOKALIKES = str.maketrans({"Z": "2", "O": "0", "D": "0", "I": "1", "L": "1", "S": "5", "B": "8", "G": "6"})


def balls_left(bgr: np.ndarray) -> int | None:
    """
    Ile kulek zostało wg nagłówka planszy ("+0 dust · 4 left · +87 each"):
    wiersz tekstu tuż nad panelem planszy, druga liczba w wierszu. None =
    nie udało się odczytać.
    """
    import screens
    hint = board_drop_hint(bgr)
    if hint is None:
        return None
    H, W = bgr.shape[:2]
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    bg = float(np.median(gray[:int(0.1 * H)]))
    mask = (cv2.medianBlur(gray, 5) > bg + 3).astype(np.uint8)
    n, _l, stats, _c = cv2.connectedComponentsWithStats(mask)
    panel_y = None
    for i in range(1, n):
        x, y, w, h, _a = (int(v) for v in stats[i])
        if w >= 0.85 * W and h >= 0.55 * H and 0.1 * H < y < 0.35 * H:
            panel_y = y
    if panel_y is None:
        return None
    band = gray[max(0, panel_y - int(0.09 * H)):panel_y - 2]
    text = band > 150
    rows = np.where(text.sum(axis=1) >= 2)[0]
    if rows.size == 0:
        return None
    # ostatni wiersz tekstu nad panelem
    end = rows[-1]
    start = end
    for r in rows[::-1][1:]:
        if start - r > 2:
            break
        start = r
    line = text[start:end + 1]
    comps = sorted((c for c in screens._components(line.astype(np.uint8)) if c[4] >= 3), key=lambda c: c[0])
    if not comps:
        return None
    ch = max(c[3] for c in comps)
    # słowa: przerwa większa niż ~0.6 wysokości znaku
    words, cur = [], [comps[0]]
    for c in comps[1:]:
        prev = cur[-1]
        if c[0] - (prev[0] + prev[2]) > 0.6 * ch:
            words.append(cur)
            cur = [c]
        else:
            cur.append(c)
    words.append(cur)
    numbers = []
    for wd in words:
        x1 = min(c[0] for c in wd)
        x2 = max(c[0] + c[2] for c in wd)
        token = screens.read_word(line[:, x1:x2])
        if any(ch_.isdigit() for ch_ in token):
            # cyfry mylone z literami przy odczycie słowa ("+124" -> "?1Z4")
            token = token.translate(_DIGIT_LOOKALIKES)
        digits = "".join(ch_ for ch_ in token if ch_.isdigit())
        # słowo jest liczbą, jeśli poza ewentualnym "+" składa się z cyfr
        if digits and len(digits) >= len(token) - 1:
            numbers.append(int(digits))
    return numbers[1] if len(numbers) >= 3 else None
