# Cykl gry i minigry

Materiał: `material/2026-10-17/` (LDPlayer 1080x2320, nagrania z przycisku
⏺ i zrzuty z 📸). Skala interfejsu tej instancji: **1.28** (klasyfikator
wykrywa ją sam).

## Cykl

1. **Profile** (cel: MAIN, max depth) -> przytrzymanie JUST LOOT
2. **"Entering combat…"** -> walka (dziennik walki, np. FORGOTTEN CRYPT)
3. **Koniec walki:** "Victory." + przycisk **PROCEED** (stan `combat_done`)
4. **"?" - TAP ANYWHERE TO REVEAL** (stan `reveal`)
5. Losowo jedno z:
   - **popup przedmiotu** (Replace / Equip & Salvage / Salvage / Keep Current,
     stan `item_popup`); karta MAIN pokazuje zmianę siły (np. -28k, +2.5k)
   - **Dust Mine** (stan `dust_mine`) - przeciąganie kulek do DROP
   - **Fishing Hole** (stan `fishing`)
   - **Depth Collapse** (stan `depth_collapse`)
6. Powrót na Profile z toastami nagród ("+288 Dust", "+15 Shards", "+2 Temper")

## Fishing Hole - `dustbot2/minigames/fishing.py`

- Wskaźnik jeździ w tę i z powrotem ze **stałą prędkością ~0.99 px/ms**
  (przy szerokości 1080), przejazd przez pasek ~0.9 s.
- Strefa nie rusza się w trakcie rzutu (na nagraniu x 530-687, 157 px),
  wskaźnik jest w niej ~160 ms na przejazd.
- Po CAST wskaźnik **staje i robi się biały**, przycisk zmienia się na
  wypełniony "CAST!". Miejsce zatrzymania = gdzie gra przyjęła tap -> bot
  kalibruje z tego swoje opóźnienie (`LatencyCalibrator`).
- Bot nie reaguje na obraz, tylko dopasowuje model ruchu do wszystkich
  klatek rzutu i oblicza moment trafienia (`FishingTracker`, `plan_cast`).
- Dokładność na nagraniu (przewidywanie do 400 ms naprzód): mediana błędu
  12 ms, 100% w strefie. Do tego dochodzi rozrzut opóźnienia tapu -
  `minitouch` mały, `adb input tap` duży.
- `AimProfile`: rozrzut celowania w strefie i opcjonalne celowe pudła.

## Depth Collapse

- 7 brązowych kafelków "TAP" w losowych miejscach (częściowo na sobie),
  timer ~5.5 s, nagroda tylko za wyczyszczenie wszystkich.
- Plan: wykrywać kafelki po kolorze, klikać od wierzchniego.

## Otwarte decyzje

- Popup przedmiotu: którą opcję wybierać (np. Equip & Salvage, gdy MAIN
  ma plus, inaczej Salvage)?
- Komunikat o aktualizacji Google Play: zaznaczać "Don't show again"?
- Fishing najlepiej z `minitouch` - najpierw `python -m dustbot2.touch.check --demo`.
