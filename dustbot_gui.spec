# -*- mode: python ; coding: utf-8 -*-
"""
dustbot_gui.spec
=================
Konfiguracja PyInstaller do zbudowania GUI (main_window.py) jako
samodzielnej aplikacji Windows, niewymagającej zainstalowanego Pythona
na komputerze docelowym.

BUDOWANIE (na Windowsie, w folderze projektu, z aktywnym venv gdzie
zainstalowano requirements.txt + pyinstaller):

    pyinstaller dustbot_gui.spec

Wynik trafia do folderu `dist/DustMineBot/` - to jest gotowa, przenośna
aplikacja: `dist/DustMineBot/DustMineBot.exe` + wszystkie potrzebne
biblioteki obok. Cały folder `dist/DustMineBot/` można spakować/
przenieść na inny komputer i uruchomić bez instalowania Pythona.

Ten .spec zakłada tryb "folder" (--onedir), nie "jeden plik"
(--onefile) - jest szybszy przy starcie i łatwiejszy w debugowaniu.
Instalator (Inno Setup, patrz installer.iss) pakuje właśnie ten folder.

WAŻNE - adb.exe:
Ten .spec dołącza `vendor/adb/` do paczki, jeśli ten folder istnieje.
Pobierz Android Platform Tools (adb.exe + towarzyszące DLL) i umieść
je w `vendor/adb/` przed budowaniem - patrz README_BUILD.md.
"""

import os
from pathlib import Path

block_cipher = None

PROJECT_ROOT = Path(os.getcwd())
VENDOR_ADB_DIR = PROJECT_ROOT / "vendor" / "adb"

# Dołącz adb.exe i jego zależności (AdbWinApi.dll, AdbWinUsbApi.dll), jeśli
# są przygotowane w vendor/adb/ - patrz README_BUILD.md po instrukcje pobrania.
adb_binaries = []
if VENDOR_ADB_DIR.exists():
    for f in VENDOR_ADB_DIR.glob("*"):
        if f.is_file():
            # (ścieżka_źródłowa, folder_docelowy_w_paczce)
            adb_binaries.append((str(f), "vendor/adb"))

a = Analysis(
    ["main_window.py"],
    pathex=[str(PROJECT_ROOT)],
    binaries=adb_binaries,
    datas=[
        ("config.example.json", "."),
        # szablony ekranów używane przez dustbot2.screens
        ("dustbot2/templates", "dustbot2/templates"),
        # minitouch dla emulatorów (wgrywany na urządzenie przy starcie bota)
        ("vendor/minitouch", "vendor/minitouch"),
    ],
    hiddenimports=[
        # PySide6 czasem wymaga jawnego wskazania tych modułów -
        # bez tego bywają błędy "no module named PySide6.XYZ" dopiero
        # przy uruchomieniu zbudowanego exe, nie przy samym budowaniu.
        "PySide6.QtCore",
        "PySide6.QtGui",
        "PySide6.QtWidgets",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # ograniczają rozmiar paczki - te moduły Qt nie są używane przez GUI
        "PySide6.QtQml",
        "PySide6.QtQuick",
        "PySide6.QtNetwork",
        "PySide6.QtWebEngineWidgets",
        "PySide6.Qt3DCore",
        "PySide6.QtMultimedia",
        "PySide6.QtPdf",
        "PySide6.QtBluetooth",
        "PySide6.QtNfc",
        "PySide6.QtSensors",
        "PySide6.QtPositioning",
        "matplotlib",
        "tkinter",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DustMineBot",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,          # GUI bez okna konsoli w tle
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,               # podmień na icon="app_icon.ico" jeśli masz ikonę
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="DustMineBot",
)
