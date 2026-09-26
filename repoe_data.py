"""
repoe_data.py

Pobiera dane o modyfikatorach Path of Exile 1 z repoe-fork.github.io
(https://repoe-fork.github.io/ - żywy eksport danych z gry, format JSON,
bez JS, aktualizowany automatycznie) i buduje mapę:

    { "Nazwa typu przedmiotu": ["czytelny tekst mod 1", "czytelny tekst mod 2", ...] }

która jest zapisywana lokalnie do item_mods_cache.json, żeby nie pobierać
danych przy każdym starcie aplikacji.

Źródła danych (PoE1, root domeny repoe-fork.github.io):
    - mods_by_base.min.json   : mody pogrupowane per typ bazowy przedmiotu (stat ID)
    - stat_translations.min.json : tłumaczenie stat ID -> czytelny tekst z '#' jako placeholder

Uruchom ten skrypt raz (python repoe_data.py), żeby zbudować/odświeżyć cache.
reroller.py automatycznie z niego korzysta.
"""

import json
import os
import re
import urllib.request

BASE_URL = "https://repoe-fork.github.io"
MODS_BY_BASE_URL = f"{BASE_URL}/mods_by_base.min.json"
STAT_TRANSLATIONS_URL = f"{BASE_URL}/stat_translations.min.json"

CACHE_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_FILE = os.path.join(CACHE_DIR, "item_mods_cache.json")

USER_AGENT = "Mozilla/5.0 (compatible; PoE-Reroller/1.0)"


def _fetch_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _build_stat_id_to_text(stat_translations: list) -> dict:
    """
    stat_translations.json to lista obiektów:
      { "ids": ["stat_id_1", "stat_id_2", ...],
        "English": [ { "string": "...{0}...", "format": [...], "condition": [...] }, ... ] }

    Dla naszych celów (dopasowanie fragmentu tekstu w schowku) wystarczy
    wziąć PIERWSZY wariant angielskiego tekstu dla każdego stat_id i
    zamienić {0}, {1}, ... na '#', usuwając też ewentualne znaki nowej linii.
    """
    stat_id_to_text = {}
    for entry in stat_translations:
        english_variants = entry.get("English", [])
        if not english_variants:
            continue
        # bierzemy pierwszy wariant jako reprezentatywny tekst
        text = english_variants[0].get("string", "")
        if not text:
            continue
        # {0}, {1}, ... -> #
        text = re.sub(r"\{\d+\}", "#", text)
        text = text.replace("\n", " ").strip()

        for stat_id in entry.get("ids", []):
            # nie nadpisuj jeśli już mamy (pierwsze wystąpienie wygrywa)
            stat_id_to_text.setdefault(stat_id, text)

    return stat_id_to_text


def _human_base_name(raw_name: str) -> str:
    """mods_by_base.json ma klucze najwyższego poziomu w miarę czytelne
    (np. 'Body Armours', 'Amulets'), więc zwracamy bez zmian, przycięte."""
    return raw_name.strip()


def build_item_mods(mods_by_base: dict, stat_id_to_text: dict) -> dict:
    """
    mods_by_base.json struktura (uproszczona):
      {
        "<Item Class, np. 'Body Armours'>": {
          "<tag_key, np. 'str_armour,armour,default'>": {
            "bases": [ "Metadata/Items/..." ],
            "mods": {
              "prefix": { "<GroupName>": { "<StatId>": weight, ... }, ... },
              "suffix": { ... },
              "corrupted": {...}, "unique": {...}, ...
            },
            "conditional_mods": null
          },
          ...
        },
        ...
      }

    Budujemy: { "Item Class": sorted(set of czytelnych tekstów modów) }
    zbierając prefix+suffix (pomijamy corrupted/unique/crafted jako mniej
    istotne dla zwykłego rerollowania, ale można to łatwo rozszerzyć).
    """
    result: dict[str, set] = {}

    RELEVANT_MOD_KINDS = ("prefix", "suffix")

    for item_class, tag_groups in mods_by_base.items():
        if not isinstance(tag_groups, dict):
            continue

        texts_for_class: set = result.setdefault(_human_base_name(item_class), set())

        for _tag_key, tag_data in tag_groups.items():
            mods = tag_data.get("mods", {}) if isinstance(tag_data, dict) else {}
            for kind in RELEVANT_MOD_KINDS:
                group_dict = mods.get(kind, {})
                if not isinstance(group_dict, dict):
                    continue
                for _group_name, stat_weight_map in group_dict.items():
                    if not isinstance(stat_weight_map, dict):
                        continue
                    for stat_id in stat_weight_map.keys():
                        text = stat_id_to_text.get(stat_id)
                        if text:
                            texts_for_class.add(text)

    # posprzątaj: usuń klasy bez żadnych modów (np. sama waluta), posortuj
    cleaned = {
        cls: sorted(texts)
        for cls, texts in result.items()
        if texts
    }
    return cleaned


def refresh_cache(verbose: bool = True) -> dict:
    if verbose:
        print("Pobieram mods_by_base.min.json ...")
    mods_by_base = _fetch_json(MODS_BY_BASE_URL)

    if verbose:
        print("Pobieram stat_translations.min.json ...")
    stat_translations = _fetch_json(STAT_TRANSLATIONS_URL)

    if verbose:
        print("Buduję mapę stat_id -> tekst ...")
    stat_id_to_text = _build_stat_id_to_text(stat_translations)

    if verbose:
        print("Buduję mapę typ przedmiotu -> lista modów ...")
    item_mods = build_item_mods(mods_by_base, stat_id_to_text)

    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump(item_mods, f, ensure_ascii=False, indent=2)

    if verbose:
        total_mods = sum(len(v) for v in item_mods.values())
        print(f"Gotowe. {len(item_mods)} typów przedmiotów, {total_mods} unikalnych tekstów modów.")
        print(f"Zapisano do: {CACHE_FILE}")

    return item_mods


def load_or_build_cache(force_refresh: bool = False) -> dict:
    if not force_refresh and os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return refresh_cache()


if __name__ == "__main__":
    import sys

    force = "--refresh" in sys.argv
    load_or_build_cache(force_refresh=force)
