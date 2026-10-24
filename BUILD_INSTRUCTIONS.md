# Budowanie samodzielnego .exe i instalatora (Windows)

Ta instrukcja prowadzi Cię przez zbudowanie Dust Mine Bot jako
samodzielnej aplikacji Windows (`DustMineBot.exe`), niewymagającej
zainstalowanego Pythona na komputerze, na którym będzie uruchamiana -
oraz opakowanie tego w prawdziwy instalator (`DustMineBot-Setup.exe`)
z kreatorem i skrótem w Menu Start.

**Wszystko poniżej wykonujesz na Windowsie** (PyInstaller buduje pod
system, na którym jest uruchamiany - nie da się zbudować .exe z innego
systemu).

---

## Najszybciej: build_installer.bat

Na Windowsie, w folderze projektu, uruchom dwuklikiem
**`build_installer.bat`**. Skrypt sam:

1. tworzy środowisko `.venv`, instaluje zależności i PyInstaller,
2. pobiera ADB (Android Platform Tools) do `vendor\adb\`, jeśli go nie ma,
3. buduje `dist\DustMineBot\DustMineBot.exe` (z serwerem scrcpy i PyAV),
4. instaluje Inno Setup 6 przez `winget`, jeśli go nie ma,
5. buduje instalator `installer_output\DustMineBot-Setup.exe` z wersją
   z `version.py` i otwiera folder z wynikiem.

Wystarczy Python 3.10–3.13 z python.org (z „Add python.exe to PATH”)
i internet przy pierwszym uruchomieniu. Poniżej te same kroki ręcznie.

## Krok 1: Przygotowanie środowiska budowania

Potrzebujesz Pythona **tylko na czas budowania** (nie na komputerze
docelowym użytkownika):

```powershell
python -m venv build_venv
build_venv\Scripts\activate
pip install -r requirements.txt
pip install pyinstaller
```

## Krok 2: Pobranie adb.exe (Android Platform Tools)

Aplikacja dołącza własny `adb.exe`, żeby działała "od ręki" bez
instalowania ADB osobno.

1. Pobierz **Android SDK Platform Tools dla Windows**:
   https://developer.android.com/tools/releases/platform-tools
   (bezpośredni link do pobrania: `platform-tools-latest-windows.zip`)

2. Rozpakuj archiwum. W folderze `platform-tools\` znajdziesz kilkanaście
   plików - potrzebujesz tylko trzech:
   - `adb.exe`
   - `AdbWinApi.dll`
   - `AdbWinUsbApi.dll`

3. Utwórz w folderze projektu strukturę i skopiuj te 3 pliki:

   ```
   dustbot\
     vendor\
       adb\
         adb.exe
         AdbWinApi.dll
         AdbWinUsbApi.dll
   ```

   (Jeśli folderu `vendor\adb\` nie utworzysz, `dustbot_gui.spec` po
   prostu pominie dołączanie ADB - aplikacja się zbuduje, ale będzie
   wymagać ADB zainstalowanego osobno / w PATH systemowym).

## Krok 3: Budowanie aplikacji (PyInstaller)

W folderze projektu (tam gdzie jest `dustbot_gui.spec`):

```powershell
pyinstaller dustbot_gui.spec
```

Po zakończeniu (może potrwać kilka minut) sprawdź, że powstał folder
`dist\DustMineBot\` zawierający `DustMineBot.exe`. **Przetestuj go od
razu**, zanim przejdziesz dalej:

```powershell
cd dist\DustMineBot
.\DustMineBot.exe
```

Powinno otworzyć się okno GUI. Jeśli nie:
- **Czarne okno / natychmiastowe zamknięcie** - uruchom z konsoli
  (`cmd`, nie double-click), żeby zobaczyć komunikat błędu. Zwykle to
  brakujący hidden import PySide6 - dopisz go do listy
  `hiddenimports` w `dustbot_gui.spec` i zbuduj ponownie.
- **Antywirus blokuje/kwarantannuje plik** - to częste fałszywe
  alarmy przy świeżo zbudowanych PyInstaller-owych exe (heurystyka
  antywirusów reaguje na spakowany kod). Dodaj wyjątek albo, jeśli
  planujesz dystrybucję szerzej, rozważ podpisanie exe certyfikatem
  code-signing (eliminuje większość takich alarmów).

## Krok 4: Instalacja Inno Setup

Pobierz i zainstaluj darmowe Inno Setup:
https://jrsoftware.org/isdl.php

(Wersja "Inno Setup Compiler" - to narzędzie z GUI, w którym otwierasz
plik `.iss` i klikasz "Compile").

## Krok 5: Budowanie instalatora

1. Otwórz `installer.iss` w Inno Setup Compiler
   (Plik → Otwórz, albo po prostu kliknij dwukrotnie plik `.iss`,
   jeśli skojarzenie jest ustawione po instalacji Inno Setup).

2. Sprawdź na górze pliku sekcję `#define`:
   ```
   #define MyAppVersion "1.0.0"
   #define MyAppPublisher "Twoja nazwa / pseudonim"
   ```
   Podmień `MyAppPublisher` na coś swojego (widoczne w "Dodaj/Usuń
   programy"). Wersję możesz zostawić albo zmieniać przy kolejnych
   buildach.

3. Kliknij **Build → Compile** (albo `Ctrl+F9`).

4. Gotowy instalator pojawi się w:
   ```
   installer_output\DustMineBot-Setup.exe
   ```

To jest **pojedynczy plik**, który możesz przesłać/rozesłać. Po
uruchomieniu przez odbiorcę pokaże się standardowy kreator instalacyjny
Windows: wybór folderu instalacji, opcjonalny skrót na Pulpicie, i na
końcu skrót w Menu Start + wpis w "Dodaj/Usuń programy" (czyli też
łatwe odinstalowanie).

---

## Aktualizacja aplikacji w przyszłości

Gdy zmienisz coś w kodzie Pythona:

1. `pyinstaller dustbot_gui.spec` (ponownie, nadpisze `dist\DustMineBot\`)
2. Zwiększ `MyAppVersion` w `installer.iss` (np. na `"1.1.0"`)
3. Skompiluj ponownie w Inno Setup Compiler

`adb.exe` w `vendor\adb\` nie trzeba aktualizować przy każdej zmianie -
tylko jeśli chcesz mieć nowszą wersję Platform Tools.

---

## Podsumowanie: co trafia do finalnego instalatora

```
DustMineBot-Setup.exe  (jeden plik do rozesłania)
    │
    └── po instalacji tworzy w Program Files:
        DustMineBot\
          DustMineBot.exe          <- główny program (uruchamia GUI)
          _internal\                <- Python + PySide6 + OpenCV + numpy
            vendor\adb\adb.exe      <- dołączony ADB (samowystarczalność)
            vendor\adb\*.dll
            ...
          config.example.json
          README.md
```

Osoba instalująca **nie potrzebuje** mieć zainstalowanego Pythona, ADB,
ani żadnych bibliotek - tylko system Windows i (do faktycznego
działania bota) uruchomiony LDPlayer z włączonym ADB debugging.
