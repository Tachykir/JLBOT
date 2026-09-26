"""
digit_templates.py
==================
Wzory cyfr 0-9 czcionki gry (Kode Mono, SIL Open Font License) do
odczytu numeru głębokości w Profile. Wygenerowane z czcionki: dwie
grubości (400 - kafelki głębokości, 700 - duża liczba), każda cyfra
przycięta do obrysu i przeskalowana do 24x36 (0-255).
"""

import base64
import zlib

import numpy as np

SIZE = (24, 36)  # (szerokość, wysokość)
DIGITS = "0123456789" * 2
ASPECTS = [0.627, 0.627, 0.627, 0.627, 0.71, 0.627, 0.627, 0.627, 0.627, 0.627, 0.671, 0.671, 0.676, 0.671, 0.757, 0.671, 0.667, 0.671, 0.671, 0.671]

_DATA = (
    "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"
    "ZM8o5koTYR68kVE8qDJW/wWkZ24A"
)

TEMPLATES = np.frombuffer(zlib.decompress(base64.b64decode(_DATA)), np.uint8).reshape(-1, SIZE[1], SIZE[0]).astype(np.float32) / 255.0
