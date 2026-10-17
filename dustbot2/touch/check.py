"""
Sprawdza minitouch na emulatorze, zanim włączysz go w bocie.

    python -m dustbot2.touch.check --serial emulator-5554
    python -m dustbot2.touch.check --serial emulator-5554 --demo

Bez --demo: wgrywa i uruchamia minitouch, wypisuje zakres dotyku i
rozmiar ekranu, po czym kończy. Niczego nie dotyka.

Z --demo: włącza systemowe "Położenie wskaźnika" (ślady dotyku na
ekranie), robi tap w środku ekranu i po jednym geście każdego kształtu,
a po naciśnięciu Enter wyłącza ślady. UWAGA: gesty trafiają w to, co
akurat jest na ekranie - otwórz najpierw ekran główny Androida, nie grę.

Jeśli ślady są w złym miejscu (np. odbite albo obrócone), osie dotyku nie
pasują do ekranu - wtedy nie włączaj minitouch w bocie i daj znać.
"""

from __future__ import annotations

import argparse
import logging
import random
import sys
import time

from adb_controller import ADBController, ADBError
from dustbot2.touch.humanize import SHAPES, plan_drag, to_commands
from dustbot2.touch.minitouch import MinitouchSession, TouchError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--serial", required=True)
    parser.add_argument("--demo", action="store_true", help="wykonaj tap i gesty z włączonymi śladami dotyku")
    parser.add_argument("--no-su", action="store_true", help="uruchom minitouch bez roota")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    try:
        adb = ADBController(serial=args.serial)
    except ADBError as e:
        print(f"Błąd połączenia: {e}", file=sys.stderr)
        return 1

    session = MinitouchSession(adb, use_su=not args.no_su)
    try:
        client = session.start()
    except (TouchError, ADBError) as e:
        print(f"\nminitouch NIE działa: {e}", file=sys.stderr)
        return 1

    try:
        b, s = client.banner, client.screen
        print(f"\nminitouch działa. Protokół v{b.version}, kontakty: {b.max_contacts}")
        print(f"Zakres dotyku: {b.max_x} x {b.max_y}, nacisk: 0-{b.max_pressure}")
        print(f"Ekran: {s.width} x {s.height}")
        if not args.demo:
            print("\nAby zobaczyć gesty na ekranie: dodaj --demo (najpierw otwórz ekran główny Androida).")
            return 0

        adb.shell("settings", "put", "system", "pointer_location", "1")
        try:
            time.sleep(1.0)
            cx, cy = s.width // 2, s.height // 2
            print(f"\nTap w środku ekranu ({cx}, {cy})")
            client.tap(cx, cy)
            time.sleep(0.8)
            rng = random.Random()
            start = (int(s.width * 0.3), int(s.height * 0.75))
            end = (int(s.width * 0.7), int(s.height * 0.3))
            for shape in SHAPES:
                plan = plan_drag(start, end, rng=rng, shape=shape)
                print(f"Gest {plan.shape:9s} {plan.duration_ms:5.0f} ms, {len(plan.steps)} punktów")
                client.send(to_commands(plan.steps, client))
                time.sleep(plan.duration_ms / 1000 + 0.6)
            input("\nSprawdź ślady na ekranie i naciśnij Enter, żeby je wyłączyć...")
        finally:
            adb.shell("settings", "put", "system", "pointer_location", "0")
        return 0
    finally:
        session.stop()


if __name__ == "__main__":
    sys.exit(main())
