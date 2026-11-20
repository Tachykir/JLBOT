"""
gui.py
======
Okno bota RUN 10 MORE: wybór instancji, Start/Stop, podgląd ekranu
z zaznaczonym przyciskiem i mapa wszystkich kliknięć na przycisku.

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
from pathlib import Path
from tkinter import ttk

import cv2
import numpy as np

import arena_bot as ab

CLICKS_FILE = ab.BASE_DIR / "clicks.json"


def to_photo(img_bgr: np.ndarray) -> tk.PhotoImage:
    ok, buf = cv2.imencode(".png", img_bgr)
    return tk.PhotoImage(data=base64.b64encode(buf.tobytes()))


class QueueLogHandler(logging.Handler):
    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def emit(self, record):
        self.q.put(("log", self.format(record)))


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("Just Loot – RUN 10 MORE")
        root.configure(bg="#111")
        self.events: queue.Queue = queue.Queue()
        self.adb_path = os.environ.get("ADB_PATH") or ab.default_adb_path()
        self.stop_event: threading.Event | None = None
        self.thread: threading.Thread | None = None
        self.button_img = cv2.imread(str(ab.TEMPLATE_PATH))
        self.clicks: list[tuple[float, float]] = self._load_clicks()
        self._photos = {}  # referencje, żeby tk nie zgubił obrazów
        self.last_frame: ab.FrameEvent | None = None
        self._redraw_pending = None
        root.geometry("1000x640")
        root.minsize(560, 380)

        handler = QueueLogHandler(self.events)
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%H:%M:%S"))
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.INFO)

        self._build()
        self.refresh_devices()
        self.redraw_mask()
        self.root.after(100, self._pump)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ------------------------------------------------------------------ UI
    def _build(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        for w in ("TFrame", "TLabel", "TLabelframe", "TLabelframe.Label"):
            style.configure(w, background="#111", foreground="#ddd")

        top = ttk.Frame(self.root, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="Urządzenie:").pack(side="left")
        self.device_var = tk.StringVar()
        self.device_box = ttk.Combobox(top, textvariable=self.device_var, width=24)
        self.device_box.pack(side="left", padx=4)
        ttk.Button(top, text="Odśwież", command=self.refresh_devices).pack(side="left")
        self.start_btn = ttk.Button(top, text="▶ Start", command=self.start)
        self.start_btn.pack(side="left", padx=(12, 4))
        self.stop_btn = ttk.Button(top, text="■ Stop", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left")
        self.status_var = tk.StringVar(value="zatrzymany")
        ttk.Label(top, textvariable=self.status_var).pack(side="left", padx=12)

        # wszystko skaluje się z oknem: obrazy rysujemy na Canvasach,
        # które dostają tyle miejsca, ile daje okno, a obraz jest
        # dopasowywany do ich rozmiaru (z zachowaniem proporcji)
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
        right.rowconfigure(0, weight=2)
        right.rowconfigure(2, weight=3)

        mask_box = ttk.LabelFrame(right, text="Mapa kliknięć na RUN 10 MORE", padding=4)
        mask_box.grid(row=0, column=0, sticky="nsew")
        row = ttk.Frame(mask_box)
        row.pack(side="bottom", fill="x", pady=(4, 0))
        self.mask_canvas = tk.Canvas(mask_box, bg="#000", highlightthickness=0, width=200, height=60)
        self.mask_canvas.pack(fill="both", expand=True)
        self.mask_canvas.bind("<Configure>", lambda e: self._schedule_redraw())
        self.heat_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(row, text="mapa ciepła", variable=self.heat_var,
                        command=self.redraw_mask).pack(side="left")
        ttk.Button(row, text="Wyczyść", command=self.clear_clicks).pack(side="right")

        stats = ttk.Frame(right, padding=(0, 6))
        stats.grid(row=1, column=0, sticky="ew")
        self.count_var = tk.StringVar()
        self.score_var = tk.StringVar(value="dopasowanie: –")
        self.last_var = tk.StringVar(value="ostatnie kliknięcie: –")
        for v in (self.count_var, self.score_var, self.last_var):
            ttk.Label(stats, textvariable=v).pack(anchor="w")
        self._update_count()

        log_box = ttk.LabelFrame(right, text="Log", padding=4)
        log_box.grid(row=2, column=0, sticky="nsew")
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
            bot = ab.ArenaBot(cfg, self.adb_path, self.stop_event,
                              on_click=lambda e: self.events.put(("click", e)),
                              on_frame=lambda e: self.events.put(("frame", e)))
        except Exception as e:
            logging.error("nie mogę wystartować: %s", e)
            return
        self.thread = threading.Thread(target=bot.run, daemon=True)
        self.thread.start()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.status_var.set(f"działa: {serial}")

    def stop(self):
        if self.stop_event:
            self.stop_event.set()
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")
        self.status_var.set("zatrzymywanie…")

        def wait():
            if self.thread and self.thread.is_alive():
                self.root.after(200, wait)
                return
            self.thread = None
            self.status_var.set("zatrzymany")
        wait()

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
        except queue.Empty:
            pass
        if last_frame is not None:
            self.show_frame(last_frame)
        self.root.after(100, self._pump)

    def show_frame(self, e: ab.FrameEvent):
        self.last_frame = e
        self.render_preview()
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
        self.redraw_mask()

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
        self.clicks.append((round(e.fx, 4), round(e.fy, 4)))
        if e.button is not None and e.button.size:
            self.button_img = e.button  # prawdziwy wygląd przycisku z gry
        self.last_var.set(f"ostatnie kliknięcie: ({e.x}, {e.y}) o {time.strftime('%H:%M:%S')}")
        self._update_count()
        self._save_clicks()
        self.redraw_mask()

    # ------------------------------------------------------- mapa kliknięć
    def redraw_mask(self):
        base = self.button_img
        if base is None:
            return
        img = self._fit(base, self.mask_canvas.winfo_width() - 4,
                        self.mask_canvas.winfo_height() - 4)
        if img is None:
            return
        th, tw = img.shape[:2]
        img = (img * 0.6).astype(np.uint8)
        n = len(self.clicks)

        if self.heat_var.get() and n:
            heat = np.zeros((th, tw), np.float32)
            for fx, fy in self.clicks:
                cv2.circle(heat, (int(fx * tw), int(fy * th)), 1, 1.0, -1)
            heat = cv2.GaussianBlur(heat, (0, 0), sigmaX=max(3, tw / 40))
            heat = (255 * heat / heat.max()).astype(np.uint8)
            colored = cv2.applyColorMap(heat, cv2.COLORMAP_JET)
            alpha = (heat.astype(np.float32) / 255 * 0.65)[..., None]
            img = (img * (1 - alpha) + colored * alpha).astype(np.uint8)

        for i, (fx, fy) in enumerate(self.clicks):
            recent = i >= n - 10
            r = max(2, tw // 110)
            cv2.circle(img, (int(fx * tw), int(fy * th)), r * 2 if recent else r,
                       (0, 0, 255) if i == n - 1 else (255, 255, 255) if recent else (0, 230, 255),
                       -1, cv2.LINE_AA)
        self._draw_on(self.mask_canvas, "mask", img)

    def clear_clicks(self):
        self.clicks.clear()
        self._save_clicks()
        self._update_count()
        self.redraw_mask()

    def _update_count(self):
        self.count_var.set(f"kliknięcia: {len(self.clicks)}")

    def _load_clicks(self) -> list[tuple[float, float]]:
        try:
            return [tuple(p) for p in json.loads(CLICKS_FILE.read_text(encoding="utf-8"))]
        except Exception:
            return []

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
