# Just Loot – RUN 10 MORE

Odchudzony fork Dust Mine Bota (LDPlayer przez ADB, działa w tle). Bot:

1. gdy na ekranie głównym pojawi się przycisk **RUN 10 MORE** (arena
   skończyła 10 walk), klika go,
2. po kliknięciu **Sprawdź teraz** w GUI wchodzi na **leaderboard** (ikona wykresu na dole),
   odczytuje LP swoje i **MossG**, wypisuje, ile brakuje, i wraca do
   **Profile** (pierwszy przycisk na dole). Historia trafia do
   `leaderboard.csv`.

- Przycisk jest wykrywany na obrazie (wzorzec `templates/run10more.png`,
  działa w każdej rozdzielczości). Bot nie klika, gdy przycisku nie ma ani
  gdy jest przyciemniony pod otwartym menu.
- Każde kliknięcie trafia w losowy punkt przycisku (gęściej przy środku,
  z dala od ramki), nigdy w ten sam punkt co poprzednio. Przerwy też są losowe.

## Uruchomienie

```bash
pip install -r requirements.txt
```

**Z oknem:** `start.bat` (najpierw robi `git pull` i doinstalowuje biblioteki, potem otwiera okno) albo:

```bash
python gui.py
```

Wybierz urządzenie z listy (to, co pokazuje `adb devices`, np.
`emulator-5566`) i kliknij **Start**. W oknie widać:

- **Ekran**: podgląd z emulatora, zielona ramka = wykryty RUN 10 MORE
- **Leaderboard**: ile brakuje do MossG, LP obu graczy, kiedy sprawdzono
  (i za ile następne, jeśli włączone automatyczne), oraz bilans dystansu **od startu** (od pierwszego
  odczytu po kliknięciu Start) i od ostatniego sprawdzenia, np.
  „od startu −308 (ostatnio +141)”. + = dystans zmalał (podbiłem), − = urósł:
  było 800, jest 790 → +10. Kolor (zielony / czerwony) według bilansu od startu.
  „Sprawdź teraz” sprawdza od razu
- **Mapy kliknięć**: RUN 10 MORE, Leaderboard i Profile - wszystkie
  kliknięcia na przycisku (mapa ciepła + kropki; czerwona = ostatnie,
  białe = 10 ostatnich). Zapisywane w `clicks.json`, „Wyczyść” je zeruje.
- **licznik kliknięć wysłanych do gry**: w tej sesji (od Start) i łącznie (z `clicks.json`), z podziałem na RUN 10 MORE / Leaderboard / Profile; po Stop bot wypisuje w logu, ile kliknięć wysłał
- wynik dopasowania i log
- **⏱ Harmonogram** (pod paskiem urządzenia), potem **Ustaw**:
  - **Start od** `8:00` + **Koniec** `23:15` (albo `90` / `1h30` = tyle po
    starcie): codzienne okno. Przed startem i po końcu gra jest **zamknięta**,
    w oknie bot ją **włącza**, jeśli jest wyłączona (sprawdza co minutę), i
    klika. Po końcu okno przesuwa się na następny dzień. Okno może iść przez
    północ (22:00–2:00).
  - sam **Start od**: gra wyłączona do tej godziny, potem działa bez końca.
  - sam **Koniec** (`90`, `1h30`, `23:15`): minutnik - o tej porze bot
    zamyka grę i kończy.
  Ustawia się przed Startem albo w trakcie; **Anuluj** wyłącza. Pod spodem
  widać stan, np. „okno 08:00–23:15 · gra wyłączona, start za 1:12:05”.

**Bez okna** (wszystkie instancje z `config.json`, a bez configu wszystkie z `adb devices`):

```bash
python arena_bot.py
```

```bash
python arena_bot.py --serial emulator-5566
```

Harmonogram bez okna: `--start-at 8:00 --stop-after 23:15` (codziennie), sam `--stop-after 90` = minutnik.

## config.json (opcjonalny)

```bash
copy config.example.json config.json
```

- `match_threshold` (0.85) – jak pewne musi być wykrycie przycisku (0–1)
- `poll_interval` ([1.0, 2.5] s) – co ile sprawdzać ekran
- `after_click_wait` ([1.5, 3.0] s) – przerwa po kliknięciu
- `reclick_after_s` (20 s) – po kliknięciu RUN 10 MORE bot nie klika go ponownie przez tyle sekund, nawet jeśli przycisk jeszcze widać
- **naturalne odstępy** (sekundy `[min, mediana, max]`, rozkład log-normalny jak
  czas reakcji człowieka): `reaction_s` ([0.4, 1.1, 6]) – zanim kliknie RUN 10
  MORE, `distraction_chance` (0.07) + `distraction_s` ([4, 9, 40]) – czasem
  dłuższe „zagapienie się”, `nav_reaction_s` ([0.3, 0.8, 3]) – przed ikoną
  leaderboardu, `look_leaderboard_s` ([1.5, 3.5, 10]) – ile „patrzy” na ranking
  przed powrotem do Profile, `tap_hold_ms` ([45, 130]) – jak długo palec
  dotyka ekranu (tapnięcie z drobnym, 0–2 px ruchem)
- `click_margin_x`, `click_margin_y` (0.06, 0.20) – margines od krawędzi przycisku
- `leaderboard_every_min` ([0, 0]) – leaderboard tylko przyciskiem „Sprawdź teraz”; np. `[5, 60]` = dodatkowo automatycznie co losowe 5–60 min
- `nav_leaderboard`, `nav_profile` – obszary ikon na pasku (ułamki ekranu `[x1, y1, x2, y2]`)

## Jak odczytywane są LP

Gra używa czcionki **Chakra Petch** (SIL OFL, `templates/ChakraPetch-OFL.txt`).
Wzory cyfr i liter w `templates/digits.npz` są wyrenderowane z tej czcionki
(4 grubości, różne rozmiary) i uzupełnione próbkami z prawdziwych zrzutów.
- moje LP: liczba w lewej kolumnie przy karcie z fioletową ramką,
- LP MossG: liczba w lewej kolumnie przy karcie z napisem „MossG”
  (szukany wzorcem `templates/mossg.png`, na jasnej i ciemnej karcie).
Gdy cyfra jest niepewna, bot nie zgaduje - wpisuje „?”.
