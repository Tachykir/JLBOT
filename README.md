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

- **DROP**: przerywany biały okrąg z napisem "DROP". Odróżniamy
  "prawdziwą biel" (niska saturacja w HSV) od jasnych, ale kolorowych
  kulek, szukamy kandydatów na okręgi transformatą Hougha i sprawdzamy:
  biały napis w środku, pusty pierścień między napisem a kreskami i
  kreski na co najmniej połowie obwodu. Wynik doprecyzowuje dopasowanie
  okręgu do kresek. Wystarczy fragment okręgu, więc DROP jest znajdowany
  także wtedy, gdy kulki częściowo go zasłaniają. Gdy kulka leży na
  DROP-ie i zasłania napis, DROP rozpoznawany jest po regularnych
  kreskach (co najmniej 5, a w miejscu znanym z poprzednich plansz - 3),
  które muszą leżeć dokładnie na okręgu (odróżnia je to od tekstu).
  Bot pamięta ostatnią pozycję DROP, gdyby kulki zasłoniły go całkiem.
  Kulkę leżącą na DROP-ie bot wyciąga poza okrąg i wrzuca z powrotem
  jednym ruchem (ruch „z DROP na DROP” gra mogłaby uznać za tapnięcie).
- **Kulki**: wszystko, co odróżnia się od tła planszy (poza białym
  DROP-em), i kolejne "najgłębsze" punkty tej maski (transformata
  odległości). Działa dla kulek nachodzących na siebie i z napisem
  "+208" w środku; punkt chwytu zawsze leży na kulce z wierzchu. Kulka
  całkiem schowana pod inną pojawia się po zebraniu tej z góry.

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

- **`serial`** — z `adb devices`. Adresy sieciowe (`127.0.0.1:5555`)
  bot łączy przez `adb connect`; seriale lokalnych emulatorów
  (`emulator-5556`) są tylko sprawdzane na liście `adb devices`.
- **`idle_target`** (domyślnie `"just_loot"`) — w trybie idle bot sam
  znajduje na każdej klatce przycisk **JUST LOOT** (koło na dole ekranu,
  w dowolnym motywie i rozdzielczości) i klika w losowy punkt jego
  środka; po walce klika PROCEED. `"fixed"` = zawsze `idle_point`.
- **Drop po wygranej walce** — zakrytą kartę („?”, „TAP ANYWHERE TO
  REVEAL”) bot odkrywa sam, a na ekranie porównania przedmiotów klika
  opcję z `drop_choice`: `"keep"` (Keep Current, domyślnie), `"replace"`,
  `"equip_salvage"` albo `"salvage"` (GUI: zakładka Ogólne).
- **Pełny ekwipunek** (`salvage_when_full`, domyślnie włączone) — gdy
  po „Keep Current” wyskoczy „Inventory Full”: OK, przytrzymanie Salvage
  na ekranie dropu, potem zakładka INVENTORY i przytrzymanie „+x DUST” na
  `salvage_items` (domyślnie 10) przedmiotach **z góry listy**. Ustaw w
  grze sortowanie tak, żeby na górze były najsłabsze przedmioty (np. Item
  Level rosnąco) — rozebrania nie da się cofnąć. Czas przytrzymania:
  `salvage_hold_ms` (GUI: zakładka Paczki).
- **`idle_point`** — [x, y] punktu awaryjnego (gdy nie widać ani JUST
  LOOT, ani PROCEED) albo stałego przy `idle_target: "fixed"`.
  Odczytaj współrzędne z `calibrate.py --grid`.
- **`board_region`** — [x1, y1, x2, y2] obszaru planszy gry, żeby
  ograniczyć wykrywanie i uniknąć fałszywych trafień w innych elementach
  UI. Ustal na podstawie sugestii z `calibrate.py`.

- **`game_package`** — nazwa pakietu gry (dla Just Loot:
  `com.timhilgenstoehlerexpo.justlootapp`). Bez niej nie działa restart
  gry ani watchdogi. Można ją też wpisać w GUI.

Pełna lista opcjonalnych parametrów (z domyślnymi wartościami) jest
w `bot.py` w klasie `BotConfig` — np. `drag_duration_ms`,
`between_drags_delay`, `idle_click_interval`, a także:

- **`foreground_lost_timeout`** (domyślnie 15 s) — jeśli gra nie jest na
  pierwszym planie dłużej niż tyle sekund (np. wysypała się do pulpitu
  Androida), bot ją restartuje. `0` wyłącza. Aplikacje z
  `foreign_app_packages` (Google Play, przeglądarka) powodują restart
  natychmiast.
- **`drag_style`** (domyślnie `"human"`) — `"human"`: przeciąganie po
  losowej krzywej (łuk C/S, przyspieszanie i zwalnianie, drżenie, czasem
  lekkie przestrzelenie celu), kulka łapana i puszczana w losowym
  miejscu, losowa kolejność zbierania. `"straight"`: prosta linia
  środek–środek, kulki po kolei. Krzywa wymaga `input motionevent`
  (nowsze Androidy) — bot sam to sprawdza i przy starcie loguje, czy
  jest dostępne; bez niego tor jest prosty, ale losowy chwyt i
  kolejność zostają.
- **Minigry** — w trybie idle bot rozpoznaje też:
  - **Depth Collapse** (brązowe kafelki „TAP”): klika wszystkie kafelki
    w „ludzkiej” kolejności, z losowym punktem kliknięcia; nachodzące na
    siebie kafelki są klikane po kolei. Wyłączane `play_debris: false`,
    odstęp między kliknięciami `tile_tap_delay` (s).
  - **Fishing Hole** (przycisk CAST): od razu klika CAST, co pomija
    minigrę. Wyłączane `skip_fishing: false`.
- **Automatyczne otwieranie paczek** (`auto_packs`, domyślnie wyłączone)
  — gdy gear jest za słaby na wybraną głębokość (w Profile pod
  „CHOOSE DEPTH” gra pisze „enemies +N% stronger — find newer gear”
  zamiast „enemies at normal strength”) albo po `packs_defeat_streak`
  (domyślnie 5) porażkach z rzędu, bot idzie do Craft → Loot Packs,
  klika „+” do maksimum, przycisk z ceną, OPEN, przewija karty
  („Next Card” albo klik przy „TAP ANYWHERE TO CONTINUE”) i sprawdza Profile ponownie — aż wrogowie wrócą do
  normalnej siły, skończy się pył (brak okna potwierdzenia) albo minie
  `packs_max_rounds` rund. Profile jest sprawdzany co
  `packs_check_interval_min` minut. Pozycje zakładek (`nav_profile`,
  `nav_craft`, `craft_loot_packs_tab`) są względne (ułamek ekranu).
  Rozpoznawanie ekranów sprawdzisz na zrzucie: `python calibrate.py
  --image zrzut.png` wypisuje, co bot na nim widzi.
- **Zachowanie jak człowiek** (GUI: zakładka Tempo):
  - `reaction_delay` (domyślnie 0.4–1.8 s) — chwila na „zauważenie”
    nowej planszy / minigry przed pierwszym ruchem,
  - `tap_hold_ms` (50–150 ms) — tapnięcie trwa jak dotyk palcem i lekko
    się przesuwa (zamiast identycznego dotyku 0 ms),
  - `mistake_chance` (3%) — rzadka, niegroźna pomyłka: kulka puszczona
    tuż obok DROP albo kliknięcie obok kafelka; bot od razu poprawia,
  - opóźnienia (odstępy klikania, między przeciągnięciami, między
    kafelkami) są losowane częściej bliżej dolnej granicy zakresu,
    czasem dłużej — jak u człowieka — zamiast równomiernie.
- **Punkty do klikania** (GUI: zakładka Punkty) — punkt awaryjny,
  zakładki PROFILE / CRAFT / LOOT PACKS i miejsce kliknięcia karty bez
  przycisku. Każdy to obszar `(x, y, promień)` (ułamki ekranu), a bot
  klika w losowe miejsce wewnątrz koła. Ustawiasz je przyciskiem „Wskaż”
  i kliknięciem na podglądzie; obszary są wtedy narysowane na podglądzie.
  Dla domyślnych (niekalibrowanych) zakładek PROFILE / CRAFT wysokość
  jest brana od wykrytego przycisku JUST LOOT, więc trafiają przy
  dowolnych proporcjach ekranu.
- **Ustawienia wspólne** — w GUI zmiana ustawienia na jednej instancji
  ustawia je na wszystkich. Osobne są tylko nazwa, urządzenie ADB i
  harmonogram (okna, limit dzienny, zamykanie gry na przerwach).
- **`max_drag_attempts_per_ball`** (domyślnie 3) — po tylu nieudanych
  przeciągnięciach tej samej kulki bot uznaje ją za niezbieralną i
  pomija do końca bieżącego ekranu gry.

### Harmonogram: okna, sesje, przerwy i limit dzienny

Bez harmonogramu bot gra bez przerwy. W GUI ("📅 Edytuj harmonogram")
albo w `config.json` możesz ustawić:

- **Okna** — kiedy bot w ogóle może grać, np. 09:00–13:00 w dni
  robocze. Poza oknami jest offline.
- **Sesje w oknie** (osobno dla każdego okna) — bot gra przez losowy
  czas (`session_minutes`, np. 40–90 min), robi losową przerwę
  (`break_minutes`, np. 10–30 min) i tak do końca okna. Okno bez
  `session_minutes` = gra przez całe okno bez przerw.
- **Rozmycie granic** (`jitter_minutes`) — start okna przesuwa się
  losowo o 0..N minut później, a koniec o 0..N minut wcześniej, inaczej
  każdego dnia.
- **Limit dzienny** (`daily_limit_hours`) — losowany raz na dzień z
  zakresu; po jego wyczerpaniu bot jest offline do północy.

Rozpoczęta plansza jest zawsze dokończona, nawet gdy sesja się skończy.
Z `close_game_during_break: true` gra jest zamykana na każdą przerwę i
uruchamiana ponownie na start kolejnej sesji. Aktualny stan (np.
"sesja do 10:19", "przerwa do 10:37") widać na liście instancji w GUI.

```json
"schedule": [
  {
    "start": "09:00", "end": "13:00", "days": [0, 1, 2, 3, 4],
    "session_minutes": {"min": 40, "max": 90},
    "break_minutes": {"min": 10, "max": 30},
    "jitter_minutes": 15
  },
  {"start": "19:00", "end": "22:30", "days": null}
],
"daily_limit_hours": {"min": 5, "max": 6}
```

Licznik limitu dziennego jest trzymany w pamięci — po ponownym
uruchomieniu aplikacji liczy od zera.

## Krok 3: uruchomienie

### Opcja A: GUI (zalecane)

```bash
python main_window.py
```

Okno:

- **Pasek narzędzi** (u góry): wczytaj / zapisz konfigurację (Ctrl+O,
  Ctrl+S), wykryj emulatory z `adb devices`, dodaj / usuń instancję,
  start / stop wszystkich. Niezapisane zmiany oznacza gwiazdka w tytule,
  a przy zamykaniu okno pyta o zapis. Przy starcie wczytuje się
  `config.json` leżący obok programu.
- **Instancje** (po lewej): lista ze statusem w kolorze (czekanie,
  zbieranie, minigra, przerwa w harmonogramie…), a pod nią Start / Pauza /
  Stop / Restart gry dla zaznaczonej instancji.
- **Podgląd** (środek): obraz na żywo z zaznaczonym tym, co bot widzi
  (legenda nad obrazem). Przyciski pod spodem: wskazanie punktu
  awaryjnego klikiem, zaznaczenie obszaru planszy przeciągnięciem,
  „Cały ekran” (zalecane), odświeżenie podglądu, gdy bot stoi.
- **Ustawienia** (po prawej), w zakładkach z opisami pod polami:
  Ogólne (urządzenie, pakiet gry, klikanie JUST LOOT, obszar planszy),
  Tempo (zakresy losowania, ludzki ruch), Minigry, Paczki, Plan
  (harmonogram) i Ochrona (automatyczne restarty gry). Zmiany działają
  od razu, także na uruchomionym bocie. Pod zakładkami — statystyki.
- **Logi** (na dole): z przełącznikiem auto-przewijania i czyszczeniem.

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
python manager.py --auto --idle-x 436 --idle-y 1488
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
| `scheduler.py` | Harmonogram: okna, losowe sesje/przerwy, limit dzienny |
| `humanize.py` | Generowanie "ludzkich" torów przeciągania |
| `screens.py` | Rozpoznawanie ekranów Profile / Craft / kart / walki |
| `packs.py` | Automatyczne otwieranie paczek, gdy gear za słaby |
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
