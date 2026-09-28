"""
Motyw GUI: systemowy / jasny / ciemny. Wybór jest zapamiętywany w
QSettings (ustawienie programu, niezależne od pliku config.json).
"""
from __future__ import annotations

from PySide6.QtCore import QSettings
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

THEMES = {"system": "Systemowy", "light": "Jasny", "dark": "Ciemny"}

_default = None  # (styl, paleta) z uruchomienia - dla "system"


def _settings() -> QSettings:
    return QSettings("DustMineBot", "DustMineBot")


def saved_theme() -> str:
    value = _settings().value("theme", "system")
    return value if value in THEMES else "system"


def _palette(dark: bool) -> QPalette:
    p = QPalette()
    if dark:
        window, base, alt, text, button = "#2b2b2b", "#1f1f1f", "#2a2a2a", "#e6e6e6", "#353535"
        disabled, highlight, link = "#7a7a7a", "#2f6fb0", "#6fb3ff"
    else:
        window, base, alt, text, button = "#f0f0f0", "#ffffff", "#f5f5f5", "#1a1a1a", "#e6e6e6"
        disabled, highlight, link = "#9a9a9a", "#3a7bd5", "#0b5cad"
    for role, color in ((QPalette.Window, window), (QPalette.WindowText, text), (QPalette.Base, base),
                        (QPalette.AlternateBase, alt), (QPalette.ToolTipBase, base), (QPalette.ToolTipText, text),
                        (QPalette.Text, text), (QPalette.Button, button), (QPalette.ButtonText, text),
                        (QPalette.BrightText, "#ff5555"), (QPalette.Highlight, highlight),
                        (QPalette.HighlightedText, "#ffffff"), (QPalette.Link, link),
                        (QPalette.PlaceholderText, disabled)):
        p.setColor(role, QColor(color))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, QColor(disabled))
    return p


def apply_theme(name: str, save: bool = True) -> None:
    global _default
    app = QApplication.instance()
    if app is None:
        return
    if _default is None:
        _default = (app.style().name(), app.palette())
    if name not in THEMES:
        name = "system"
    if name == "system":
        app.setStyle(_default[0])
        app.setPalette(_default[1])
    else:
        # Fusion - wygląda tak samo na każdym systemie i w pełni słucha palety
        app.setStyle("Fusion")
        app.setPalette(_palette(name == "dark"))
    # widżety z własnym arkuszem stylów trzymają paletę z chwili utworzenia
    # - ponowne ustawienie arkusza przelicza je z nową paletą
    for w in app.allWidgets():
        sheet = w.styleSheet()
        if sheet:
            w.setStyleSheet("")
            w.setStyleSheet(sheet)
    if save:
        _settings().setValue("theme", name)
