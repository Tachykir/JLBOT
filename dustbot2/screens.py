"""
screens.py
==========
Rozpoznawanie ekranów gry na podstawie szablonów (template matching).

Każdy szablon to mały, charakterystyczny fragment ekranu (napis, logo),
wycięty ze zrzutu referencyjnego o szerokości `reference_width` (540 px).
Klatka z emulatora jest skalowana do tej samej szerokości, więc szablony
działają niezależnie od rozdzielczości instancji.

Dopasowanie używa TM_CCOEFF_NORMED, który jest odporny na liniowe zmiany
jasności i kontrastu - dlatego ekran przyciemniony przez animację albo
popup nadal pasuje do szablonu zrobionego z jasnej wersji.

Szablony i ich parametry są w templates/manifest.json. Nowy ekran dodaje
się narzędziem `python -m dustbot2.tools.make_template` bez zmiany kodu.

Stany:
    splash, loading, dust_mine, item_popup, create_account, home -
        z szablonów (pole "state" w manifeście)
    black   - prawie czarna klatka (zawieszenie, przejście)
    unknown - żaden szablon nie przekroczył progu
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

TEMPLATES_DIR = Path(__file__).parent / "templates"

BLACK = "black"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class Template:
    name: str
    state: str | None  # None = szablon tylko do lokalizowania (np. DROP)
    image: np.ndarray  # skala szarości, w rozdzielczości referencyjnej
    search: tuple[float, float, float, float]  # x1, y1, x2, y2 jako ułamki klatki
    threshold: float
    # Wyższy priorytet wygrywa, gdy pasuje kilka stanów naraz - np. popup
    # nałożony na przyciemniony ekran pod spodem (dopasowanie jest odporne
    # na przyciemnienie, więc ekran bazowy też by pasował).
    priority: int = 0
    # Skala interfejsu materiału, z którego wycięto szablon (patrz
    # ScreenClassifier.ui_scale). Pozwala mieszać szablony z emulatorów
    # o różnym DPI.
    ui_scale: float = 1.0


@dataclass(frozen=True)
class Match:
    name: str
    score: float
    # środek dopasowania w pikselach ORYGINALNEJ klatki
    x: int
    y: int
    w: int
    h: int


@dataclass(frozen=True)
class ScreenState:
    state: str
    score: float
    match: Match | None  # najlepsze dopasowanie (także gdy state == unknown)


class ScreenClassifier:
    """
    `ui_scale` - wielkość interfejsu gry względem szerokości ekranu, w
    porównaniu z pierwszymi zrzutami 540x960 (=1.0). Zależy od DPI
    emulatora: np. LDPlayer 1080x2320 ma ok. 1.28. Jeśli nie jest podana,
    klasyfikator wykrywa ją sam przy pierwszych nierozpoznanych klatkach
    (nie częściej niż co `calibrate_interval` sekund).
    """

    CALIBRATION_RANGE = (0.6, 1.8)

    def __init__(
        self,
        templates: list[Template],
        reference_width: int = 540,
        scales: tuple[float, ...] = (0.9, 1.0, 1.1),
        black_threshold: float = 10.0,
        ui_scale: float | None = None,
        calibrate_interval: float = 30.0,
    ):
        self.templates = {t.name: t for t in templates}
        self.reference_width = reference_width
        self.black_threshold = black_threshold
        self.ui_scale = ui_scale
        self.auto_calibrate = ui_scale is None
        self.calibrate_interval = calibrate_interval
        self._fallback_scales = tuple(s for s in scales if abs(s - 1.0) > 1e-3)
        self._resized: dict[tuple[str, float], np.ndarray] = {}
        self._last_state: str | None = None
        self._last_calibration = float("-inf")

    @classmethod
    def load(cls, directory: Path = TEMPLATES_DIR, **kwargs) -> "ScreenClassifier":
        manifest = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
        templates = []
        for entry in manifest["templates"]:
            image = cv2.imread(str(directory / entry["file"]), cv2.IMREAD_GRAYSCALE)
            if image is None:
                raise FileNotFoundError(f"Brak pliku szablonu: {directory / entry['file']}")
            templates.append(Template(
                name=entry["name"],
                state=entry.get("state"),
                image=image,
                search=tuple(entry["search"]),
                threshold=float(entry["threshold"]),
                priority=int(entry.get("priority", 0)),
                ui_scale=float(entry.get("ui_scale", 1.0)),
            ))
        return cls(templates, reference_width=manifest.get("reference_width", 540), **kwargs)

    # ------------------------------------------------------------------ #
    def normalize(self, frame_bgr: np.ndarray) -> tuple[np.ndarray, float]:
        """Klatka w skali szarości o szerokości referencyjnej + współczynnik skali."""
        gray = frame_bgr if frame_bgr.ndim == 2 else cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY)
        factor = self.reference_width / gray.shape[1]
        if abs(factor - 1.0) > 1e-3:
            gray = cv2.resize(gray, None, fx=factor, fy=factor, interpolation=cv2.INTER_AREA)
        return gray, factor

    def classify(self, frame_bgr: np.ndarray) -> ScreenState:
        """
        Zwraca stan ekranu. Szablony są sprawdzane w kolejności: najwyższy
        priorytet, potem stan z poprzedniej klatki - i kończymy na pierwszym
        trafieniu. W pętli gry większość klatek ma ten sam stan, więc
        zwykle wystarczają 1-2 dopasowania. Sąsiednie skale (+-10%) są
        sprawdzane dopiero wtedy, gdy w podstawowej nic nie pasuje.
        """
        gray, factor = self.normalize(frame_bgr)
        if float(gray.mean()) < self.black_threshold:
            return ScreenState(BLACK, 1.0, None)

        result = self._classify(gray, factor)
        if result.state == UNKNOWN and self._should_calibrate() and self._calibrate(gray, factor):
            result = self._classify(gray, factor)
        return result

    def locate(self, frame_bgr: np.ndarray, name: str) -> Match | None:
        """Szuka jednego szablonu; zwraca dopasowanie powyżej progu albo None."""
        template = self.templates[name]
        gray, factor = self.normalize(frame_bgr)
        for scales in ((1.0,), self._fallback_scales):
            match = self._match(gray, factor, template, scales)
            if match is not None and match.score >= template.threshold:
                return match
        return None

    # ------------------------------------------------------------------ #
    def _classify(self, gray: np.ndarray, factor: float) -> ScreenState:
        order = sorted(
            (t for t in self.templates.values() if t.state is not None),
            key=lambda t: (-t.priority, t.state != self._last_state),
        )
        best: Match | None = None
        for scales in ((1.0,), self._fallback_scales):
            for template in order:
                match = self._match(gray, factor, template, scales)
                if match is None:
                    continue
                if match.score >= template.threshold:
                    self._last_state = template.state
                    return ScreenState(template.state, match.score, match)
                if best is None or match.score > best.score:
                    best = match

        self._last_state = None
        return ScreenState(UNKNOWN, best.score if best else 0.0, best)

    def _should_calibrate(self) -> bool:
        return self.auto_calibrate and time.monotonic() - self._last_calibration >= self.calibrate_interval

    def _calibrate(self, gray: np.ndarray, factor: float) -> bool:
        """
        Szuka skali interfejsu, przy której któryś szablon stanu przekracza
        swój próg. Najpierw co 0.05, potem dokładniej wokół najlepszej.
        Zwraca True, jeśli skala się zmieniła.
        """
        self._last_calibration = time.monotonic()
        templates = [t for t in self.templates.values() if t.state is not None]

        def best_at(ui: float) -> tuple[float, bool]:
            top, passed = 0.0, False
            for t in templates:
                m = self._match(gray, factor, t, (1.0,), ui_scale=ui)
                if m is not None:
                    top = max(top, m.score)
                    passed = passed or m.score >= t.threshold
            return top, passed

        low, high = self.CALIBRATION_RANGE
        coarse = max(np.arange(low, high + 1e-9, 0.05), key=lambda ui: best_at(ui)[0])
        fine = max(np.arange(coarse - 0.04, coarse + 0.041, 0.01), key=lambda ui: best_at(ui)[0])
        score, passed = best_at(fine)
        if not passed:
            return False
        changed = self.ui_scale is None or abs(self.ui_scale - fine) > 1e-3
        self.ui_scale = round(float(fine), 3)
        return changed

    def _template_image(self, template: Template, scale: float) -> np.ndarray:
        key = (template.name, round(scale, 3))
        image = self._resized.get(key)
        if image is None:
            image = self._resized[key] = _resize(template.image, scale)
        return image

    def _match(self, gray: np.ndarray, factor: float, template: Template,
               scales: tuple[float, ...], ui_scale: float | None = None) -> Match | None:
        h, w = gray.shape
        fx1, fy1, fx2, fy2 = template.search
        x1, y1 = int(fx1 * w), int(fy1 * h)
        x2, y2 = int(np.ceil(fx2 * w)), int(np.ceil(fy2 * h))
        region = gray[y1:y2, x1:x2]
        base = (ui_scale if ui_scale is not None else (self.ui_scale or 1.0)) / template.ui_scale

        best: Match | None = None
        for scale in scales:
            image = self._template_image(template, base * scale)
            th, tw = image.shape
            if th > region.shape[0] or tw > region.shape[1] or th < 4 or tw < 4:
                continue
            result = cv2.matchTemplate(region, image, cv2.TM_CCOEFF_NORMED)
            _, score, _, (mx, my) = cv2.minMaxLoc(result)
            if not np.isfinite(score):
                continue
            if best is None or score > best.score:
                cx = (x1 + mx + tw / 2) / factor
                cy = (y1 + my + th / 2) / factor
                best = Match(template.name, float(score), round(cx), round(cy),
                             round(tw / factor), round(th / factor))
        return best


def _resize(image: np.ndarray, scale: float) -> np.ndarray:
    if abs(scale - 1.0) < 1e-3:
        return image
    interpolation = cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR
    return cv2.resize(image, None, fx=scale, fy=scale, interpolation=interpolation)
