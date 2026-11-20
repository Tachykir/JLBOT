# Just Loot – Arena Bot

Odchudzony fork Dust Mine Bota: zostaje tylko jedna pętla areny
(LDPlayer przez ADB, działa w tle).

## Pętla

1. Klik **„…”** (prawy dolny róg paska nawigacji)
2. Klik **Arena** w menu
3. Przewinięcie w dół o ok. **1,5 ekranu** (kilka losowych swipe'ów)
4. Klik **FIND MATCH** – przycisk jest wykrywany na obrazie (jedyny szeroki,
   wypełniony biały przycisk); gdy go nie widać, bot lekko przewija i
   próbuje ponownie, a na końcu klika obszar zapasowy
5. **Restart aplikacji**, czekanie aż gra wstanie (widać koło JUST LOOT), od nowa

Każde kliknięcie to losowy punkt w obszarze przycisku (nigdy ten sam punkt
dwa razy z rzędu), przerwy, miejsca i długości swipe'ów też są losowe.

## Uruchomienie

```bash
pip install -r requirements.txt
```

```bash
copy config.example.json config.json
```

```bash
python arena_bot.py
```

Opcje:

- `--serial 127.0.0.1:5555` – tylko jedna instancja
- `--loops 5` – tylko 5 pętli (domyślnie bez końca, Ctrl+C kończy)
- `--no-initial-restart` – nie restartuj gry przed pierwszą pętlą
- `--adb ŚCIEŻKA` – własny adb (domyślnie `vendor/adb/adb.exe` na Windowsie)
- `-v` – więcej logów

Na Windowsie można też odpalić `start.bat`.

## config.json

Pola na najwyższym poziomie są domyślne dla wszystkich instancji, pola w
`instances` je nadpisują. Wszystkie opcje (z wartościami domyślnymi) są w
klasie `Config` w `arena_bot.py`, m.in.:

- `more_button`, `arena_menu_item`, `find_match_fallback` – obszary kliknięć
  jako ułamki ekranu `[x1, y1, x2, y2]` (działają w każdej rozdzielczości 9:16)
- `scroll_screens` (1.5), `scroll_jitter` (0.15) – ile przewinąć
- `delay_after_*`, `delay_between_loops` – losowe przerwy `[od, do]` w sekundach
- `startup_min_s`, `startup_timeout_s` – czekanie na grę po restarcie
