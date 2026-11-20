# Just Loot – RUN 10 MORE

Odchudzony fork Dust Mine Bota (LDPlayer przez ADB, działa w tle). Bot
robi jedną rzecz: gdy na ekranie głównym pojawi się przycisk
**RUN 10 MORE** (arena skończyła 10 walk), klika go.

- Przycisk jest wykrywany na obrazie (wzorzec `templates/run10more.png`,
  działa w każdej rozdzielczości). Bot nie klika, gdy przycisku nie ma ani
  gdy jest przyciemniony pod otwartym menu.
- Każde kliknięcie trafia w losowy punkt przycisku (gęściej przy środku,
  z dala od ramki), nigdy w ten sam punkt co poprzednio. Przerwy też są losowe.

## Uruchomienie

```bash
pip install -r requirements.txt
```

**Z oknem:** `start.bat` albo:

```bash
python gui.py
```

Wybierz urządzenie z listy (to, co pokazuje `adb devices`, np.
`emulator-5566`) i kliknij **Start**. W oknie widać:

- **Ekran**: podgląd z emulatora, zielona ramka = wykryty RUN 10 MORE
- **Mapa kliknięć**: wszystkie kliknięcia na przycisku (mapa ciepła +
  kropki; czerwona = ostatnie, białe = 10 ostatnich). Zapisywana w
  `clicks.json`, przycisk „Wyczyść” ją zeruje.
- licznik kliknięć, wynik dopasowania i log

**Bez okna** (wszystkie instancje z `config.json`, a bez configu wszystkie z `adb devices`):

```bash
python arena_bot.py
```

```bash
python arena_bot.py --serial emulator-5566
```

## config.json (opcjonalny)

```bash
copy config.example.json config.json
```

- `match_threshold` (0.85) – jak pewne musi być wykrycie przycisku (0–1)
- `poll_interval` ([1.0, 2.5] s) – co ile sprawdzać ekran
- `after_click_wait` ([1.5, 3.0] s) – przerwa po kliknięciu
- `click_margin_x`, `click_margin_y` (0.06, 0.20) – margines od krawędzi przycisku
