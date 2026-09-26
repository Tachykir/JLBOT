"""
Reroller - port skryptu AutoHotkey v2 na Python z GUI.

Funkcja: trzyma Shift, klika i kopiuje zaznaczony tekst (Ctrl+Alt+C),
sprawdzając czy w schowku pojawi się jeden z modyfikatorów wybranych
przez użytkownika dla danego typu przedmiotu (np. "roll" modów w grze).

Źródło danych o modyfikatorach: repoe-fork.github.io - żywy, publiczny
eksport danych z gry Path of Exile 1 w formacie JSON (bez JS, aktualizowany
automatycznie). Przy pierwszym uruchomieniu (lub gdy brak pliku cache)
aplikacja pobiera i przetwarza te dane, zapisując wynik do
item_mods_cache.json, żeby kolejne starty były natychmiastowe.
Aby ręcznie odświeżyć dane do najnowszej wersji gry, uruchom:
    python repoe_data.py --refresh

Przepływ pracy:
    1. Wybierz typ przedmiotu z listy (np. "Body Armours", "Amulets").
    2. Zaznacz checkboxami mody, które Cię interesują (jest ich sporo -
       lista jest przewijalna, można też użyć pola szukania).
    3. Ustaw kursor myszy nad przedmiotem w grze.
    4. Uruchom skanowanie (F2) - trzyma Shift i powtarza klik + kopiowanie,
       aż w schowku pojawi się jeden z zaznaczonych modów.

Skróty globalne (działają nawet gdy gra jest aktywna):
    F2        - start skanowania
    F12       - pauza / wznów
    Ctrl+Esc  - awaryjny stop (puszcza Shift, zatrzymuje wszystko)

Wymagane biblioteki:
    pip install keyboard pyautogui pyperclip

UWAGA:
    - Biblioteka `keyboard` na Linuksie/macOS zwykle wymaga uprawnień
      administratora (root/sudo) do globalnego przechwytywania klawiszy.
      Na Windows zazwyczaj działa bez tego.
    - pyautogui.click() klika w AKTUALNEJ pozycji myszy (tak jak `Click`
      w AHK bez podanych współrzędnych) - upewnij się, że kursor stoi
      nad odpowiednim miejscem w grze przed uruchomieniem F2.
    - Teksty modów pochodzą z tłumaczeń gry i zawierają '#' w miejscu
      liczb (np. "+# to maximum Life") - dopasowanie w schowku szuka
      fragmentu tekstu WOKÓŁ '#', więc działa niezależnie od konkretnej
      wylosowanej wartości.
"""

import os
import re
import threading
import time
import queue
import tkinter as tk
from tkinter import scrolledtext, messagebox

import keyboard
import pyautogui
import pyperclip

import repoe_data

INTERVAL_MS = 300  # przerwa między iteracjami (jak w AHK)
CLIP_WAIT_TIMEOUT = 2.0  # sekundy - odpowiednik ClipWait(2)


def mod_text_to_match_fragments(mod_text: str) -> list[str]:
    """
    Zamienia tekst mod ze znacznikiem '#' (np. '+# to maximum Life' lub
    '#% increased Rarity of Items found') na listę fragmentów tekstu
    dzielonych po '#', pomijając puste kawałki i sam pojedynczy znak '+'.
    Fragment jest uznany za dopasowany jeśli WSZYSTKIE jego niepuste
    kawałki występują w tekście schowka (w dowolnej kolejności wystarczy
    zwykle jeden, bo mody są zazwyczaj jednolinijkowe - upraszczamy do
    najdłuższego fragmentu, który z reguły jest częścią opisową po liczbie).
    """
    parts = [p.strip(" +") for p in mod_text.split("#")]
    parts = [p for p in parts if p]
    return parts if parts else [mod_text]


def mod_matches_clipboard(mod_text: str, clipboard_text: str) -> bool:
    """Prosta, odporna heurystyka: mod pasuje, jeśli najdłuższy fragment
    tekstu (część opisowa, zwykle po liczbie) występuje w schowku."""
    fragments = mod_text_to_match_fragments(mod_text)
    if not fragments:
        return False
    longest = max(fragments, key=len)
    return longest.lower() in clipboard_text.lower()


class RerollerApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("Reroller (Python port z AHK) - PoE mody")
        self.root.geometry("620x680")
        self.root.resizable(False, False)

        # --- dane ---
        self.item_mods: dict[str, list[str]] = {}
        self.data_status_var = tk.StringVar(value="Ładowanie bazy modów...")

        # --- stan (odpowiedniki zmiennych globalnych z AHK) ---
        self.selected_item_type: str | None = None
        self.mod_vars: dict[str, tk.BooleanVar] = {}  # mod_text -> checkbox var
        self.paused = False
        self.searching = False
        self._stop_requested = False
        self._search_thread: threading.Thread | None = None
        self._active_search_mods: list[str] = []

        self.log_queue: "queue.Queue[str]" = queue.Queue()

        self._build_gui()
        self._register_hotkeys()
        self._poll_log_queue()

        # Ładowanie danych (cache lub pobranie) w osobnym wątku, żeby nie blokować GUI
        threading.Thread(target=self._load_data_background, daemon=True).start()

    # ---------------------------------------------------------------
    # Ładowanie danych o modach
    # ---------------------------------------------------------------
    def _load_data_background(self):
        try:
            cache_exists = os.path.exists(repoe_data.CACHE_FILE)
            if not cache_exists:
                self.log("Brak lokalnego cache modów - pobieram dane z repoe-fork.github.io (jednorazowo)...")
            self.item_mods = repoe_data.load_or_build_cache(force_refresh=False)
            self.data_status_var.set(f"Baza modów: {len(self.item_mods)} typów przedmiotów")
            self.log(f"Baza modów wczytana ({len(self.item_mods)} typów przedmiotów).")
            self.root.after(0, self._populate_item_type_menu)
        except Exception as e:
            self.data_status_var.set("Błąd ładowania bazy modów")
            self.log(f"Nie udało się pobrać/wczytać bazy modów: {e}")
            self.log("Sprawdź połączenie internetowe i spróbuj ponownie, lub uruchom: python repoe_data.py --refresh")

    def _populate_item_type_menu(self):
        item_types = sorted(self.item_mods.keys())
        menu = self.item_type_menu["menu"]
        menu.delete(0, "end")
        for name in item_types:
            menu.add_command(label=name, command=lambda v=name: self._on_item_type_selected(v))
        if item_types:
            self.item_type_var.set(item_types[0])
            self._on_item_type_selected(item_types[0])

    # ---------------------------------------------------------------
    # GUI
    # ---------------------------------------------------------------
    def _build_gui(self):
        # --- status danych ---
        tk.Label(self.root, textvariable=self.data_status_var, fg="#555").pack(anchor="w", padx=10, pady=(8, 0))

        # --- Krok 1: wybór typu przedmiotu ---
        frame_type = tk.Frame(self.root, padx=10, pady=6)
        frame_type.pack(fill=tk.X)

        tk.Label(frame_type, text="1. Typ przedmiotu:", font=("Segoe UI", 10, "bold")).pack(anchor="w")

        self.item_type_var = tk.StringVar(value="(ładowanie...)")
        self.item_type_menu = tk.OptionMenu(frame_type, self.item_type_var, "(ładowanie...)")
        self.item_type_menu.config(width=40)
        self.item_type_menu.pack(anchor="w", pady=(2, 0))

        # --- Krok 2: filtr + lista modów (checkboxy) ---
        frame_mods_label = tk.Frame(self.root, padx=10)
        frame_mods_label.pack(fill=tk.X, pady=(8, 0))
        tk.Label(frame_mods_label, text="2. Wybierz interesujące mody:", font=("Segoe UI", 10, "bold")).pack(
            anchor="w"
        )

        filter_frame = tk.Frame(self.root, padx=10)
        filter_frame.pack(fill=tk.X, pady=(4, 4))
        tk.Label(filter_frame, text="Filtr:").pack(side=tk.LEFT)
        self.filter_var = tk.StringVar(value="")
        filter_entry = tk.Entry(filter_frame, textvariable=self.filter_var)
        filter_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 0))
        self.filter_var.trace_add("write", lambda *a: self._rebuild_mod_checkboxes())

        mods_container = tk.Frame(self.root, padx=10)
        mods_container.pack(fill=tk.BOTH, expand=False)

        canvas = tk.Canvas(mods_container, height=260, highlightthickness=1, highlightbackground="#ccc")
        scrollbar = tk.Scrollbar(mods_container, orient="vertical", command=canvas.yview)
        self.mods_frame = tk.Frame(canvas)

        self.mods_frame.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=self.mods_frame, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)

        canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        # --- Przyciski akcji ---
        btn_frame = tk.Frame(self.root, padx=10, pady=8)
        btn_frame.pack(fill=tk.X)

        self.btn_start = tk.Button(btn_frame, text="Start (F2)", command=self.start_search)
        self.btn_start.pack(side=tk.LEFT, padx=(0, 6))

        self.btn_pause = tk.Button(btn_frame, text="Pauza (F12)", command=self.toggle_pause)
        self.btn_pause.pack(side=tk.LEFT, padx=6)

        self.btn_stop = tk.Button(btn_frame, text="Stop (Ctrl+Esc)", command=self.stop_all)
        self.btn_stop.pack(side=tk.LEFT, padx=6)

        self.btn_refresh_data = tk.Button(btn_frame, text="Odśwież bazę modów", command=self._refresh_data_clicked)
        self.btn_refresh_data.pack(side=tk.LEFT, padx=6)

        # status
        self.status_var = tk.StringVar(value="Status: bezczynny")
        tk.Label(self.root, textvariable=self.status_var, font=("Segoe UI", 10, "bold")).pack(
            anchor="w", padx=10, pady=(4, 0)
        )

        # log
        tk.Label(self.root, text="Log:").pack(anchor="w", padx=10, pady=(8, 0))
        self.log_box = scrolledtext.ScrolledText(self.root, height=10, state="disabled", wrap="word")
        self.log_box.pack(fill=tk.BOTH, expand=True, padx=10, pady=(2, 10))

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def _refresh_data_clicked(self):
        self.log("Odświeżam bazę modów z repoe-fork.github.io...")
        self.data_status_var.set("Odświeżanie bazy modów...")

        def worker():
            try:
                self.item_mods = repoe_data.refresh_cache(verbose=False)
                self.data_status_var.set(f"Baza modów: {len(self.item_mods)} typów przedmiotów (odświeżono)")
                self.log("Baza modów odświeżona.")
                self.root.after(0, self._populate_item_type_menu)
            except Exception as e:
                self.data_status_var.set("Błąd odświeżania bazy modów")
                self.log(f"Błąd odświeżania: {e}")

        threading.Thread(target=worker, daemon=True).start()

    def _on_item_type_selected(self, value: str):
        self.item_type_var.set(value)
        self.selected_item_type = value
        self.filter_var.set("")
        self._rebuild_mod_checkboxes()

    def _rebuild_mod_checkboxes(self):
        # zapamiętaj poprzednie zaznaczenia po tekście moda, żeby filtrowanie nie czyściło wyboru
        previous_checked = {mod for mod, var in self.mod_vars.items() if var.get()}

        for widget in self.mods_frame.winfo_children():
            widget.destroy()
        self.mod_vars.clear()

        if not self.selected_item_type:
            return

        mods = self.item_mods.get(self.selected_item_type, [])
        filter_text = self.filter_var.get().strip().lower()
        if filter_text:
            mods = [m for m in mods if filter_text in m.lower()]

        if not mods:
            tk.Label(self.mods_frame, text="(brak modów pasujących do filtra)").pack(anchor="w")
            return

        for mod_text in mods:
            var = tk.BooleanVar(value=(mod_text in previous_checked))
            cb = tk.Checkbutton(self.mods_frame, text=mod_text, variable=var, anchor="w", justify="left")
            cb.pack(fill=tk.X, anchor="w")
            self.mod_vars[mod_text] = var

    def _on_close(self):
        self.stop_all()
        try:
            keyboard.unhook_all_hotkeys()
        except Exception:
            pass
        self.root.destroy()

    def log(self, msg: str):
        """Bezpieczne logowanie z dowolnego wątku."""
        self.log_queue.put(msg)

    def _poll_log_queue(self):
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self.log_box.configure(state="normal")
                self.log_box.insert(tk.END, msg + "\n")
                self.log_box.see(tk.END)
                self.log_box.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self._poll_log_queue)

    def set_status(self, text: str):
        self.status_var.set(f"Status: {text}")

    def get_selected_mods(self) -> list[str]:
        # zbierz zaznaczone mody ze WSZYSTKICH typów, ale że checkboxy są
        # tylko dla aktualnie wybranego typu + widocznego filtra, w praktyce
        # to są mody z bieżącego typu (filtrowanie nie usuwa zaznaczeń, patrz wyżej)
        return [mod for mod, var in self.mod_vars.items() if var.get()]

    # ---------------------------------------------------------------
    # Skróty globalne (odpowiednik hotkeyów AHK)
    # ---------------------------------------------------------------
    def _register_hotkeys(self):
        keyboard.add_hotkey("f2", self.start_search)
        keyboard.add_hotkey("f12", self.toggle_pause)
        keyboard.add_hotkey("ctrl+esc", self.stop_all)

    # ---------------------------------------------------------------
    # F2: start skanowania (jednorazowe, w osobnym wątku)
    # ---------------------------------------------------------------
    def start_search(self):
        if self.searching:
            self.log("Skrypt już działa. F12 = pauza, Ctrl+Esc = stop.")
            return

        selected_mods = self.get_selected_mods()
        if not selected_mods:
            self.log("Nie zaznaczono żadnych modów! Zaznacz co najmniej jeden checkbox.")
            return

        self._active_search_mods = selected_mods
        self._stop_requested = False
        self.paused = False
        self.searching = True
        self.set_status("szukam...")
        self.log(f"Start skanowania. Szukane mody ({len(selected_mods)}): " + "; ".join(selected_mods))

        self._search_thread = threading.Thread(target=self._search_loop, daemon=True)
        self._search_thread.start()

    def _search_loop(self):
        terms = self._active_search_mods

        # trzymamy Shift przez cały czas skanowania, tak jak w AHK
        pyautogui.keyDown("shift")
        try:
            while True:
                if self._stop_requested:
                    break

                if self.paused:
                    self.set_status("PAUZA (F12 aby wznowić)")
                    time.sleep(0.5)
                    continue

                self.set_status("szukam...")

                pyautogui.click()
                time.sleep(0.15)

                pyautogui.hotkey("ctrl", "alt", "c")
                time.sleep(0.15)

                if not self._clip_wait(CLIP_WAIT_TIMEOUT):
                    self.log("Brak danych w schowku. Próba ponownie...")
                    time.sleep(INTERVAL_MS / 1000)
                    continue

                clipboard_text = pyperclip.paste() or ""

                found_term = None
                for term in terms:
                    if mod_matches_clipboard(term, clipboard_text):
                        found_term = term
                        break

                if found_term:
                    self.log(f'Mod znaleziony: "{found_term}". Wciśnij F2 aby ponownie rozpocząć.')
                    try:
                        pyperclip.copy("")
                    except Exception:
                        pass
                    break

                time.sleep(INTERVAL_MS / 1000)
        finally:
            pyautogui.keyUp("shift")
            self.searching = False
            self.set_status("bezczynny")

    def _clip_wait(self, timeout: float) -> bool:
        """Odpowiednik ClipWait(2) - czeka aż w schowku pojawi się jakiś tekst."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                current = pyperclip.paste()
            except Exception:
                current = None
            if current:
                return True
            time.sleep(0.05)
        return False

    # ---------------------------------------------------------------
    # F12: pauza / wznów
    # ---------------------------------------------------------------
    def toggle_pause(self):
        if not self.searching:
            self.log("Nic nie jest uruchomione - pauza nie ma efektu.")
            return
        self.paused = not self.paused
        self.log("Pauza włączona" if self.paused else "Pauza wyłączona")

    # ---------------------------------------------------------------
    # Ctrl+Esc: awaryjny stop
    # ---------------------------------------------------------------
    def stop_all(self):
        self._stop_requested = True
        self.paused = False
        try:
            pyautogui.keyUp("shift")
        except Exception:
            pass
        self.log("Zatrzymano.")
        self.set_status("zatrzymany")


def main():
    root = tk.Tk()
    app = RerollerApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
