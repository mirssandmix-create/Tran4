"""PoomCatoManga — แปลมังงะ / มันฮวา / มานฮวาในหน้าเว็บเป็นภาษาไทย (Windows)."""
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
from ttkbootstrap.style import ThemeDefinition

from pcm.config import (APP_AUTHOR, APP_NAME, APP_VERSION, ENGINES, SOURCE_LANGS, TARGET_LANGS, THEMES, Settings,
                        local_data_dir, resource_path)

LOG = logging.getLogger("poomcatomanga")
DETAIL = logging.getLogger("poomcatomanga.detail")   # log.txt only (same name in pcm/session.py)
UI_FONT = "Leelawadee UI"
LOG_KEEP_BYTES = 2_000_000   # log.txt is moved to log-old.txt at launch once it is this big

# Two original themes: violet night / soft day
PALETTES = {
    "dark": ("poomcato-night", "dark", {
        "primary": "#8B5CF6", "secondary": "#2C2F3E", "success": "#34D399", "info": "#F472B6",
        "warning": "#FBBF24", "danger": "#F87171", "light": "#E6E8EF", "dark": "#0D0E14",
        "bg": "#14151D", "fg": "#E6E8EF", "selectbg": "#8B5CF6", "selectfg": "#FFFFFF",
        "border": "#2C2F3E", "inputfg": "#EEF0F6", "inputbg": "#1D1F2A", "active": "#262938"}),
    "light": ("poomcato-day", "light", {
        "primary": "#7C3AED", "secondary": "#E7E3F1", "success": "#10B981", "info": "#DB2777",
        "warning": "#D97706", "danger": "#DC2626", "light": "#F5F3FA", "dark": "#1F2330",
        "bg": "#F7F5FB", "fg": "#1F2330", "selectbg": "#7C3AED", "selectfg": "#FFFFFF",
        "border": "#D9D4E6", "inputfg": "#1F2330", "inputbg": "#FFFFFF", "active": "#EDE9F6"}),
}


MUTED = {"dark": "#A3A8BA", "light": "#6B7280"}   # readable secondary text on each theme


def register_themes(style) -> None:
    for name, mode, colors in PALETTES.values():
        style.register_theme(ThemeDefinition(name, colors, mode))


def theme_name(key: str) -> str:
    return PALETTES.get(key, PALETTES["dark"])[0]


def palette(key: str) -> dict:
    return PALETTES.get(key, PALETTES["dark"])[2]


class QueueLogHandler(logging.Handler):
    """Thread-safe: worker threads only put into a queue, the Tk thread drains it."""

    def __init__(self, q: queue.Queue):
        super().__init__()
        self.q = q

    def filter(self, record):
        return not record.name.startswith(DETAIL.name) and super().filter(record)

    def emit(self, record):
        try:
            self.q.put(("log", record.levelno, self.format(record)))
        except Exception:
            pass


_log_lock = []   # (fd, stem) of the log.lock this process holds until it exits


def _claim(stem: str) -> int | None:
    """Lock stem.lock for the life of this process. None = another running copy holds it.
    A viewer that has log.txt open doesn't count: only app instances take this lock."""
    fd = os.open(stem + ".lock", os.O_RDWR | os.O_CREAT)
    try:
        import msvcrt
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)   # released by Windows when the process ends
    except ImportError:
        pass
    except OSError:
        os.close(fd)
        return None
    return fd


def _written_elsewhere(path: str) -> bool:
    """Another process has path open for writing: a copy built before log.lock (it opened log.txt
    with mode "w" and would write over our lines). Readers such as Get-Content -Wait don't count."""
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
    # read access, sharing read+delete but not write: refused while any handle can write the file
    h = k32.CreateFileW(path, 0x80000000, 0x1 | 0x4, None, 3, 0, None)
    if h is None or h == ctypes.c_void_p(-1).value:
        return ctypes.get_last_error() == 32   # ERROR_SHARING_VIOLATION
    k32.CloseHandle(wintypes.HANDLE(h))
    return False


def open_log_file() -> logging.FileHandler | None:
    """log.txt in the data folder. Appended to, so relaunching the app doesn't wipe the session
    that went wrong. A second copy running at the same time writes log-2.txt instead (each copy
    holds log.lock / log-N.lock while it runs). Over LOG_KEEP_BYTES the file moves to log-old.txt,
    which only the copy holding its lock ever does."""
    try:
        folder = local_data_dir()
    except OSError:
        return None
    for n in range(1, 10):
        stem = os.path.join(folder, "log" if n == 1 else f"log-{n}")
        held = _log_lock[0][1] if _log_lock else None
        if held and held != stem:
            continue
        fd = None
        if not held:
            try:
                fd = _claim(stem)
            except OSError:
                continue
            if fd is None:
                continue
        path = stem + ".txt"
        if fd is not None and _written_elsewhere(path):
            os.close(fd)
            continue
        try:
            if os.path.getsize(path) > LOG_KEEP_BYTES:
                os.replace(path, stem + "-old.txt")
        except OSError:   # missing, or a viewer has it open: keep appending
            pass
        try:
            fh = logging.FileHandler(path, mode="a", encoding="utf-8", errors="replace")
        except OSError:
            if fd is not None:
                os.close(fd)
            continue
        if fd is not None:
            _log_lock.append((fd, stem))
        return fh
    return None


def _label(options, code):
    return next((o[1] for o in options if o[0] == code), options[0][1])


def _code(options, label):
    return next((o[0] for o in options if o[1] == label), options[0][0])


def _logo(size: int):
    try:
        img = tk.PhotoImage(file=resource_path("logo.png"))
        factor = max(1, round(img.width() / size))
        return img.subsample(factor, factor)
    except Exception:
        return None


# ---------------------------------------------------------------- settings ---

class SettingsWindow(ttk.Toplevel):
    """จัดการ: a sidebar of sections on the left, the chosen section on the right."""

    PAGES = [
        ("local", "◎  AI ในเครื่อง"),
        ("gemini", "◆  Gemini"),
        ("claude", "●  Claude"),
        ("openai", "◇  OpenAI / อื่นๆ"),
        ("names", "✎  ชื่อตัวละคร"),
        ("look", "Aa  ตัวหนังสือและธีม"),
        ("save", "⬇  บันทึกรูป"),
        ("advanced", "⚙  ขั้นสูง"),
        ("about", "ⓘ  เกี่ยวกับ"),
    ]

    def __init__(self, app: "App", page: str = "local"):
        super().__init__(title=f"ตั้งค่า — {APP_NAME}", resizable=(True, True))
        self.app, s = app, app.settings
        self.transient(app.root)
        self.geometry("820x580")
        self.minsize(720, 500)
        self.vars: dict[str, tk.Variable] = {}
        self._logo = _logo(96)

        body = ttk.Frame(self)
        body.pack(fill=BOTH, expand=True)
        side = ttk.Frame(body, padding=(10, 14), bootstyle=SECONDARY)
        side.pack(side=LEFT, fill=Y)
        ttk.Label(side, text="ตั้งค่า", font=(UI_FONT, 14, "bold"), bootstyle=(INVERSE, SECONDARY)).pack(anchor=W, padx=6, pady=(0, 10))
        self.page_var = tk.StringVar(value=page)
        for key, label in self.PAGES:
            ttk.Radiobutton(side, text=label, value=key, variable=self.page_var, command=self._show,
                            bootstyle="primary-toolbutton", width=22).pack(fill=X, pady=2)
        self.content = ttk.Frame(body, padding=(22, 18))
        self.content.pack(side=LEFT, fill=BOTH, expand=True)

        self.pages: dict[str, ttk.Frame] = {}
        self.toggles: dict[str, ttk.Checkbutton] = {}
        for key, _ in self.PAGES:
            f = ttk.Frame(self.content)
            f.columnconfigure(1, weight=1)
            self.pages[key] = f
            getattr(self, f"_page_{key}")(f, s)

        bar = ttk.Frame(self, padding=(16, 10))
        bar.pack(fill=X, side=BOTTOM)
        ttk.Separator(self).pack(fill=X, side=BOTTOM)
        ttk.Button(bar, text="บันทึกการตั้งค่า", bootstyle=PRIMARY, command=self.save, width=16).pack(side=RIGHT)
        ttk.Button(bar, text="ยกเลิก", bootstyle="primary-outline", command=self.destroy, width=10).pack(side=RIGHT, padx=8)
        self._show()

    # -- helpers
    def _title(self, f, text, sub=""):
        ttk.Label(f, text=text, font=(UI_FONT, 15, "bold")).grid(row=0, column=0, columnspan=3, sticky=W)
        if sub:
            ttk.Label(f, text=sub, wraplength=520, justify=LEFT, style="Muted.TLabel").grid(
                row=1, column=0, columnspan=3, sticky=W, pady=(2, 14))

    def _entry(self, f, row, label, key, show=None):
        ttk.Label(f, text=label).grid(row=row, column=0, sticky=W, padx=(0, 12), pady=6)
        v = tk.StringVar(value=str(getattr(self.app.settings, key)))
        self.vars[key] = v
        ttk.Entry(f, textvariable=v, show=show).grid(row=row, column=1, columnspan=2, sticky=EW, pady=6)
        return v

    def _toggle(self, f, row, label, key, command=None):
        v = tk.BooleanVar(value=bool(getattr(self.app.settings, key)))
        self.vars[key] = v
        self.toggles[key] = ttk.Checkbutton(f, text=label, variable=v, bootstyle="success-round-toggle", command=command)
        self.toggles[key].grid(row=row, column=0, columnspan=3, sticky=W, pady=6)
        return v

    def _spin(self, f, row, label, key, lo, hi, step=1):
        ttk.Label(f, text=label).grid(row=row, column=0, sticky=W, padx=(0, 12), pady=6)
        v = tk.IntVar(value=int(getattr(self.app.settings, key)))
        self.vars[key] = v
        ttk.Spinbox(f, from_=lo, to=hi, increment=step, textvariable=v, width=8).grid(row=row, column=1, sticky=W, pady=6)
        return v

    def _show(self):
        for f in self.pages.values():
            f.pack_forget()
        self.pages[self.page_var.get()].pack(fill=BOTH, expand=True)

    # -- pages
    def _page_local(self, f, s):
        self._title(f, "AI ในเครื่อง (ฟรี)",
                    "รันโมเดลบนเครื่องคุณเอง ไม่เสียค่าใช้จ่าย ใช้ LM Studio (แนะนำสำหรับการ์ดจอ AMD) หรือ Ollama\n"
                    "1) ติดตั้ง LM Studio (หรือ Bionic)   2) ดาวน์โหลดโมเดล เช่น Gemma 4 12B\n"
                    "3) เปิดเซิร์ฟเวอร์: LM Studio แท็บ Developer / Bionic เมนู Local Model API   "
                    "(Ollama ใช้ http://localhost:11434/v1)")
        self._entry(f, 2, "Server URL", "local_base_url")
        self._entry(f, 3, "ชื่อโมเดล (ว่าง = ตัวที่โหลดอยู่)", "local_model")
        self._toggle(f, 4, "ให้ AI คิดก่อนตอบ (แม่นขึ้นนิดหน่อย แต่ช้าลงเกือบ 10 เท่า)", "local_thinking")
        ttk.Button(f, text="ทดสอบการเชื่อมต่อ", bootstyle="info-outline", command=self.test_local).grid(
            row=5, column=1, sticky=W, pady=10)
        self.local_status = ttk.Label(f, text="", wraplength=520, justify=LEFT)
        self.local_status.grid(row=6, column=0, columnspan=3, sticky=W)

    def _page_gemini(self, f, s):
        self._title(f, "Google Gemini", "สร้าง API key ได้ที่ aistudio.google.com — โควตาฟรีมีน้อย (วันละไม่กี่ครั้ง)")
        self._entry(f, 2, "API key", "gemini_api_key", show="•")
        self._entry(f, 3, "โมเดล", "gemini_model")

    def _page_claude(self, f, s):
        self._title(f, "Claude", "สร้าง API key ได้ที่ platform.claude.com — เสียเงินตามการใช้งาน\n"
                                 "ประหยัด: claude-haiku-4-5   •   สมดุล: claude-sonnet-5-5   •   ดีที่สุด: claude-opus-5-5")
        self._entry(f, 2, "API key", "claude_api_key", show="•")
        self._entry(f, 3, "โมเดล", "claude_model")
        ttk.Label(f, text="ความละเอียดการคิด").grid(row=4, column=0, sticky=W, pady=6)
        v = tk.StringVar(value=s.claude_effort)
        self.vars["claude_effort"] = v
        ttk.Combobox(f, textvariable=v, values=["low", "medium", "high"], state="readonly", width=10).grid(
            row=4, column=1, sticky=W, pady=6)

    def _page_openai(self, f, s):
        self._title(f, "OpenAI / บริการอื่นๆ", "ใช้ได้กับ OpenAI, DeepSeek, OpenRouter หรือบริการที่ใช้รูปแบบเดียวกัน")
        self._entry(f, 2, "Base URL", "openai_base_url")
        self._entry(f, 3, "API key", "openai_api_key", show="•")
        self._entry(f, 4, "โมเดล", "openai_model")

    def _page_names(self, f, s):
        f.rowconfigure(3, weight=1)
        self._title(f, "ชื่อตัวละครและคำเฉพาะ",
                    "ใส่บรรทัดละ 1 คำ เช่น  タロウ = ทาโร่   แล้ว AI จะแปลแบบเดิมทุกครั้ง (AI จะจำชื่อใหม่ที่เจอเองด้วย)")
        self.glossary = scrolledtext.ScrolledText(f, height=10, font=(UI_FONT, 11), relief=FLAT)
        self.glossary.grid(row=3, column=0, columnspan=3, sticky=NSEW)
        self.glossary.insert("1.0", s.glossary)
        self._toggle(f, 4, "ส่งภาพทั้งหน้าให้ AI ดูด้วย (รู้ว่าใครพูด แปลน้ำเสียงดีขึ้น แต่ช้าลง/เปลืองขึ้น)", "send_image_to_ai")

    def _page_look(self, f, s):
        self._title(f, "ตัวหนังสือและธีม")
        ttk.Label(f, text="ธีม").grid(row=2, column=0, sticky=W, pady=6)
        tv = tk.StringVar(value=s.theme)
        self.vars["theme"] = tv
        tf = ttk.Frame(f)
        tf.grid(row=2, column=1, sticky=W)
        for code, label in THEMES:
            ttk.Radiobutton(tf, text=label, value=code, variable=tv, bootstyle="primary-toolbutton", width=8).pack(side=LEFT, padx=(0, 4))
        ttk.Label(f, text="ฟอนต์คำแปล").grid(row=3, column=0, sticky=W, pady=6)
        fams = sorted({x for x in tkfont.families() if not x.startswith("@")})
        preferred = [x for x in ("Leelawadee UI", "Leelawadee", "Tahoma", "Noto Sans Thai", "Sarabun", "Mitr", "Itim",
                                 "Kanit", "Prompt", "Mali", "Sriracha") if x in fams]
        v = tk.StringVar(value=s.font_family)
        self.vars["font_family"] = v
        ttk.Combobox(f, textvariable=v, values=preferred + [x for x in fams if x not in preferred], width=28).grid(
            row=3, column=1, sticky=W)
        self._toggle(f, 4, "ตัวหนา", "font_bold")
        self._spin(f, 5, "ขนาดตัวอักษรเล็กสุด (px)", "min_font_px", 8, 30)

    def _page_save(self, f, s):
        self._title(f, "บันทึกรูป", "ปิดไว้เป็นค่าเริ่มต้น — เปิดใช้งานเมื่อต้องการเก็บรูปที่แปลแล้วลงเครื่อง\n"
                                    "เมื่อเปิด ปุ่ม \"บันทึกตอนนี้\" จะปรากฏในหน้าหลัก")
        self.save_box = ttk.Labelframe(f, text="ตัวเลือกการบันทึก", padding=12)
        self._toggle(f, 2, "เปิดใช้งานการบันทึกรูป", "save_enabled", command=self._sync_save_box)
        self.save_box.grid(row=3, column=0, columnspan=3, sticky=EW, pady=(8, 0))
        self.save_box.columnconfigure(1, weight=1)
        ttk.Label(self.save_box, text="โฟลเดอร์").grid(row=0, column=0, sticky=W, padx=(0, 10))
        v = tk.StringVar(value=s.save_dir or os.path.join(os.path.expanduser("~"), "Pictures", APP_NAME))
        self.vars["save_dir"] = v
        self.save_entry = ttk.Entry(self.save_box, textvariable=v)
        self.save_entry.grid(row=0, column=1, sticky=EW)
        self.save_pick = ttk.Button(self.save_box, text="เลือก…", bootstyle="primary-outline", command=self._pick_dir)
        self.save_pick.grid(row=0, column=2, padx=(8, 0))
        av = tk.BooleanVar(value=s.auto_save)
        self.vars["auto_save"] = av
        self.save_auto = ttk.Checkbutton(self.save_box, text="บันทึกอัตโนมัติทุกรูปที่แปลเสร็จ", variable=av,
                                         bootstyle="success-round-toggle")
        self.save_auto.grid(row=1, column=0, columnspan=3, sticky=W, pady=(10, 0))
        ttk.Label(self.save_box, text="รูปจะถูกเก็บแยกโฟลเดอร์ตามชื่อตอน และตั้งชื่อตามลำดับในหน้า",
                  style="Muted.TLabel").grid(row=2, column=0, columnspan=3, sticky=W, pady=(8, 0))
        self._sync_save_box()

    def _sync_save_box(self):
        on = bool(self.vars["save_enabled"].get())
        for w in (self.save_entry, self.save_pick, self.save_auto):
            w.configure(state=NORMAL if on else DISABLED)

    def _pick_dir(self):
        d = filedialog.askdirectory(parent=self, title="เลือกโฟลเดอร์สำหรับบันทึกรูป",
                                    initialdir=self.vars["save_dir"].get() or None)
        if d:
            self.vars["save_dir"].set(d)

    def _page_advanced(self, f, s):
        self._title(f, "ขั้นสูง")
        self._spin(f, 2, "แปลพร้อมกันกี่รูป", "concurrency", 1, 8)
        self._spin(f, 3, "ข้ามรูปที่เล็กกว่า (px)", "min_image_side", 50, 1000, 50)
        self._toggle(f, 4, "จำการล็อกอิน/คุกกี้ของเว็บไว้ (ผ่าน Cloudflare ครั้งเดียวพอ)", "keep_browser_profile")
        if self.app.use_open_chrome.get():   # the user's own Chrome keeps its own logins
            self.toggles["keep_browser_profile"].configure(state=DISABLED)
            ttk.Label(f, text="ไม่มีผลเมื่อใช้ Chrome ที่เปิดอยู่ — ใช้การล็อกอินและคุกกี้ใน Chrome ของคุณอยู่แล้ว",
                      style="Muted.TLabel", wraplength=520).grid(row=5, column=0, columnspan=3, sticky=W)
        ttk.Label(f, text=f"ไฟล์ log และแคชอยู่ที่: {local_data_dir()}", style="Muted.TLabel", wraplength=520).grid(
            row=6, column=0, columnspan=3, sticky=W, pady=(16, 0))

    def _page_about(self, f, s):
        if self._logo:
            ttk.Label(f, image=self._logo).grid(row=0, column=0, sticky=W, pady=(0, 10))
        ttk.Label(f, text=APP_NAME, font=(UI_FONT, 20, "bold"), bootstyle=PRIMARY).grid(row=1, column=0, sticky=W)
        ttk.Label(f, text=f"เวอร์ชัน {APP_VERSION}  •  สร้างโดย {APP_AUTHOR}").grid(row=2, column=0, sticky=W)
        ttk.Label(f, text="อ่านมังงะ มันฮวา มานฮวา ในหน้าเว็บเป็นภาษาไทย — อ่านตัวหนังสือด้วย Google Lens "
                          "จัดวางคำแปลใหม่ให้พอดีช่องคำพูด", wraplength=520, justify=LEFT).grid(row=3, column=0, sticky=W, pady=(10, 0))
        ttk.Label(f, text="ซอฟต์แวร์โอเพนซอร์สที่ใช้: SeleniumBase, chrome-lens-py, ttkbootstrap, Pillow, NumPy, httpx, "
                          "Anthropic SDK (สัญญาอนุญาตของแต่ละโครงการ)", style="Muted.TLabel", wraplength=520,
                  justify=LEFT).grid(row=4, column=0, sticky=W, pady=(18, 0))

    # -- actions
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
        try:
            self.app.settings.save()
        except Exception as e:
            LOG.warning("บันทึกการตั้งค่าไม่ได้: %s", e)
        self.app.apply_settings()
        if self.app.session and self.app.session.running:
            LOG.info("บันทึกการตั้งค่าแล้ว — ตัวแปลและฟอนต์ใหม่จะมีผลเมื่อเริ่มแปลครั้งถัดไป")
        self.destroy()

    def test_local(self):
        # read only the URL field: testing must not commit other edits (Cancel still cancels)
        self.local_status.configure(text="กำลังตรวจสอบ…")
        base = self.vars["local_base_url"].get().strip().rstrip("/")
        name = self.vars["local_model"].get().strip()

        def work():
            import httpx
            from pcm.translate import match_model
            try:
                r = httpx.get(base + "/models", timeout=httpx.Timeout(5, connect=3))
                r.raise_for_status()
                models = [m.get("id") for m in r.json().get("data", []) if m.get("id")]
                msg = "เชื่อมต่อได้ ✓  โมเดลที่มี: " + (", ".join(models) if models else "(ยังไม่ได้โหลดโมเดล)")
                if name:
                    found = match_model(name, models)
                    msg += (f"\nชื่อโมเดล \"{name}\" → ใช้ {found} ✓" if found else
                            f"\n⚠ ไม่พบโมเดล \"{name}\" — เว้นช่องว่างไว้ หรือพิมพ์ชื่อตามรายการด้านบน")
            except Exception as e:
                msg = (f"เชื่อมต่อไม่ได้: {e}\n"
                       "เปิดเซิร์ฟเวอร์แล้วหรือยัง? (LM Studio แท็บ Developer / Bionic เมนู Local Model API)")
            self.app.events.put(("call", lambda: self.local_status.winfo_exists() and self.local_status.configure(text=msg)))

        threading.Thread(target=work, daemon=True).start()


# -------------------------------------------------------------------- main ---

class App:
    def __init__(self, root: ttk.Window):
        self.root = root
        self.events: queue.Queue = queue.Queue()
        self._install_logging()
        self.settings = Settings.load()
        self.session = None
        self._state = "idle"
        self._guide = None
        register_themes(root.style)
        root.style.theme_use(theme_name(self.settings.theme))
        root.title(f"{APP_NAME}  {APP_VERSION}")
        root.geometry("760x700")
        root.minsize(640, 600)
        self._set_icon()
        self._fonts()
        # variables the settings window and tests use
        self.save_enabled = tk.BooleanVar(value=self.settings.save_enabled)
        self.save_dir = tk.StringVar(value=self.settings.save_dir)
        self.auto_save = tk.BooleanVar(value=self.settings.auto_save)
        self._build()
        self._install_thai_keyboard_shortcuts()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self._drain)
        if getattr(self.settings, "load_warning", ""):
            LOG.warning(self.settings.load_warning)

    # ---------------------------------------------------------------- ui ---
    def _fonts(self):
        st = self.root.style
        pal = palette(self.settings.theme)
        st.configure("Muted.TLabel", foreground=MUTED[self.settings.theme], background=pal["bg"], font=(UI_FONT, 10))
        st.configure("CardLink.TLabel", foreground=pal["info"], background=pal["secondary"], font=(UI_FONT, 10, "underline"))
        for name in ("TLabel", "TButton", "TCheckbutton", "TRadiobutton", "TEntry", "TCombobox", "TLabelframe.Label"):
            st.configure(name, font=(UI_FONT, 10))
        self.root.option_add("*TCombobox*Listbox.font", (UI_FONT, 10))

    def _set_icon(self):
        if sys.platform.startswith("win"):
            try:
                import ctypes
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("PoomCato.PoomCatoManga")
            except Exception:
                pass
        try:
            ico = resource_path("icon.ico")
            if os.path.exists(ico):
                self.root.iconbitmap(ico)
            self._icon_img = tk.PhotoImage(file=resource_path("logo.png"))
            self.root.iconphoto(True, self._icon_img)
        except Exception:
            pass

    def _card(self, parent, step: str, title: str):
        outer = ttk.Frame(parent, padding=(16, 12), bootstyle=SECONDARY)
        head = ttk.Frame(outer, bootstyle=SECONDARY)
        head.pack(fill=X, pady=(0, 8))
        ttk.Label(head, text=f" {step} ", font=(UI_FONT, 10, "bold"), bootstyle=(INVERSE, PRIMARY)).pack(side=LEFT)
        ttk.Label(head, text=title, font=(UI_FONT, 11, "bold"), bootstyle=(INVERSE, SECONDARY)).pack(side=LEFT, padx=8)
        inner = ttk.Frame(outer, bootstyle=SECONDARY)
        inner.pack(fill=X)
        return outer, inner

    def _build(self):
        s = self.settings
        page = ttk.Frame(self.root, padding=(22, 16))
        page.pack(fill=BOTH, expand=True)

        # header
        head = ttk.Frame(page)
        head.pack(fill=X, pady=(0, 14))
        self._logo = _logo(48)
        if self._logo:
            ttk.Label(head, image=self._logo).pack(side=LEFT, padx=(0, 12))
        titles = ttk.Frame(head)
        titles.pack(side=LEFT)
        ttk.Label(titles, text=APP_NAME, font=(UI_FONT, 20, "bold"), bootstyle=PRIMARY).pack(anchor=W)
        ttk.Label(titles, text="อ่านมังงะ มันฮวา มานฮวา เป็นภาษาไทยในหน้าเว็บ", style="Muted.TLabel").pack(anchor=W)
        ttk.Button(head, text="⚙  ตั้งค่า", bootstyle="primary-outline", command=lambda: self.open_settings()).pack(side=RIGHT)
        self.theme_btn = ttk.Button(head, bootstyle="primary-outline", command=self.toggle_theme)
        self.theme_btn.pack(side=RIGHT, padx=8)

        # step 1: link
        c1, in1 = self._card(page, "1", "ลิงก์ตอนที่จะอ่าน")
        c1.pack(fill=X, pady=(0, 10))
        in1.columnconfigure(0, weight=1)
        self.url = tk.StringVar(value=s.last_url)
        self.url_entry = ttk.Entry(in1, textvariable=self.url, font=(UI_FONT, 11))
        self.url_entry.grid(row=0, column=0, sticky=EW, ipady=4)
        ttk.Button(in1, text="วางลิงก์", bootstyle=PRIMARY, command=self.paste_url).grid(row=0, column=1, padx=(8, 0))
        # attach mode: translate in the Chrome the user already has open
        self.use_open_chrome = tk.BooleanVar(value=s.browser_mode == "attach")
        mode = ttk.Frame(in1, bootstyle=SECONDARY)
        mode.grid(row=1, column=0, columnspan=2, sticky=EW, pady=(10, 0))
        self.mode_toggle = ttk.Checkbutton(mode, text="ใช้ Chrome ที่เปิดอยู่ (ไม่ต้องวางลิงก์)", variable=self.use_open_chrome,
                                           bootstyle="success-round-toggle", command=self._sync_mode)
        self.mode_toggle.pack(side=LEFT)
        self.mode_help = ttk.Label(mode, text="วิธีเปิดใช้ ›", style="CardLink.TLabel", cursor="hand2")
        self.mode_help.bind("<Button-1>", lambda e: self.show_attach_guide())
        self.mode_hint = ttk.Label(in1, text="เว้นช่องลิงก์ว่าง = แปลแท็บที่เปิดอยู่ด้านหน้าใน Chrome   •   "
                                             "ใส่ลิงก์ = เปิดในแท็บใหม่ของ Chrome", bootstyle=(INVERSE, SECONDARY))

        # step 2: languages + translator
        c2, in2 = self._card(page, "2", "ภาษาและตัวแปล")
        c2.pack(fill=X, pady=(0, 14))
        self.src = tk.StringVar(value=_label(SOURCE_LANGS, s.source_lang))
        self.tgt = tk.StringVar(value=_label(TARGET_LANGS, s.target_lang))
        self.engine = tk.StringVar(value=_label(ENGINES, s.engine))
        ttk.Label(in2, text="จาก", bootstyle=(INVERSE, SECONDARY)).grid(row=0, column=0, sticky=W)
        ttk.Combobox(in2, textvariable=self.src, values=[x[1] for x in SOURCE_LANGS], state="readonly", width=24).grid(
            row=0, column=1, sticky=W, padx=(8, 0))
        ttk.Label(in2, text="→  เป็น", bootstyle=(INVERSE, SECONDARY)).grid(row=0, column=2, sticky=W, padx=(14, 0))
        ttk.Combobox(in2, textvariable=self.tgt, values=[x[1] for x in TARGET_LANGS], state="readonly", width=14).grid(
            row=0, column=3, sticky=W, padx=(8, 0))
        ttk.Label(in2, text="ตัวแปล", bootstyle=(INVERSE, SECONDARY)).grid(row=1, column=0, sticky=W, pady=(10, 0))
        eng = ttk.Combobox(in2, textvariable=self.engine, values=[x[1] for x in ENGINES], state="readonly", width=24)
        eng.grid(row=1, column=1, sticky=W, padx=(8, 0), pady=(10, 0))
        eng.bind("<<ComboboxSelected>>", lambda e: self._engine_hint())
        self.engine_hint = ttk.Label(in2, text="", bootstyle=(INVERSE, SECONDARY))
        self.engine_hint.grid(row=1, column=2, columnspan=2, sticky=W, padx=(14, 0), pady=(10, 0))
        self._engine_hint()

        # step 3: the one big button
        self.btn_start = ttk.Button(page, text="", bootstyle=SUCCESS, command=self.start_stop)
        self.btn_start.pack(fill=X, ipady=10)
        self.root.style.configure("success.TButton", font=(UI_FONT, 13, "bold"))
        self.root.style.configure("danger.TButton", font=(UI_FONT, 13, "bold"))

        # status card
        st = ttk.Frame(page, padding=(16, 12), bootstyle=SECONDARY)
        st.pack(fill=X, pady=(14, 0))
        top = ttk.Frame(st, bootstyle=SECONDARY)
        top.pack(fill=X)
        self.status = ttk.Label(top, text="", font=(UI_FONT, 10, "bold"), bootstyle=(INVERSE, SECONDARY))
        self.status.pack(side=LEFT)
        self.progress = tk.StringVar(value="0 / 0 หน้า")
        ttk.Label(top, textvariable=self.progress, font=(UI_FONT, 14, "bold"), bootstyle=(INVERSE, SECONDARY)).pack(side=RIGHT)
        self.pb = ttk.Progressbar(st, mode="determinate", bootstyle="success-striped")
        self.pb.pack(fill=X, pady=(10, 8))
        self.actions = ttk.Frame(st, bootstyle=SECONDARY)
        self.actions.pack(fill=X)
        self.btn_toggle = ttk.Button(self.actions, text="⇄  ต้นฉบับ / คำแปล", bootstyle="info-outline", command=self.toggle)
        self.btn_retry = ttk.Button(self.actions, text="↻  แปลที่พลาดใหม่", bootstyle="warning-outline", command=self.retry)
        self.btn_save = ttk.Button(self.actions, text="⬇  บันทึกตอนนี้", bootstyle="primary-outline", command=self.save_now)
        self.failed_lbl = ttk.Label(self.actions, text="", bootstyle=(INVERSE, SECONDARY))

        # details (log), hidden until asked for
        self.details_btn = ttk.Button(page, text="▸  รายละเอียดการทำงาน", bootstyle="link", command=self.toggle_details)
        self.details_btn.pack(anchor=W, pady=(10, 0))
        self.log_frame = ttk.Frame(page)
        self.log = scrolledtext.ScrolledText(self.log_frame, height=10, font=(UI_FONT, 10), wrap=WORD, relief=FLAT)
        self.log.pack(fill=BOTH, expand=True)
        self.log.tag_configure("warn", foreground="#F59E0B")
        self.log.tag_configure("err", foreground="#EF4444")
        self.tip = ttk.Label(page, text="เคล็ดลับ: ในหน้าเว็บกด Alt+T เพื่อสลับดูต้นฉบับ/คำแปล", style="Muted.TLabel")
        self.tip.pack(side=BOTTOM, anchor=W, pady=(8, 0))
        self._details = False
        self.apply_settings()
        self._sync_mode()
        self._set_state("idle")
        self.url_entry.focus_set()

    def _engine_hint(self):
        code = _code(ENGINES, self.engine.get())
        hint = next((e[2] for e in ENGINES if e[0] == code), "")
        self.engine_hint.configure(text=hint)

    def _sync_mode(self):
        """Show what the link field means in the chosen browser mode."""
        on = bool(self.use_open_chrome.get())
        self.settings.browser_mode = "attach" if on else "own"
        if on:
            self.mode_help.pack(side=LEFT, padx=(14, 0))
            self.mode_hint.grid(row=2, column=0, columnspan=2, sticky=W, pady=(6, 0))
        else:
            self.mode_help.pack_forget()
            self.mode_hint.grid_remove()

    def _card_styles(self):
        """Widgets on a card need the card's colour (the theme paints them with the page background)."""
        st, pal = self.root.style, palette(self.settings.theme)
        bg = pal["secondary"]
        fg = st.lookup("secondary.Inverse.TLabel", "foreground") or pal["fg"]
        self.mode_toggle.configure(bootstyle="success-round-toggle")   # builds the base style for this theme
        st.configure("Card.success.Round.Toggle", background=bg, foreground=fg)
        st.map("Card.success.Round.Toggle", background=[("selected", bg)],
               foreground=[("disabled", MUTED[self.settings.theme])])
        self.mode_toggle.configure(style="Card.success.Round.Toggle")

    def toggle_details(self):
        self._details = not self._details
        if self._details:
            self.log_frame.pack(fill=BOTH, expand=True, pady=(6, 0), before=self.tip)
            self.details_btn.configure(text="▾  ซ่อนรายละเอียด")
        else:
            self.log_frame.pack_forget()
            self.details_btn.configure(text="▸  รายละเอียดการทำงาน")

    def toggle_theme(self):
        self.settings.theme = "light" if self.settings.theme == "dark" else "dark"
        self.apply_settings()
        try:
            self.settings.save()
        except Exception:
            pass

    def apply_settings(self):
        """Reflect settings that change the main window (theme, saving on/off)."""
        s = self.settings
        self.root.style.theme_use(theme_name(s.theme))
        self._fonts()
        self.root.style.configure("success.TButton", font=(UI_FONT, 13, "bold"))
        self.root.style.configure("danger.TButton", font=(UI_FONT, 13, "bold"))
        pal = palette(s.theme)
        self._card_styles()
        self.log.configure(background=pal["inputbg"], foreground=pal["inputfg"], insertbackground=pal["fg"])
        self.theme_btn.configure(text="โหมดสว่าง" if s.theme == "dark" else "โหมดมืด")
        self.save_enabled.set(s.save_enabled)
        self.save_dir.set(s.save_dir)
        self.auto_save.set(s.auto_save)
        self._layout_actions()
        self._push_save_opts()

    def _layout_actions(self):
        for w in (self.btn_toggle, self.btn_retry, self.btn_save, self.failed_lbl):
            w.pack_forget()
        if self._state != "idle":
            self.btn_toggle.pack(side=LEFT)
            self.btn_retry.pack(side=LEFT, padx=8)
            if self.settings.save_enabled:
                self.btn_save.pack(side=LEFT)
        self.failed_lbl.pack(side=RIGHT)

    def _set_state(self, state: str):
        self._state = state
        if state == "idle":
            self.btn_start.configure(text="▶   เริ่มอ่านและแปล", bootstyle=SUCCESS)
            self.status.configure(text="●  พร้อมใช้งาน")
        elif state == "starting":
            self.btn_start.configure(text="■   หยุด", bootstyle=DANGER)
            self.status.configure(text="●  กำลังเชื่อมต่อ Chrome ที่เปิดอยู่…" if self.use_open_chrome.get()
                                  else "●  กำลังเปิด Chrome…")
        elif state == "running":
            self.btn_start.configure(text="■   หยุด", bootstyle=DANGER)
            self.status.configure(text="●  กำลังแปล — เลื่อนอ่านได้เลย")
        elif state == "stopping":
            self.btn_start.configure(text="กำลังหยุด…", bootstyle=SECONDARY)
            self.status.configure(text="●  กำลังหยุด…")
        self._layout_actions()

    def open_settings(self, page: str = ""):
        if not page:
            code = _code(ENGINES, self.engine.get())
            page = code if code in ("local", "gemini", "claude", "openai") else "local"
        SettingsWindow(self, page)

    def _install_logging(self):
        handler = QueueLogHandler(self.events)
        handler.setFormatter(logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S"))
        root_log = logging.getLogger()
        root_log.addHandler(handler)
        root_log.setLevel(logging.INFO)
        fh = open_log_file()   # same log on disk so problems can be looked at later, plus per-bubble details
        if fh:
            fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
            root_log.addHandler(fh)
        else:
            LOG.warning("เขียนไฟล์ log ไม่ได้")
        for noisy in ("httpx", "httpcore", "seleniumbase", "selenium", "urllib3", "anthropic", "httpx2",
                      "websockets", "asyncio", "chrome_lens_py", "filelock"):
            logging.getLogger(noisy).setLevel(logging.WARNING)
        for very_noisy in ("uc", "uc.connection", "uc.browser", "uc.tab"):  # CDP event-parsing chatter
            logging.getLogger(very_noisy).setLevel(logging.CRITICAL)
        DETAIL.info("===== %s %s เริ่มทำงาน (%s, pid %d, %s) =====", APP_NAME, APP_VERSION,
                    "exe" if getattr(sys, "frozen", False) else "python " + sys.version.split()[0], os.getpid(),
                    os.path.basename(fh.baseFilename) if fh else "ไม่มีไฟล์ log")

        # the exe has no console (sys.stderr is None): unhandled errors would otherwise vanish
        def unhandled(where, exc_info):
            LOG.error("เกิดข้อผิดพลาด: %s", exc_info[1])
            DETAIL.error("unhandled (%s)", where, exc_info=exc_info)

        def thread_error(a):
            if a.exc_type is not SystemExit:
                unhandled(getattr(a.thread, "name", "thread"), (a.exc_type, a.exc_value, a.exc_traceback))

        self.root.report_callback_exception = lambda *exc: unhandled("tk", exc)
        threading.excepthook = thread_error

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

    def _push_save_opts(self):
        """Saving options are the only ones that apply to a session that is already running."""
        s = self.settings
        s.save_enabled = bool(self.save_enabled.get())
        s.save_dir = self.save_dir.get().strip()
        s.auto_save = bool(self.auto_save.get())
        if self.session and self.session.running:
            self.session.update_save(s.save_dir, s.save_enabled and s.auto_save)

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
        s.source_lang = _code(SOURCE_LANGS, self.src.get())
        s.target_lang = _code(TARGET_LANGS, self.tgt.get())
        s.engine = _code(ENGINES, self.engine.get())
        s.browser_mode = "attach" if self.use_open_chrome.get() else "own"
        self._push_save_opts()
        try:
            s.save()
        except Exception as e:
            LOG.warning("บันทึกการตั้งค่าไม่ได้: %s", e)
        return s

    def start_stop(self):
        if self._state in ("starting", "running"):
            self.stop()
        elif self._state == "idle":
            self.start()

    def start(self):
        if self.session and self.session.running:
            return
        s = self._collect_settings()
        if not s.last_url and s.browser_mode != "attach":   # attach mode: no link = the tab in front
            Messagebox.show_error("กรุณาวางลิงก์ตอนที่จะอ่านก่อน", "ยังไม่มีลิงก์", parent=self.root)
            return
        url = self._normalize_url(s.last_url)
        if url != s.last_url:
            s.last_url = url
            self.url.set(url)
            try:
                s.save()
            except Exception as e:
                LOG.warning("บันทึกการตั้งค่าไม่ได้: %s", e)
        from pcm.session import Session
        self.session = Session(s, self.events)
        self.session.update_save(s.save_dir, s.save_enabled and s.auto_save)
        self.session.start()
        self.pb.configure(value=0)
        self.progress.set("0 / 0 หน้า")
        self.failed_lbl.configure(text="")
        self._set_state("starting")

    def stop(self):
        if self.session:
            self.session.stop()
        self._set_state("stopping")

    def save_now(self):
        if self.session and self.settings.save_enabled:
            self._collect_settings()
            self.session.request_save(self.settings.save_dir)

    def retry(self):
        if self.session:
            self.session.retry_failed()

    def toggle(self):
        if self.session:
            self.session.toggle_original()

    def show_attach_guide(self, why: str = ""):
        """How to let the app use the Chrome that is already open (Chrome 144+, once per browser)."""
        from pcm.attach import INSPECT_URL
        if self._guide is not None and self._guide.winfo_exists():
            self._guide.lift()
            return
        w = self._guide = ttk.Toplevel(title="ใช้ Chrome ที่เปิดอยู่", resizable=(False, False))
        w.transient(self.root)
        body = ttk.Frame(w, padding=(22, 18))
        body.pack(fill=BOTH, expand=True)
        ttk.Label(body, text="ใช้ Chrome ที่เปิดอยู่", font=(UI_FONT, 15, "bold")).pack(anchor=W)
        reason = {"missing": "ต้องเปิดการอนุญาตใน Chrome ก่อน (ทำครั้งเดียว)",
                  "stale": "ไม่พบ Chrome ที่เปิดการอนุญาตไว้ — เปิด Chrome ก่อน หรือติ๊กอนุญาตอีกครั้ง"}.get(why, "")
        if reason:
            ttk.Label(body, text=reason, bootstyle=WARNING, wraplength=540, justify=LEFT).pack(anchor=W, pady=(4, 0))
        steps = ttk.Frame(body)
        steps.pack(fill=X, pady=(12, 0))
        steps.columnconfigure(1, weight=1)

        def step(row, n, text):
            ttk.Label(steps, text=f" {n} ", font=(UI_FONT, 10, "bold"), bootstyle=(INVERSE, PRIMARY)).grid(
                row=row, column=0, sticky=NW, pady=(6, 0), padx=(0, 10))
            ttk.Label(steps, text=text, wraplength=500, justify=LEFT).grid(row=row, column=1, sticky=W, pady=(6, 0))

        step(0, 1, "เปิด Chrome แล้ววางลิงก์นี้ในแถบที่อยู่ กด Enter")
        link = ttk.Frame(steps)
        link.grid(row=1, column=1, sticky=EW, pady=(6, 0))
        link.columnconfigure(0, weight=1)
        field = ttk.Entry(link, font=(UI_FONT, 11))
        field.insert(0, INSPECT_URL)
        field.configure(state="readonly")
        field.grid(row=0, column=0, sticky=EW, ipady=2)
        copied = ttk.Label(steps, text="คัดลอกแล้ว — ไปที่ Chrome แล้ววางในแถบที่อยู่ (Ctrl+V) กด Enter",
                           bootstyle=SUCCESS, wraplength=500, justify=LEFT)

        def copy():
            self.root.clipboard_clear()
            self.root.clipboard_append(INSPECT_URL)
            copied.grid(row=2, column=1, sticky=W, pady=(4, 0))

        ttk.Button(link, text="คัดลอกลิงก์", bootstyle=PRIMARY, command=copy).grid(row=0, column=1, padx=(8, 0))
        step(3, 2, "ติ๊กช่อง \"Allow remote debugging for this browser instance\" (ทำครั้งเดียว ใช้ได้จนกว่าจะเอาติ๊กออก)")
        step(4, 3, "กลับไปที่แท็บมังงะ แล้วกด \"เริ่มอ่านและแปล\" ในแอปนี้")
        step(5, 4, "Chrome จะถาม \"Allow remote debugging?\" — กด Allow (อนุญาต) ด้วยเมาส์ (Chrome ถามทุกครั้งที่เริ่ม)")
        ttk.Label(body, style="Muted.TLabel", wraplength=540, justify=LEFT, text=(
            "• ต้องใช้ Chrome 144 ขึ้นไป  • แอปเปิดลิงก์ chrome:// ให้ไม่ได้ (Chrome ไม่ยอม) จึงต้องวางเอง\n"
            "• ระหว่างแปล Chrome จะขึ้นแถบ \"Chrome is being controlled by automated test software\" เป็นเรื่องปกติ "
            "และหายไปเมื่อกดหยุด — แอปไม่ปิด Chrome หรือแท็บของคุณ\n"
            "• ถ้าติ๊กแล้วยังใช้ไม่ได้ องค์กรอาจปิดความสามารถนี้ไว้ (นโยบาย RemoteDebuggingAllowed)\n"
            "• Microsoft Edge: ใช้ edge://inspect/#remote-debugging แทน (ถ้ามีตัวเลือกนี้)")).pack(anchor=W, pady=(14, 0))
        bar = ttk.Frame(body)
        bar.pack(fill=X, pady=(16, 0))

        def retry():
            w.destroy()
            if self._state == "idle":
                self.use_open_chrome.set(True)
                self._sync_mode()
                self.start()

        ttk.Button(bar, text="ลองอีกครั้ง", bootstyle=PRIMARY, command=retry, width=12).pack(side=RIGHT)
        ttk.Button(bar, text="ปิด", bootstyle="primary-outline", command=w.destroy, width=8).pack(side=RIGHT, padx=8)
        w.update_idletasks()
        x = self.root.winfo_rootx() + max(0, (self.root.winfo_width() - w.winfo_width()) // 2)
        y = self.root.winfo_rooty() + 60
        w.geometry(f"+{x}+{y}")

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
                    if level >= logging.WARNING and not self._details:
                        self.details_btn.configure(text="▸  รายละเอียดการทำงาน  (มีข้อความใหม่)")
                elif kind == "progress":
                    _, done, total, failed, busy = ev
                    if self._state == "starting":
                        self._set_state("running")
                    self.pb.configure(maximum=max(total, 1), value=min(done + failed, total))
                    self.progress.set(f"{done} / {total} หน้า")
                    extra = []
                    if busy:
                        extra.append(f"กำลังแปล {busy}")
                    if failed:
                        extra.append(f"พลาด {failed}")
                    self.failed_lbl.configure(text="   ".join(extra))
                elif kind == "stopped":
                    self._set_state("idle")
                    self.status.configure(text="●  หยุดแล้ว")
                elif kind == "status":
                    if self._state in ("starting", "running"):
                        self.status.configure(text="●  " + ev[1])
                elif kind == "attach_failed":
                    _, why, msg = ev
                    if why in ("missing", "stale"):
                        self.root.after(10, self.show_attach_guide, why)
                    else:   # after this drain, so the window is idle again behind the message
                        self.root.after(10, lambda m=msg: Messagebox.show_warning(
                            m, "ใช้ Chrome ที่เปิดอยู่ไม่ได้", parent=self.root))
                elif kind == "call":
                    ev[1]()
        except queue.Empty:
            pass
        except Exception:  # never let the UI loop die
            DETAIL.warning("ui event error", exc_info=True)
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
    """Hidden diagnostics: PoomCatoManga.exe --selftest URL  (headless; writes selftest.log)."""
    import time
    from pcm.session import Session
    os.environ["PCM_HEADLESS"] = "1"
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
    root = ttk.Window()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
