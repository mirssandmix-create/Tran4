"""Ghost Manga 5 — แปลมังงะ/มันฮวา/มานฮวาในหน้าเว็บ (Windows)."""
from __future__ import annotations

import logging
import os
import queue
import re
import sys
import threading
import tkinter as tk
from tkinter import filedialog, font as tkfont, scrolledtext

import ttkbootstrap as ttk
from ttkbootstrap.constants import *  # noqa: F401,F403
from ttkbootstrap.dialogs import Messagebox

from gm.config import APP_NAME, APP_VERSION, ENGINES, SOURCE_LANGS, TARGET_LANGS, Settings, resource_path

LOG = logging.getLogger("ghostmanga")


class QueueLogHandler(logging.Handler):
    """Thread-safe: worker threads only put into a queue, the Tk thread drains it."""

    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def emit(self, record):
        try:
            self.q.put(("log", record.levelno, self.format(record)))
        except Exception:
            pass


def _label_for(options, code):
    return next((label for c, label in options if c == code), options[0][1])


def _code_for(options, label):
    return next((c for c, lab in options if lab == label), options[0][0])


class SettingsDialog(ttk.Toplevel):
    def __init__(self, app: "App"):
        super().__init__(title="ตั้งค่าตัวแปลและการแสดงผล", resizable=(True, True))
        self.app, s = app, app.settings
        self.transient(app.root)
        self.geometry("620x640")
        nb = ttk.Notebook(self, padding=8)
        nb.pack(fill=BOTH, expand=True)
        self.vars: dict[str, tk.Variable] = {}

        def entry(parent, row, label, key, show=None, width=48):
            ttk.Label(parent, text=label).grid(row=row, column=0, sticky=W, padx=(0, 8), pady=4)
            v = tk.StringVar(value=str(getattr(s, key)))
            self.vars[key] = v
            ttk.Entry(parent, textvariable=v, width=width, show=show).grid(row=row, column=1, sticky=EW, pady=4)
            return v

        # --- local AI
        f = ttk.Frame(nb, padding=10); f.columnconfigure(1, weight=1)
        nb.add(f, text="AI ในเครื่อง (ฟรี)")
        ttk.Label(f, wraplength=560, justify=LEFT, text=(
            "ใช้ LM Studio (แนะนำสำหรับการ์ดจอ AMD) หรือ Ollama รันโมเดลในเครื่อง ไม่เสียเงิน\n"
            "1) ติดตั้ง LM Studio  2) ดาวน์โหลดโมเดลที่เก่งภาษาไทย เช่น Gemma 3 12B หรือ Typhoon (ภาษาไทยโดยเฉพาะ)\n"
            "3) เปิดแท็บ Developer แล้วกด Start Server (ค่าเริ่มต้น http://localhost:1234/v1)\n"
            "Ollama ใช้ http://localhost:11434/v1")).grid(row=0, column=0, columnspan=2, sticky=W, pady=(0, 8))
        entry(f, 1, "Server URL", "local_base_url")
        entry(f, 2, "ชื่อโมเดล (ว่าง = ใช้ตัวที่โหลดอยู่)", "local_model")
        ttk.Button(f, text="ทดสอบการเชื่อมต่อ", bootstyle=INFO, command=self.test_local).grid(row=3, column=1, sticky=W, pady=6)
        self.local_status = ttk.Label(f, text="", wraplength=540)
        self.local_status.grid(row=4, column=0, columnspan=2, sticky=W)

        # --- gemini
        f = ttk.Frame(nb, padding=10); f.columnconfigure(1, weight=1)
        nb.add(f, text="Gemini")
        ttk.Label(f, wraplength=560, justify=LEFT, text="สร้าง API key ฟรีได้ที่ aistudio.google.com (มีโควตาฟรีแต่จำกัดจำนวนครั้ง)").grid(
            row=0, column=0, columnspan=2, sticky=W, pady=(0, 8))
        entry(f, 1, "API key", "gemini_api_key", show="•")
        entry(f, 2, "โมเดล", "gemini_model")

        # --- claude
        f = ttk.Frame(nb, padding=10); f.columnconfigure(1, weight=1)
        nb.add(f, text="Claude")
        ttk.Label(f, wraplength=560, justify=LEFT, text="สร้าง API key ที่ platform.claude.com (เสียเงินตามการใช้งาน)").grid(
            row=0, column=0, columnspan=2, sticky=W, pady=(0, 8))
        entry(f, 1, "API key", "claude_api_key", show="•")
        entry(f, 2, "โมเดล", "claude_model")
        ttk.Label(f, text="ความละเอียดการคิด (effort)").grid(row=3, column=0, sticky=W, pady=4)
        v = tk.StringVar(value=s.claude_effort); self.vars["claude_effort"] = v
        ttk.Combobox(f, textvariable=v, values=["low", "medium", "high"], state="readonly", width=10).grid(row=3, column=1, sticky=W)

        # --- openai compatible
        f = ttk.Frame(nb, padding=10); f.columnconfigure(1, weight=1)
        nb.add(f, text="OpenAI-compatible")
        ttk.Label(f, wraplength=560, justify=LEFT, text="OpenAI, DeepSeek, OpenRouter หรือบริการที่ใช้รูปแบบเดียวกัน").grid(
            row=0, column=0, columnspan=2, sticky=W, pady=(0, 8))
        entry(f, 1, "Base URL", "openai_base_url")
        entry(f, 2, "API key", "openai_api_key", show="•")
        entry(f, 3, "โมเดล", "openai_model")

        # --- translation style
        f = ttk.Frame(nb, padding=10); f.columnconfigure(0, weight=1); f.rowconfigure(3, weight=1)
        nb.add(f, text="คำแปล/ชื่อตัวละคร")
        v = tk.BooleanVar(value=s.send_image_to_ai); self.vars["send_image_to_ai"] = v
        ttk.Checkbutton(f, variable=v, bootstyle="round-toggle",
                        text="ส่งภาพทั้งหน้าให้ AI ดูด้วย (รู้ว่าใครพูด แปลน้ำเสียงได้ดีขึ้น แต่ช้าลง/เปลืองขึ้น ต้องเป็นโมเดลที่ดูภาพได้)").grid(
            row=0, column=0, sticky=W, pady=(0, 8))
        ttk.Label(f, text="คำศัพท์/ชื่อตัวละครที่ต้องการให้แปลแบบเดิมเสมอ (บรรทัดละ 1 คำ เช่น  タロウ = ทาโร่ )").grid(row=2, column=0, sticky=W)
        self.glossary = scrolledtext.ScrolledText(f, height=10, font=("Leelawadee UI", 10))
        self.glossary.grid(row=3, column=0, sticky=NSEW)
        self.glossary.insert("1.0", s.glossary)

        # --- display / processing
        f = ttk.Frame(nb, padding=10); f.columnconfigure(1, weight=1)
        nb.add(f, text="การแสดงผล")
        ttk.Label(f, text="ฟอนต์คำแปล").grid(row=0, column=0, sticky=W, pady=4)
        fams = sorted({x for x in tkfont.families() if not x.startswith("@")})
        preferred = [x for x in ("Leelawadee UI", "Leelawadee", "Tahoma", "Noto Sans Thai", "Sarabun", "Mitr", "Itim",
                                 "Kanit", "Prompt", "Mali", "Sriracha") if x in fams]
        v = tk.StringVar(value=s.font_family); self.vars["font_family"] = v
        ttk.Combobox(f, textvariable=v, values=preferred + [x for x in fams if x not in preferred], width=30).grid(row=0, column=1, sticky=W)
        v = tk.BooleanVar(value=s.font_bold); self.vars["font_bold"] = v
        ttk.Checkbutton(f, text="ตัวหนา", variable=v).grid(row=1, column=1, sticky=W, pady=4)
        ttk.Label(f, text="ขนาดตัวอักษรเล็กสุด (px)").grid(row=2, column=0, sticky=W, pady=4)
        v = tk.IntVar(value=s.min_font_px); self.vars["min_font_px"] = v
        ttk.Spinbox(f, from_=8, to=30, textvariable=v, width=6).grid(row=2, column=1, sticky=W)
        ttk.Label(f, text="แปลพร้อมกันกี่รูป").grid(row=3, column=0, sticky=W, pady=4)
        v = tk.IntVar(value=s.concurrency); self.vars["concurrency"] = v
        ttk.Spinbox(f, from_=1, to=8, textvariable=v, width=6).grid(row=3, column=1, sticky=W)
        ttk.Label(f, text="ข้ามรูปที่เล็กกว่า (px)").grid(row=4, column=0, sticky=W, pady=4)
        v = tk.IntVar(value=s.min_image_side); self.vars["min_image_side"] = v
        ttk.Spinbox(f, from_=50, to=1000, increment=50, textvariable=v, width=6).grid(row=4, column=1, sticky=W)
        v = tk.BooleanVar(value=s.keep_browser_profile); self.vars["keep_browser_profile"] = v
        ttk.Checkbutton(f, variable=v, text="จำการล็อกอิน/คุกกี้ของเว็บไว้ (ผ่าน Cloudflare ครั้งเดียวพอ)").grid(
            row=5, column=0, columnspan=2, sticky=W, pady=8)

        bar = ttk.Frame(self, padding=8)
        bar.pack(fill=X)
        ttk.Button(bar, text="บันทึก", bootstyle=SUCCESS, command=self.save).pack(side=RIGHT)
        ttk.Button(bar, text="ยกเลิก", bootstyle=SECONDARY, command=self.destroy).pack(side=RIGHT, padx=8)

    def _apply(self):
        s = self.app.settings
        for key, v in self.vars.items():
            try:
                val = v.get()
            except tk.TclError:
                continue
            if isinstance(getattr(s, key), str):
                val = str(val).strip()
            setattr(s, key, val)
        s.glossary = self.glossary.get("1.0", "end").strip()

    def save(self):
        self._apply()
        self.app.settings.save()
        if self.app.session and self.app.session.running:
            LOG.info("บันทึกการตั้งค่าแล้ว — จะมีผลเมื่อกดเริ่มแปลครั้งถัดไป")
        self.destroy()

    def test_local(self):
        # read only the URL field: testing must not commit other edits (Cancel still cancels)
        self.local_status.configure(text="กำลังตรวจสอบ…")
        base = self.vars["local_base_url"].get().strip().rstrip("/")

        def work():
            import httpx
            try:
                r = httpx.get(base + "/models", timeout=5)
                r.raise_for_status()
                models = [m.get("id") for m in r.json().get("data", [])]
                msg = "เชื่อมต่อได้ ✓ โมเดลที่มี: " + (", ".join(models) if models else "(ยังไม่ได้โหลดโมเดล)")
            except Exception as e:
                msg = f"เชื่อมต่อไม่ได้: {e}\nเปิด LM Studio → Developer → Start Server แล้วหรือยัง?"
            self.app.events.put(("call", lambda: self.local_status.winfo_exists() and self.local_status.configure(text=msg)))

        threading.Thread(target=work, daemon=True).start()


class App:
    def __init__(self, root: ttk.Window):
        self.root = root
        self.settings = Settings.load()
        self.events: queue.Queue = queue.Queue()
        self.session = None
        root.title(f"{APP_NAME}  v{APP_VERSION}")
        root.geometry("820x700")
        root.minsize(680, 560)
        self._set_icon()
        self._build()
        handler = QueueLogHandler(self.events)
        handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.INFO)
        try:   # same log on disk (overwritten each launch) so problems can be looked at later
            from gm.config import local_data_dir
            fh = logging.FileHandler(os.path.join(local_data_dir(), "log.txt"), mode="w", encoding="utf-8")
            fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
            logging.getLogger().addHandler(fh)
        except Exception:
            pass
        for noisy in ("httpx", "httpcore", "seleniumbase", "selenium", "urllib3", "anthropic", "httpx2",
                      "websockets", "asyncio", "chrome_lens_py", "filelock"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        for very_noisy in ("uc", "uc.connection", "uc.browser", "uc.tab"):  # CDP event-parsing chatter
            logging.getLogger(very_noisy).setLevel(logging.CRITICAL)
        self._install_thai_keyboard_shortcuts()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self._drain)
        if getattr(self.settings, "load_warning", ""):
            LOG.warning(self.settings.load_warning)

    # ---------------------------------------------------------------- ui ---
    def _set_icon(self):
        if sys.platform.startswith("win"):
            try:
                import ctypes
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("GhostManga5")
            except Exception:
                pass
        try:
            ico = resource_path("icon.ico")
            if os.path.exists(ico):
                self.root.iconbitmap(ico)
            png = resource_path("logo.png")
            if os.path.exists(png):
                self.root.iconphoto(True, tk.PhotoImage(file=png))
        except Exception:
            pass

    def _build(self):
        s = self.settings
        m = ttk.Frame(self.root, padding=12)
        m.pack(fill=BOTH, expand=True)
        m.columnconfigure(0, weight=1)

        r = ttk.Frame(m); r.grid(row=0, column=0, sticky=EW, pady=(0, 8)); r.columnconfigure(1, weight=1)
        ttk.Label(r, text="ลิงก์หน้ามังงะ").grid(row=0, column=0, padx=(0, 8))
        self.url = tk.StringVar(value=s.last_url)
        self.url_entry = ttk.Entry(r, textvariable=self.url)
        self.url_entry.grid(row=0, column=1, sticky=EW)
        ttk.Button(r, text="วาง", bootstyle=SECONDARY, command=self.paste_url).grid(row=0, column=2, padx=(6, 0))

        o = ttk.Labelframe(m, text="การแปล", padding=8); o.grid(row=1, column=0, sticky=EW)
        ttk.Label(o, text="ภาษาต้นฉบับ").grid(row=0, column=0, sticky=W, padx=(0, 6))
        self.src = tk.StringVar(value=_label_for(SOURCE_LANGS, s.source_lang))
        ttk.Combobox(o, textvariable=self.src, values=[x[1] for x in SOURCE_LANGS], state="readonly", width=22).grid(row=0, column=1, sticky=W)
        ttk.Label(o, text="แปลเป็น").grid(row=0, column=2, sticky=W, padx=(16, 6))
        self.tgt = tk.StringVar(value=_label_for(TARGET_LANGS, s.target_lang))
        ttk.Combobox(o, textvariable=self.tgt, values=[x[1] for x in TARGET_LANGS], state="readonly", width=16).grid(row=0, column=3, sticky=W)
        ttk.Label(o, text="ตัวแปล").grid(row=1, column=0, sticky=W, padx=(0, 6), pady=(8, 0))
        self.engine = tk.StringVar(value=_label_for(ENGINES, s.engine))
        ttk.Combobox(o, textvariable=self.engine, values=[x[1] for x in ENGINES], state="readonly", width=38).grid(
            row=1, column=1, columnspan=3, sticky=W, pady=(8, 0))
        ttk.Button(o, text="ตั้งค่าตัวแปล…", bootstyle=INFO, command=lambda: SettingsDialog(self)).grid(
            row=1, column=3, sticky=E, pady=(8, 0))
        o.columnconfigure(3, weight=1)

        sv = ttk.Labelframe(m, text="บันทึกรูป", padding=8); sv.grid(row=2, column=0, sticky=EW, pady=(8, 0)); sv.columnconfigure(1, weight=1)
        ttk.Label(sv, text="โฟลเดอร์").grid(row=0, column=0, padx=(0, 6))
        self.save_dir = tk.StringVar(value=s.save_dir or os.path.join(os.path.expanduser("~"), "Pictures", "GhostManga"))
        ttk.Entry(sv, textvariable=self.save_dir).grid(row=0, column=1, sticky=EW)
        ttk.Button(sv, text="เลือก…", bootstyle=SECONDARY, command=self.pick_dir).grid(row=0, column=2, padx=(6, 0))
        self.auto_save = tk.BooleanVar(value=s.auto_save)
        ttk.Checkbutton(sv, text="บันทึกอัตโนมัติทุกรูปที่แปลเสร็จ (แยกโฟลเดอร์ตามชื่อตอน ชื่อไฟล์เรียงตามลำดับในหน้า)",
                        variable=self.auto_save, bootstyle="round-toggle", command=self._push_save_opts).grid(
            row=1, column=0, columnspan=3, sticky=W, pady=(6, 0))

        b = ttk.Frame(m); b.grid(row=3, column=0, sticky=EW, pady=10)
        self.btn_start = ttk.Button(b, text="▶ เริ่มแปล", bootstyle=SUCCESS, command=self.start, width=12)
        self.btn_start.pack(side=LEFT)
        self.btn_stop = ttk.Button(b, text="■ หยุด", bootstyle=DANGER, command=self.stop, width=8, state=DISABLED)
        self.btn_stop.pack(side=LEFT, padx=6)
        self.btn_save = ttk.Button(b, text="💾 บันทึกตอนนี้", bootstyle=SECONDARY, command=self.save_now, state=DISABLED)
        self.btn_save.pack(side=LEFT, padx=6)
        self.btn_retry = ttk.Button(b, text="↻ แปลรูปที่พลาดใหม่", bootstyle=WARNING, command=self.retry, state=DISABLED)
        self.btn_retry.pack(side=LEFT, padx=6)
        self.btn_toggle = ttk.Button(b, text="⇄ ดูต้นฉบับ", bootstyle=SECONDARY, command=self.toggle, state=DISABLED)
        self.btn_toggle.pack(side=LEFT, padx=6)

        p = ttk.Frame(m); p.grid(row=4, column=0, sticky=EW); p.columnconfigure(0, weight=1)
        self.pb = ttk.Progressbar(p, mode="determinate", bootstyle=SUCCESS)
        self.pb.grid(row=0, column=0, sticky=EW)
        self.progress = tk.StringVar(value="ยังไม่เริ่ม")
        ttk.Label(p, textvariable=self.progress, width=34, anchor=E).grid(row=0, column=1, padx=(8, 0))

        self.log = scrolledtext.ScrolledText(m, height=14, font=("Leelawadee UI", 10), wrap=WORD)
        self.log.grid(row=5, column=0, sticky=NSEW, pady=(8, 0))
        self.log.tag_configure("warn", foreground="#e0a000")
        self.log.tag_configure("err", foreground="#ff5555")
        m.rowconfigure(5, weight=1)
        ttk.Label(m, text="เคล็ดลับ: ในหน้าเว็บกด Alt+T เพื่อสลับดูต้นฉบับ/คำแปล", bootstyle=SECONDARY).grid(
            row=6, column=0, sticky=W, pady=(6, 0))
        self.url_entry.focus_set()

    def _install_thai_keyboard_shortcuts(self):
        """Ctrl+C/V/X/A don't work in Tk when the keyboard is in Thai layout: map by control char."""
        mapping = {"\x16": "<<Paste>>", "\x03": "<<Copy>>", "\x18": "<<Cut>>"}

        def on_key(event):
            ch = getattr(event, "char", "")
            if ch in mapping and event.keysym.lower() not in ("v", "c", "x"):
                try:
                    event.widget.event_generate(mapping[ch])
                except Exception:
                    pass
                return "break"
            if ch == "\x01" and event.keysym.lower() != "a":
                try:
                    event.widget.select_range(0, "end")
                except Exception:
                    try:
                        event.widget.tag_add("sel", "1.0", "end")
                    except Exception:
                        pass
                return "break"

        self.root.bind_all("<KeyPress>", on_key, add="+")

    # ------------------------------------------------------------ actions ---
    def paste_url(self):
        try:
            self.url.set(self.root.clipboard_get().strip())
        except Exception:
            pass

    def pick_dir(self):
        d = filedialog.askdirectory(title="เลือกโฟลเดอร์สำหรับบันทึกรูป", initialdir=self.save_dir.get() or None)
        if d:
            self.save_dir.set(d)
            self._push_save_opts()

    def _push_save_opts(self):
        """Saving options are the only ones that apply to a session that is already running."""
        if self.session and self.session.running:
            self.session.update_save(self.save_dir.get().strip(), bool(self.auto_save.get()))

    @staticmethod
    def _normalize_url(text: str) -> str:
        import pathlib
        t = text.strip().strip('"')
        if not t:
            return t
        if re.match(r"^[a-zA-Z]:[\\/]", t) or t.startswith("\\\\"):      # Windows path
            p = pathlib.Path(t)
            return p.resolve().as_uri() if p.exists() else t
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", t) or re.match(r"^(about|data|chrome|file):", t, re.I):
            return t                                                       # already has a scheme
        if re.match(r"^(localhost|127\.0\.0\.1|\[::1\])(:\d+)?(/|$)", t, re.I):
            return "http://" + t                                           # local dev servers are plain http
        return "https://" + t                                              # host, host:port, host/path

    def _collect_settings(self):
        s = self.settings
        s.last_url = self.url.get().strip()
        s.source_lang = _code_for(SOURCE_LANGS, self.src.get())
        s.target_lang = _code_for(TARGET_LANGS, self.tgt.get())
        s.engine = _code_for(ENGINES, self.engine.get())
        s.save_dir = self.save_dir.get().strip()
        s.auto_save = bool(self.auto_save.get())
        try:
            s.save()
        except Exception as e:
            LOG.warning("บันทึกการตั้งค่าไม่ได้: %s", e)
        return s

    def start(self):
        if self.session and self.session.running:
            return
        s = self._collect_settings()
        if not s.last_url:
            Messagebox.show_error("กรุณาใส่ลิงก์หน้ามังงะก่อน", "ยังไม่มีลิงก์", parent=self.root)
            return
        url = self._normalize_url(s.last_url)
        if url != s.last_url:
            s.last_url = url
            self.url.set(url)
            try:
                s.save()
            except Exception as e:
                LOG.warning("บันทึกการตั้งค่าไม่ได้: %s", e)
        from gm.session import Session
        self.session = Session(s, self.events)
        self.session.start()
        self._set_running(True)

    def stop(self):
        if self.session:
            self.session.stop()
        self.btn_stop.configure(state=DISABLED)

    def save_now(self):
        if self.session:
            self._collect_settings()
            self._push_save_opts()
            self.session.request_save(self.settings.save_dir)

    def retry(self):
        if self.session:
            self.session.retry_failed()

    def toggle(self):
        if self.session:
            self.session.toggle_original()

    def _set_running(self, on: bool):
        self.btn_start.configure(state=DISABLED if on else NORMAL)
        for btn in (self.btn_stop, self.btn_save, self.btn_retry, self.btn_toggle):
            btn.configure(state=NORMAL if on else DISABLED)

    def _drain(self):
        try:
            for _ in range(200):
                ev = self.events.get_nowait()
                kind = ev[0]
                if kind == "log":
                    _, level, msg = ev
                    tag = "err" if level >= logging.ERROR else "warn" if level >= logging.WARNING else None
                    self.log.insert("end", msg + "\n", tag)
                    self.log.see("end")
                elif kind == "progress":
                    _, done, total, failed, busy = ev
                    self.pb.configure(maximum=max(total, 1), value=min(done + failed, total))
                    txt = f"แปลแล้ว {done} / {total}"
                    if failed:
                        txt += f"  (พลาด {failed})"
                    if busy:
                        txt += f"  กำลังทำ {busy}"
                    self.progress.set(txt)
                elif kind == "stopped":
                    self._set_running(False)
                    self.progress.set(self.progress.get() + "  — หยุดแล้ว")
                elif kind == "call":
                    ev[1]()
        except queue.Empty:
            pass
        except Exception as e:  # never let the UI loop die
            LOG.debug("ui event error: %s", e)
        self.root.after(80, self._drain)

    def on_close(self):
        try:
            self._collect_settings()
        except Exception:
            pass
        if self.session and self.session.running:
            self.session.stop()
            self.session.join(timeout=8)
        self.root.destroy()


def selftest(url: str, seconds: int = 90) -> int:
    """Hidden diagnostics: GhostManga5.exe --selftest URL  (headless; writes selftest.log)."""
    import time
    from gm.config import local_data_dir
    from gm.session import Session
    os.environ["GM5_HEADLESS"] = "1"
    log_path = os.path.join(local_data_dir(), "selftest.log")
    logging.basicConfig(filename=log_path, filemode="w", encoding="utf-8", level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    for name in ("uc", "uc.connection", "websockets", "httpx", "httpcore", "chrome_lens_py"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    s = Settings()
    s.last_url, s.engine, s.keep_browser_profile = url, "google", False
    events: queue.Queue = queue.Queue()
    sess = Session(s, events)
    sess.start()
    done = total = 0
    t0 = time.time()
    while time.time() - t0 < seconds and sess.running:
        try:
            ev = events.get(timeout=1)
        except queue.Empty:
            continue
        if ev[0] == "progress":
            done, total = ev[1], ev[2]
            if total and done >= total and ev[4] == 0:
                break
    sess.stop()
    sess.join(20)
    LOG.info("SELFTEST RESULT done=%d total=%d", done, total)
    return 0 if done > 0 else 1


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--selftest":
        sys.exit(selftest(sys.argv[2]))
    root = ttk.Window(themename="superhero")
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
