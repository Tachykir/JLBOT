# Dust Mine Bot

Bot do automatycznego zbierania "pyłu" w grze mobilnej (ekran "Dust Mine
Explosion") na emulatorze **LDPlayer**. Steruje przez **ADB**, więc
działa całkowicie **w tle** — nie potrzebuje focusa okna, może pracować
z zminimalizowanym lub zasłoniętym oknem LDPlayer. Obsługuje **wiele
instancji** LDPlayer równolegle (osobny proces na każdą).

## Jak to działa

1. **Tryb idle** — bot klika jeden ustalony punkt (`idle_point`), aż
   na ekranie pojawi się plansza z kulkami do zebrania.
2. **Tryb zbierania** — bot na bieżąco wykrywa pozycję punktu **DROP**
   (bo się przesuwa) oraz wszystkie kolorowe kulki, i przeciąga je
   kolejno do DROP-a, aż plansza będzie pusta.
3. Powrót do trybu idle, pętla od nowa.

Detekcja (`vision.py`) używa **OpenCV** i nie opiera się na sztywnych
współrzędnych ani zrzutach wzorcowych (template matching) — analizuje
obraz "na żywo":

- **Kulki**: wykrywane przez transformatę Hougha (`HoughCircles`) jako
  jednolicie wypełnione, kolorowe koła na czarnym tle.
- **DROP**: to trudniejszy przypadek, bo to *przerywany* biały okrąg
  z tekstem "DROP" w środku — Hough sobie z tym nie radzi. Zamiast tego:
  odróżniamy "prawdziwą biel" (niska saturacja w HSV) od jasnych,
  ale kolorowych kulek (wysoka saturacja), łączymy przerywaną kreskę
  w spójny kontur przez dylatację morfologiczną, liczymy środek ciężkości
  jasnych pikseli, a promień okręgu ustalamy skanując profil jasności
  wzdłuż rosnących promieni wokół tego środka.

Dzięki temu bot **działa niezależnie od tego, gdzie aktualnie jest
DROP i jaki układ/kolor mają kulki** — nie trzeba nic przeuczać przy
zmianie układu planszy.

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

## Krok 1: kalibracja detekcji na Twoim ekranie

Zanim uruchomisz bota, sprawdź czy detekcja działa poprawnie na Twojej
rozdzielczości/układzie okna. Zrób zrzut ekranu z gry w momencie, gdy
widać kulki, i uruchom:

```bash
python calibrate.py --image sciezka/do/zrzutu.png --grid
```

Otwórz wygenerowany `detection_preview.png` — powinny być zaznaczone
**zielonym** wszystkie kulki i **czerwonym** punkt DROP. Siatka (`--grid`)
pomoże Ci odczytać dokładne współrzędne do configu.

Możesz też pobrać zrzut bezpośrednio z podłączonej instancji ADB:

```bash
python calibrate.py --serial 127.0.0.1:5555 --grid
```

Narzędzie na końcu wypisze **sugerowany `board_region`** — warto go
ręcznie poszerzyć, żeby na pewno obejmował cały czarny prostokąt planszy
przy dowolnym układzie kulek (nie tylko ten jeden, który akurat
sfotografowałeś).

Jeśli detekcja się myli (np. źle łapie DROP), daj znać z przykładowym
screenem — próg jasności/saturacji w `vision.py` (`value_thresh`,
`sat_thresh` w `_find_drop_point`) może wymagać drobnej korekty pod
konkretny motyw graficzny gry.

## Krok 2: config.json

Skopiuj `config.example.json` do `config.json` i uzupełnij dla każdej
instancji:

```json
{
  "instances": [
    {
      "serial": "127.0.0.1:5555",
      "name": "instance-1",
      "idle_point": [360, 640],
      "board_region": [22, 377, 677, 1254]
    }
  ]
}
```

- **`serial`** — z `adb devices`.
- **`idle_point`** — [x, y] punktu, w który bot klika, czekając na
  pojawienie się ekranu z kulkami (np. przycisk otwierający "eksplozję").
  Odczytaj współrzędne z `calibrate.py --grid`.
- **`board_region`** — [x1, y1, x2, y2] obszaru planszy gry, żeby
  ograniczyć wykrywanie i uniknąć fałszywych trafień w innych elementach
  UI. Ustal na podstawie sugestii z `calibrate.py`.

Pełna lista opcjonalnych parametrów (z domyślnymi wartościami) jest
w `bot.py` w klasie `BotConfig` — np. `drag_duration_ms`,
`between_drags_delay`, `idle_click_interval`.

## Krok 3: uruchomienie

### Opcja A: GUI (zalecane)

```bash
python main_window.py
```

Okno pozwala na:

- **Dodawanie instancji** przyciskiem "+ Dodaj" albo automatyczne
  wykrycie wszystkich podłączonych urządzeń ADB przyciskiem
  "🔍 Auto-wykryj z adb devices".
- **Podgląd na żywo** wybranej instancji — po starcie bota obraz
  odświeża się automatycznie z nałożoną wizualizacją detekcji
  (zielone obwódki = wykryte kulki, czerwona = punkt DROP). Możesz też
  pobrać pojedynczy zrzut ręcznie przyciskiem "⟳ Odśwież teraz"
  (przydatne, gdy bot jest jeszcze zatrzymany).
- **Ustawianie `idle_point` klikiem** — wciśnij "🎯 Kliknij: ustaw
  idle_point", potem kliknij bezpośrednio na obrazie w miejscu, gdzie
  bot ma klikać czekając na grę.
- **Ustawianie `board_region` przeciągnięciem** — wciśnij
  "▭ Przeciągnij: ustaw board_region", potem przeciągnij prostokąt na
  obrazie obejmujący całą planszę gry.
- **Edycję pozostałych parametrów** (drag_duration_ms, interwały itd.)
  w panelu po prawej — zmiany stosują się natychmiast, także do już
  uruchomionej instancji.
- **Start / Stop / Pauza** dla pojedynczej instancji albo wszystkich
  naraz.
- **Zapis / wczytanie configu** do/z pliku JSON (ten sam format co
  `config.example.json`, więc GUI i CLI (`manager.py`) są w pełni
  kompatybilne — możesz skonfigurować w GUI, zapisać, i potem odpalać
  bez GUI przez `manager.py`, albo odwrotnie).
- **Logi na żywo** wszystkich instancji na dole okna.

Każda instancja działa we własnym wątku wewnątrz jednego procesu GUI
(a nie osobnym procesie jak w `manager.py`) — to celowe uproszczenie,
które pozwala GUI na bezpośredni, płynny podgląd i edycję na żywo bez
komunikacji międzyprocesowej. Dla typowej liczby instancji (4-8) nie
jest to ograniczeniem wydajności, bo dominujący koszt (zrzut ekranu
przez ADB, przetwarzanie OpenCV) i tak zwalnia blokadę GIL w trakcie
wywołań.

### Opcja B: linia poleceń (bez GUI)

Wiele instancji naraz, z pliku konfiguracyjnego:

```bash
python manager.py --config config.json
```

Albo automatyczne wykrycie wszystkich podłączonych urządzeń ADB
(wszystkie dostają ten sam `idle_point` — użyteczne, gdy okna LDPlayer
mają identyczny układ):

```bash
python manager.py --auto --idle-x 360 --idle-y 640
```

Zatrzymanie: `Ctrl+C` w terminalu — manager grzecznie zatrzyma wszystkie
procesy potomne.

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
| `vision.py` | Detekcja kulek i punktu DROP przez OpenCV |
| `bot.py` | Logika jednej instancji bota (pętla idle → zbieranie → idle) |
| `manager.py` | CLI: uruchamianie wielu instancji bota równolegle (multiprocessing) |
| `main_window.py` | GUI: główne okno aplikacji (uruchom to, żeby odpalić GUI) |
| `gui_worker.py` | Most między `bot.py` a GUI - uruchamia bota w wątku Qt |
| `preview_widget.py` | Widget podglądu z interaktywną edycją idle_point/board_region klikiem |
| `calibrate.py` | Narzędzie CLI do podglądu/kalibracji detekcji na pojedynczym zrzucie |
| `config.example.json` | Przykładowa konfiguracja wielu instancji |
| `dustbot_gui.spec` | Konfiguracja PyInstaller do budowy samodzielnego .exe (patrz BUILD_INSTRUCTIONS.md) |
| `installer.iss` | Skrypt Inno Setup budujący instalator Windows z kreatorem |

## Typowe problemy

- **`adb: command not found`** — dodaj ADB do PATH albo użyj pełnej
  ścieżki do `adb.exe` z folderu instalacyjnego LDPlayer.
- **Bot nie widzi urządzenia** — sprawdź `adb devices`, upewnij się że
  "ADB debugging" jest włączone w ustawieniach danej instancji LDPlayer
  i że port się zgadza.
- **DROP nie jest wykrywany** — uruchom `calibrate.py` na aktualnym
  zrzucie, sprawdź `detection_preview.png`; jeśli motyw graficzny gry
  różni się kolorystycznie od testowanego (np. inny odcień tła), może
  być potrzebna korekta progów w `vision.py`.
- **Drag nie działa w grze** — zwiększ `drag_duration_ms` w configu
  (niektóre silniki gier wymagają dłuższego gestu, żeby uznać go za
  przeciągnięcie, a nie zwykłe tapnięcie).
