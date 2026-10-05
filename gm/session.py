"""Translation session: owns the visible Chrome (pure CDP, no chromedriver),
discovers manga images in the active tab, and runs each one through
bytes -> OCR -> translate -> erase -> typeset -> back into the page.

Everything runs on one asyncio loop in a worker thread. The Tk UI talks to it
only through thread-safe calls (run_coroutine_threadsafe) and the events queue.
Every page call carries the page agent's token plus the source/version the job
was made for, so a late result can never land on another image or chapter.
"""
from __future__ import annotations

import asyncio
import base64
import copy
import hashlib
import io
import json
import logging
import os
import queue
import re
import shutil
import tempfile
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field

import httpx
from PIL import Image, ImageDraw

from . import cdp as safe
from .cache import DiskCache
from .config import Settings, local_data_dir, resource_path
from .ocr import LensOCR
from .translate import Translator
from .typeset import Renderer, clean_and_place, encode_output

LOG = logging.getLogger("ghostmanga")
PIPELINE_VERSION = "5.0-r3"  # bump to invalidate cached translations

IMAGE_MAGIC = (b"\xff\xd8\xff", b"\x89PNG", b"GIF8", b"BM")


def _looks_like_image(data: bytes) -> bool:
    if not data or len(data) < 64:
        return False
    if data.startswith(IMAGE_MAGIC):
        return True
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    if data[4:8] == b"ftyp":  # avif / heic
        return True
    return False


def _mime_of(data: bytes) -> str:
    return "image/png" if data.startswith(b"\x89PNG") else "image/jpeg"


def _decode(raw: bytes) -> tuple[Image.Image, str]:
    img = Image.open(io.BytesIO(raw))
    img.load()
    if img.mode in ("RGBA", "LA", "P", "PA"):
        rgba = img.convert("RGBA")
        bg = Image.new("RGB", rgba.size, (255, 255, 255))
        bg.paste(rgba, mask=rgba.split()[-1])
        img = bg
    else:
        img = img.convert("RGB")
    return img, hashlib.sha1(raw).hexdigest()


def _safe_name(s: str, limit: int = 100) -> str:
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', " ", s or "").strip(" .")
    s = re.sub(r"\s+", " ", s)
    return s[:limit].strip(" .") or "manga"


def _vision_image(img: Image.Image, blocks) -> bytes | None:
    """Downscaled page with red bubble ids, for LLMs that can see images."""
    W, H = img.size
    if H > 3.2 * W:
        return None  # long strips become unreadable when shrunk
    s = min(1.0, 1536 / max(W, H))
    small = img.resize((max(1, int(W * s)), max(1, int(H * s))))
    d = ImageDraw.Draw(small)
    for b in blocks:
        x, y = b.box[0] * s, b.box[1] * s
        d.rectangle([x, y, x + 16 + 7 * len(b.id), y + 16], fill=(220, 0, 0))
        d.text((x + 3, y + 2), b.id, fill=(255, 255, 255))
    buf = io.BytesIO()
    small.save(buf, "JPEG", quality=80)
    return buf.getvalue()


class _Gone(Exception):
    """The element no longer shows what the job was made for (page flip, re-render, removal)."""


class _Away(Exception):
    """The tab is showing another document right now (navigation); the job may come back (bfcache)."""


def _chapter_path(path: str) -> str:
    """URL path that identifies a chapter: readers that put the page number in the URL
    (?page=3, /chapter/x/3, /page-3) must not turn every page into its own chapter."""
    p = (path or "/").split("?", 1)[0].split("#", 1)[0]
    p = re.sub(r"/(?:p|page)?[-_]?\d{1,4}/?$", "", p, flags=re.I)
    return p.rstrip("/") or "/"


@dataclass
class Job:
    tab: object
    tid: str
    origin: float
    token: str
    chap: tuple
    id: int
    kind: str
    src: str
    version: float
    ord: int
    vtop: float
    vbot: float
    seq: int
    attempts: int = 0


@dataclass
class TabState:
    origin: float
    token: str
    path: str = ""
    vh: float = 800.0


@dataclass
class Result:
    ord: int
    path: str
    translated: bool
    digest: str
    seq: int = 0


@dataclass
class Chapter:
    title: str = ""
    url: str = ""
    total: int = 0
    done: int = 0
    failed: int = 0
    busy: int = 0
    seq: int = 0
    results: dict = field(default_factory=dict)       # seq -> Result
    failed_jobs: list = field(default_factory=list)
    names: dict = field(default_factory=dict)         # digest -> file stem
    used: dict = field(default_factory=dict)          # file stem -> digest
    folder: str | None = None
    warned_csp: bool = False
    warned_frames: bool = False
    warned_empty: bool = False
    created: float = field(default_factory=time.monotonic)


class Session:
    def __init__(self, settings: Settings, events: queue.Queue):
        # snapshot: edits in the UI while running must not change this session's behaviour
        self.s = copy.deepcopy(settings)
        self.events = events
        self.loop: asyncio.AbstractEventLoop | None = None
        self.thread: threading.Thread | None = None
        self._running = False
        self.browser = None
        self._tab = None
        self.cur_tid = None
        self.cur_chap = None
        self.tabs: dict[str, TabState] = {}
        self.chapters: dict[tuple, Chapter] = {}
        self.prepared: dict[str, int] = {}
        self.handlers: set[str] = set()
        self.pending: list[Job] = []
        self.reqs: "OrderedDict[str, tuple]" = OrderedDict()
        self.ua = ""
        self._profile_tmp = None
        self.tmpdir = tempfile.mkdtemp(prefix="gm5_session_")
        self._files = 0
        self._folders: dict[str, tuple] = {}
        self._save_lock = threading.Lock()
        self._last_chip = None
        self._err_origin = None
        with open(resource_path("gm/page.js"), "r", encoding="utf-8") as f:
            self.page_js = f.read().replace("__GM_MIN_SIDE__", str(int(self.s.min_image_side)))
        # if the app exits before the worker thread finishes, don't leave temp files behind
        import atexit
        atexit.register(self._cleanup_files)

    def _cleanup_files(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)
        if self._profile_tmp:
            shutil.rmtree(self._profile_tmp, ignore_errors=True)

    # ------------------------------------------------------------ public ---
    @property
    def running(self) -> bool:
        return self._running

    def start(self):
        self._running = True
        self.thread = threading.Thread(target=self._thread_main, name="gm-session", daemon=True)
        self.thread.start()

    def stop(self):
        if self.loop and self._running:
            try:
                self.loop.call_soon_threadsafe(self._request_stop)
            except RuntimeError:
                pass

    def join(self, timeout=None):
        if self.thread:
            self.thread.join(timeout)

    def update_save(self, save_dir: str, auto_save: bool):
        """The only settings that may change while running."""
        self.s.save_dir, self.s.auto_save = save_dir, bool(auto_save)

    def request_save(self, folder: str):
        self._call(self._save_all(folder))

    def retry_failed(self):
        self._call(self._retry_failed())

    def toggle_original(self):
        self._call(self._toggle())

    def _call(self, coro):
        if self.loop and self._running:
            try:
                asyncio.run_coroutine_threadsafe(coro, self.loop)
                return
            except RuntimeError:
                pass
        coro.close()

    # ---------------------------------------------------------- plumbing ---
    def _emit(self, *ev):
        try:
            self.events.put(ev)
        except Exception:
            pass

    def _request_stop(self):
        if hasattr(self, "stop_event"):
            self.stop_event.set()

    def _thread_main(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        try:
            self.loop.run_until_complete(self._main())
        except Exception as e:
            LOG.error("เกิดข้อผิดพลาดร้ายแรง: %s", e)
        finally:
            try:
                pending = [t for t in asyncio.all_tasks(self.loop) if not t.done()]
                for t in pending:
                    t.cancel()
                if pending:
                    self.loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            self.loop.close()
            shutil.rmtree(self.tmpdir, ignore_errors=True)
            self._running = False
            self._emit("stopped")

    async def _main(self):
        self.stop_event = asyncio.Event()
        self.work_event = asyncio.Event()
        self.cache = DiskCache()
        self.ocr = LensOCR()
        self.translator = Translator(self.s)
        self.renderer = Renderer(self.s.font_family, bool(self.s.font_bold), int(self.s.min_font_px))
        self.http = httpx.AsyncClient(follow_redirects=True, timeout=25)
        tasks = []
        try:
            LOG.info("กำลังเปิด Chrome…")
            await self._launch()
            await self._ensure_prepared(self.browser.main_tab)
            n = max(1, min(8, int(self.s.concurrency)))
            tasks = [asyncio.create_task(self._worker(i)) for i in range(n)]
            tasks.append(asyncio.create_task(self._poll_loop()))
            tasks.append(asyncio.create_task(self.translator.prepare()))
            if not self.stop_event.is_set():
                LOG.info("เปิดหน้า: %s", self.s.last_url)
                nav = asyncio.create_task(self._navigate(self.browser.main_tab, self.s.last_url))
                stop = asyncio.create_task(self.stop_event.wait())
                await asyncio.wait({nav, stop}, return_when=asyncio.FIRST_COMPLETED)
                nav.cancel()
                stop.cancel()
            if not self.stop_event.is_set():
                LOG.info("พร้อมแปล — เลื่อนอ่านได้เลย รูปที่เห็นอยู่จะถูกแปลก่อน (กด Alt+T ในหน้าเว็บเพื่อสลับต้นฉบับ)")
            await self.stop_event.wait()
        except Exception as e:
            LOG.error("เริ่มทำงานไม่สำเร็จ: %s", e)
        finally:
            self.stop_event.set()
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for closer in (self.renderer.close, self.ocr.aclose, self.translator.aclose, self.http.aclose):
                try:
                    await asyncio.wait_for(closer(), 10)
                except Exception:
                    pass
            if self.browser is not None:
                try:
                    self.browser.stop()
                except Exception:
                    pass
                await asyncio.sleep(0.3)
            if self._profile_tmp:
                shutil.rmtree(self._profile_tmp, ignore_errors=True)
            LOG.info("หยุดแล้ว")

    # ----------------------------------------------------------- browser ---
    async def _launch(self):
        from seleniumbase.undetected.cdp_driver import cdp_util
        if self.s.keep_browser_profile:
            profile = os.path.join(local_data_dir(), "chrome-profile")
        else:
            profile = self._profile_tmp = tempfile.mkdtemp(prefix="gm5_profile_")
        headless = os.environ.get("GM5_HEADLESS") == "1"  # automated tests only
        try:
            self.browser = await asyncio.wait_for(cdp_util.start_async(user_data_dir=profile, headless=headless), 60)
        except Exception as e:
            if not self.s.keep_browser_profile:
                raise
            LOG.warning("ใช้โปรไฟล์เดิมไม่ได้ (%s) — เปิดแบบชั่วคราวแทน", e)
            profile = self._profile_tmp = tempfile.mkdtemp(prefix="gm5_profile_")
            self.browser = await asyncio.wait_for(cdp_util.start_async(user_data_dir=profile, headless=headless), 60)

    async def _ensure_prepared(self, tab):
        """(Re)apply our per-tab setup. A reconnected DevTools socket loses all of it."""
        import mycdp as cdp
        tid = tab.target_id
        if self.prepared.get(tid) == safe.socket_id(tab) and self.prepared.get(tid) != id(None):
            return
        if tid not in self.handlers:
            self.handlers.add(tid)
            try:
                tab.add_handler(cdp.network.ResponseReceived, self._on_response(tid))
            except Exception:
                pass
        for cmd in (cdp.page.enable(),
                    cdp.network.enable(max_total_buffer_size=200_000_000, max_resource_buffer_size=40_000_000),
                    cdp.page.set_bypass_csp(enabled=True),
                    cdp.page.add_script_to_evaluate_on_new_document(source=self.page_js)):
            try:
                await safe.call(tab, cmd, 10)
            except Exception as e:
                LOG.debug("prepare %s: %s", tid[:6], e)
        try:
            await safe.evaluate(tab, self.page_js, 10)
        except Exception:
            pass
        self.prepared[tid] = safe.socket_id(tab)

    def _on_response(self, tid):
        import mycdp as cdp
        kinds = (cdp.network.ResourceType.IMAGE, cdp.network.ResourceType.FETCH,
                 cdp.network.ResourceType.XHR, cdp.network.ResourceType.OTHER)

        def handler(ev, *_):
            try:
                if ev.type_ in kinds:
                    url = ev.response.url
                    self.reqs[url] = (ev.request_id, tid)
                    self.reqs.move_to_end(url)
                    while len(self.reqs) > 4000:
                        self.reqs.popitem(last=False)
            except Exception:
                pass
        return handler

    async def _navigate(self, tab, url):
        import mycdp as cdp
        try:
            await safe.call(tab, cdp.page.navigate(url), 30)
        except Exception as e:
            LOG.warning("เปิดหน้าเว็บช้าหรือมีปัญหา: %s", e)
            return
        for _ in range(60):
            if self.stop_event.is_set():
                return
            await asyncio.sleep(0.5)
            try:
                if await safe.evaluate(tab, "document.readyState", 3) in ("interactive", "complete"):
                    return
            except Exception:
                pass

    def _browser_alive(self) -> bool:
        proc = getattr(self.browser, "_process", None)
        if proc is None:
            return True
        return proc.returncode is None

    async def _live_tabs(self):
        try:
            await asyncio.wait_for(self.browser.update_targets(), 5)
        except Exception:
            pass
        return list(self.browser.tabs)

    def _prune_tabs(self, tabs):
        live = {t.target_id for t in tabs}
        for tid in [t for t in self.tabs if t not in live]:
            self.tabs.pop(tid, None)
            self.prepared.pop(tid, None)
        keep = []
        for j in self.pending:
            if j.tid in live:
                keep.append(j)
            elif j.chap in self.chapters:
                ch = self.chapters[j.chap]
                ch.total = max(0, ch.total - 1)
        self.pending = keep

    async def _active_tab(self, tabs):
        order = ([self._tab] if self._tab in tabs else []) + [t for t in tabs if t is not self._tab]
        for t in order:
            try:
                if await safe.evaluate(t, "document.visibilityState", 3) == "visible":
                    return t
            except Exception:
                continue
        return self._tab if self._tab in tabs else (tabs[0] if tabs else None)

    def _live(self, job: Job) -> bool:
        ts = self.tabs.get(job.tid)
        return bool(ts and ts.origin == job.origin and ts.token == job.token)

    # -------------------------------------------------------------- poll ---
    async def _poll_loop(self):
        fails = 0
        no_tab = 0
        last_tabs = 0.0
        while not self.stop_event.is_set():
            await asyncio.sleep(0.35)
            if not self._browser_alive():
                LOG.info("ปิดเบราว์เซอร์แล้ว — หยุดการแปล")
                self.stop_event.set()
                return
            try:
                now = time.monotonic()
                if self._tab is None or now - last_tabs > 1.5:
                    last_tabs = now
                    tabs = await self._live_tabs()
                    self._prune_tabs(tabs)
                    tab = await self._active_tab(tabs)
                    if tab is None:
                        no_tab += 1
                        if no_tab > 15:
                            LOG.info("ไม่มีแท็บเหลือแล้ว — หยุดการแปล")
                            self.stop_event.set()
                        continue
                    no_tab = 0
                    self._tab = tab
                await self._ensure_prepared(self._tab)
                await asyncio.wait_for(self._poll_tab(self._tab), 15)
                fails = 0
            except asyncio.CancelledError:
                raise
            except Exception as e:
                fails += 1
                self._tab = None
                if fails in (5, 20, 60):
                    LOG.debug("poll error: %s", e)

    async def _poll_tab(self, tab):
        info = json.loads(await safe.evaluate(tab,
            "JSON.stringify({u:location.href,t:document.title,o:performance.timeOrigin,g:!!window.__gm})", 5))
        url = info.get("u", "")
        if url.startswith("chrome-error://"):
            if self._err_origin != info.get("o"):
                self._err_origin = info.get("o")
                LOG.warning("เปิดหน้าเว็บไม่ได้ — ตรวจสอบลิงก์/อินเทอร์เน็ต แล้วพิมพ์ลิงก์ใหม่ในแถบที่อยู่ของ Chrome ได้เลย")
            return
        if not url.startswith(("http://", "https://", "file:")):
            return
        if not info.get("g"):
            await safe.evaluate(tab, self.page_js, 10)
        scan = json.loads(await safe.evaluate(tab, "window.__gm ? __gm.scan() : 'null'", 10) or "null")
        if not scan:
            return
        tid, origin, token = tab.target_id, info["o"], scan["token"]
        ts = self.tabs.get(tid)
        if not ts or ts.origin != origin or ts.token != token:
            ts = self.tabs[tid] = TabState(origin=origin, token=token)
        ts.path, ts.vh = _chapter_path(scan.get("path", "")), float(scan.get("vh") or 800)
        chap = (tid, origin, token, ts.path)
        ch = self.chapters.get(chap)
        if ch is None:
            ch = self.chapters[chap] = Chapter(title=info.get("t") or "", url=url)
            LOG.info("หน้าใหม่: %s", info.get("t") or url)
        if info.get("t"):
            ch.title = info["t"]
        self.cur_tid, self.cur_chap = tid, chap

        for it in scan.get("items", []):
            ch.total += 1
            ch.seq += 1
            job = Job(tab=tab, tid=tid, origin=origin, token=token, chap=chap, id=int(it["id"]),
                      kind=it.get("kind", "img"), src=it.get("src") or "", version=float(it.get("version") or 0),
                      ord=int(it.get("ord", 0)), vtop=float(it.get("vtop", 0)), vbot=float(it.get("vbot", 0)),
                      seq=ch.seq)
            if job.kind == "canvas":   # a newer drawing replaces the queued older one
                for old in [j for j in self.pending if j.tid == tid and j.token == token and j.id == job.id]:
                    self.pending.remove(old)
                    oc = self.chapters.get(old.chap)
                    if oc:
                        oc.total = max(0, oc.total - 1)
            self.pending.append(job)
        if scan.get("items"):
            self.work_event.set()

        await self._refresh_positions(tab, tid, token)

        if scan.get("frames") and not ch.warned_frames and ch.total == 0:
            ch.warned_frames = True
            LOG.warning("รูปในหน้านี้อยู่ในกรอบ (iframe) ของเว็บอื่น ซึ่งยังแปลไม่ได้ — ลองเปิดลิงก์นี้โดยตรง: %s",
                        scan["frames"][0])
        elif ch.total == 0 and not ch.warned_empty and time.monotonic() - ch.created > 20:
            ch.warned_empty = True
            LOG.info("ยังไม่เจอรูปมังงะในหน้านี้ — เปิดหน้าอ่านตอน (หน้าที่มีรูปมังงะ) แล้วลองเลื่อนลง "
                     "(รูปที่เล็กกว่า %dpx จะถูกข้าม)", int(self.s.min_image_side))
        if not ch.warned_csp:
            csp = await safe.evaluate(tab, "window.__gm ? JSON.stringify(__gm.csp.splice(0)) : '[]'", 5)
            if csp and "img-src" in csp:
                ch.warned_csp = True
                LOG.warning("เว็บนี้บล็อกการแสดงรูปที่แปลแล้ว (CSP) — กด F5 รีโหลดหน้าเว็บ 1 ครั้งแล้วจะใช้ได้")
        self._report()
        await self._chip(tab)

    async def _drop_in_page(self, job: Job):
        """Tell the page agent a job was dropped so the image can be queued again later."""
        if job.kind != "img" or not self._live(job):
            return
        try:
            await safe.evaluate(job.tab, "window.__gm && __gm.drop(%s, %s)" % (json.dumps(job.token), json.dumps([job.id])), 5)
        except Exception:
            pass

    async def _refresh_positions(self, tab, tid, token):
        """Live viewport positions of this document's queued jobs (scrolling changes priority)."""
        mine = [j for j in self.pending if j.tid == tid and j.token == token][:300]
        if not mine:
            return
        pos = await safe.evaluate(tab, "window.__gm ? __gm.pos(%s, %s) : null"
                                  % (json.dumps(token), json.dumps([j.id for j in mine])), 5)
        if not isinstance(pos, dict):
            return
        for j in mine:
            p = pos.get(str(j.id), "missing")
            if p is None or p == "stale":   # element removed / now showing another page
                if j in self.pending:
                    self.pending.remove(j)
                    ch = self.chapters.get(j.chap)
                    if ch:
                        ch.total = max(0, ch.total - 1)
                    await self._drop_in_page(j)
            elif isinstance(p, list) and len(p) == 2:
                j.vtop, j.vbot = float(p[0]), float(p[1])

    def _doc_chapters(self, chap=None) -> list[Chapter]:
        """All chapters of the document `chap` (default: the current one) belongs to."""
        chap = chap or self.cur_chap
        if not chap:
            return []
        return [c for k, c in self.chapters.items() if k[:3] == chap[:3]]

    def _report(self):
        chs = self._doc_chapters()
        self._emit("progress", sum(c.done for c in chs), sum(c.total for c in chs),
                   sum(c.failed for c in chs), sum(c.busy for c in chs))

    async def _chip(self, tab):
        chs = self._doc_chapters()
        done, total = sum(c.done for c in chs), sum(c.total for c in chs)
        busy, failed = sum(c.busy for c in chs), sum(c.failed for c in chs)
        if total == 0:
            text = "Ghost Manga: กำลังหารูป…"
        else:
            text = f"Ghost Manga: แปลแล้ว {done}/{total}"
            if busy:
                text += f" • กำลังแปล {busy}"
            if failed:
                text += f" • พลาด {failed}"
            text += " • Alt+T สลับต้นฉบับ"
        key = (tab.target_id, text)
        if self._last_chip == key:
            return
        try:
            await safe.evaluate(tab, "window.__gm && __gm.setStatus(%s)" % json.dumps(text, ensure_ascii=False), 5)
            self._last_chip = key
        except Exception:
            pass

    # ----------------------------------------------------------- workers ---
    def _take(self) -> Job | None:
        live = [j for j in self.pending if self._live(j)]
        if not live:
            return None

        cur_doc = self.cur_chap[:3] if self.cur_chap else None

        def prio(j: Job):
            cur = 0 if j.chap[:3] == cur_doc else (1 if j.tid == self.cur_tid else 2)
            vh = self.tabs[j.tid].vh
            if j.vbot > 0 and j.vtop < vh:
                return (cur, 0, max(0.0, j.vtop), -j.seq)  # on screen (newest first on ties)
            if j.vtop >= vh:
                return (cur, 1, j.vtop - vh, -j.seq)       # ahead of the reader
            return (cur, 2, -j.vbot, -j.seq)               # already scrolled past

        job = min(live, key=prio)
        self.pending.remove(job)
        return job

    async def _worker(self, n: int):
        while not self.stop_event.is_set():
            job = self._take()
            if job is None:
                self.work_event.clear()
                try:
                    await asyncio.wait_for(self.work_event.wait(), 0.5)
                except asyncio.TimeoutError:
                    pass
                continue
            ch = self.chapters.get(job.chap)
            if ch is None:
                continue
            ch.busy += 1
            try:
                await self._process(job, ch)
            except asyncio.CancelledError:
                raise
            except _Away:
                # the tab navigated; keep the job dormant in case the page comes back (bfcache)
                self.pending.append(job)
                dormant = [j for j in self.pending if j.tid == job.tid and not self._live(j)]
                for old in dormant[:-400]:
                    self.pending.remove(old)
            except _Gone:
                ch.total = max(0, ch.total - 1)
                await self._drop_in_page(job)
            except Exception as e:
                ch.failed += 1
                ch.failed_jobs.append(job)
                LOG.warning("รูปที่ %d แปลไม่สำเร็จ: %s", job.ord + 1, e)
                if job.kind == "img" and self._live(job):
                    try:
                        await safe.evaluate(job.tab, "window.__gm && __gm.release(%s, %s)"
                                            % (json.dumps(job.token), json.dumps([job.src])), 5)
                    except Exception:
                        pass
            finally:
                ch.busy -= 1
                if self.cur_chap and job.chap[:3] == self.cur_chap[:3]:
                    self._report()

    # -------------------------------------------------------- image bytes ---
    def _args(self, job: Job) -> str:
        return "%s, %d, %s, %s" % (json.dumps(job.token), job.id, json.dumps(job.src), repr(job.version))

    async def _get_bytes(self, job: Job) -> tuple[bytes, str]:
        import mycdp as cdp
        tab, src, errors = job.tab, job.src, []
        if job.kind == "img" and src.startswith("http"):
            # A: Chrome's own copy of the image (no new request, no hotlink/CORS problems)
            try:
                tree = await safe.call(tab, cdp.page.get_frame_tree(), 5)
                body, is_b64 = await safe.call(tab, cdp.page.get_resource_content(tree.frame.id_, src), 8)
                data = base64.b64decode(body) if is_b64 else body.encode("latin-1", "ignore")
                if _looks_like_image(data):
                    return data, "cache"
                errors.append("cache: not image")
            except Exception as e:
                errors.append(f"cache: {str(e)[:60]}")
            # B: network buffer captured while the page loaded
            rid = self.reqs.get(src)
            if rid and rid[1] == job.tid:
                try:
                    body, is_b64 = await safe.call(tab, cdp.network.get_response_body(rid[0]), 8)
                    data = base64.b64decode(body) if is_b64 else body.encode("latin-1", "ignore")
                    if _looks_like_image(data):
                        return data, "network"
                except Exception as e:
                    errors.append(f"network: {str(e)[:60]}")
        # C: ask the page (blob:, same-origin, CORS images, canvas readers)
        try:
            r = await safe.evaluate(tab, "window.__gm ? __gm.grab(%s, %s) : {err:'gone'}"
                                    % (self._args(job), "true" if job.kind == "img" else "false"), 30, True)
            if isinstance(r, dict) and str(r.get("data", "")).startswith("data:"):
                data = base64.b64decode(r["data"].split(",", 1)[1])
                if _looks_like_image(data):
                    return data, "page"
            if isinstance(r, dict) and r.get("err") == "gone":
                raise _Gone()
            errors.append(f"page: {r.get('err') if isinstance(r, dict) else r}")
        except _Gone:
            raise
        except Exception as e:
            errors.append(f"page: {str(e)[:60]}")
        # D: plain HTTP with the browser's cookies, user agent and referer
        if job.kind == "img" and src.startswith("http"):
            try:
                if not self.ua:
                    self.ua = await safe.evaluate(tab, "navigator.userAgent", 5)
                cookies = await safe.call(tab, cdp.network.get_cookies(urls=[src]), 5)
                page_url = await safe.evaluate(tab, "location.href", 5)
                headers = {"User-Agent": self.ua, "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                           "Referer": page_url}
                r = await self.http.get(src, headers=headers, cookies={c.name: c.value for c in cookies})
                if r.status_code == 200 and _looks_like_image(r.content):
                    return r.content, "http"
                errors.append(f"http: {r.status_code}")
            except Exception as e:
                errors.append(f"http: {type(e).__name__}")
        # E: screenshot of the element as rendered
        try:
            return await self._screenshot(job), "screenshot"
        except _Gone:
            raise
        except Exception as e:
            errors.append(f"screenshot: {str(e)[:60]}")
        raise RuntimeError("ดึงรูปไม่ได้ (" + "; ".join(errors) + ")")

    async def _screenshot(self, job: Job) -> bytes:
        import mycdp as cdp
        tab = job.tab
        r = await safe.evaluate(tab, "window.__gm ? JSON.stringify(__gm.rect(%s)) : 'null'" % self._args(job), 5)
        r = json.loads(r or "null")
        if not r:
            raise _Gone()
        if r["w"] < 10 or r["h"] < 10:
            raise RuntimeError("element not visible")
        scale = max(0.5, min(3.0, (r["nw"] or r["w"]) / max(r["w"], 1) / max(r["dpr"], 0.5)))
        await safe.evaluate(tab, "window.__gm && __gm.hideChip(true)", 5)
        try:
            data = await safe.call(tab, cdp.page.capture_screenshot(
                format_="png", capture_beyond_viewport=True,
                clip=cdp.page.Viewport(x=r["x"], y=r["y"], width=r["w"], height=r["h"], scale=scale)), 30)
        finally:
            try:
                await safe.evaluate(tab, "window.__gm && __gm.hideChip(false)", 5)
            except Exception:
                pass
        return base64.b64decode(data)

    # ----------------------------------------------------------- process ---
    def _cache_key(self, digest: str) -> str:
        s, r = self.s, self.renderer
        return DiskCache.key(digest, s.source_lang, s.target_lang, self.translator.cache_tag,
                             r.font, str(r.bold), str(r.min_px), PIPELINE_VERSION)

    def _keep_file(self, data: bytes) -> str:
        self._files += 1
        path = os.path.join(self.tmpdir, f"{self._files}.bin")
        with open(path, "wb") as f:
            f.write(data)
        return path

    async def _process(self, job: Job, ch: Chapter):
        loop = asyncio.get_running_loop()
        job.attempts += 1
        if not self._live(job):
            raise _Away()
        raw, how = await self._get_bytes(job)
        try:
            img, digest = await loop.run_in_executor(None, _decode, raw)
        except Exception:
            # e.g. a format Pillow can't read: let Chrome re-encode it, else screenshot
            r = await safe.evaluate(job.tab, "window.__gm ? __gm.grab(%s, false) : {err:'gone'}" % self._args(job), 30, True)
            if isinstance(r, dict) and str(r.get("data", "")).startswith("data:"):
                raw = base64.b64decode(r["data"].split(",", 1)[1])
            else:
                raw = await self._screenshot(job)
            img, digest = await loop.run_in_executor(None, _decode, raw)
        s = self.s
        engine_at_key = self.translator.engine.name
        key = self._cache_key(digest)
        out = await loop.run_in_executor(None, self.cache.get, key)
        if out is None:
            blocks = await self.ocr.read(img, s.source_lang, s.target_lang)
            if not self._live(job):
                raise _Away()
            if not blocks and getattr(blocks, "partial", False):
                raise RuntimeError("อ่านตัวหนังสือในรูปนี้ได้ไม่ครบ (Google Lens ไม่ตอบบางส่วน)")
            if not blocks:
                out = b""  # no text: keep the original
            else:
                vision = None
                if s.send_image_to_ai and self.translator.engine.name != "google":
                    vision = await loop.run_in_executor(None, _vision_image, img, blocks)
                used = await self.translator.translate_page(blocks, s.source_lang, getattr(blocks, "lang", ""),
                                                            s.target_lang, vision)
                if not any(b.translation for b in blocks):
                    raise RuntimeError("ไม่ได้คำแปลกลับมา")
                cleaned, placements = await loop.run_in_executor(None, clean_and_place, img, blocks)
                final = await self.renderer.render(cleaned, placements, s.target_lang)
                out, _mime = await loop.run_in_executor(None, encode_output, final)
                if used == engine_at_key and not getattr(blocks, "partial", False):
                    await loop.run_in_executor(None, self.cache.put, key, out)
        if not self._live(job):
            raise _Away()   # the result is cached on disk, so a revisit is instant
        stored_only = False
        if out:
            orig_png = None
            if job.kind == "canvas":
                buf = io.BytesIO()
                img.save(buf, "PNG", compress_level=1)
                orig_png = base64.b64encode(buf.getvalue()).decode()
            res = await safe.evaluate(job.tab, "window.__gm ? __gm.apply(%s, %s, %s, %s) : 'gone'" % (
                self._args(job), json.dumps(base64.b64encode(out).decode()), json.dumps(_mime_of(out)),
                json.dumps(orig_png)), 60, True)
            if res in ("gone", "stale"):
                raise _Gone()
            stored_only = res == "stored"   # page moved on, but keeps it for when that page returns
        data = out or raw
        path = await loop.run_in_executor(None, self._keep_file, data)
        ch.results[job.seq] = Result(ord=job.ord, path=path, translated=bool(out), digest=digest, seq=job.seq)
        if stored_only:
            ch.total = max(0, ch.total - 1)
        else:
            ch.done += 1
        if s.auto_save and s.save_dir:
            try:
                await loop.run_in_executor(None, self._save_results, job.chap, s.save_dir, [ch.results[job.seq]])
            except Exception as e:
                LOG.warning("บันทึกอัตโนมัติไม่ได้: %s", e)

    # ------------------------------------------------------------ saving ---
    def _chapter_dir(self, chap, base: str) -> str:
        ch = self.chapters[chap]
        if ch.folder is None:
            name = _safe_name(ch.title or ch.url)
            stem, n = name, 2

            def taken(nm):
                if nm in self._folders:               # used by another chapter of this session
                    return self._folders[nm] != chap
                p = os.path.join(base, nm)            # left over from an earlier run: don't overwrite it
                return os.path.isdir(p) and any(os.scandir(p))

            while taken(name):
                name = f"{stem} ({n})"
                n += 1
            self._folders[name] = chap
            ch.folder = name
        path = os.path.join(base, ch.folder)
        os.makedirs(path, exist_ok=True)
        return path

    @staticmethod
    def _stem(ch: Chapter, res: Result) -> str:
        """Page-order file name; never shared by two different images of a chapter."""
        if res.digest in ch.names:
            return ch.names[res.digest]
        stem, k = f"{res.ord + 1:03d}", 2
        while stem in ch.used:
            stem = f"{res.ord + 1:03d}_{k}"
            k += 1
        ch.names[res.digest] = stem
        ch.used[stem] = res.digest
        return stem

    def _save_results(self, chap, base: str, results: list[Result]) -> str:
        with self._save_lock:   # runs in executor threads; names must be assigned one at a time
            return self._save_results_locked(chap, base, results)

    def _save_results_locked(self, chap, base: str, results: list[Result]) -> str:
        ch = self.chapters[chap]
        folder = self._chapter_dir(chap, base)
        for res in results:
            with open(res.path, "rb") as f:
                data = f.read()
            stem = os.path.join(folder, self._stem(ch, res))
            if data[:3] == b"\xff\xd8\xff":
                with open(stem + ".jpg", "wb") as f:
                    f.write(data)
            else:
                img, _ = _decode(data)
                if max(img.size) > 65500:
                    img.save(stem + ".png", "PNG")
                else:
                    img.save(stem + ".jpg", "JPEG", quality=92)
        return folder

    async def _save_all(self, folder: str):
        """Save every chapter of the current document (each into its own folder)."""
        if not folder:
            LOG.warning("ยังไม่ได้เลือกโฟลเดอร์สำหรับบันทึก")
            return
        cur = self.cur_chap
        chaps = [k for k, c in self.chapters.items() if cur and k[:3] == cur[:3] and c.results]
        if not chaps:
            LOG.info("ยังไม่มีรูปที่แปลเสร็จในหน้านี้")
            return
        loop = asyncio.get_running_loop()
        for chap in chaps:
            items = sorted(self.chapters[chap].results.values(), key=lambda r: (r.ord, r.seq))
            try:
                target = await loop.run_in_executor(None, self._save_results, chap, folder, items)
                LOG.info("บันทึก %d รูปไว้ที่ %s", len(items), target)
            except Exception as e:
                LOG.error("บันทึกไม่ได้: %s", e)

    # -------------------------------------------------------------- misc ---
    async def _retry_failed(self):
        jobs = []
        for ch in self._doc_chapters():
            jobs += ch.failed_jobs
            ch.failed = max(0, ch.failed - len(ch.failed_jobs))
            ch.failed_jobs = []
        if not jobs:
            LOG.info("ไม่มีรูปที่แปลพลาดในหน้านี้")
            return
        self.pending.extend(jobs)
        self.work_event.set()
        LOG.info("กำลังแปลใหม่ %d รูป", len(jobs))

    async def _toggle(self):
        tab = self._tab
        if tab is None:
            return
        try:
            showing_orig = await safe.evaluate(tab, "window.__gm ? __gm.toggle() : null", 10, True)
            LOG.info("แสดง%s", "ต้นฉบับ" if showing_orig else "คำแปล")
        except Exception as e:
            LOG.warning("สลับไม่ได้: %s", e)
