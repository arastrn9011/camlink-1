"""CamLink PC 3.0 - telefonu web kamerası yap, tüm ayarlar bilgisayardan."""
import json, os, queue, socket, subprocess, sys, threading, time
from io import BytesIO
from tkinter import messagebox
import tkinter as tk
import numpy as np
from PIL import Image, ImageTk
import customtkinter as ctk
try:
    import pyvirtualcam
except Exception:
    pyvirtualcam = None

PORT = 5000
APPDIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "CamLink")
CFG = os.path.join(APPDIR, "settings.json")

DEFAULTS = {"cam": 0, "res": "1280x720", "fps": 30, "jq": 85, "preview": False, "vfps": False,
            "focus": "continuous-video", "exp": 0, "ae_lock": False, "awb_lock": False, "wb": "auto",
            "zoom": 0, "torch": False, "anti": "auto", "iso": "auto", "stab": False}
APPDEF = {"mode": "usb", "ip": "", "rot": 0, "mirror": False, "theme": "Koyu", "autoconnect": False,
          "scale": "Dengeli", "overlay": True}
FOCUS_TR = {"continuous-video": "Sürekli (video)", "continuous-picture": "Sürekli (hızlı)",
            "auto": "Otomatik (tek seferlik)", "infinity": "Sonsuz", "macro": "Yakın (makro)",
            "fixed": "Sabit", "edof": "EDOF"}
WB_TR = {"auto": "Otomatik", "incandescent": "Akkor lamba", "fluorescent": "Floresan",
         "warm-fluorescent": "Sıcak floresan", "daylight": "Gün ışığı", "cloudy-daylight": "Bulutlu",
         "twilight": "Alacakaranlık", "shade": "Gölge"}
ANTI_TR = {"auto": "Otomatik", "50hz": "50 Hz (Türkiye)", "60hz": "60 Hz", "off": "Kapalı"}
THEMES = {"Koyu": "dark", "Açık": "light", "Sistem": "system"}
SCALES = {"Hızlı": Image.Resampling.NEAREST, "Dengeli": Image.Resampling.BILINEAR,
          "Kaliteli": Image.Resampling.BICUBIC}
ROT = {90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_90}


def tr(m, k):
    return m.get(k, k)


def untr(m, label):
    for k, v in m.items():
        if v == label:
            return k
    return label


class Stop(Exception):
    pass


def load_cfg():
    try:
        with open(CFG, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_cfg(d):
    try:
        os.makedirs(APPDIR, exist_ok=True)
        with open(CFG, "w", encoding="utf-8") as f:
            json.dump(d, f)
    except Exception:
        pass


def adb_path():
    base = getattr(sys, "_MEIPASS", None)
    if base:
        p = os.path.join(base, "platform-tools", "adb.exe")
        if os.path.exists(p):
            return p
    return "adb"


def run_adb(*args):
    return subprocess.run([adb_path(), *args], capture_output=True, text=True, timeout=20,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def usb_forward():
    try:
        r = run_adb("devices")
    except FileNotFoundError:
        raise RuntimeError("adb bulunamadı. Platform-tools kur veya exe sürümünü kullan")
    devs = [l.split("\t") for l in r.stdout.splitlines() if "\t" in l]
    if not any(len(d) == 2 and d[1].strip() == "device" for d in devs):
        if any(len(d) == 2 and "unauthorized" in d[1] for d in devs):
            raise RuntimeError("Telefonda 'USB hata ayıklamaya izin ver' sorusunu onayla")
        raise RuntimeError("USB'de telefon yok. Kabloyu ve 'USB hata ayıklama'yı kontrol et")
    r = run_adb("forward", f"tcp:{PORT}", f"tcp:{PORT}")
    if r.returncode != 0:
        raise RuntimeError("adb forward başarısız: " + r.stderr.strip())


class Receiver(threading.Thread):
    """Telefondan paketleri okur; JPEG karelerini çözücüye, durum bilgilerini arayüze iletir."""

    def __init__(self, app, usb, host):
        super().__init__(daemon=True)
        self.app, self.usb, self.host = app, usb, host
        self.stop_ev = threading.Event()
        self.sock = None
        self.wlock = threading.Lock()

    def send_json(self, d):
        s = self.sock
        if s is None:
            return
        body = json.dumps(d).encode("utf-8")
        pkt = bytes([20]) + len(body).to_bytes(4, "big") + body
        with self.wlock:
            try:
                s.sendall(pkt)
            except Exception:
                pass

    def run(self):
        while not self.stop_ev.is_set():
            try:
                self.session()
            except Stop:
                return
            except Exception as e:
                self.app.status = str(e) or e.__class__.__name__
            self.app.info = ""
            for _ in range(15):
                if self.stop_ev.wait(0.1):
                    return

    def recv(self, sock, n):
        buf = bytearray(n)
        mv = memoryview(buf)
        got = 0
        while got < n:
            if self.stop_ev.is_set():
                raise Stop()
            try:
                k = sock.recv_into(mv[got:], n - got)
            except socket.timeout:
                continue
            if k == 0:
                raise ConnectionError("Bağlantı kapandı. Telefonda CamLink açık mı?")
            got += k
        return bytes(buf)

    def session(self):
        host = self.host
        if self.usb:
            self.app.status = "USB: telefon aranıyor..."
            usb_forward()
            host = "127.0.0.1"
        self.app.status = "Telefona bağlanılıyor..."
        sock = socket.create_connection((host, PORT), timeout=5)
        try:
            sock.settimeout(1.0)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
            except Exception:
                pass
            self.sock = sock
            self.app.status = "Bağlandı"
            self.app.events.put(("connected", None))
            while True:
                hdr = self.recv(sock, 5)
                typ = hdr[0]
                n = int.from_bytes(hdr[1:5], "big")
                if n > 8_000_000:
                    raise ConnectionError("Geçersiz veri")
                data = self.recv(sock, n) if n else b""
                self.app.bytes += n + 5
                if typ == 2:
                    self.app.submit(data)
                elif typ == 9:
                    try:
                        _, w, h, fps = data.decode().split(";")
                        self.app.info = f"MJPEG {w}x{h}"
                    except Exception:
                        pass
                elif typ == 10:
                    self.app.events.put(("state", json.loads(data)))
                elif typ == 12:
                    self.app.events.put(("pstats", json.loads(data)))
                elif typ == 8:
                    self.app.info = ""
                    self.app.status = "Telefon yayını durdurdu"
        finally:
            self.sock = None
            try:
                sock.close()
            except Exception:
                pass


class App:
    def __init__(self):
        saved = load_cfg()
        self.A = {**APPDEF, **saved.get("app", {})}
        self.S = {**DEFAULTS, **saved.get("cam", {})}
        ctk.set_appearance_mode(THEMES.get(self.A["theme"], "dark"))
        ctk.set_default_color_theme("blue")
        self.root = ctk.CTk()
        self.root.title("CamLink")
        self.root.geometry("1220x740")
        self.root.minsize(980, 600)

        self.events = queue.Queue()
        self.rx = None
        self.status, self.info = "Hazır", ""
        self.bytes = self.frames = self.dropped = 0
        self.plock = threading.Lock()
        self.pending_jpeg = None
        self.dec_ev = threading.Event()
        self.dec_ms = 0.0
        self.fid = self.shown = 0
        self.disp = self.out = self.photo = None
        self.rot, self.mirror = self.A["rot"], self.A["mirror"]
        self.resample = SCALES.get(self.A["scale"], Image.Resampling.BILINEAR)
        self.view = (0, 0)
        self.ui_lock = False
        self.pending = {}
        self._flush_id = None
        self.stream_on = False
        self.pstats = {}
        self.pushed = False
        self.zoom_ratios = []
        self.exp_step = 1.0
        self.focus_rev = {}
        self.vcam = None
        self.vcam_size = None
        self.closing = False
        self._last = (time.time(), 0, 0)

        self.build_ui()
        self.reflect(self.S)
        threading.Thread(target=self.decode_loop, daemon=True).start()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.root.bind("<F11>", lambda e: self.fullscreen())
        self.root.bind("<Escape>", lambda e: self.root.attributes("-fullscreen", False))
        self.root.after(30, self.tick)
        self.root.after(1000, self.stats_tick)
        if self.A["autoconnect"]:
            self.root.after(600, self.toggle)

    def run(self):
        self.root.mainloop()

    # ---------------- arayüz ----------------
    def head(self, parent, text):
        ctk.CTkLabel(parent, text=text, anchor="w", font=ctk.CTkFont(size=13, weight="bold")).pack(
            fill="x", padx=6, pady=(14, 2))

    def lab(self, parent, text):
        lb = ctk.CTkLabel(parent, text=text, anchor="w", text_color=("gray35", "gray65"))
        lb.pack(fill="x", padx=6, pady=(8, 0))
        return lb

    def switch(self, parent, text, cmd):
        sw = ctk.CTkSwitch(parent, text=text, command=cmd)
        sw.pack(fill="x", padx=6, pady=(8, 0))
        return sw

    def build_ui(self):
        r = self.root
        r.grid_columnconfigure(1, weight=1)
        r.grid_rowconfigure(0, weight=1)
        side = ctk.CTkFrame(r, width=360, corner_radius=0)
        side.grid(row=0, column=0, sticky="nsew")
        side.grid_propagate(False)
        main = ctk.CTkFrame(r, corner_radius=0, fg_color="transparent")
        main.grid(row=0, column=1, sticky="nsew")

        ctk.CTkLabel(side, text="CamLink", font=ctk.CTkFont(size=24, weight="bold")).pack(
            anchor="w", padx=16, pady=(14, 6))

        # --- bağlantı kartı ---
        card = ctk.CTkFrame(side)
        card.pack(fill="x", padx=12, pady=(0, 8))
        self.seg_mode = ctk.CTkSegmentedButton(card, values=["USB", "WiFi"], command=self.on_mode)
        self.seg_mode.pack(fill="x", padx=10, pady=(10, 6))
        self.ip_var = tk.StringVar(value=self.A["ip"])
        self.ip_entry = ctk.CTkEntry(card, textvariable=self.ip_var, placeholder_text="Telefon IP (örn. 192.168.1.20)")
        self.btn_conn = ctk.CTkButton(card, text="Bağlan", height=38, command=self.toggle)
        self.btn_conn.pack(fill="x", padx=10, pady=(6, 6), side="bottom")
        self.lbl_status = ctk.CTkLabel(card, text="Hazır", anchor="w", justify="left", wraplength=300)
        self.lbl_status.pack(fill="x", padx=12, pady=(0, 4), side="bottom")
        self.seg_mode.set("USB" if self.A["mode"] == "usb" else "WiFi")
        self.on_mode(self.seg_mode.get())

        tabs = ctk.CTkTabview(side)
        tabs.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        t1, t2, t3 = tabs.add("Kamera"), tabs.add("Görüntü"), tabs.add("Ayarlar")
        f1 = ctk.CTkScrollableFrame(t1, fg_color="transparent")
        f2 = ctk.CTkScrollableFrame(t2, fg_color="transparent")
        f3 = ctk.CTkScrollableFrame(t3, fg_color="transparent")
        for f in (f1, f2, f3):
            f.pack(fill="both", expand=True)

        # --- Kamera sekmesi ---
        self.btn_stream = ctk.CTkButton(f1, text="Yayını başlat", height=36, command=self.toggle_stream)
        self.btn_stream.pack(fill="x", padx=6, pady=(8, 0))
        self.head(f1, "Kamera")
        self.seg_cam = ctk.CTkSegmentedButton(f1, values=["Arka", "Ön"], command=lambda v: self.q("cam", 1 if v == "Ön" else 0))
        self.seg_cam.pack(fill="x", padx=6)
        self.lab(f1, "Çözünürlük")
        self.om_res = ctk.CTkOptionMenu(f1, values=["1280x720"], command=lambda v: self.q("res", v))
        self.om_res.pack(fill="x", padx=6)
        self.lab(f1, "Hedef FPS")
        self.seg_fps = ctk.CTkSegmentedButton(f1, values=["15", "24", "30", "60"], command=lambda v: self.q("fps", int(v)))
        self.seg_fps.pack(fill="x", padx=6)
        self.lb_jq = self.lab(f1, "JPEG kalitesi: 85")
        self.sl_jq = ctk.CTkSlider(f1, from_=40, to=100, number_of_steps=60, command=self.on_jq)
        self.sl_jq.pack(fill="x", padx=6)
        ctk.CTkLabel(f1, anchor="w", justify="left", wraplength=300, text_color=("gray40", "gray60"),
                     text="Düşük kalite = daha hızlı ve akıcı. 75-85 arası çoğu durumda yeterli.").pack(
            fill="x", padx=6, pady=(2, 0))
        self.sw_vfps = self.switch(f1, "Karanlıkta fps düşebilir (daha parlak)", lambda: self.q("vfps", bool(self.sw_vfps.get())))
        self.sw_prev = self.switch(f1, "Telefon ekranında önizleme", lambda: self.q("preview", bool(self.sw_prev.get())))

        # --- Görüntü sekmesi ---
        self.head(f2, "Odak")
        self.om_focus = ctk.CTkOptionMenu(f2, values=["-"], command=lambda v: self.q("focus", self.focus_rev.get(v, v)))
        self.om_focus.pack(fill="x", padx=6)
        ctk.CTkButton(f2, text="Şimdi odakla", command=lambda: self.send({"action": "af"})).pack(fill="x", padx=6, pady=(8, 0))
        self.head(f2, "Işık")
        self.lb_exp = self.lab(f2, "Pozlama: 0.0 EV")
        self.sl_exp = ctk.CTkSlider(f2, from_=-1, to=1, number_of_steps=2, command=self.on_exp)
        self.sl_exp.pack(fill="x", padx=6)
        self.sw_ae = self.switch(f2, "Pozlamayı kilitle", lambda: self.q("ae_lock", bool(self.sw_ae.get())))
        self.lab(f2, "ISO")
        self.om_iso = ctk.CTkOptionMenu(f2, values=["auto"], command=lambda v: self.q("iso", v))
        self.om_iso.pack(fill="x", padx=6)
        self.sw_torch = self.switch(f2, "Fener", lambda: self.q("torch", bool(self.sw_torch.get())))
        self.head(f2, "Renk")
        self.om_wb = ctk.CTkOptionMenu(f2, values=["-"], command=lambda v: self.q("wb", untr(WB_TR, v)))
        self.om_wb.pack(fill="x", padx=6)
        self.sw_awb = self.switch(f2, "Beyaz dengesini kilitle", lambda: self.q("awb_lock", bool(self.sw_awb.get())))
        self.head(f2, "Yakınlaştırma")
        self.lb_zoom = self.lab(f2, "Yakınlaştırma: 1.0x")
        self.sl_zoom = ctk.CTkSlider(f2, from_=0, to=1, number_of_steps=1, command=self.on_zoom)
        self.sl_zoom.pack(fill="x", padx=6)
        self.head(f2, "Diğer")
        self.lab(f2, "Titreşim önleme (ışık flicker)")
        self.om_anti = ctk.CTkOptionMenu(f2, values=["-"], command=lambda v: self.q("anti", untr(ANTI_TR, v)))
        self.om_anti.pack(fill="x", padx=6)
        self.sw_stab = self.switch(f2, "Video sabitleme", lambda: self.q("stab", bool(self.sw_stab.get())))
        ctk.CTkButton(f2, text="Kamera ayarlarını sıfırla", fg_color="gray40", hover_color="gray30",
                      command=self.reset_cam).pack(fill="x", padx=6, pady=(16, 8))

        # --- Ayarlar sekmesi (PC uygulaması) ---
        self.head(f3, "Görünüm")
        self.lab(f3, "Tema")
        seg_theme = ctk.CTkSegmentedButton(f3, values=list(THEMES), command=self.on_theme)
        seg_theme.pack(fill="x", padx=6)
        seg_theme.set(self.A["theme"])
        self.lab(f3, "Önizleme ölçekleme")
        seg_scale = ctk.CTkSegmentedButton(f3, values=list(SCALES), command=self.on_scale)
        seg_scale.pack(fill="x", padx=6)
        seg_scale.set(self.A["scale"])
        sw = self.switch(f3, "Ekranda istatistik göster", lambda: self.on_overlay(sw))
        sw.select() if self.A["overlay"] else sw.deselect()
        self.head(f3, "Başlangıç")
        sw2 = self.switch(f3, "Açılışta otomatik bağlan", lambda: self.on_auto(sw2))
        sw2.select() if self.A["autoconnect"] else sw2.deselect()
        self.head(f3, "Hakkında")
        ctk.CTkLabel(f3, anchor="w", justify="left", wraplength=300, text_color=("gray40", "gray60"),
                     text="CamLink 3.0\nKısayollar: F11 tam ekran, Esc çıkış, S fotoğraf\n"
                          "Sanal kamera için OBS Studio kurulu olmalı.").pack(fill="x", padx=6)

        # --- sağ taraf: araç çubuğu + görüntü + durum çubuğu ---
        bar = ctk.CTkFrame(main, height=46)
        bar.pack(fill="x", padx=10, pady=(10, 6))
        ctk.CTkButton(bar, text="Döndür 90°", width=100, command=self.rotate).pack(side="left", padx=8, pady=8)
        self.sw_mirror = ctk.CTkSwitch(bar, text="Aynala", command=self.on_mirror)
        self.sw_mirror.pack(side="left", padx=8)
        self.sw_vcam = ctk.CTkSwitch(bar, text="Sanal kamera")
        self.sw_vcam.pack(side="left", padx=8)
        ctk.CTkButton(bar, text="Tam ekran", width=90, fg_color="gray40", hover_color="gray30",
                      command=self.fullscreen).pack(side="right", padx=8)
        ctk.CTkButton(bar, text="Fotoğraf çek", width=100, command=self.snapshot).pack(side="right", padx=4)
        if self.mirror:
            self.sw_mirror.select()

        self.canvas = tk.Canvas(main, bg="black", highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=10)
        self.canvas.bind("<Configure>", lambda e: setattr(self, "view", (e.width, e.height)))
        self.cimg = self.canvas.create_image(0, 0, anchor="center")
        self.hint = self.canvas.create_text(0, 0, fill="#9aa0ac", font=("Segoe UI", 13), justify="center",
                                            text="Telefonda CamLink'i aç, burada Bağlan'a bas")
        self.overlay = self.canvas.create_text(12, 10, anchor="nw", fill="#35ff9a", font=("Consolas", 11), text="")
        self.stat = ctk.CTkLabel(main, text="", anchor="w")
        self.stat.pack(fill="x", padx=14, pady=(6, 8))
        self.root.bind("s", lambda e: self.snapshot())

    # ---------------- ayar gönderme ----------------
    def send(self, d):
        rx = self.rx
        if rx is not None:
            rx.send_json(d)

    def q(self, key, val):
        if self.ui_lock:
            return
        self.S[key] = val
        self.pending[key] = val
        if self._flush_id is None:
            self._flush_id = self.root.after(60, self.flush)

    def flush(self):
        self._flush_id = None
        if self.pending:
            d, self.pending = self.pending, {}
            self.send(d)
            self.save()

    def on_jq(self, v):
        v = int(v)
        self.lb_jq.configure(text=f"JPEG kalitesi: {v}")
        self.q("jq", v)

    def on_exp(self, v):
        v = int(round(v))
        self.lb_exp.configure(text=f"Pozlama: {v * self.exp_step:+.1f} EV")
        self.q("exp", v)

    def on_zoom(self, v):
        v = int(round(v))
        self.lb_zoom.configure(text=f"Yakınlaştırma: {self.zoom_text(v)}")
        self.q("zoom", v)

    def zoom_text(self, idx):
        r = self.zoom_ratios[idx] if 0 <= idx < len(self.zoom_ratios) else 100
        return f"{r / 100:.1f}x"

    def reset_cam(self):
        self.S = dict(DEFAULTS)
        self.reflect(self.S)
        self.send(dict(self.S))
        self.save()

    def toggle_stream(self):
        self.send({"action": "stop" if self.stream_on else "start"})

    # ---------------- telefondan gelen durum ----------------
    def drain(self):
        try:
            while True:
                kind, val = self.events.get_nowait()
                if kind == "state":
                    self.on_state(val)
                elif kind == "pstats":
                    self.pstats = val
                elif kind == "connected":
                    self.pushed = False
        except queue.Empty:
            pass

    def on_state(self, ev):
        caps = ev.get("caps")
        if caps:
            self.apply_caps(caps)
        self.stream_on = bool(ev.get("running"))
        self.btn_stream.configure(text="Yayını durdur" if self.stream_on else "Yayını başlat")
        if not self.pushed:
            self.pushed = True
            self.send({**self.S, "action": "start"})
            return
        st = ev.get("state")
        if st:
            self.reflect(st)

    def apply_caps(self, c):
        self.ui_lock = True
        try:
            sizes = c.get("sizes") or []
            if sizes:
                self.om_res.configure(values=sizes)
            fps = [str(x) for x in (c.get("fps") or [])]
            if fps:
                self.seg_fps.configure(values=fps)
            self.seg_cam.configure(state="normal" if c.get("cams", 1) > 1 else "disabled")
            fl = c.get("focus") or []
            self.focus_rev = {tr(FOCUS_TR, x): x for x in fl}
            self.om_focus.configure(values=[tr(FOCUS_TR, x) for x in fl] or ["-"], state="normal" if fl else "disabled")
            wb = c.get("wb") or []
            self.om_wb.configure(values=[tr(WB_TR, x) for x in wb] or ["-"], state="normal" if wb else "disabled")
            an = c.get("anti") or []
            self.om_anti.configure(values=[tr(ANTI_TR, x) for x in an] or ["-"], state="normal" if an else "disabled")
            iso = [x for x in (c.get("iso") or []) if x]
            self.om_iso.configure(values=iso or ["auto"], state="normal" if iso else "disabled")
            e = c.get("exp") or {}
            mn, mx = e.get("min", 0), e.get("max", 0)
            if mx > mn:
                self.exp_step = e.get("step", 1.0) or 1.0
                self.sl_exp.configure(from_=mn, to=mx, number_of_steps=mx - mn, state="normal")
            else:
                self.sl_exp.configure(state="disabled")
            z = c.get("zoom")
            if z and z.get("max", 0) > 0:
                self.zoom_ratios = z.get("ratios") or []
                self.sl_zoom.configure(from_=0, to=z["max"], number_of_steps=z["max"], state="normal")
            else:
                self.zoom_ratios = []
                self.sl_zoom.configure(state="disabled")
            for key, sw in (("ae_lock", self.sw_ae), ("awb_lock", self.sw_awb), ("torch", self.sw_torch), ("stab", self.sw_stab)):
                sw.configure(state="normal" if c.get(key) else "disabled")
        finally:
            self.ui_lock = False

    @staticmethod
    def _sw(sw, val):
        sw.select() if val else sw.deselect()

    def reflect(self, st):
        self.ui_lock = True
        try:
            self.S.update(st)
            S = self.S
            self.seg_cam.set("Ön" if S["cam"] else "Arka")
            self.om_res.set(str(S["res"]))
            try:
                self.seg_fps.set(str(S["fps"]))
            except Exception:
                pass
            self.sl_jq.set(S["jq"])
            self.lb_jq.configure(text=f"JPEG kalitesi: {S['jq']}")
            self._sw(self.sw_vfps, S["vfps"])
            self._sw(self.sw_prev, S["preview"])
            self.om_focus.set(tr(FOCUS_TR, S["focus"]))
            self.sl_exp.set(S["exp"])
            self.lb_exp.configure(text=f"Pozlama: {S['exp'] * self.exp_step:+.1f} EV")
            self._sw(self.sw_ae, S["ae_lock"])
            self.om_iso.set(str(S["iso"]))
            self._sw(self.sw_torch, S["torch"])
            self.om_wb.set(tr(WB_TR, S["wb"]))
            self._sw(self.sw_awb, S["awb_lock"])
            self.sl_zoom.set(S["zoom"])
            self.lb_zoom.configure(text=f"Yakınlaştırma: {self.zoom_text(int(S['zoom']))}")
            self.om_anti.set(tr(ANTI_TR, S["anti"]))
            self._sw(self.sw_stab, S["stab"])
        finally:
            self.ui_lock = False

    # ---------------- bağlantı ----------------
    def on_mode(self, v):
        if v == "WiFi":
            self.ip_entry.pack(fill="x", padx=10, pady=(0, 4), before=self.lbl_status)
        else:
            self.ip_entry.pack_forget()

    def toggle(self):
        if self.rx is not None:
            self.rx.stop_ev.set()
            self.rx = None
            self.btn_conn.configure(text="Bağlan")
            self.status, self.info, self.stream_on = "Bağlantı kesildi", "", False
            self.btn_stream.configure(text="Yayını başlat")
            return
        usb = self.seg_mode.get() == "USB"
        host = self.ip_var.get().strip()
        if not usb and not host:
            messagebox.showinfo("CamLink", "Telefonun ekranında yazan WiFi IP adresini gir.")
            return
        self.A["mode"], self.A["ip"] = ("usb" if usb else "wifi"), host
        self.save()
        self.rx = Receiver(self, usb, host)
        self.rx.start()
        self.btn_conn.configure(text="Bağlantıyı kes")

    # ---------------- görüntü hattı ----------------
    def submit(self, data):
        with self.plock:
            if self.pending_jpeg is not None:
                self.dropped += 1
            self.pending_jpeg = data
        self.dec_ev.set()

    def decode_loop(self):
        while not self.closing:
            self.dec_ev.wait(0.5)
            self.dec_ev.clear()
            with self.plock:
                data, self.pending_jpeg = self.pending_jpeg, None
            if data is None:
                continue
            try:
                t0 = time.perf_counter()
                img = Image.open(BytesIO(data))
                img.load()
                if img.mode != "RGB":
                    img = img.convert("RGB")
                self.process(img)
                self.dec_ms = self.dec_ms * 0.9 + (time.perf_counter() - t0) * 100.0
            except Exception:
                pass

    def process(self, img):
        if self.rot:
            img = img.transpose(ROT[self.rot])
        if self.mirror:
            img = img.transpose(Image.Transpose.FLIP_LEFT_RIGHT)
        vw, vh = self.view
        disp = img
        if vw > 10 and vh > 10:
            s = min(vw / img.width, vh / img.height)
            disp = img.resize((max(1, int(img.width * s)), max(1, int(img.height * s))), self.resample)
        self.out = img
        self.disp = disp
        self.frames += 1
        self.fid += 1

    def tick(self):
        try:
            self.drain()
            if self.fid != self.shown and self.disp is not None:
                self.shown = self.fid
                self.photo = ImageTk.PhotoImage(self.disp)
                cw, ch = self.view
                self.canvas.itemconfig(self.cimg, image=self.photo)
                self.canvas.coords(self.cimg, cw // 2, ch // 2)
                self.canvas.itemconfig(self.hint, state="hidden")
                if self.sw_vcam.get():
                    self.send_vcam(self.out)
            else:
                cw, ch = self.view
                self.canvas.coords(self.hint, cw // 2, ch // 2)
            if not self.sw_vcam.get() and self.vcam is not None:
                self.close_vcam()
        except Exception as e:
            self.status = f"Gösterim hatası: {e}"
        if not self.closing:
            self.root.after(5, self.tick)

    def close_vcam(self):
        try:
            self.vcam.close()
        except Exception:
            pass
        self.vcam = None

    def send_vcam(self, img):
        if img is None:
            return
        if pyvirtualcam is None:
            self.sw_vcam.deselect()
            messagebox.showerror("CamLink", "Sanal kamera modülü yüklü değil.")
            return
        if self.vcam is None or self.vcam_size != img.size:
            if self.vcam is not None:
                self.close_vcam()
            try:
                self.vcam = pyvirtualcam.Camera(img.width, img.height, 30, fmt=pyvirtualcam.PixelFormat.RGB)
                self.vcam_size = img.size
            except Exception as e:
                self.vcam = None
                self.sw_vcam.deselect()
                messagebox.showerror("CamLink", "Sanal kamera açılamadı.\nOBS Studio'yu (obsproject.com) kur, "
                                     "bir kez aç, kapat, sonra tekrar dene.\n\n" + str(e))
                return
        self.vcam.send(np.array(img))

    def stats_tick(self):
        now = time.time()
        t0, f0, b0 = self._last
        dt = max(now - t0, 0.001)
        fps = (self.frames - f0) / dt
        mbit = (self.bytes - b0) * 8 / dt / 1e6
        self._last = (now, self.frames, self.bytes)
        live = fps > 0.5
        ps = self.pstats if self.stream_on else {}
        txt = self.status
        if live:
            txt = "● Yayın alınıyor"
        elif self.rx is not None and self.stream_on:
            txt = "Telefon yayında ama kare gelmiyor, 'Telefon ekranında önizleme'yi aç"
        self.lbl_status.configure(text=txt, text_color=("#0a8f3c", "#35ff9a") if live else ("gray30", "gray70"))
        extra = f"{self.info}   {fps:.0f} fps   {mbit:.1f} Mbit/s   çözme {self.dec_ms:.0f} ms   atlanan {self.dropped}"
        if ps:
            extra += f"   |   telefon: {ps.get('cap', 0)} yakalama / {ps.get('sent', 0)} gönderim fps, JPEG {ps.get('ms', 0)} ms"
        self.stat.configure(text=extra if (live or ps) else self.status)
        self.canvas.itemconfig(self.overlay, text=f"{fps:.0f} fps  {mbit:.1f} Mbit/s" if (live and self.A["overlay"]) else "")
        if not self.closing:
            self.root.after(1000, self.stats_tick)

    # ---------------- PC ayarları ----------------
    def rotate(self):
        self.rot = (self.rot + 90) % 360
        self.A["rot"] = self.rot
        self.save()

    def on_mirror(self):
        self.mirror = bool(self.sw_mirror.get())
        self.A["mirror"] = self.mirror
        self.save()

    def on_theme(self, v):
        self.A["theme"] = v
        ctk.set_appearance_mode(THEMES.get(v, "dark"))
        self.save()

    def on_scale(self, v):
        self.A["scale"] = v
        self.resample = SCALES.get(v, Image.Resampling.BILINEAR)
        self.save()

    def on_overlay(self, sw):
        self.A["overlay"] = bool(sw.get())
        self.save()

    def on_auto(self, sw):
        self.A["autoconnect"] = bool(sw.get())
        self.save()

    def fullscreen(self):
        self.root.attributes("-fullscreen", not self.root.attributes("-fullscreen"))

    def snapshot(self):
        if self.out is None:
            self.status = "Henüz görüntü yok"
            return
        d = os.path.join(os.path.expanduser("~"), "Pictures", "CamLink")
        os.makedirs(d, exist_ok=True)
        p = os.path.join(d, time.strftime("camlink_%Y%m%d_%H%M%S.png"))
        self.out.save(p)
        self.status = "Fotoğraf kaydedildi: " + p

    def save(self):
        save_cfg({"app": self.A, "cam": self.S})

    def close(self):
        self.closing = True
        self.save()
        if self.rx is not None:
            self.rx.stop_ev.set()
        if self.vcam is not None:
            self.close_vcam()
        self.root.destroy()


if __name__ == "__main__":
    App().run()
