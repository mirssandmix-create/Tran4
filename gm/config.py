"""User settings: stored as JSON in %APPDATA%\\GhostManga5\\settings.json."""
from __future__ import annotations

import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field, fields

APP_NAME = "Ghost Manga 5"
APP_VERSION = "5.0.0"


def app_data_dir() -> str:
    base = os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(base, "GhostManga5")
    os.makedirs(path, exist_ok=True)
    return path


def local_data_dir() -> str:
    """Machine-local data (browser profile, image cache): %LOCALAPPDATA%\\GhostManga5."""
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or os.path.expanduser("~")
    path = os.path.join(base, "GhostManga5")
    os.makedirs(path, exist_ok=True)
    return path


def resource_path(name: str) -> str:
    """Path to a bundled read-only resource (works for PyInstaller and plain python)."""
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, name)


# Target languages offered in the UI: (code, Thai label)
TARGET_LANGS = [
    ("th", "ไทย"),
    ("en", "อังกฤษ"),
    ("ja", "ญี่ปุ่น"),
    ("ko", "เกาหลี"),
    ("zh-CN", "จีน (ตัวย่อ)"),
    ("zh-TW", "จีน (ตัวเต็ม)"),
    ("vi", "เวียดนาม"),
    ("id", "อินโดนีเซีย"),
]

SOURCE_LANGS = [
    ("auto", "ตรวจจับอัตโนมัติ"),
    ("ja", "ญี่ปุ่น (มังงะ)"),
    ("zh", "จีน (มานฮวา)"),
    ("ko", "เกาหลี (มันฮวา/เว็บตูน)"),
    ("en", "อังกฤษ"),
]

# Translation engines: (id, Thai label)
ENGINES = [
    ("google", "Google แปลฟรี (ไม่ต้องตั้งค่า)"),
    ("local", "AI ในเครื่อง ฟรี (LM Studio / Ollama)"),
    ("gemini", "Google Gemini (API key)"),
    ("claude", "Claude (API key)"),
    ("openai", "OpenAI-compatible อื่นๆ (API key)"),
]

ENGINE_DEFAULTS = {
    "local": {"base_url": "http://localhost:1234/v1", "model": ""},
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta", "model": "gemini-2.5-flash"},
    "claude": {"base_url": "", "model": "claude-opus-5-5"},
    "openai": {"base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini"},
}


@dataclass
class Settings:
    last_url: str = ""
    source_lang: str = "auto"
    target_lang: str = "th"
    engine: str = "google"
    # per-engine connection settings
    local_base_url: str = ENGINE_DEFAULTS["local"]["base_url"]
    local_model: str = ENGINE_DEFAULTS["local"]["model"]
    gemini_api_key: str = ""
    gemini_model: str = ENGINE_DEFAULTS["gemini"]["model"]
    claude_api_key: str = ""
    claude_model: str = ENGINE_DEFAULTS["claude"]["model"]
    claude_effort: str = "low"
    openai_base_url: str = ENGINE_DEFAULTS["openai"]["base_url"]
    openai_api_key: str = ""
    openai_model: str = ENGINE_DEFAULTS["openai"]["model"]
    send_image_to_ai: bool = False      # vision context for LLM engines (better tone, costs more)
    glossary: str = ""                 # "ต้นฉบับ = คำแปล" per line, kept consistent by LLM engines
    # rendering
    font_family: str = "Leelawadee UI"
    font_bold: bool = True
    min_font_px: int = 11
    # processing
    concurrency: int = 3
    timeout_sec: int = 90
    min_image_side: int = 250          # skip icons/avatars smaller than this
    # saving
    save_dir: str = ""
    auto_save: bool = False
    # browser
    keep_browser_profile: bool = True  # remember logins / Cloudflare clearance between runs
    extra: dict = field(default_factory=dict)

    @property
    def path(self) -> str:
        return os.path.join(app_data_dir(), "settings.json")

    @classmethod
    def load(cls) -> "Settings":
        """Load settings. `load_warning` is set (and the bad file backed up) if it was unreadable."""
        s = cls()
        s.load_warning = ""
        try:
            with open(s.path, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("not a JSON object")
        except FileNotFoundError:
            return s
        except (ValueError, UnicodeDecodeError) as e:
            backup = s.path + time.strftime(".broken-%Y%m%d-%H%M%S")
            try:
                os.replace(s.path, backup)
            except Exception:
                backup = ""
            s.load_warning = f"ไฟล์ตั้งค่าเสีย ({e}) — ใช้ค่าเริ่มต้นแทน" + (f" (สำรองไว้ที่ {backup})" if backup else "")
            return s
        except OSError as e:
            s.load_warning = f"อ่านไฟล์ตั้งค่าไม่ได้: {e}"
            return s
        defaults = cls()
        for f in fields(cls):
            if f.name not in data:
                continue
            v, d = data[f.name], getattr(defaults, f.name)
            try:   # keep types sane even if the file was hand-edited
                if isinstance(d, bool):
                    v = bool(v)
                elif isinstance(d, int):
                    v = int(v)
                elif isinstance(d, str):
                    v = "" if v is None else str(v)
                elif isinstance(d, dict) and not isinstance(v, dict):
                    continue
            except (TypeError, ValueError):
                continue
            setattr(s, f.name, v)
        return s

    def save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.path)

    def glossary_pairs(self) -> list[tuple[str, str]]:
        pairs = []
        for line in (self.glossary or "").splitlines():
            if "=" in line:
                a, b = line.split("=", 1)
                if a.strip() and b.strip():
                    pairs.append((a.strip(), b.strip()))
        return pairs
