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
  `salvage_hold_ms` (GUI: zakładka Głębokość i paczki).
- **Nieznane ekrany** — gdy bot nie rozpoznaje ekranu, nic nie klika. Po
  `unknown_screen_after_s` (30 s) zapisuje jeden zrzut do folderu
  `unknown_screens` obok programu i pisze o tym w logu — prześlij taki
  zrzut, żeby dodać obsługę ekranu (`save_unknown_screens`, GUI: Ochrona).
- **`idle_point`** — punkt awaryjny: klikany na nieznanym ekranie tylko
  przy `idle_fallback_enabled: true` (domyślnie wyłączone) albo zawsze
  przy `idle_target: "fixed"` (GUI: zakładka Punkty).
- **`board_region`** — zwykle niepotrzebny (wykrywanie działa na całym
  ekranie); w GUI w zwijanej sekcji „Zaawansowane” zakładki Ogólne.
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
  Presety (`swipe_presets.py`) to osobne rodzaje gestu, wybierane jako styl:
  prosty skok, łuk w górę/w dół, litera S, przestrzelenie, szeroki łuk, nerwowy.
  Preset przeciąga kulkę własnymi wartościami (czas, punkty, kształt), a nie
  ustawieniami ręcznymi; suwaki tempa i tryb turbo nadal działają na czas.
- **`drag_grab_radius`** / **`drag_release_radius`** (domyślnie `0.4` i
  `0.3`) — obszar, z którego bot losuje punkt chwytu kulki i punkt
  puszczenia nad DROP, jako ułamek promienia kulki i DROP-a: `0` = zawsze
  środek, `1` = aż do brzegu. GUI: zakładka Ogólne, „Styl ruchu”
  (w procentach).
- **`drag_jitter_px`** (domyślnie `1.2`) — drżenie dłoni na torze
  przeciągnięcia: losowe odchylenie punktów toru w pikselach. `0` = palec
  idzie gładko. GUI: zakładka Tempo, „Styl ruchu” („Drżenie dłoni na torze”).
- **`drag_points`** (domyślnie brak = automatycznie) — zakres liczby
  punktów pośrednich toru jednego przeciągnięcia, np. `{"min": 8, "max": 12}`.
  Mało punktów = prosty, skokowy ruch; dużo = płynniejszy, dłuższy gest.
  GUI: „Punkty na torze kulki” (zaznacz „Ustaw liczbę punktów ręcznie”).
- **`just_loot_click_radius`** (domyślnie `0.45`) — obszar kliknięcia
  przycisku JUST LOOT, ten sam ułamek promienia przycisku. GUI: „Klikanie
  w trybie czekania”.
- **Minigry** — w trybie idle bot rozpoznaje też:
  - **Depth Collapse** (brązowe kafelki „TAP”): klika wszystkie kafelki
    w „ludzkiej” kolejności, z losowym punktem kliknięcia; nachodzące na
    siebie kafelki są klikane po kolei. Wyłączane `play_debris: false`,
    odstęp między kliknięciami `tile_tap_delay` (s; GUI: zakładka Minigry, w ms).
  - **Fishing Hole** (przycisk CAST): od razu klika CAST, co pomija
    minigrę. Wyłączane `skip_fishing: false`.
- **Automatyczne otwieranie paczek** (`auto_packs`, domyślnie wyłączone)
  — gdy gear jest za słaby na wybraną głębokość (w Profile pod
  „CHOOSE DEPTH” gra pisze „enemies +N% stronger — find newer gear”
  zamiast „enemies at normal strength”) albo po `packs_defeat_streak`
  (domyślnie 5) porażkach z rzędu, bot idzie do Craft → Loot Packs,
  ustawia „+” na maksimum 20 (odczytuje liczbę - gdy już jest 20, nie klika), przycisk z ceną, OPEN, przewija karty
  („Next Card” albo klik przy „TAP ANYWHERE TO CONTINUE”) i sprawdza Profile ponownie — aż wrogowie wrócą do
  normalnej siły, skończy się pył (brak okna potwierdzenia) albo minie
  `packs_max_rounds` rund. Profile jest sprawdzany co
  `packs_check_interval_min` minut. Pozycje zakładek (`nav_profile`,
  `nav_craft`, `craft_loot_packs_tab`) są względne (ułamek ekranu).
  Rozpoznawanie ekranów sprawdzisz na zrzucie: `python calibrate.py
  --image zrzut.png` wypisuje, co bot na nim widzi.
  Karta z paczki, która zostanie na ekranie (np. przerwane otwieranie
  albo paczki otwarte ręcznie), jest rozpoznawana także w zwykłym
  trybie — bot klika obok karty, aż wróci ekran główny.
- **Farm na głębokości** (GUI: zakładka „Głębokość i paczki”, `farm_depth`,
  0 = nie zmieniaj; osobno dla każdej instancji) — bot wchodzi w Profile →
  „CHOOSE DEPTH” i ustawia podaną głębokość: do 79 klika kafelek z
  numerem (albo strzałkę, gdy numeru nie widać), od 80 klika strzałki
  ‹ › co 1. Numer odczytuje z ekranu (wzory cyfr czcionki gry Kode Mono,
  licencja SIL OFL, w `digit_templates.py`). Sprawdza od razu po zmianie
  ustawienia i potem co `depth_check_interval_min` minut (domyślnie 15),
  zawsze tylko na ekranie z paskiem nawigacji (nie w trakcie walki).
  Gdy głębokość nie jest odblokowana, zostaje najbliższa możliwa.
  Tryb `max_pct` — maksymalna odblokowana minus `depth_below_max_pct`
  procent (np. 362 i 10% → 325); maksimum bot sprawdza raz na godzinę.
  Na karcie „Wszystkie instancje” pole „Głębokość na wszystkich” ustawia
  tryb (i liczbę) na każdej instancji naraz.
  Tryb (`depth_mode`): `fixed` — wpisana głębokość; `max` — maksymalna
  odblokowana (bot klika „›”, aż przestanie się dać albo strzałka
  zgaśnie); `max_normal` — najwyższa, na której gra pisze „enemies at
  normal strength” (od obecnej w górę po 1, aż pojawi się bonus wrogów,
  wtedy krok wstecz). Tryby automatyczne same podnoszą głębokość przy
  kolejnych sprawdzeniach. Uwaga: przy `max_normal` sprawdzanie Profile
  nie otworzy paczek (wrogowie nigdy nie mają bonusu) — paczki otworzą
  się po serii porażek.
- **Fishing Hole — bez fałszywych trafień**: CAST musi mieć zamkniętą
  ramkę z krótkim napisem na środku i wskaźnik w torze nad sobą, ekran
  nie może mieć paska nawigacji (JUST LOOT), a minigra musi być widoczna
  na dwóch klatkach z rzędu.
- **Pusty ekran = zawieszona gra** — ciemny ekran bez treści (np. został
  sam dymek „+1 RAID XP”) jest traktowany jak czarny ekran: po
  `black_screen_timeout` (domyślnie 20 s) bot restartuje grę. Zakryta
  karta „?” i karty z paczek się nie liczą. Wymaga pakietu gry.
- **Rozbieranie stosów kopii** — przycisk „+x DUST” bot znajduje po
  jaśniejszym pionowym pasku karty, więc działa też przy stosach kopii
  legendary/mythic („MYTHIC 2× · …”), gdzie karty są węższe, a przycisk
  jest na środku ekranu.
- **Wydajność** (GUI: zakładka Tempo → Wydajność, `processing_width`,
  domyślnie 540) — zrzut ekranu jest przed analizą pomniejszany do tej
  szerokości (1080×1920 → 540×960): ok. 3× mniej CPU na klatkę, a
  rozpoznawanie działa tak samo (sprawdzone na wszystkich zrzutach).
  Kliknięcia są przeliczane na pełną rozdzielczość; `idle_point` i
  `board_region` zostają w pikselach urządzenia. 0 = pełna rozdzielczość.
  OpenCV działa w jednym wątku na instancję, a podgląd w GUI odświeża się
  tylko na widocznej karcie (szczegóły ~5×/s, miniatury 2×/s).
  Najwięcej daje jednak sam emulator: niższa rozdzielczość (np. 540×960)
  i FPS (np. 30) w ustawieniach LDPlayera.
- **Fishing Hole z celowaniem** (`fishing_aim`, domyślnie włączone; GUI:
  Minigry → Rzut) — `fishing.py` (przeniesione z gałęzi dustbot2) wykrywa
  wskaźnik, pasek i jasną strefę, dopasowuje ruch tam i z powrotem ze stałą
  prędkością do wszystkich klatek rzutu i naciska CAST tak, żeby wskaźnik
  stanął na środku strefy. Po rzucie odczytuje, gdzie stanął, i poprawia
  opóźnienie (`fishing_latency_ms`, start 150 ms). W logu każdy rzut: cel,
  miejsce zatrzymania, trafienie/pudło, błąd w ms. Statystyki: „Fishing
  trafione” / „Fishing rzuty”.
- **Suwaki szybkości** (Tempo → Szybkość; `speed_board_pct`,
  `speed_react_pct`, `speed_tap_pct`, 25–400 %, domyślnie 100) — mnożą czasy
  swojej grupy, także w turbo: plansza (przeciąganie, przytrzymanie na DROP,
  przerwy między kulkami), reakcja i przejścia (czas reakcji, kafelki minigier,
  pozostałe przerwy), tapanie w walce. 200 % = czasy o połowę krótsze.
  Przeciągnięcie (150 ms) i przytrzymanie na DROP (80 ms) mają minimum, przy
  którym kulka jeszcze trafia. Czasy samej gry się nie zmieniają.
- **Restart po nieznanym ekranie** (`restart_on_unknown`, domyślnie włączony;
  Ochrona → Nieznane ekrany) — po `unknown_screen_after_s` na nierozpoznanym
  ekranie bot zapisuje zrzut i restartuje grę.
- **Obraz przez scrcpy** (`capture_method`, domyślnie `"scrcpy"`; GUI:
  Tempo → Wydajność → „Obraz z emulatora”) — zamiast `screencap` przy
  każdej klatce bot uruchamia na emulatorze serwer scrcpy 3.1
  (`vendor/scrcpy/scrcpy-server`, Apache 2.0), który wysyła ekran jako
  wideo H.264 już zmniejszone do `processing_width`. Dekoder (PyAV, `av`
  w requirements) trzyma w tle najnowszą klatkę, więc zrzut trwa kilka
  ms zamiast 150–400 ms. Gra działa w swojej normalnej rozdzielczości.
  `stream_fps` (domyślnie 30) ogranicza liczbę klatek. Przy błędzie
  strumienia (brak `av`, serwer nie wstał, zerwane połączenie) bot sam
  wraca do `screencap` i próbuje ponownie po 30 s. Metoda i czas zrzutu
  są widoczne w linii wydajności na karcie instancji.
- **Dokładny status** — zamiast ogólnego „czekanie / zbieranie” lista
  instancji i kafelki w „Wszystkie instancje” pokazują, co bot robi w tej
  chwili, np. „Walka na głębokości 1137 – czekam na wynik”, „Ekran dropu –
  wybieram „Keep Current””, „Dust Mine – przeciągam kulkę do DROP (na
  planszy 4, przeciągnięcie 2)”, „Profile – ustawiam głębokość: 1100 →
  990”, „Paczki – przeklikuję karty (45)”, „Inventory – rozbieram
  przedmioty (3/10)”, „Pusty ekran – restart gry za 12 s”, „Harmonogram:
  przerwa do 14:30”.
- **Czat** — gdy otworzy się okno czatu („CHAT”, zakładki GLOBAL / RAID /
  WHISPERS), bot zamyka je przyciskiem X w prawym górnym rogu.
- **Paczki od razu po ostrzeżeniu** — przy włączonym „Otwieraj paczki,
  gdy gear jest za słaby” bot sprawdza ostrzeżenie „enemies +N% stronger”
  na każdym ekranie Profile (co ~5 s), a nie tylko co
  `packs_check_interval_min`. Gdy zabraknie pyłu, kolejna próba po
  `packs_check_interval_min`; po udanych rundach - po minucie.
- **Gear / Depth / Dust na podglądzie wszystkich** — kafelek instancji
  pokazuje ostatnie odczyty z gry: GEAR LEVEL (statystyki na Profile),
  głębokość (nagłówek walki albo wybór głębokości na Profile) i pył
  (nagłówek ekranu Craft - odczytywany, gdy bot jest w Craft, np. przy
  otwieraniu paczek). „?” = jeszcze nie odczytano.
- **Podgląd: co bot widzi i robi** (karta szczegółów) — na pasku u góry
  opis bieżącej czynności, a na obrazie ostatnie akcje (znikają po ~2.5 s):
  żółte ramki = element, który bot znalazł i klika, pomarańczowe kółka =
  kliknięcia, błękitne linie ze strzałką = przeciągnięcia / przewijanie.
  Podgląd odświeża się także w menu gry (paczki, Profile, Inventory - status
  „menu gry”). Bot tylko zapisuje współrzędne akcji; rysuje GUI (najwyżej
  ~5 razy na sekundę, tylko na widocznej karcie), więc nie spowalnia bota.
- **Settings** — gdy otworzy się ekran ustawień gry (Music / Sound effects
  / Auto Salvage), bot klika „Close” w prawym górnym rogu. Wiersze Auto
  Salvage (COMMON / RARE / ...) nie są brane za panel wyboru dropu.
- **Motyw jasny / ciemny** — lista „🌓 Motyw” na pasku narzędzi:
  Systemowy / Jasny / Ciemny. Wybór jest zapamiętywany (ustawienie
  programu, nie config.json) i przywracany przy następnym uruchomieniu.
- **Tryb turbo** (`turbo`, GUI: zakładka Tempo „⚡ Tryb turbo” albo przyciski
  „⚡ Turbo” / „🐢 Normalne tempo” w „Wszystkie instancje”) — maksymalna
  szybkość: czas reakcji ~0, przeciągnięcie kulki 0.2–0.28 s, przerwy między
  przeciągnięciami 0.02–0.06 s, tapnięcia 10–25 ms, bez celowych pomyłek,
  pozostałe stałe przerwy ~3× krótsze (czekanie na ekrany i tak reaguje od
  razu). Nie zmienia czekania po restarcie gry ani przerw harmonogramu.
  Ustawienia z zakładki Tempo zostają zapisane i wracają po wyłączeniu.
  Turbo celowo rezygnuje z „ludzkiego” rytmu.
- **Zwiększanie głębokości po walce** (`depth_push_taps`, GUI: „Głębokość i
  paczki” → „Po każdej walce zwiększ głębokość”, 0 = wyłączone) — po każdej
  walce, przed kliknięciem JUST LOOT, bot klika szybko N razy strzałkę „›”
  przy wyborze głębokości. Na maks. odblokowanej głębokości strzałka jest
  przygaszona i bot jej nie klika. Nie działa w trybach „stała głębokość”
  (> 0) i „maks. − X%”, bo one same ustawiają konkretną głębokość.
- **Nowe ekrany gry (październik)** — DROP na planszy Dust Mine leży w
  losowym miejscu (bot szuka go w samym panelu planszy i nie używa pozycji
  z poprzedniej planszy); panel dropu z wyróżnionym, wyższym wierszem
  „Equip & Salvage current item”; ekran startowy (żółte logo), „Loading your
  save…” i czarny ekran = bot czeka; „Create your account” = bot nic nie
  klika (ostrzeżenie w logu); przygaszony przycisk „JUST HUNT” (wydarzenie
  HUNT bez walk) = bot wybiera loadout `loadout_name` (domyślnie MAIN).
- **Log akcji (CSV)** (`action_log`, GUI: strona „🧾 Log akcji” → „Zapisuj log
  akcji”) — każde tapnięcie, przeciągnięcie, swipe i przytrzymanie bota trafia
  do `action_logs/RRRR-MM-DD_<instancja>.csv` obok programu. Kolumny: `time`,
  `t_ms` (czas monotoniczny), `instance`, `state` (idle / collecting / debris /
  fishing / menu), `activity` (opis czynności jak w GUI), `action` (tap / hold /
  swipe / drag), `x`,`y`,`end_x`,`end_y` (piksele urządzenia), `points`, `path`
  (cały tor „x:y|x:y|…”), `duration_ms`, `hold_ms`, `turbo`, `drag_style`.
  Strona pokazuje ostatnie 300 wpisów wybranej instancji (odświeżane co 2 s).
- **Szybsze akcje i pomiar wydajności** — tapnięcia, swipe'y i przeciągnięcia
  idą przez jedną stale otwartą sesję `adb shell` zamiast osobnego procesu
  adb.exe na każde kliknięcie (przy błędzie sesji bot na 60 s wraca do
  osobnych wywołań). Strona Instancje (pod statystykami) i kafelki pokazują
  średni czas zrzutu ekranu, narzut jednej akcji i liczbę zrzutów na sekundę.
  Największy koszt to zwykle sam zrzut: przy ekranie 1080×1920 to ~8 MB na
  klatkę, przy 540×960 ~2 MB - ustaw w LDPlayerze 540×960 (bot i tak pracuje
  na 540 px).
- **Interfejs 1.3** — boczna nawigacja (Przegląd / Instancje / Log akcji),
  kafle z podsumowaniem wszystkich instancji, kafelki instancji z plakietką
  stanu i siatką liczb, rzadsze akcje dla wszystkich w menu „Więcej”, wspólny
  wygląd jasny / ciemny (motyw „Systemowy” bierze go z ustawień systemu).
- **Czas gry** na kafelku instancji — czas realnej gry od startu bota,
  bez przerw z harmonogramu (przerwy, poza oknem, limit dzienny) i bez
  pauzy.
- **Paczki według Gear Level** (`gear_depth_margin`, domyślnie 55, GUI:
  „Paczki, gdy głębokość ≥ Gear Level +”, 0 = wyłączone) — bot odczytuje
  GEAR LEVEL ze statystyk na ekranie Profile (2 zgodne odczyty) i porównuje
  z głębokością z nagłówka walki. Gdy głębokość ≥ Gear Level + margines,
  idzie otwierać paczki — nie potrzebuje linii „enemies +N% stronger”,
  którą po walce zasłaniają dymki „+Dust” / „+Raid XP”.
- **Liczba paczek za jednym razem** (`packs_per_run`, GUI: „Otwieraj za
  jednym razem”) — 0 = rundy po maks. 20 paczek, aż gear wystarczy
  (`packs_max_rounds`); N > 0 = otwiera dokładnie N paczek (zakupy po
  maks. 20) i wraca do walki.
- **Głębokość z ekranu walki** — bot czyta „Depth N” z nagłówka każdej
  walki. Gdy walka jest na innej głębokości niż ustawiona (stała /
  maks. − X%), na głębokości 1 (loadout), albo w trybach maksymalnych
  `depth_repeat_fights` (domyślnie 3) walk z rzędu jest na tej samej
  głębokości, od razu sprawdza / podkręca wybór w Profile (nie czeka na
  `depth_check_interval_min`). Przed odczytem Profile czeka, aż znikną
  dymki z nagrodą po walce („+24 Dust”), które zasłaniają wybór głębokości.
- **Loadout przy głębokości 1** (`loadout_name`, domyślnie `MAIN`, GUI:
  „Głębokość i paczki”) — gdy przy sprawdzaniu głębokości wybrana jest
  głębokość 1 (np. gra przełączyła loadout na HUNT), bot zawsze przewija Profile
  do kart loadoutów, odczytuje ich nazwy i wybiera ten o podanej nazwie,
  a potem ustawia głębokość wg trybu. Sprawdzanie odbywa się co
  `depth_check_interval_min` minut, także gdy tryb głębokości to „nie
  zmieniaj”. Pusta nazwa = wyłączone.
- **Dociąganie kulki do środka DROP** — gra przesuwa kulkę za palcem z
  opóźnieniem, więc puszczenie palca od razu po dojechaniu upuszczało ją
  przed środkiem. Teraz palec stoi na DROP `drag_end_hold_ms` (domyślnie
  180–350 ms, GUI: Tempo → „Przytrzymanie na DROP”) z drobnym drżeniem, a
  tor ma więcej punktów na dłuższej drodze (co najwyżej ~1.5 promienia
  kulki między punktami). Bez `input motionevent` (prosty swipe) bot
  przeciąga 1.5× wolniej i celuje odrobinę za środek. Celowa „ludzka”
  pomyłka (`mistake_chance`) jest teraz widoczna w logu.
- **Licznik „N left”** — bot czyta z nagłówka planszy, ile kulek zostało;
  nie kończy planszy, dopóki gra pokazuje, że coś zostało (czeka ~3 s,
  ponawia pominięte kulki, potem zapisuje zrzut do diagnozy).
- **Zasłonięty DROP** — gdy kulki zasłaniają prawie cały okrąg DROP, a
  bot nie zna jeszcze jego położenia (np. zaraz po starcie), szuka go tam,
  gdzie zawsze leży na planszy: na środku, na ~79% wysokości panelu planszy.
- **Otwórz X paczek** (karta „Wszystkie instancje” → „📦 Otwórz paczki na
  wszystkich”) — pyta o liczbę (1–1000) i na każdej działającej
  instancji przy najbliższym ekranie z paskiem nawigacji: Craft → Loot
  Packs, „+” do podanej liczby (odczytuje liczbę między „−” i „+”),
  cena, OPEN, karty, „Done”. Gdy gra ograniczy jeden zakup, kupuje
  kolejne partie; gdy zabraknie pyłu, kończy na tym, co się dało.
- **Zakładka i paczka w Craft** — przed zakupem bot sprawdza po napisach,
  czy wybrana jest zakładka LOOT PACKS (jeśli nie - klika ją) i paczka
  SEEKER (nazwa nad „Loot Pack”, kropki pod kartą; jeśli nie - przewija
  karuzelę do pierwszej paczki). Gdy SEEKER nie da się wybrać, nie kupuje.
  Po otwieraniu paczek (automatycznym i „Otwórz X paczek”) wraca na
  zakładkę Profile.
- **„Not Enough Dust”** — okno z samym „OK” (zamiast CANCEL / OPEN) po
  kliknięciu ceny: bot klika OK i kończy otwieranie paczek (automatyczne
  i „Otwórz X paczek”). Takie okno z samym OK zamyka też w zwykłym trybie.
- **Ekran „Pack Opened”** — przycisk „Done” po kartach z paczki jest
  klikany w procedurze paczek i w zwykłym trybie.
- **Szybkie klikanie kart** — seria kliknięć „+” i przeklikiwanie kart z
  paczek idą jednym poleceniem ADB (2–3 kliknięcia na zrzut ekranu);
  karty z dropu po walce („?” i „TAP ANYWHERE TO CONTINUE”) oraz panel
  wyboru dropu (Keep Current) bot klika co ~0.15–0.45 s bez pełnego
  wykrywania planszy; po wyborze na panelu czeka min. 1.2 s, zanim
  kliknie go ponownie.
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

Cały plan (sesje, przerwy, rozmycie granic i limit dzienny) losuje się z
**ziarna** `schedule_seed` (domyślnie 1). Ten sam numer daje ten sam plan;
przycisk „🎲 Losuj ponownie” w edytorze zmienia ziarno. Edytor pokazuje
też **podgląd dnia** (od dziś na tydzień do przodu): pasek doby z sesjami
(zielony), przerwami (żółty), czasem poza oknami (szary) i wyczerpanym
limitem (czerwony) - dokładnie to, co bot ułoży z danym ziarnem.

Licznik limitu dziennego jest trzymany w pamięci — po ponownym
uruchomieniu aplikacji liczy od zera.

### Język interfejsu

Domyślnie GUI jest po polsku. Wybór „English” na pasku narzędzi (obok motywu)
przełącza etykiety, przyciski, podpowiedzi i zakładki na angielski - zmiana
działa po ponownym uruchomieniu programu. Tłumaczenia są w pliku
`translations_en.json` (polski tekst → angielski); tekst bez tłumaczenia
zostaje po polsku. Po polsku pozostają komunikaty okien z pytaniami oraz
wpisy logu i opisy działań bota.

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
- **Karta „Wszystkie instancje”** (startowa): kafelek dla każdej
  instancji — podgląd na żywo, status, statystyki i przyciski Start /
  Pauza / Stop / Rozbierz / Szczegóły — oraz pasek akcji dla wszystkich
  naraz: start, pauza, wznowienie, stop, rozebranie przedmiotów, restart
  gry. Na górze podsumowanie (ile działa, łączne statystyki).
- **Karta „Szczegóły instancji”** — widok opisany niżej.
- **Wymuszone rozbieranie** („🧹 Rozbierz teraz” / „Rozbierz na
  wszystkich”) — pyta o liczbę przedmiotów (1–1000) i rozbiera je z góry
  listy Inventory przy najbliższym ekranie z paskiem nawigacji, niezależnie
  od ustawienia „Pełny ekwipunek”. Działa na uruchomionych instancjach.
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
| `human_motion.py` | Generowanie "ludzkich" torów przeciągania |
| `screens.py` | Rozpoznawanie ekranów Profile / Craft / kart / walki |
| `packs.py` | Automatyczne otwieranie paczek, gdy gear za słaby |
| `manager.py` | CLI: uruchamianie wielu instancji bota równolegle (multiprocessing) |
| `main_window.py` | GUI: główne okno aplikacji (uruchom to, żeby odpalić GUI) |
| `dashboard.py` | GUI: przegląd i sterowanie wszystkimi instancjami |
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
