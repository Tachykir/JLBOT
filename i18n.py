"""
i18n.py
=======
Tłumaczenie tekstów interfejsu (polski -> angielski).

Teksty w kodzie GUI są po polsku. Tabela `translations_en.json` mapuje
polski tekst na angielski. Po zbudowaniu okna `translate_tree()` przechodzi
po wszystkich widgetach i podmienia etykiety, przyciski, podpowiedzi,
nagłówki kart, pola tekstowe, sufiksy i pozycje list rozwijanych. Tekst,
którego nie ma w tabeli, zostaje po polsku - brakujące tłumaczenie nigdy
nie psuje interfejsu.

Język zapisujemy w QSettings; zmiana działa po ponownym uruchomieniu.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import QSettings
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (QAbstractButton, QComboBox, QDoubleSpinBox, QGroupBox, QLabel,
                               QLineEdit, QSpinBox, QTabWidget, QWidget)

LANGUAGES = {"pl": "Polski", "en": "English"}
DEFAULT_LANGUAGE = "pl"
_TABLE_PATH = Path(__file__).with_name("translations_en.json")
_table: dict[str, str] | None = None


def _load_table() -> dict[str, str]:
    global _table
    if _table is None:
        try:
            with open(_TABLE_PATH, encoding="utf-8") as f:
                _table = json.load(f)
        except (OSError, ValueError):
            _table = {}
    return _table


def current_language() -> str:
    try:
        lang = QSettings("DustMineBot", "DustMineBot").value("ui_language", DEFAULT_LANGUAGE)
    except Exception:
        return DEFAULT_LANGUAGE
    return lang if lang in LANGUAGES else DEFAULT_LANGUAGE


def set_language(lang: str) -> None:
    QSettings("DustMineBot", "DustMineBot").setValue("ui_language", lang)


def tr(text: str) -> str:
    """Tekst w aktualnym języku (dla tekstów ustawianych już w kodzie)."""
    if current_language() != "en":
        return text
    return _load_table().get(text, text)


def map_tree(root: QWidget, fn: Callable[[str], str]) -> None:
    """Przepuszcza przez `fn` każdy tekst widgetów pod `root` (włącznie z nim)."""
    widgets = [root, *root.findChildren(QWidget)]
    for w in widgets:
        if isinstance(w, (QLabel, QAbstractButton)):
            if w.text():
                w.setText(fn(w.text()))
        if isinstance(w, QGroupBox) and w.title():
            w.setTitle(fn(w.title()))
        if isinstance(w, QLineEdit) and w.placeholderText():
            w.setPlaceholderText(fn(w.placeholderText()))
        if isinstance(w, (QSpinBox, QDoubleSpinBox)):
            w.setPrefix(fn(w.prefix()))
            w.setSuffix(fn(w.suffix()))
            if w.specialValueText():
                w.setSpecialValueText(fn(w.specialValueText()))
        if isinstance(w, QComboBox):
            for i in range(w.count()):
                w.setItemText(i, fn(w.itemText(i)))
        if isinstance(w, QTabWidget):
            for i in range(w.count()):
                w.setTabText(i, fn(w.tabText(i)))
                if w.tabToolTip(i):
                    w.setTabToolTip(i, fn(w.tabToolTip(i)))
        if w.toolTip():
            w.setToolTip(fn(w.toolTip()))
        if w.isWindow() and w.windowTitle():
            w.setWindowTitle(fn(w.windowTitle()))
    for action in root.findChildren(QAction):
        if action.text():
            action.setText(fn(action.text()))
        if action.toolTip():
            action.setToolTip(fn(action.toolTip()))
        if action.statusTip():
            action.setStatusTip(fn(action.statusTip()))


def translate_tree(root: QWidget) -> None:
    """Tłumaczy drzewo widgetów, jeśli wybrano angielski."""
    if current_language() == "en":
        map_tree(root, lambda s: _load_table().get(s, s))
