# Dust Mine Bot

Bot do automatycznego zbierania "pyłu" w grze mobilnej (ekran "Dust Mine
Explosion") na emulatorze **LDPlayer**. Steruje przez **ADB**, więc
działa całkowicie **w tle** — nie potrzebuje focusa okna, może pracować
z zminimalizowanym lub zasłoniętym oknem LDPlayer. Obsługuje **wiele
instancji** LDPlayer równolegle (osobny proces na każdą).

## Jak to działa

Bot rozpoznaje, co jest na ekranie, szablonami (`dustbot2/screens.py`,
szablony w `dustbot2/templates/`) i sam wybiera akcję:

- **Profile** - klika JUST LOOT (przycisk znajdowany na ekranie),
- **koniec walki** - PROCEED, **"?"** - odsłonięcie nagrody,
- **popup przedmiotu** - Keep Current,
- **Dust Mine** - przeciąga kulki do DROP (kulki: `vision.py`, DROP:
  szablon), **Fishing Hole** i **Depth Collapse** - patrz `docs/minigry.md`,
- walka, ładowanie i inne nierozpoznane ekrany - czeka.

Nic nie trzeba kalibrować: szablony i obszar planszy dopasowują się do
rozdzielczości i DPI emulatora same.

## Instalacja

```bash
pip install -r requirements.txt
```

Potrzebujesz też zainstalowanego **ADB** (Android Debug Bridge)
dostępnego w PATH — najprościej z pakietu `platform-tools` od Google,
albo z folderu instalacyjnego LDPlayer (LDPlayer ma swój `adb.exe`
w katalogu instalacji, możesz go użyć podając pełną ścieżkę w
`--adb-path`, jeśli rozszerzysz kod o taką opcję, albo dodać ten
folder do PATH).

## Konfiguracja LDPlayer (ADB debugging)

W ustawieniach każdej instancji LDPlayer:
`Ustawienia (koło zębate) -> Inne -> włącz "ADB debugging" / "Uruchom ADB"`.

Sprawdź porty, na których nasłuchują Twoje instancje:

```bash
adb devices
```

LDPlayer standardowo przydziela porty parami zaczynając od 5555, 5557,
5559, 5561... dla kolejnych instancji (numer instancji × 2 + 5554), ale
**zawsze zweryfikuj to** przez `adb devices`, bo bywa to inaczej
skonfigurowane zależnie od wersji LDPlayer.

## Konfiguracja (config.json)

Najprościej skonfigurować w GUI i zapisać przyciskiem "Zapisz config".
Ręcznie: skopiuj `config.example.json` do `config.json`. Wymagane są
tylko `serial` (z `adb devices`) i `name`; reszta ma wartości domyślne
(pełna lista w `bot.py`, klasa `BotConfig`). Najważniejsze opcje:

- **`daily_play`** - harmonogram dzienny: okno (`window_start`,
  `window_end`) i `play_hours`; bot sam układa sesje (`session_minutes`)
  i nieregularne przerwy (min. `min_break_minutes`). Brak = gra bez przerwy.
- **`touch_backend`** - `"adb"` albo `"minitouch"` (patrz niżej).
- **`game_package`** - pakiet gry; potrzebny do restartu gry przez
  watchdogi i zamykania gry na czas przerw (`close_game_during_break`).

Starsze pola `idle_point` i `board_region` nie są już używane - jeśli są w
starym pliku, bot je pomija.

## Uruchomienie

### Opcja A: GUI (zalecane)

```bash
python main_window.py
```

Okno pozwala na:

- **Dodawanie instancji** przyciskiem "+ Dodaj" albo automatyczne
  wykrycie wszystkich podłączonych urządzeń ADB przyciskiem
  "🔍 Auto-wykryj z adb devices".
- **Podgląd na żywo** wybranej instancji (zielone obwódki = kulki,
  czerwona = DROP); "⟳ Odśwież teraz" pobiera zrzut przy zatrzymanym
  bocie.
- **📸 Zrzut ekranu (F9)** i **⏺ Nagrywaj (F10)** - materiał do nowych
  ekranów i minigier (patrz niżej).
- **Konfigurację** po prawej: wejście dotykowe, odstępy tapów, czas
  przeciągania i **📅 harmonogram dzienny** z podglądem przykładowego
  planu dnia.
- **Start / Stop / Pauza / Restart gry** dla pojedynczej instancji albo
  wszystkich naraz, **statystyki** (cykle, kulki, trafienia w fishingu).
- **Zapis / wczytanie configu** - ten sam format co `config.example.json`,
  więc GUI i `manager.py` są w pełni kompatybilne.
- **Logi na żywo** wszystkich instancji na dole okna.

Każda instancja działa we własnym wątku wewnątrz jednego procesu GUI
(a nie osobnym procesie jak w `manager.py`) - pozwala to na płynny
podgląd bez komunikacji międzyprocesowej. Dominujący koszt (zrzut przez
ADB, OpenCV) zwalnia blokadę GIL, więc kilka instancji działa sprawnie.

### Opcja B: linia poleceń (bez GUI)

```bash
python manager.py --config config.json
python manager.py --auto            # wszystkie urządzenia z `adb devices`
```

Zatrzymanie: `Ctrl+C` w terminalu - manager grzecznie zatrzyma wszystkie
procesy potomne.

## Zróżnicowane gesty (minitouch)

Domyślnie bot przeciąga kulki przez `adb shell input swipe` (prosta linia,
stała prędkość). Z `minitouch` każdy gest ma inny kształt (łuk, S, haczyk,
"L", rzut), zmienną prędkość i nacisk - patrz `dustbot2/touch/humanize.py`.
Wymaga roota na emulatorze (LDPlayer ma go domyślnie). Binarka jest w
`vendor/minitouch/` i bot sam wgrywa ją na emulator.

1. Sprawdź, czy działa (na ekranie głównym Androida, nie w grze):

   ```bash
   python -m dustbot2.touch.check --serial emulator-5554 --demo
   ```

   Na ekranie pojawią się ślady tapu i 6 gestów. Muszą iść od dołu-lewej
   do góry-prawej części ekranu. Jeśli są odbite lub obrócone - nie
   włączaj minitouch i zgłoś to.
2. Włącz w `config.json` dla instancji: `"touch_backend": "minitouch"`.

Jeśli minitouch się nie uruchomi albo przestanie działać, bot sam wraca do
`adb input` i zapisuje ostrzeżenie w logu.

## Ekrany między walkami i minigry

Bot rozpoznaje ekrany gry szablonami (`dustbot2/screens.py`) i sam
obsługuje: PROCEED po walce, odsłonięcie nagrody "?", popup przedmiotu
(Keep Current), Fishing Hole, Depth Collapse i Dust Mine; na Profile klika
JUST LOOT. Szczegóły i opcje: `docs/minigry.md`.

## Nagrywanie minigier (materiał do nowych modułów)

Bot potrafi zbierać materiał z ekranów, których jeszcze nie obsługuje
(np. minigra fishing):

- **Przycisk "⏺ Nagrywaj (F10)" w GUI** - działa też przy zatrzymanym
  bocie. Kliknij, zagraj ręcznie w minigrę i kliknij "⏹ Zatrzymaj
  nagrywanie" (maks. 3 min). Jeśli bot działa, jest wstrzymany na czas
  nagrania. Zapisuje się wideo (`video.mp4`), Twoje dotknięcia z dokładnym
  czasem (`touches.txt`), zrzuty klatek (`frames/`) i `meta.json` do
  synchronizacji.
- **Przycisk "📸 Zrzut ekranu (F9)"** - zapisuje aktualny widok wybranej
  instancji do `captures/Screenshots/` (działa też przy zatrzymanym bocie).
- **Bez GUI**: `python recorder.py --serial emulator-5556 --seconds 30 --label fishing`
- **Automatycznie**: bot zapisuje zrzuty nierozpoznanych ekranów do
  `captures/Unknown/` (opcja `capture_unknown_screens`, domyślnie włączona).
  Z `"record_unknown_seconds": 30` w config.json bot dodatkowo nagrywa
  30 s przy pierwszym nieznanym ekranie (nie częściej niż co
  `record_cooldown` sekund).

Nagrania trafiają do `captures/recordings/<data>_<instancja>_<etykieta>/`.
Wrzuć cały folder do repo albo wyślij go - to wystarczy, żeby zbudować
obsługę nowej minigry.

## Budowanie jako samodzielny .exe / instalator (Windows)

Jeśli chcesz rozesłać aplikację komuś bez Pythona zainstalowanego na
komputerze, możesz zbudować ją jako samodzielny `.exe` (przez
PyInstaller) i opakować w prawdziwy instalator z kreatorem (przez Inno
Setup). Pełna instrukcja krok po kroku: **[BUILD_INSTRUCTIONS.md](BUILD_INSTRUCTIONS.md)**.

Gotowe pliki konfiguracyjne do tego już są w projekcie:
- `dustbot_gui.spec` — konfiguracja PyInstaller
- `installer.iss` — skrypt Inno Setup (kreator instalacyjny + skrót w Menu Start)

## Struktura projektu

| Plik | Rola |
|---|---|
| `adb_controller.py` | Cienka warstwa nad ADB: screenshot, tap, swipe, per-instancja |
| `vision.py` | Detekcja kulek (OpenCV) |
| `bot.py` | Logika jednej instancji: rozpoznanie ekranu -> akcja, Dust Mine, harmonogram |
| `dustbot2/screens.py` | Rozpoznawanie ekranów szablonami (`dustbot2/templates/`) |
| `dustbot2/minigames/` | Fishing Hole i Depth Collapse |
| `dustbot2/schedule.py` | Harmonogram dzienny (sesje i przerwy) |
| `dustbot2/touch/` | minitouch i zróżnicowane gesty |
| `recorder.py` | Nagrywanie minigier i zapis nieznanych ekranów |
| `manager.py` | CLI: wiele instancji równolegle (multiprocessing) |
| `main_window.py` | GUI: główne okno aplikacji (uruchom to, żeby odpalić GUI) |
| `gui_worker.py` | Most między `bot.py` a GUI - bot i nagrywanie w wątkach Qt |
| `preview_widget.py` | Widget podglądu z wizualizacją detekcji |
| `config.example.json` | Przykładowa konfiguracja |
| `docs/` | Opis ekranów, minigier i decyzji |
| `material/` | Nagrania i zrzuty z gry (materiał do szablonów i testów) |
| `dustbot_gui.spec` | Konfiguracja PyInstaller do budowy samodzielnego .exe (patrz BUILD_INSTRUCTIONS.md) |
| `installer.iss` | Skrypt Inno Setup budujący instalator Windows z kreatorem |

## Typowe problemy

- **`adb: command not found`** — dodaj ADB do PATH albo użyj pełnej
  ścieżki do `adb.exe` z folderu instalacyjnego LDPlayer.
- **Bot nie widzi urządzenia** — sprawdź `adb devices`, upewnij się że
  "ADB debugging" jest włączone w ustawieniach danej instancji LDPlayer
  i że port się zgadza.
- **Bot nie rozpoznaje ekranu** - zrób zrzut przyciskiem 📸 F9 (albo
  zajrzyj do `captures/Unknown/`, gdzie bot sam zapisuje nieznane ekrany)
  i dodaj szablon: `python -m dustbot2.tools.make_template ...`.
- **Drag nie działa w grze** — zwiększ `drag_duration_ms` w configu
  (niektóre silniki gier wymagają dłuższego gestu, żeby uznać go za
  przeciągnięcie, a nie zwykłe tapnięcie).
