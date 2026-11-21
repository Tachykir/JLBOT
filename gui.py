"""
gui.py
======
Okno bota: wybór instancji, Start/Stop, podgląd ekranu, LP z leaderboardu
(ile brakuje do MossG) i mapy kliknięć na każdym klikanym przycisku
(RUN 10 MORE, leaderboard, Profile).

    python gui.py

Tylko tkinter (jest w standardowym Pythonie) + OpenCV.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import queue
import threading
import time
import tkinter as tk
from tkinter import ttk

import cv2
import numpy as np

import arena_bot as ab

CLICKS_FILE = ab.BASE_DIR / "clicks.json"

# klikane przyciski: klucz -> (tytuł, obrazek startowy)
BUTTONS = {
    "run10": ("RUN 10 MORE", ab.TEMPLATE_PATH),
    "leaderboard": ("Leaderboard", ab.BASE_DIR / "templates" / "nav_leaderboard.png"),
    "profile": ("Profile", ab.BASE_DIR / "templates" / "nav_profile.png"),
}


def to_photo(img_bgr: np.ndarray) -> tk.PhotoImage:
    ok, buf = cv2.imencode(".png", img_bgr)
    return tk.PhotoImage(data=base64.b64encode(buf.tobytes()))


def fmt(v: int | None) -> str:
    return f"{v:,}".replace(",", " ") if v is not None else "?"


class QueueLogHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def emit(self, record):
        self.q.put(("log", self.format(record)))


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Just Loot – Arena")
        root.configure(bg="#111")
        self.events: queue.Queue = queue.Queue()
        self.adb_path = os.environ.get("ADB_PATH") or ab.default_adb_path()
        self.stop_event: threading.Event | None = None
        self.thread: threading.Thread | None = None
        self.bot: ab.ArenaBot | None = None
        self.prev_lb: ab.LeaderboardEvent | None = None
        self.session_lb: ab.LeaderboardEvent | None = None  # pierwszy odczyt po Start
        self.stop_at: float | None = None  # koniec (time.time())
        self.start_at: float | None = None  # "start od" (time.time())
        self.button_imgs = {k: cv2.imread(str(p)) for k, (_t, p) in BUTTONS.items()}
        self.clicks: dict[str, list[tuple[float, float]]] = self._load_clicks()
        self._photos = {}  # referencje, żeby tk nie zgubił obrazów
        self.last_frame: ab.FrameEvent | None = None
        self._redraw_pending = None
        root.geometry("1100x720")
        root.minsize(600, 420)

        handler = QueueLogHandler(self.events)
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.INFO)

        self._build()
        self.refresh_devices()
        self._load_last_leaderboard()
        self.root.after(100, self._pump)
        self.root.after(1000, self._tick)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ------------------------------------------------------------------ UI
    def _build(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        for w in ("TFrame", "TLabel", "TLabelframe", "TLabelframe.Label", "TCheckbutton"):
            style.configure(w, background="#111", foreground="#ddd")
        style.configure("Big.TLabel", font=("Segoe UI", 16, "bold"), foreground="#7ee08a")
        style.configure("Dim.TLabel", foreground="#888")

        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="Urządzenie:").pack(side="left")
        self.device_var = tk.StringVar()
        self.device_box = ttk.Combobox(top, textvariable=self.device_var, width=22)
        self.device_box.pack(side="left", padx=4)
        ttk.Button(top, text="Odśwież", command=self.refresh_devices).pack(side="left")
        self.start_btn = ttk.Button(top, text="▶ Start", command=self.start)
        self.start_btn.pack(side="left", padx=(12, 4))
        self.stop_btn = ttk.Button(top, text="■ Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left")
        self.status_var = tk.StringVar(value="zatrzymany")
        ttk.Label(top, textvariable=self.status_var).pack(side="left", padx=12)

        # harmonogram: "Start od" (gra się włącza) i "Koniec" (gra się zamyka);
        # poza oknem gra jest wyłączona
        timer = ttk.Frame(self.root, padding=(8, 0, 8, 6))
        timer.pack(fill="x")
        ttk.Label(timer, text="⏱ Start od:").pack(side="left")
        self.start_var = tk.StringVar()
        e1 = ttk.Entry(timer, textvariable=self.start_var, width=6)
        e1.pack(side="left", padx=4)
        ttk.Label(timer, text="Koniec:").pack(side="left", padx=(6, 0))
        self.timer_var = tk.StringVar()
        entry = ttk.Entry(timer, textvariable=self.timer_var, width=6)
        entry.pack(side="left", padx=4)
        for e in (e1, entry):
            e.bind("<Return>", lambda ev: self.set_timer())
        ttk.Label(timer, text="(8:00 · 23:15, 90 = 90 min, 1h30)",
                  style="Dim.TLabel").pack(side="left")
        ttk.Button(timer, text="Ustaw", command=self.set_timer).pack(side="left", padx=(8, 4))
        ttk.Button(timer, text="Anuluj", command=self.cancel_timer).pack(side="left")
        self.timer_info_var = tk.StringVar(value="harmonogram wyłączony")
        self.timer_label = ttk.Label(self.root, textvariable=self.timer_info_var,
                                     padding=(8, 0, 8, 6), background="#111", foreground="#ddd")
        self.timer_label.pack(fill="x")

        # wszystko skaluje się z oknem: obrazy rysujemy na Canvasach, a obraz
        # jest dopasowywany do ich rozmiaru (z zachowaniem proporcji)
        body = ttk.Frame(self.root, padding=(8, 0, 8, 8))
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=2, uniform="col")
        body.columnconfigure(1, weight=3, uniform="col")
        body.rowconfigure(0, weight=1)

        left = ttk.LabelFrame(body, text="Ekran", padding=4)
        left.grid(row=0, column=0, sticky="nsew")
        self.preview = tk.Canvas(left, bg="#000", highlightthickness=0, width=200, height=200)
        self.preview.pack(fill="both", expand=True)
        self.preview.bind("<Configure>", lambda e: self._schedule_redraw())

        right = ttk.Frame(body, padding=(8, 0, 0, 0))
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(1, weight=3)
        right.rowconfigure(3, weight=2)

        # --- leaderboard
        lb = ttk.LabelFrame(right, text="Leaderboard", padding=6)
        lb.grid(row=0, column=0, sticky="ew")
        lb.columnconfigure(0, weight=1)
        self.lb_missing_var = tk.StringVar(value="brakuje: ?")
        self.lb_detail_var = tk.StringVar(value="MossG ? LP · ja ? LP")
        self.lb_when_var = tk.StringVar(value="jeszcze nie sprawdzano")
        self.lb_delta_var = tk.StringVar(value="")
        head = ttk.Frame(lb)
        head.grid(row=0, column=0, sticky="w")
        ttk.Label(head, textvariable=self.lb_missing_var, style="Big.TLabel").pack(anchor="w")
        self.lb_delta_label = ttk.Label(head, textvariable=self.lb_delta_var,
                                        font=("Segoe UI", 12, "bold"))
        self.lb_delta_label.pack(anchor="w")
        ttk.Label(lb, textvariable=self.lb_detail_var).grid(row=1, column=0, sticky="w")
        ttk.Label(lb, textvariable=self.lb_when_var, style="Dim.TLabel").grid(row=2, column=0, sticky="w")
        self.lb_btn = ttk.Button(lb, text="Sprawdź teraz", command=self.check_now, state="disabled")
        self.lb_btn.grid(row=0, column=1, rowspan=3, sticky="e")

        # --- mapy kliknięć: RUN 10 MORE na całą szerokość, pod nim ikony
        maps = ttk.LabelFrame(right, text="Mapy kliknięć", padding=4)
        maps.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        maps.columnconfigure(0, weight=1, uniform="m")
        maps.columnconfigure(1, weight=1, uniform="m")
        maps.rowconfigure(1, weight=3)
        maps.rowconfigure(3, weight=2)
        self.map_canvas: dict[str, tk.Canvas] = {}
        self.map_title: dict[str, tk.StringVar] = {}
        layout = {"run10": (0, 0, 2), "leaderboard": (2, 0, 1), "profile": (2, 1, 1)}
        for key, (r, c, span) in layout.items():
            var = tk.StringVar()
            self.map_title[key] = var
            ttk.Label(maps, textvariable=var).grid(row=r, column=c, columnspan=span, sticky="w")
            cv = tk.Canvas(maps, bg="#000", highlightthickness=0, width=100, height=40)
            cv.grid(row=r + 1, column=c, columnspan=span, sticky="nsew", padx=2, pady=(0, 4))
            cv.bind("<Configure>", lambda e: self._schedule_redraw())
            self.map_canvas[key] = cv
        row = ttk.Frame(maps)
        row.grid(row=4, column=0, columnspan=2, sticky="ew")
        self.heat_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="mapa ciepła", variable=self.heat_var,
                        command=self.redraw_maps).pack(side="left")
        ttk.Button(row, text="Wyczyść", command=self.clear_clicks).pack(side="right")

        stats = ttk.Frame(right, padding=(0, 4))
        stats.grid(row=2, column=0, sticky="ew")
        self.score_var = tk.StringVar(value="dopasowanie: –")
        self.last_var = tk.StringVar(value="ostatnie kliknięcie: –")
        for v in (self.score_var, self.last_var):
            ttk.Label(stats, textvariable=v).pack(anchor="w")
        self._update_titles()

        log_box = ttk.LabelFrame(right, text="Log", padding=4)
        log_box.grid(row=3, column=0, sticky="nsew")
        self.log = tk.Text(log_box, height=4, width=40, bg="#000", fg="#ccc", insertbackground="#ccc",
                           relief="flat", font=("Consolas", 9))
        self.log.pack(fill="both", expand=True)

    # ------------------------------------------------------------ urządzenia
    def refresh_devices(self):
        devs = ab.list_devices(self.adb_path)
        for inst in ab.load_instances(ab.BASE_DIR / "config.json"):
            s = inst.get("serial")
            if s and s not in devs:
                devs.append(s)
        self.device_box["values"] = devs
        if devs and self.device_var.get() not in devs:
            self.device_var.set(devs[0])
        logging.info("urządzenia: %s", ", ".join(devs) or "brak")

    def _config_for(self, serial: str) -> ab.Config:
        for inst in ab.load_instances(ab.BASE_DIR / "config.json"):
            if inst.get("serial") == serial:
                return ab.Config.from_dict(inst)
        return ab.Config(serial=serial)

    # ------------------------------------------------------------ start/stop
    def start(self):
        serial = self.device_var.get().strip()
        if not serial or self.thread:
            return
        self.stop_event = threading.Event()
        cfg = self._config_for(serial)
        try:
            self.bot = ab.ArenaBot(cfg, self.adb_path, self.stop_event,
                                   on_click=lambda e: self.events.put(("click", e)),
                                   on_frame=lambda e: self.events.put(("frame", e)),
                                   on_leaderboard=lambda e: self.events.put(("lb", e)))
        except Exception as e:
            logging.error("nie mogę wystartować: %s", e)
            return
        self.bot.stop_at = self.stop_at
        self.bot.start_at = self.start_at
        self.session_lb = None  # "od startu" liczymy od nowa
        self.thread = threading.Thread(target=self.bot.run, daemon=True)
        self.thread.start()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.lb_btn.configure(state="normal")
        self.status_var.set(f"działa: {serial}")

    def stop(self):
        if self.stop_event:
            self.stop_event.set()
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.lb_btn.configure(state="disabled")
        self.status_var.set("zatrzymywanie…")

        def wait():
            if self.thread and self.thread.is_alive():
                self.root.after(200, wait)
                return
            self.thread = None
            self.bot = None
            self.status_var.set("zatrzymany")
        wait()

    # ------------------------------------------------------------ harmonogram
    def set_timer(self):
        start_text, stop_text = self.start_var.get().strip(), self.timer_var.get().strip()
        if not start_text and not stop_text:
            self.cancel_timer()
            return
        try:
            self.start_at, self.stop_at = ab.make_window(start_text, stop_text)
        except ValueError:
            self.timer_info_var.set("nie rozumiem – start np. 8:00, koniec np. 23:15, 90 albo 1h30")
            self.timer_label.configure(foreground="#ff7b7b")
            return
        self.timer_label.configure(foreground="#ddd")
        if self.bot:
            self.bot.start_at, self.bot.stop_at = self.start_at, self.stop_at
        hm = lambda t: time.strftime("%H:%M", time.localtime(t)) if t else "…"
        if self.start_at:
            logging.info("harmonogram: gra włączona %s–%s%s, poza oknem wyłączona",
                         hm(self.start_at), hm(self.stop_at), " codziennie" if self.stop_at else "")
        else:
            logging.info("minutnik: zamknięcie gry o %s", hm(self.stop_at))
        self._update_timer()

    def cancel_timer(self):
        self.stop_at = self.start_at = None
        if self.bot:
            self.bot.stop_at = self.bot.start_at = None
        self.timer_label.configure(foreground="#ddd")
        self.timer_info_var.set("harmonogram wyłączony")

    def _update_timer(self):
        # bot przesuwa okno na kolejne dni - bierzemy jego wartości
        start = self.bot.start_at if self.bot else self.start_at
        stop = self.bot.stop_at if self.bot else self.stop_at
        if start is None and stop is None:
            return
        now = time.time()
        hm = lambda t: time.strftime("%H:%M", time.localtime(t))

        def left(t):
            h, rem = divmod(max(0, int(t - now)), 3600)
            return f"{h}:{rem // 60:02d}:{rem % 60:02d}"

        idle = "" if self.bot else " (po kliknięciu Start)"
        if start is None:
            self.timer_info_var.set(f"koniec o {hm(stop)} – zostało {left(stop)}{idle}")
            self.timer_label.configure(foreground="#ddd")
        elif now < start:
            span = f"{hm(start)}–{hm(stop)}" if stop else f"od {hm(start)}"
            self.timer_info_var.set(f"okno {span} · gra wyłączona, start za {left(start)}{idle}")
            self.timer_label.configure(foreground="#aaa")
        elif stop is None:
            self.timer_info_var.set(f"od {hm(start)} · gra włączona{idle}")
            self.timer_label.configure(foreground="#7ee08a")
        else:
            self.timer_info_var.set(f"okno {hm(start)}–{hm(stop)} · gra włączona, "
                                    f"koniec za {left(stop)}{idle}")
            self.timer_label.configure(foreground="#7ee08a")

    def check_now(self):
        if self.bot:
            self.bot.check_leaderboard_now.set()
            self.lb_when_var.set("sprawdzam…")

    def on_close(self):
        if self.stop_event:
            self.stop_event.set()
        self.root.destroy()

    # ----------------------------------------------------------- zdarzenia
    def _pump(self):
        last_frame = None
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "log":
                    self.log.insert("end", data + "\n")
                    self.log.see("end")
                elif kind == "click":
                    self.on_click(data)
                elif kind == "frame":
                    last_frame = data  # rysujemy tylko najnowszą klatkę
                elif kind == "lb":
                    self.show_leaderboard(data)
        except queue.Empty:
            pass
        if last_frame is not None:
            self.show_frame(last_frame)
        self.root.after(100, self._pump)

    def _tick(self):
        self._update_timer()
        # bot skończył sam (minutnik) - przywróć przyciski
        if self.thread and not self.thread.is_alive():
            finished = (self.start_at is None and self.stop_at is not None
                        and time.time() >= self.stop_at)
            self.thread = None
            self.bot = None
            self.start_btn.configure(state="normal")
            self.stop_btn.configure(state="disabled")
            self.lb_btn.configure(state="disabled")
            if finished:
                at = time.strftime("%H:%M", time.localtime(self.stop_at))
                self.status_var.set(f"zakończony minutnikiem o {at}")
                self.timer_info_var.set(f"gra zamknięta o {at}")
                self.stop_at = None
            else:
                self.status_var.set("zatrzymany")
        # odliczanie do następnego sprawdzenia leaderboardu
        if self.bot and self.bot.auto_leaderboard and self.lb_when_var.get() != "sprawdzam…":
            left = int(self.bot.next_leaderboard - time.monotonic())
            base = self.lb_when_var.get().split(" · następne")[0]
            if left > 0:
                self.lb_when_var.set(f"{base} · następne za {left // 60}:{left % 60:02d}")
        self.root.after(1000, self._tick)

    def show_leaderboard(self, ev: ab.LeaderboardEvent):
        missing = ev.missing
        name = ev.rival_name
        if ev.valid and self.session_lb is None and self.bot is not None:
            self.session_lb = ev
        d = ab.gap_change(self.prev_lb, ev)
        total = ab.gap_change(self.session_lb, ev)
        if missing is None:
            self.lb_missing_var.set("brakuje: ? (nie odczytano)")
        elif missing > 0:
            self.lb_missing_var.set(f"brakuje {fmt(missing)} LP do {name}")
        else:
            self.lb_missing_var.set(f"przewaga {fmt(-missing)} LP nad {name}")
        # + = dystans zmalał (podbiłem), − = urósł; na górze bilans od Start
        main = total if total is not None else None
        if main is not None:
            text = f"od startu {ab.signed(main)}"
            if d is not None:
                text += f"  (ostatnio {ab.signed(d)})"
            self.lb_delta_var.set(text)
            self.lb_delta_label.configure(foreground="#7ee08a" if main > 0 else
                                          "#ff7b7b" if main < 0 else "#aaa")
        elif d is not None:
            self.lb_delta_var.set(f"ostatnio {ab.signed(d)}")
            self.lb_delta_label.configure(foreground="#aaa")
        else:
            self.lb_delta_var.set("")
        self.lb_detail_var.set(f"{name} {fmt(ev.rival_lp)} LP · ja {fmt(ev.my_lp)} LP")
        when = "sprawdzono " + time.strftime("%H:%M:%S", time.localtime(ev.when))
        if d is not None:
            when += f" (poprzednio {time.strftime('%H:%M', time.localtime(self.prev_lb.when))})"
        self.lb_when_var.set(when)
        if ev.valid:
            self.prev_lb = ev

    def _load_last_leaderboard(self):
        hist = ab.load_leaderboard_history()
        if not hist:
            return
        self.prev_lb = hist[-2] if len(hist) > 1 else None
        self.show_leaderboard(hist[-1])

    def show_frame(self, e: ab.FrameEvent):
        self.last_frame = e
        self.render_preview()
        if e.rect is not None or e.score:
            self.score_var.set(f"dopasowanie: {e.score:.2f} – "
                               + ("RUN 10 MORE widoczny" if e.visible else "brak przycisku"))

    def _schedule_redraw(self):
        # przy przeciąganiu okna przychodzi dużo zdarzeń - rysujemy raz
        if self._redraw_pending:
            self.root.after_cancel(self._redraw_pending)
        self._redraw_pending = self.root.after(60, self._redraw_all)

    def _redraw_all(self):
        self._redraw_pending = None
        self.render_preview()
        self.redraw_maps()

    @staticmethod
    def _fit(img: np.ndarray, cw: int, ch: int) -> np.ndarray | None:
        h, w = img.shape[:2]
        k = min(cw / w, ch / h)
        nw, nh = int(w * k), int(h * k)
        if nw < 2 or nh < 2:
            return None
        interp = cv2.INTER_AREA if k < 1 else cv2.INTER_CUBIC
        return cv2.resize(img, (nw, nh), interpolation=interp)

    def _draw_on(self, canvas: tk.Canvas, key: str, img: np.ndarray | None):
        canvas.delete("all")
        if img is None:
            return
        self._photos[key] = to_photo(img)
        canvas.create_image(canvas.winfo_width() // 2, canvas.winfo_height() // 2,
                            image=self._photos[key], anchor="center")

    def render_preview(self):
        e = self.last_frame
        if e is None:
            return
        cw, ch = self.preview.winfo_width(), self.preview.winfo_height()
        h, w = e.frame.shape[:2]
        k = min(cw / w, ch / h)
        img = cv2.resize(e.frame, (max(2, int(w * k)), max(2, int(h * k))),
                         interpolation=cv2.INTER_AREA)
        if e.rect and e.visible:
            x1, y1, x2, y2 = (int(v * k) for v in e.rect)
            cv2.rectangle(img, (x1, y1), (x2, y2), (0, 220, 0), 2)
        self._draw_on(self.preview, "preview", img)

    def on_click(self, e: ab.ClickEvent):
        self.clicks.setdefault(e.key, []).append((round(e.fx, 4), round(e.fy, 4)))
        if e.button is not None and e.button.size:
            self.button_imgs[e.key] = e.button  # prawdziwy wygląd przycisku z gry
        name = BUTTONS.get(e.key, (e.key,))[0]
        self.last_var.set(f"ostatnie kliknięcie: {name} ({e.x}, {e.y}) o {time.strftime('%H:%M:%S')}")
        self._update_titles()
        self._save_clicks()
        self.redraw_map(e.key)

    # ------------------------------------------------------- mapy kliknięć
    def redraw_maps(self):
        for key in BUTTONS:
            self.redraw_map(key)

    def redraw_map(self, key: str):
        base = self.button_imgs.get(key)
        canvas = self.map_canvas.get(key)
        if base is None or canvas is None:
            return
        img = self._fit(base, canvas.winfo_width() - 4, canvas.winfo_height() - 4)
        if img is None:
            return
        th, tw = img.shape[:2]
        img = (img * 0.6).astype(np.uint8)
        pts = self.clicks.get(key, [])
        n = len(pts)

        if self.heat_var.get() and n:
            heat = np.zeros((th, tw), np.float32)
            for fx, fy in pts:
                cv2.circle(heat, (int(fx * tw), int(fy * th)), 1, 1.0, -1)
            heat = cv2.GaussianBlur(heat, (0, 0), sigmaX=max(3, max(tw, th) / 40))
            heat = (255 * heat / heat.max()).astype(np.uint8)
            colored = cv2.applyColorMap(heat, cv2.COLORMAP_JET)
            alpha = (heat.astype(np.float32) / 255 * 0.65)[..., None]
            img = (img * (1 - alpha) + colored * alpha).astype(np.uint8)

        r = max(2, max(tw, th) // 110)
        for i, (fx, fy) in enumerate(pts):
            recent = i >= n - 10
            cv2.circle(img, (int(fx * tw), int(fy * th)), r * 2 if recent else r,
                       (0, 0, 255) if i == n - 1 else (255, 255, 255) if recent else (0, 230, 255),
                       -1, cv2.LINE_AA)
        self._draw_on(canvas, "map_" + key, img)

    def clear_clicks(self):
        self.clicks = {}
        self._save_clicks()
        self._update_titles()
        self.redraw_maps()

    def _update_titles(self):
        for key, (title, _p) in BUTTONS.items():
            self.map_title[key].set(f"{title} – kliknięcia: {len(self.clicks.get(key, []))}")

    def _load_clicks(self) -> dict[str, list[tuple[float, float]]]:
        try:
            data = json.loads(CLICKS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
        if isinstance(data, list):  # stary format: same kliknięcia RUN 10 MORE
            data = {"run10": data}
        return {k: [tuple(p) for p in v] for k, v in data.items()}

    def _save_clicks(self):
        try:
            CLICKS_FILE.write_text(json.dumps(self.clicks), encoding="utf-8")
        except Exception as e:
            logging.warning("nie zapisano clicks.json: %s", e)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
