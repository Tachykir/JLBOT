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
    def __init__(
        self,
        templates: list[Template],
        reference_width: int = 540,
        scales: tuple[float, ...] = (0.9, 1.0, 1.1),
        black_threshold: float = 10.0,
    ):
        self.templates = {t.name: t for t in templates}
        self.reference_width = reference_width
        self.scales = scales
        self.black_threshold = black_threshold
        # szablony przeskalowane raz, przy starcie
        self._scaled = {
            t.name: {s: _resize(t.image, s) for s in (1.0, *scales)} for t in templates
        }
        self._fallback_scales = tuple(s for s in scales if abs(s - 1.0) > 1e-3)
        self._last_state: str | None = None

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
        zwykle wystarczają 1-2 dopasowania. Inne skale szablonów są
        sprawdzane dopiero wtedy, gdy w skali 1.0 nic nie pasuje.
        """
        gray, factor = self.normalize(frame_bgr)
        if float(gray.mean()) < self.black_threshold:
            return ScreenState(BLACK, 1.0, None)

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
    def _match(self, gray: np.ndarray, factor: float, template: Template,
               scales: tuple[float, ...]) -> Match | None:
        h, w = gray.shape
        fx1, fy1, fx2, fy2 = template.search
        x1, y1 = int(fx1 * w), int(fy1 * h)
        x2, y2 = int(np.ceil(fx2 * w)), int(np.ceil(fy2 * h))
        region = gray[y1:y2, x1:x2]

        best: Match | None = None
        for scale in scales:
            image = self._scaled[template.name][scale]
            th, tw = image.shape
            if th > region.shape[0] or tw > region.shape[1]:
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
