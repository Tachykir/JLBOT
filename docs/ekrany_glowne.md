# Ekrany główne: rozpoznanie i powrót do celu

**Cel:** zakładka PROFILE, przewinięta do góry, wybrany build **MAIN**,
głębokość ustawiona na maksimum.

Pozycje podane jako ułamki ekranu (x, y) z zrzutów 932x2000 - orientacyjne.
Wszystko, co może się przesuwać, bot szuka szablonem, a nie stałym punktem.

## 1. Nakładki - zamknij najpierw (najwyższy priorytet)

| Nakładka | Jak rozpoznać | Jak zamknąć |
|---|---|---|
| Komunikat o aktualizacji przez Google Play | tekst "If game update via Google Play…", "Don't show again" | X w prawym górnym rogu (~0.94, 0.085) |
| Chat | napis "CHAT", zakładki GLOBAL/RAID/WHISPERS | X (~0.92, 0.035) |
| Settings | napis "Settings", sekcja LANGUAGE | "Close" (~0.89, 0.033) |
| Game Glossary | napis "GAME GLOSSARY" | przycisk CLOSE na dole (~0.5, 0.9) |
| Season Pass (panel od dołu) | napis "SEASON PASS", TRACK/SHOP | "Close" - pozycja zależy od panelu, szukać szablonem |
| Okienka statystyk (Armor, Health, Attack, Defense, Resist, Gear Level) | turkusowy tytuł + przycisk GOT IT | GOT IT - wysokość zależy od długości okienka, szukać szablonem |

## 2. Inne zakładki - wróć do PROFILE

Inventory, Craft (Loot Packs / Season Pass pod spodem), Raid, Arena,
Dungeons, Board (Progression), Collection.

- Rozpoznanie: aktywna zakładka w dolnym pasku ma turkusową ikonę i napis,
  pozostałe są szare. Wystarczy sprawdzić kolor w 8 polach paska.
- Akcja: tap w PROFILE (~0.10, 0.925).

## 3. PROFILE - doprowadź do celu

1. **Przewinięty w dół** (widać sloty Ring1/Main/Off, PET, TEST BUILD,
   FIGHT; nie widać kart buildów) -> przewiń do góry gestem w dół.
2. **Karty buildów** (MAIN, #2, #3, +; czasem przed nimi HUNT EVENT) -
   pozycja MAIN się zmienia. Szukać napisu "MAIN" w rzędzie kart.
   Wybrana karta ma turkusową ramkę - jeśli MAIN jej nie ma, tap w MAIN.
3. **Głębokość** (CHOOSE DEPTH, strzałki < i >): strzałka > przygaszona =
   maksimum. Jeśli jasna -> tap w > i sprawdź ponownie.
   Pozycja strzałek zależy od tego, czy jest baner "Hunt N is live" -
   szukać względem napisu "CHOOSE DEPTH".
4. **Nie klikać:** baner "Hunt N is live - tap here to join", JUST LOOT
   (chyba że to akcja bota), FIGHT, SHOP, przyciski w prawym górnym rogu.

## Otwarte pytania

- Czy w komunikacie o aktualizacji zaznaczać "Don't show again"?
- Zrzuty 932x2000 są z telefonu czy z LDPlayera? Szablony muszą pochodzić
  z tej samej proporcji ekranu, na której działa bot.
- Czy ponowny tap w PROFILE przewija ekran do góry?
