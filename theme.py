"""
Wygląd GUI: motyw systemowy / jasny / ciemny i wspólny arkusz stylów.

Wszystkie kolory pochodzą z jednego zestawu tokenów (TOKENS) - paleta Qt i
arkusz QSS są z niego budowane, więc oba motywy wyglądają spójnie. Wybór
motywu jest zapamiętywany w QSettings (ustawienie programu, niezależne od
pliku config.json). "Systemowy" bierze jasny albo ciemny z ustawień systemu.
"""
from __future__ import annotations

from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

THEMES = {"system": "Systemowy", "light": "Jasny", "dark": "Ciemny"}

# Tokeny kolorów. Akcent: turkus z interfejsu gry (przyciski, DROP).
TOKENS = {
    "dark": {
        "bg": "#12161b", "panel": "#181d23", "panel2": "#1e252c", "base": "#0e1216",
        "line": "#2a333d", "text": "#e3e8ed", "muted": "#8b97a3", "disabled": "#5d6873",
        "accent": "#2cc4b4", "accent_text": "#06211e", "accent_soft": "#163a37",
        "danger": "#e5695c", "warn": "#e2b04a", "ok": "#4cc38a",
    },
    "light": {
        "bg": "#eef1f4", "panel": "#ffffff", "panel2": "#f5f7f9", "base": "#ffffff",
        "line": "#d4dbe2", "text": "#18202a", "muted": "#5d6977", "disabled": "#a3adb7",
        "accent": "#0d8f84", "accent_text": "#ffffff", "accent_soft": "#d6f0ed",
        "danger": "#c2412d", "warn": "#a86d00", "ok": "#1e8a57",
    },
}

_current = "dark"


def _settings() -> QSettings:
    return QSettings("DustMineBot", "DustMineBot")


def saved_theme() -> str:
    value = _settings().value("theme", "system")
    return value if value in THEMES else "system"


def current_tokens() -> dict:
    """Tokeny aktywnego motywu (np. do kolorów rysowanych w kodzie)."""
    return TOKENS[_current]


def _system_is_dark(app: QApplication) -> bool:
    try:
        return app.styleHints().colorScheme() == Qt.ColorScheme.Dark
    except AttributeError:  # Qt < 6.5
        return app.palette().color(QPalette.Window).lightness() < 128


def _palette(t: dict) -> QPalette:
    p = QPalette()
    for role, key in ((QPalette.Window, "bg"), (QPalette.WindowText, "text"), (QPalette.Base, "base"),
                      (QPalette.AlternateBase, "panel2"), (QPalette.ToolTipBase, "panel"),
                      (QPalette.ToolTipText, "text"), (QPalette.Text, "text"), (QPalette.Button, "panel2"),
                      (QPalette.ButtonText, "text"), (QPalette.Highlight, "accent"),
                      (QPalette.HighlightedText, "accent_text"), (QPalette.Link, "accent"),
                      (QPalette.PlaceholderText, "disabled"), (QPalette.Mid, "line"), (QPalette.Dark, "line")):
        p.setColor(role, QColor(t[key]))
    p.setColor(QPalette.BrightText, QColor(t["danger"]))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        p.setColor(QPalette.Disabled, role, QColor(t["disabled"]))
    return p


def stylesheet(t: dict) -> str:
    """Arkusz stylów całej aplikacji z tokenów motywu."""
    return f"""
QWidget {{ font-size: 13px; }}
QMainWindow, QDialog {{ background: {t['bg']}; }}
QToolTip {{ background: {t['panel']}; color: {t['text']}; border: 1px solid {t['line']}; padding: 4px 6px; }}

/* nawigacja po lewej */
QFrame#navRail {{ background: {t['panel']}; border-right: 1px solid {t['line']}; }}
QLabel#appName {{ font-size: 16px; font-weight: 700; color: {t['text']}; }}
QLabel#appVersion {{ color: {t['muted']}; font-size: 11px; }}
QListWidget#nav {{ background: transparent; border: none; outline: 0; }}
QListWidget#nav::item {{ padding: 9px 12px; margin: 1px 8px; border-radius: 8px; color: {t['muted']}; }}
QListWidget#nav::item:hover {{ background: {t['panel2']}; color: {t['text']}; }}
QListWidget#nav::item:selected {{ background: {t['accent_soft']}; color: {t['text']}; font-weight: 600; }}

/* pasek narzędzi */
QToolBar {{ background: {t['panel']}; border: none; border-bottom: 1px solid {t['line']}; spacing: 4px; padding: 4px 8px; }}
QToolBar QToolButton {{ padding: 5px 10px; border-radius: 6px; color: {t['text']}; }}
QToolBar QToolButton:hover {{ background: {t['panel2']}; }}
QStatusBar {{ background: {t['panel']}; color: {t['muted']}; border-top: 1px solid {t['line']}; }}

/* przyciski */
QPushButton {{ background: {t['panel2']}; color: {t['text']}; border: 1px solid {t['line']};
              border-radius: 7px; padding: 6px 12px; }}
QPushButton:hover {{ border-color: {t['accent']}; }}
QPushButton:pressed {{ background: {t['accent_soft']}; }}
QPushButton:disabled {{ color: {t['disabled']}; border-color: {t['line']}; background: {t['panel']}; }}
QPushButton[accent="true"] {{ background: {t['accent']}; color: {t['accent_text']}; border-color: {t['accent']}; font-weight: 600; }}
QPushButton[accent="true"]:disabled {{ background: {t['panel2']}; color: {t['disabled']}; border-color: {t['line']}; }}
QPushButton[danger="true"] {{ color: {t['danger']}; }}
QPushButton[flat="true"] {{ background: transparent; border-color: transparent; }}
QPushButton:checked {{ background: {t['accent_soft']}; border-color: {t['accent']}; }}

/* pola */
QLineEdit, QSpinBox, QDoubleSpinBox, QTimeEdit, QComboBox, QPlainTextEdit {{
    background: {t['base']}; color: {t['text']}; border: 1px solid {t['line']}; border-radius: 6px; padding: 4px 6px;
    selection-background-color: {t['accent']}; selection-color: {t['accent_text']}; }}
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QTimeEdit:focus, QComboBox:focus {{ border-color: {t['accent']}; }}
QComboBox QAbstractItemView {{ background: {t['panel']}; color: {t['text']}; border: 1px solid {t['line']};
                               selection-background-color: {t['accent_soft']}; selection-color: {t['text']}; }}
QCheckBox {{ spacing: 8px; }}

/* zakładki ustawień */
QTabWidget::pane {{ border: 1px solid {t['line']}; border-radius: 8px; background: {t['panel']}; top: -1px; }}
QTabBar::tab {{ background: transparent; color: {t['muted']}; padding: 7px 8px; border: none;
               border-bottom: 2px solid transparent; }}
QTabBar::tab:hover {{ color: {t['text']}; }}
QTabBar::tab:selected {{ color: {t['text']}; border-bottom: 2px solid {t['accent']}; font-weight: 600; }}

/* grupy, karty, listy, tabele */
QGroupBox {{ background: {t['panel']}; border: 1px solid {t['line']}; border-radius: 8px; margin-top: 14px; padding: 10px 8px 8px 8px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {t['muted']}; font-weight: 600; }}
QFrame#card {{ background: {t['panel']}; border: 1px solid {t['line']}; border-radius: 10px; }}
QFrame#card:hover {{ border-color: {t['accent']}; }}
QFrame#panel {{ background: {t['panel']}; border: 1px solid {t['line']}; border-radius: 10px; }}
QLabel#cardTitle {{ font-size: 15px; font-weight: 700; }}
QLabel#muted, QLabel#hint {{ color: {t['muted']}; font-size: 11px; }}
QLabel#pageTitle {{ font-size: 18px; font-weight: 700; }}
QLabel#section {{ color: {t['muted']}; font-weight: 700; font-size: 11px; letter-spacing: 1px; padding-top: 8px; }}
QLabel#metricValue {{ font-size: 13px; font-weight: 700; }}
QLabel#metricName {{ color: {t['muted']}; font-size: 11px; }}
QLabel#pill {{ border-radius: 9px; padding: 2px 9px; font-size: 11px; font-weight: 600;
              background: {t['panel2']}; border: 1px solid {t['line']}; }}
QListWidget {{ background: {t['base']}; border: 1px solid {t['line']}; border-radius: 8px; padding: 4px; }}
QListWidget::item {{ padding: 6px; border-radius: 6px; }}
QListWidget::item:selected {{ background: {t['accent_soft']}; color: {t['text']}; }}
QTableWidget {{ background: {t['base']}; alternate-background-color: {t['panel2']}; border: 1px solid {t['line']};
               border-radius: 8px; gridline-color: {t['line']}; }}
QHeaderView::section {{ background: {t['panel']}; color: {t['muted']}; border: none; border-bottom: 1px solid {t['line']};
                       padding: 5px 6px; font-weight: 600; }}
QScrollArea {{ background: transparent; border: none; }}
QSplitter::handle {{ background: {t['bg']}; }}

/* paski przewijania */
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {t['line']}; border-radius: 4px; min-height: 30px; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {t['line']}; border-radius: 4px; min-width: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: none; }}
"""


def apply_theme(name: str, save: bool = True) -> None:
    global _current
    app = QApplication.instance()
    if app is None:
        return
    if name not in THEMES:
        name = "system"
    dark = _system_is_dark(app) if name == "system" else name == "dark"
    _current = "dark" if dark else "light"
    t = TOKENS[_current]
    # Fusion - wygląda tak samo na każdym systemie i w pełni słucha palety
    app.setStyle("Fusion")
    app.setPalette(_palette(t))
    app.setStyleSheet(stylesheet(t))
    # widżety z własnym arkuszem stylów trzymają paletę z chwili utworzenia
    # - ponowne ustawienie arkusza przelicza je z nową paletą
    for w in app.allWidgets():
        sheet = w.styleSheet()
        if sheet:
            w.setStyleSheet("")
            w.setStyleSheet(sheet)
    if save:
        _settings().setValue("theme", name)
